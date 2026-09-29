"""Which of a run's documents a promotion would publish, and what it costs to decide.

Two things are asserted here that the code cannot make obvious on its own.

COST. The product allow-list is applied to the PRODUCT PREFIX, before any job directory
underneath it is listed. A real run carries ~36 product directories and the allow-list has
three, so filtering first is a 12x saving on the walk — and a refactor that filters the
RESULTS instead would still be correct, still pass a naive test, and quietly pay for
listing thirty-three products nobody asked for. So the fake records every prefix it is
asked for and the excluded ones must never appear.

And the run must never be listed recursively: 380,609 keys / 64.4s measured against 986
prefixes / 0.9s by delimiter, on run 2026-08-21-02. The fake makes a recursive listing
raise rather than merely be slow.

POLICY. `region_map.PRODUCTS` is the allow-list, and the near-namesakes in the corpus —
170_Data_Privacy_(US_States), 165_Data_Privacy_-_Snippets — are excluded BY that rule
rather than by a special case, so adding a product stays one edit in one place.
"""
import json

import pytest

pytest.importorskip("numpy")

from aosphere_core_index.regions.region_map import PRODUCTS  # noqa: E402
from aosphere_core_index.service import promotion_monitor as PM  # noqa: E402

RUN = "2026-08-21-02"
PREFIX = f"corpus/{RUN}"


def sc(gate="pass", worst=90.0):
    return json.dumps({"gate": gate, "worst_score": worst,
                       "pages": {"total": 10, "states": {}, "counts": {}},
                       "stage3": {"tables_total": 1, "tables_filled": 1, "tables_failed": 0},
                       "tables": []}).encode()


# One key per artefact, sized so the byte totals are checkable.
KEYS: dict[str, bytes] = {}


def job(product_dir, label, gate="pass", viewer=True, inspect=True, toc=False):
    base = f"{PREFIX}/{product_dir}/{label}"
    if gate is not None:
        KEYS[f"{base}/scorecard.json"] = sc(gate)
    if viewer:
        KEYS[f"{base}/viewer.html"] = b"v" * 1000
    if inspect:
        KEYS[f"{base}/inspect.html"] = b"i" * 500
    if toc:
        KEYS[f"{base}/rescued_by_toc.json"] = b"{}"
    KEYS[f"{base}/source.pdf"] = b"%PDF" * 100
    KEYS[f"{base}/03_stage3_final/index.md"] = b"# x"      # must never be listed


job("155_Data_Privacy", "Spain__172575")
job("155_Data_Privacy", "France__172576", gate="review")
job("155_Data_Privacy", "Chad__172577", gate="fail")
job("155_Data_Privacy", "Peru__172578", gate="error")
job("155_Data_Privacy", "Togo__172579", gate=None)                  # never finished
job("155_Data_Privacy", "Kenya__172580", viewer=False)              # extracted, no viewer
job("104_Shareholding_Disclosure", "Australia__104001", toc=True)
job("124_Marketing_Restrictions_-_Asset_Management", "Kenya__183509")
# Excluded products — near-namesakes and unrelated ones. Their job dirs must never be listed.
job("170_Data_Privacy_(US_States)", "California__9001")
job("165_Data_Privacy_-_Snippets", "Spain__9002")
job("125_G20", "Brazil__9003")
job("117_diligence", "Chile__9004")
# Run-level bookkeeping is not a product.
KEYS[f"{PREFIX}/_progress/shard-0.json"] = b"{}"
KEYS[f"{PREFIX}/_promotion/{RUN}-p1/state.json"] = b"{}"


