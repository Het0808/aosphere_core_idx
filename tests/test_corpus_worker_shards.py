"""Shards must be disjoint, complete and balanced by pages — with no coordination between workers.

The GPU workers have no queue and take no locks. Each one computes the whole partition and keeps
its own slice, which is only safe if every worker computes the SAME partition from the same work
list. If that ever stopped holding, two pods would extract the same document (paying twice for
it) or a document would fall through every shard and quietly never be extracted — and with 500+
documents nobody would notice which one.

Balancing by pages rather than by document count is the other half. These documents run 8 to 165
pages, so an even split by count can leave one worker hours behind the rest; a Job is only
finished when its slowest pod is.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import corpus_worker as W  # noqa: E402


def _jobs(pages: list[int]) -> list[tuple[str, dict]]:
    """A work list whose page counts are known without touching a real PDF."""
    return [(f"prod_{i % 3}", {"label": f"doc_{i:03d}", "pdf": Path(f"/nonexistent/{i}.pdf"),
                               "_pages": n})
            for i, n in enumerate(pages)]


@pytest.fixture(autouse=True)
def _pages_from_fixture(monkeypatch):
    """page_count() and pdf_sha1() both open the PDF; here they read the fixture instead.

    Content hashes default to the path, i.e. every document distinct — a test that cares about
    duplicates sets W._SHA explicitly."""
    lookup: dict[str, int] = {}
    monkeypatch.setattr(W, "page_count", lambda pdf: lookup.get(str(pdf), 1))
    monkeypatch.setattr(W.R, "pdf_sha1", lambda pdf: _SHA.get(str(pdf), str(pdf)))
    _SHA.clear()
    yield lookup


_SHA: dict[str, str] = {}


def _prime(lookup, jobs):
    for _p, j in jobs:
        lookup[str(j["pdf"])] = j["_pages"]


@pytest.mark.parametrize("shards", [1, 2, 3, 6, 11])
def test_shards_are_disjoint_and_complete(_pages_from_fixture, shards):
    jobs = _jobs([8, 165, 54, 91, 12, 38, 95, 67, 80, 19, 43, 121, 7, 52, 88])
    _prime(_pages_from_fixture, jobs)

    seen: set[str] = set()
    total = 0
    for i in range(shards):
        part = W.assign_shard(jobs, i, shards)
        keys = {f"{p}/{j['label']}" for p, j in part}
        assert not (seen & keys), f"shard {i} overlaps an earlier shard: {seen & keys}"
        seen |= keys
        total += len(part)
    assert total == len(jobs), "a document fell through every shard"
    assert len(seen) == len(jobs)


def _loads(lookup, jobs, shards):
    return [sum(lookup[str(j["pdf"])] for _p, j in W.assign_shard(jobs, i, shards))
            for i in range(shards)]


def test_pages_are_balanced_when_balance_is_achievable(_pages_from_fixture):
    """Mixed sizes that can be split evenly must be split evenly."""
    jobs = _jobs([100, 90, 80, 70, 60, 50])
    _prime(_pages_from_fixture, jobs)
    loads = _loads(_pages_from_fixture, jobs, 3)
    assert loads == [150, 150, 150], loads


def test_one_enormous_document_bounds_the_run_and_is_not_stacked_on(_pages_from_fixture):
    """A single 300-page document among 300 pages of small ones.

    No partition can beat 300 for the slowest shard — that one document IS the floor. What the
    balancer must not do is pile small documents on top of it: the other two shards should carry
    everything else between them, so the run finishes at 300 rather than at 400."""
    jobs = _jobs([300] + [10] * 30)
    _prime(_pages_from_fixture, jobs)
    loads = _loads(_pages_from_fixture, jobs, 3)
    assert sum(loads) == 600
    assert max(loads) == 300, f"the big document's shard picked up extra work: {loads}"
    assert sorted(loads) == [150, 150, 300], loads

    # By document count the split would be ~10 each, so the big shard would carry 300 + 90 = 390.
    by_count = 300 + 10 * (len(jobs) // 3 - 1)
    assert max(loads) < by_count


def test_the_partition_is_deterministic(_pages_from_fixture):
    """Two pods computing the partition independently must agree, or they collide."""
    jobs = _jobs([54, 12, 91, 8, 67, 33, 120, 45])
    _prime(_pages_from_fixture, jobs)
    for i in range(4):
        assert W.assign_shard(jobs, i, 4) == W.assign_shard(jobs, i, 4)


def test_a_single_shard_takes_everything(_pages_from_fixture):
    """One worker gets the whole list — largest document first, so a long tail of small documents
    cannot leave the biggest one unstarted at the end of a run."""
    jobs = _jobs([10, 20, 30])
    _prime(_pages_from_fixture, jobs)
    got = W.assign_shard(jobs, 0, 1)
    assert sorted(got, key=lambda pj: pj[1]["label"]) == sorted(jobs, key=lambda pj: pj[1]["label"])
    assert [j["_pages"] for _p, j in got] == [30, 20, 10]


def test_more_shards_than_documents_leaves_empty_shards(_pages_from_fixture):
    """A Job sized larger than the remaining work must not crash or double-assign — the surplus
    pods simply find nothing and exit 0, which is what makes resume safe near the end of a run."""
    jobs = _jobs([10, 20])
    _prime(_pages_from_fixture, jobs)
    parts = [W.assign_shard(jobs, i, 5) for i in range(5)]
    assert sum(len(p) for p in parts) == 2
    assert sum(1 for p in parts if not p) == 3


def test_copies_of_one_document_stay_in_a_single_shard(_pages_from_fixture):
    """The DP US case: one 281-page memorandum filed under 33 jurisdictions.

    run_corpus clones a duplicate from a finished twin instead of re-extracting it, but only a
    twin it can see on its own disk. If the copies were spread over 6 pods, five of them would
    re-extract the 281 pages for real — which is how a 25%-redundant corpus quietly costs full
    price. Copies must therefore land in ONE shard, with the representative ordered first so it
    is extracted before the others look for it."""
    jobs = _jobs([281] * 33 + [40, 60, 20, 90])
    _prime(_pages_from_fixture, jobs)
    for _p, j in jobs[:33]:
        _SHA[str(j["pdf"])] = "the-us-dp-memorandum"

    placed: dict[str, int] = {}
    for i in range(6):
        for p, j in W.assign_shard(jobs, i, 6):
            placed[f"{p}/{j['label']}"] = i

    shards_used = {placed[f"{p}/{j['label']}"] for p, j in jobs[:33]}
    assert len(shards_used) == 1, f"the 33 copies were split across shards {shards_used}"

    home = shards_used.pop()
    order = [f"{p}/{j['label']}" for p, j in W.assign_shard(jobs, home, 6)]
    copies = [f"{p}/{j['label']}" for p, j in jobs[:33]]
    assert order.index(copies[0]) == min(order.index(c) for c in copies), \
        "the representative must be extracted before its clones look for it"


def test_the_shard_weight_counts_a_duplicate_group_once(_pages_from_fixture):
    """33 copies of a 281-page document are 281 pages of GPU work, not 9,273.

    Weighting by file would make the balancer treat clone work as extraction work and starve the
    other shards of real pages."""
    jobs = _jobs([281] * 33 + [281])
    _prime(_pages_from_fixture, jobs)
    for _p, j in jobs[:33]:
        _SHA[str(j["pdf"])] = "one-document"

    # Two units of real work (the group, and the standalone document) over two shards: one each.
    parts = [W.assign_shard(jobs, i, 2) for i in range(2)]
    assert sorted(len(p) for p in parts) == [1, 33], [len(p) for p in parts]


def test_page_count_falls_back_to_size_when_the_pdf_is_unreadable(tmp_path):
    """Balancing must degrade, not explode, on a corrupt or missing PDF."""
    missing = tmp_path / "gone.pdf"
    assert W.page_count(missing) == 1

    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf" * 20_000)          # ~180KB -> ~6 pages by the size proxy
    assert W.page_count(junk) >= 1