class FakeS3:
    """Records every prefix and key asked for, so the cost contract can be asserted."""

    def __init__(self, bucket="bkt", region=None):
        self.bucket = bucket
        self.asked_prefixes: list[str] = []
        self.asked_keys: list[str] = []

    def list_common_prefixes(self, prefix):
        self.asked_prefixes.append(prefix)
        out = set()
        for k in KEYS:
            if k.startswith(prefix):
                rest = k[len(prefix):]
                if "/" in rest:
                    out.add(prefix + rest.split("/", 1)[0] + "/")
        return sorted(out)

    def list_level_objects(self, prefix):
        self.asked_prefixes.append(prefix)
        return [{"key": k, "size": len(v)} for k, v in sorted(KEYS.items())
                if k.startswith(prefix) and "/" not in k[len(prefix):]]

    def list_level_with_prefixes(self, prefix):
        """`list_level_objects` + `list_common_prefixes`, from one listing — what
        walk_run_jobs now asks for so it can see a nested 04_stage4_ai/ directory
        without a second LIST call."""
        keys = [{"key": k, "size": len(v)} for k, v in sorted(KEYS.items())
                if k.startswith(prefix) and "/" not in k[len(prefix):]]
        out = set()
        for k in KEYS:
            if k.startswith(prefix):
                rest = k[len(prefix):]
                if "/" in rest:
                    out.add(prefix + rest.split("/", 1)[0] + "/")
        self.asked_prefixes.append(prefix)
        return keys, sorted(out)

    def list_keys(self, prefix, suffix=None):                # pragma: no cover
        raise AssertionError("a run must NEVER be listed recursively "
                             "(380,609 keys / 64.4s on run 2026-08-21-02)")

    def get_bytes(self, key):
        self.asked_keys.append(key)
        return KEYS[key]


def plan(**kw):
    s3 = FakeS3()
    p = PM.plan_run(s3, RUN, PREFIX, f"{RUN}-p1", fanout=4, **kw)
    return p, s3


def by_job(p):
    return {d["job"]: d for d in p["docs"]}


# ---------------- what is selected ----------------
def test_pass_and_review_are_promoted_and_nothing_else_is():
    p, _ = plan()
    d = by_job(p)
    assert d["155_Data_Privacy/Spain__172575"]["decision"] == "promote"
    assert d["155_Data_Privacy/France__172576"]["decision"] == "promote"
    for label, reason in (("Chad__172577", "gate"), ("Peru__172578", "gate")):
        row = d[f"155_Data_Privacy/{label}"]
        assert (row["decision"], row["reason"]) == ("skip", reason)


def test_a_document_that_never_finished_is_reported_as_unfinished_not_as_failed():
    """No scorecard means no verdict. It still appears in the plan with its reason, because
    "this run has 40 documents that never finished" is something an operator wants to see
    BEFORE promoting — and it is not the same statement as "40 documents failed"."""
    row = by_job(plan()[0])["155_Data_Privacy/Togo__172579"]
    assert (row["decision"], row["reason"]) == ("skip", "no_scorecard")
    assert row["gate"] is None


def test_a_document_with_no_viewer_is_held_back_and_named():
    """build_review_artefacts is best-effort and 123 documents in run 2026-08-21-02 were
    extracted before it existed at all. A manifest row pointing at a viewer that is not
    there is a guaranteed 404 the reviewer cannot tell apart from a broken one."""
    row = by_job(plan()[0])["155_Data_Privacy/Kenya__172580"]
    assert (row["decision"], row["reason"]) == ("blocked", "no_viewer")


def test_only_the_three_indexable_products_are_promoted():
    promoted = {d["product"] for d in plan()[0]["docs"] if d["decision"] == "promote"}
    assert promoted == set(PRODUCTS)
    assert len(promoted) == 3


def test_an_explicit_gate_selection_is_honoured():
    p, _ = plan(gates=("pass",))
    assert by_job(p)["155_Data_Privacy/France__172576"]["reason"] == "gate"


def test_an_explicit_product_selection_narrows_further():
    p, _ = plan(products=("Data Privacy",))
    assert {d["product"] for d in p["docs"] if d["decision"] == "promote"} == {"Data Privacy"}


# ---------------- what it cost ----------------
def test_the_excluded_products_job_directories_are_NEVER_listed():
    """The saving is the point: filter the product PREFIX, not the results."""
    _, s3 = plan()
    for excluded in ("170_Data_Privacy_(US_States)", "165_Data_Privacy_-_Snippets",
                     "125_G20", "117_diligence"):
        assert not any(excluded in a for a in s3.asked_prefixes), \
            f"{excluded} was walked despite not being an indexable product"
    assert any("155_Data_Privacy" in a for a in s3.asked_prefixes)


def test_run_level_bookkeeping_directories_are_not_treated_as_products():
    _, s3 = plan()
    assert not any("/_progress" in a for a in s3.asked_prefixes)
    assert not any("/_promotion" in a for a in s3.asked_prefixes)


def test_the_run_is_never_listed_recursively():
    plan()          # FakeS3.list_keys raises; reaching it fails the test


def test_exactly_one_scorecard_get_per_candidate_and_nothing_else_is_fetched():
    """That single GET is the ONLY read of each scorecard in the whole promotion: the copy
    stage moves the object server-side and takes its stats from the plan row."""
    _, s3 = plan()
    assert all(k.endswith("/scorecard.json") for k in s3.asked_keys)
    # Seven documents in an indexable product have a scorecard; Togo has none, so it is
    # never fetched. Each is read exactly once — no duplicates.
    assert len(s3.asked_keys) == len(set(s3.asked_keys)) == 7
    assert not any("source.pdf" in k or "03_stage3_final" in k for k in s3.asked_keys)


def test_the_plan_carries_the_bytes_it_is_about_to_copy():
    """The size came back with the listing, so reporting it costs nothing extra."""
    row = by_job(plan()[0])["155_Data_Privacy/Spain__172575"]
    assert row["objects"] == 3
    assert row["bytes"] == 1000 + 500 + len(sc())
    e = PM.eligibility(plan()[0])
    # Three artefacts per promoted document (viewer, scorecard, inspect). rescued_by_toc.json
    # is READ where it exists but never copied, so it is not an object in this total.
    assert e["bytes"] > 0 and e["objects"] == 3 * e["eligible"] == 12


# ---------------- the plan object itself ----------------
def test_the_summary_is_bounded_and_the_rows_are_not_in_it():
    """The screen shows counts and reasons; a 1000-document plan is never shipped whole."""
    e = PM.eligibility(plan()[0])
    assert "docs" not in e
    assert e["eligible"] == 4
    assert e["excluded"]["skip/gate"] == 2
    assert e["excluded"]["blocked/no_viewer"] == 1
    assert e["by_product"]["Data Privacy"] == {"pass": 1, "review": 1}
    for examples in e["examples"].values():
        assert len(examples) <= 20


def test_the_toc_rescue_file_is_flagged_but_not_fetched_at_plan_time():
    """<1KB each, and only a minority of documents have one — so the listing records
    whether to bother and the copy stage pays for it only where it exists."""
    p, s3 = plan()
    d = by_job(p)
    assert d["104_Shareholding_Disclosure/Australia__104001"]["has_toc_rescue"] is True
    assert d["155_Data_Privacy/Spain__172575"]["has_toc_rescue"] is False
    assert not any("rescued_by_toc" in k for k in s3.asked_keys)


def test_the_plan_records_where_it_would_publish():
    p, _ = plan()
    assert p["target_prefix"] == f"index/{RUN}-p1/doc-gallery"
    assert p["version"] == f"{RUN}-p1"


def test_a_promoted_row_carries_its_stats_and_a_skipped_one_does_not():
    """Bounded by construction: only the rows that will become manifest entries carry the
    dozen numbers, and no row ever carries the 97KB scorecard it came from."""
    d = by_job(plan()[0])
    assert d["155_Data_Privacy/Spain__172575"]["stats"]["pages"] == 10
    assert "stats" not in d["155_Data_Privacy/Chad__172577"]
    assert not any("dimensions" in json.dumps(r) for r in plan()[0]["docs"])


# ---------------- --exclude ----------------
def test_without_exclude_a_healthy_document_promotes():
    p, _ = plan(products=PRODUCTS)
    target = "104_Shareholding_Disclosure/Australia__104001"
    assert by_job(p)[target]["decision"] == "promote"


def test_an_excluded_job_is_skipped_and_NAMED():
    """One document whose extraction cannot convert must not sink a run of 451.

    Iceland is why this exists: tree_to_content produced 0 sections from its stage-3 tree,
    which fails the content stage for every other document too. Excluding it in the PLAN
    holds it back from the gallery AND the index together, and the region keeps whatever
    the seed carried over — the incumbent's 79 sections, rather than an empty hole.
    """
    target = "104_Shareholding_Disclosure/Australia__104001"
    p, _ = plan(products=PRODUCTS, exclude=[target])
    d = by_job(p)
    assert (d[target]["decision"], d[target]["reason"]) == ("skip", "excluded")
    assert d["155_Data_Privacy/Spain__172575"]["decision"] == "promote"


def test_a_trailing_slash_does_not_defeat_the_match():
    target = "104_Shareholding_Disclosure/Australia__104001"
    p, _ = plan(products=PRODUCTS, exclude=[f"{target}/"])
    assert by_job(p)[target]["decision"] == "skip"


def test_an_excluded_job_reaches_NEITHER_the_gallery_nor_the_index():
    """Excluding in the plan is what makes those two the same decision.

    Dropping a document from only one side recreates precisely what the content stage's
    failure guard exists to prevent: a document the gallery offers and the index cannot
    answer about.
    """
    target = "104_Shareholding_Disclosure/Australia__104001"
    p, _ = plan(products=PRODUCTS, exclude=[target])
    assert target not in {d["job"] for d in p["docs"] if d["decision"] == "promote"}
    assert PM.eligibility(p)["excluded"].get("skip/excluded") == 1


def test_excluding_nothing_is_the_same_as_not_excluding():
    a, _ = plan(products=PRODUCTS)
    b, _ = plan(products=PRODUCTS, exclude=[])
    c, _ = plan(products=PRODUCTS, exclude=None)
    same = lambda p: {d["job"]: d["decision"] for d in p["docs"]}  # noqa: E731
    assert same(a) == same(b) == same(c)


# ---------------- stage 4/5 (post-AI) overrides the plan-time verdict ----------------

def test_a_stage4_post_ai_verdict_overrides_the_frozen_stage3_gate(monkeypatch):
    """scorecard.json is frozen at stage 3 (the --resume completion marker); when stage
    4/5 ran, scorecard_post_ai.json is what the document now looks like, and a promotion
    that only ever read scorecard.json would publish (or gate on) a verdict the AI pass
    had already overturned -- the same rule extraction_monitor.job_detail and
    run_corpus._final_verdict already apply to the same question elsewhere in the app."""
    base = f"{PREFIX}/155_Data_Privacy/Norway__172590"
    keys = dict(KEYS)
    keys[f"{base}/scorecard.json"] = sc("pass", 96.0)
    keys[f"{base}/scorecard_post_ai.json"] = sc("fail", 41.0)
    keys[f"{base}/viewer.html"] = b"v" * 1000
    keys[f"{base}/inspect.html"] = b"i" * 500
    keys[f"{base}/source.pdf"] = b"%PDF" * 100
    keys[f"{base}/03_stage3_final/index.md"] = b"# x"
    monkeypatch.setitem(globals(), "KEYS", keys)

    p, s3 = plan()
    row = by_job(p)["155_Data_Privacy/Norway__172590"]
    assert row["gate"] == "fail" and row["worst_score"] == 41.0, \
        "the plan must gate on the post-AI verdict, not the frozen stage-3 pass"
    assert (row["decision"], row["reason"]) == ("skip", "gate")
    assert row["has_post_ai"] is True
    assert f"{base}/scorecard_post_ai.json" in s3.asked_keys
