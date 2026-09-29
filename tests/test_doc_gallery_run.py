"""The Doc Gallery can be pointed at an extraction RUN instead of the published gallery.

Why this exists: a run writes to corpus/<run>/, which is not where the gallery reads from
(index/<version>/doc-gallery/), because publishing is a separate step against a finished corpus.
So the only way to look at a run's output was one document at a time, through the links on the
monitor's progress rows — which is no way to judge whether a run is worth publishing.

The two properties under test are the ones that would go wrong quietly:

  * the DEFAULT is untouched. No ?run= means the published gallery, the same store, the same
    manifest — every deployed environment and every existing caller must be unaffected.
  * the listing stays SCOPED and CACHED. A finished run is ~80,000 objects and one sequential
    walk of it measured 28 seconds; the manifest is fanned out per product prefix and cached,
    and a run's contents only grow, so a slightly stale manifest under-reports rather than lies.
"""

import json

import pytest

from aosphere_core_index.service import doc_gallery as G

RUN = "2026-08-21-02"


def _sc(gate, worst, weakest="completeness"):
    return json.dumps({
        "gate": gate, "worst_score": worst, "weakest_dimension": weakest,
        "dimensions": {weakest: {"label": "Completeness", "score": worst, "critical": False}},
        "pages": {"total": 24, "counts": {"silent": 0, "flagged": 1}},
        "headings": {"matched": 30, "total": 31},
        "tables": [{"bucket": "ok"}, {"bucket": "failed"}],
        "active_finding_count": 2,
    }).encode()


# One product with three job dirs, a second product, and the run-level bookkeeping dirs.
KEYS = {
    f"corpus/{RUN}/155_Data_Privacy/Spain__172575/scorecard.json": _sc("pass", 96.1),
    f"corpus/{RUN}/155_Data_Privacy/Spain__172575/viewer.html": b"<html>spain</html>",
    f"corpus/{RUN}/155_Data_Privacy/Spain__172575/inspect.html": b"<html>inspect</html>",
    f"corpus/{RUN}/155_Data_Privacy/Spain__172575/source.pdf": b"%PDF",
    f"corpus/{RUN}/155_Data_Privacy/Spain__172575/03_stage3_final/01-intro.md": b"# intro",
    f"corpus/{RUN}/155_Data_Privacy/Denmark__178994/scorecard.json": _sc("fail", 58.8),
    # finished extraction whose viewer failed to build — build_review_artefacts is best-effort
    f"corpus/{RUN}/155_Data_Privacy/Denmark__178994/03_stage3_final/01-intro.md": b"# intro",
    # in flight: no scorecard, so not a gallery row
    f"corpus/{RUN}/155_Data_Privacy/Angola__160001/03_stage3_final/01-intro.md": b"# intro",
    f"corpus/{RUN}/124_MRAM/Austria__170369/scorecard.json": _sc("review", 74.0),
    f"corpus/{RUN}/124_MRAM/Austria__170369/viewer.html": b"<html>austria</html>",
    # a nested scorecard belongs to a retry attempt, not to a document
    f"corpus/{RUN}/124_MRAM/Austria__170369/attempt-2/scorecard.json": _sc("pass", 99.0),
    # run-level bookkeeping: never documents
    f"corpus/{RUN}/_progress/shard-0.json": b"{}",
    f"corpus/{RUN}/_failures/124_MRAM__Austria__170369.log": b"boom",
    f"corpus/{RUN}/_permanent/107_netalytics__Bahamas__84696.json": b"{}",
}


class FakeS3:
    """Records what was asked for, so the cost contract can be asserted."""

    def __init__(self, bucket=None, region=None):
        self.bucket = bucket or "bkt"
        self.asked_prefixes: list[str] = []
        self.asked_keys: list[str] = []

    def list_common_prefixes(self, prefix):
        self.asked_prefixes.append(prefix)
        out = set()
        for k in KEYS:
            if k.startswith(prefix):
                rest = k[len(prefix):]
                if "/" in rest:                      # only DIRECTORIES are common prefixes
                    out.add(prefix + rest.split("/", 1)[0] + "/")
        return sorted(out)

    def list_level(self, prefix):
        """Delimiter='/' — keys directly under the prefix, never below it."""
        self.asked_prefixes.append(prefix)
        return sorted(k for k in KEYS
                      if k.startswith(prefix) and "/" not in k[len(prefix):])

    def list_level_objects(self, prefix):
        """`list_level` plus each key's size — what walk_run_jobs asks for, so a
        promotion can total the bytes it is about to copy from the same listing."""
        return [{"key": k, "size": len(KEYS[k])} for k in self.list_level(prefix)]

    def list_level_with_prefixes(self, prefix):
        """`list_level_objects` + `list_common_prefixes`, from ONE listing — what
        walk_run_jobs now asks for so it can see a nested 04_stage4_ai/ directory
        without a second LIST call. Records exactly one entry in asked_prefixes,
        since real S3 answers both from one paginated response."""
        self.asked_prefixes.append(prefix)
        keys, prefixes = [], set()
        for k in KEYS:
            if not k.startswith(prefix):
                continue
            rest = k[len(prefix):]
            if "/" in rest:
                prefixes.add(prefix + rest.split("/", 1)[0] + "/")
            else:
                keys.append({"key": k, "size": len(KEYS[k])})
        return sorted(keys, key=lambda o: o["key"]), sorted(prefixes)

    def list_keys(self, prefix, suffix=None):
        self.asked_prefixes.append(prefix)
        return sorted(k for k in KEYS if k.startswith(prefix)
                      and (suffix is None or k.endswith(suffix)))

    def get_bytes(self, key):
        self.asked_keys.append(key)
        return KEYS[key]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Module state is process-wide by design (one store per process); reset it per test."""
    G._store = None
    G._run_stores.clear()
    G._version_stores.clear()
    G._sc_summary_cache.clear()
    monkeypatch.setenv("ACI_EXTRACTION_BUCKET", "aosphere-tenant-dev1-core-index")
    monkeypatch.setenv("ACI_EXTRACTION_PREFIX", "corpus")
    monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3", FakeS3)
    yield
    G._store = None
    G._run_stores.clear()
    G._version_stores.clear()


# ---------------------------------------------------------------- run ids

@pytest.mark.parametrize("run, ok", [
    ("2026-08-21-02", True),
    ("baseline-v2", True),
    ("_progress", False),          # run-level bookkeeping is not a run
    ("a/b", False),                # a run id is ONE path segment
    ("../index", False),
    ("", False),
    ("x" * 65, False),
])
def test_a_run_id_is_one_safe_path_segment(run, ok):
    assert G.valid_run(run) is ok


def test_an_invalid_run_is_refused_before_it_reaches_s3():
    with pytest.raises(ValueError):
        G.store("../index")


# ---------------------------------------------------------------- the default is untouched

def test_no_run_means_the_published_gallery(tmp_path, monkeypatch):
    """The whole point: the default path must not change."""
    monkeypatch.delenv("ACI_DOC_GALLERY_BUCKET", raising=False)
    monkeypatch.setenv("ACI_DOC_GALLERY_DIR", str(tmp_path))
    (tmp_path / "manifest.json").write_text(json.dumps(
        [{"slug": "s1", "product": "p", "region": "r", "status": "ok", "viewer": "v.html"}]))
    assert isinstance(G.store(), G.LocalDocStore)
    assert [d["slug"] for d in G.gallery_list()] == ["s1"]


def test_a_run_store_is_reused_rather_than_rebuilt_per_request():
    a, b = G.store(RUN), G.store(RUN)
    assert a is b


# ---------------------------------------------------------------- the synthesised manifest

def test_a_run_manifest_is_synthesised_from_what_the_run_contains():
    man = {d["slug"]: d for d in G.store(RUN).manifest()}
    assert sorted(man) == [
        "124_MRAM__Austria__170369",
        "155_Data_Privacy__Denmark__178994",
        "155_Data_Privacy__Spain__172575",
    ], "one row per finished document, and only those"

    spain = man["155_Data_Privacy__Spain__172575"]
    assert spain["product"] == "155_Data_Privacy"
    assert spain["region"] == "Spain" and spain["doc_id"] == "172575"
    assert spain["gate"] == "pass" and spain["worst_score"] == 96.1
    assert spain["viewer"] == "155_Data_Privacy/Spain__172575/viewer.html"
    assert spain["inspect"] == "155_Data_Privacy/Spain__172575/inspect.html"


def test_a_document_still_extracting_is_not_a_row():
    """No scorecard = not finished. Listing it would show a row of dashes for a document
    that may yet fail."""
    assert not any("Angola" in d["slug"] for d in G.store(RUN).manifest())


def test_a_retry_attempts_nested_scorecard_is_not_a_second_document():
    """corpus/<run>/<product>/<label>/attempt-2/scorecard.json is the same document, and
    counting it would inflate the run's totals — the same depth bug that once made resume
    think nested attempts were finished documents."""
    slugs = [d["slug"] for d in G.store(RUN).manifest()]
    assert slugs.count("124_MRAM__Austria__170369") == 1
    assert not any("attempt-2" in s for s in slugs)


def test_run_level_bookkeeping_directories_are_not_products():
    st = G.store(RUN)
    st.manifest()
    assert not any("_progress" in p or "_failures" in p or "_permanent" in p
                   for p in st.s3.asked_prefixes), st.s3.asked_prefixes


def test_a_document_with_no_viewer_says_so_instead_of_offering_a_dead_link():
    """123 of the 986 documents in run 2026-08-21-02 have no viewer — they finished between
    08-21 14:53 and 20:50, before build_review_artefacts existed in the worker image, while
    every document from 23:08 onwards has one. A table that links them anyway hands the reviewer
    a guaranteed 404 they cannot tell apart from a broken viewer."""
    rows = {d["slug"]: d for f in G.gallery_tree("all", RUN)["folders"]
            for j in f["jurisdictions"] for d in j["documents"]}
    assert rows["155_Data_Privacy__Spain__172575"]["has_viewer"] is True
    assert rows["155_Data_Privacy__Denmark__178994"]["has_viewer"] is False
    # and the flat list, which the card view reads
    lst = {d["slug"]: d for d in G.gallery_list(RUN)}
    assert lst["155_Data_Privacy__Denmark__178994"]["has_viewer"] is False


def test_a_document_whose_viewer_failed_to_build_has_no_viewer_but_is_still_listed():
    """build_review_artefacts is best-effort on purpose: a document with no viewer is still a
    successfully extracted document. It must not be dropped, and asking for its viewer must be
    a miss rather than a KeyError."""
    man = {d["slug"]: d for d in G.store(RUN).manifest()}
    dk = man["155_Data_Privacy__Denmark__178994"]
    assert "viewer" not in dk
    assert G.viewer_html("155_Data_Privacy__Denmark__178994", RUN) is None


def test_the_viewer_is_proxied_from_the_run_prefix():
    assert G.viewer_html("155_Data_Privacy__Spain__172575", RUN) == b"<html>spain</html>"
    assert G.inspect_html("155_Data_Privacy__Spain__172575", RUN) == b"<html>inspect</html>"
    assert G.scorecard_json("155_Data_Privacy__Spain__172575", RUN)["gate"] == "pass"


def test_a_jurisdiction_with_punctuation_still_produces_a_url_safe_slug():
    """Slugs travel in a URL PATH segment, and jurisdictions carry spaces, commas, parentheses
    and accents — "Canada (Alberta, British Columbia, ...)", "Aruba, Curaçao and St. Maarten"."""
    slug = G._run_slug("153_ISDA_e-contracts", "Canada (Alberta, British Columbia)__69636")
    assert "/" not in slug and " " not in slug and "(" not in slug
    assert slug.endswith("__69636")


# ---------------------------------------------------------------- cost

def test_the_manifest_is_built_once_and_then_served_from_cache():
    st = G.store(RUN)
    st.manifest()
    calls = len(st.s3.asked_prefixes)
    st.manifest()
    st.manifest()
    assert len(st.s3.asked_prefixes) == calls, "a cached manifest must not re-list the run"


def test_the_ttl_expiring_rebuilds_the_manifest(monkeypatch):
    st = G.store(RUN)
    st.manifest()
    calls = len(st.s3.asked_prefixes)
    st._built_at -= G.RUN_TTL_S + 1
    st.manifest()
    assert len(st.s3.asked_prefixes) > calls


def test_the_run_is_never_listed_recursively():
    """The performance contract, and the reason this store exists in the shape it does.

    A recursive listing of run 2026-08-21-02 returned 380,609 keys in 64.4 seconds — a job dir
    holds three artefacts the gallery wants and several hundred it does not. The build walks to
    the job directories by delimiter (986 prefixes, 0.9s) and reads one level of each instead,
    so list_keys must never be called at all."""
    class NoRecursion(FakeS3):
        def list_keys(self, prefix, suffix=None):
            raise AssertionError(f"recursive listing of {prefix} — 380,609 keys and 64s")

    st = G.RunDocStore("bkt", "corpus", RUN, "eu-west-1")
    st.s3 = NoRecursion("bkt")
    man = st._build()
    assert len(man) == 3
    root = f"corpus/{RUN}/"
    assert all(p.startswith(root) for p in st.s3.asked_prefixes)
    assert f"{root}155_Data_Privacy/Spain__172575/" in st.s3.asked_prefixes


def test_the_scorecard_table_reuses_the_scorecards_the_build_already_fetched():
    """The tree needs a dozen numbers out of every scorecard. Re-fetching them one at a time
    downstream cost 303 seconds for a 986-document run — longer than the build itself."""
    st = G.store(RUN)
    st.manifest()
    fetched = len(st.s3.asked_keys)
    G.gallery_tree("all", RUN)
    assert len(st.s3.asked_keys) == fetched, "the tree must not re-fetch a single scorecard"


def test_no_fetched_object_is_retained_as_bytes():
    """Scorecards averaged 97KB on run 2026-08-21-02 — 96MB for the run, in the same process as
    the vector matrix. Only the summary survives the build; a viewer (~1.7MB) is never held."""
    st = G.store(RUN)
    st.manifest()
    G.gallery_tree("all", RUN)
    G.viewer_html("155_Data_Privacy__Spain__172575", RUN)
    held = [v for v in vars(st).values() if isinstance(v, dict)
            and any(isinstance(x, (bytes, bytearray)) for x in v.values())]
    assert held == [], f"the store is holding raw bytes: {held}"
    # what IS kept is the summary, and it is scalars
    key = (st.cache_scope, "155_Data_Privacy/Spain__172575/scorecard.json", None)
    assert G._sc_summary_cache[key]["pages"] == 24


def test_only_finished_documents_scorecards_are_fetched():
    st = G.store(RUN)
    st.manifest()
    assert sorted(st.s3.asked_keys) == [
        f"corpus/{RUN}/124_MRAM/Austria__170369/scorecard.json",
        f"corpus/{RUN}/155_Data_Privacy/Denmark__178994/scorecard.json",
        f"corpus/{RUN}/155_Data_Privacy/Spain__172575/scorecard.json",
    ], "one GET per finished document, and nothing else"


def test_a_listing_failure_leaves_the_previous_manifest_rather_than_emptying_the_screen():
    st = G.store(RUN)
    first = st.manifest()
    assert first

    def boom(*a, **k):
        raise RuntimeError("throttled")

    st.s3.list_common_prefixes = boom
    st._built_at -= G.RUN_TTL_S + 1
    assert st.manifest() == first
    assert "throttled" in (st.last_error or "")


def test_an_unreadable_run_is_reported_as_an_error_not_as_an_empty_run(monkeypatch):
    """The two states produce the SAME empty manifest, and confusing them sends a reviewer
    looking for documents that never arrive instead of at a broken listing."""
    class Broken(FakeS3):
        def list_common_prefixes(self, prefix):
            raise RuntimeError("AccessDenied")

    monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3", Broken)
    tree = G.gallery_tree("all", "2026-08-24-09")
    assert tree["totals"]["documents"] == 0
    assert "AccessDenied" in tree["error"]
    assert G.read_error("2026-08-24-09") == tree["error"]


def test_a_healthy_run_reports_no_error():
    assert G.gallery_tree("all", RUN)["error"] is None
    assert G.read_error(RUN) is None


def test_an_unopenable_run_store_is_a_lookup_error_the_api_can_turn_into_503(monkeypatch):
    """Constructing the S3 client reads the AWS environment and raises on a broken one. That
    is unavailability, not a bug — and it is the one line in the run path the manifest guard
    does not cover."""
    def boom(*a, **k):
        raise RuntimeError("ProfileNotFound")

    monkeypatch.setattr("aosphere_core_index.aws.s3_readonly.ReadOnlyS3", boom)
    with pytest.raises(LookupError, match="cannot open extraction run"):
        G.store("2026-08-24-10")


def test_a_run_is_refused_when_no_extraction_bucket_is_configured(monkeypatch):
    monkeypatch.delenv("ACI_EXTRACTION_BUCKET", raising=False)
    monkeypatch.delenv("ACI_DOC_GALLERY_BUCKET", raising=False)
    with pytest.raises(LookupError, match="not configured"):
        G.store("2026-08-24-11")


# ---------------------------------------------------------------- the tree

def test_the_tree_for_a_run_rolls_up_the_runs_own_documents():
    tree = G.gallery_tree("all", RUN)
    assert tree["run"] == RUN
    assert tree["totals"]["documents"] == 3
    assert tree["totals"]["pass"] == 1 and tree["totals"]["fail"] == 1
    assert tree["totals"]["review"] == 1
    # worst product first
    assert tree["folders"][0]["product"] == "155_Data_Privacy"
    assert tree["folders"][0]["verdict"] == "fail"


def test_the_tree_reads_page_and_heading_numbers_from_the_scorecard_not_publish_stats():
    """A run has no parse_report stats — the publisher writes those. Every number the table
    ranks on has to come from the scorecard itself, or a run's rows would be empty."""
    tree = G.gallery_tree("all", RUN)
    docs = [d for f in tree["folders"] for j in f["jurisdictions"] for d in j["documents"]]
    spain = next(d for d in docs if d["doc_id"] == "172575")
    assert spain["pages"] == 24 and spain["pages_flagged"] == 1
    assert spain["headings_matched"] == 30 and spain["headings_total"] == 31
    assert spain["tables"] == 2 and spain["tables_failed"] == 1
    assert spain["findings"] == 2


def test_two_runs_do_not_serve_each_others_cached_scorecard_summaries(monkeypatch):
    """Every run addresses its documents by the SAME relative paths. Without a per-store cache
    scope, opening run B would show run A's numbers for the identical path — the worst kind of
    bug here, because the screen looks right.

    Asserted on numbers that come from the SCORECARD SUMMARY (pages, findings), not from the
    manifest: gate and worst_score are read per store and would agree even with a poisoned
    cache, so asserting on them would test nothing."""
    other = "2026-08-24-01"
    a = G.gallery_tree("all", RUN)

    b_keys = {k.replace(RUN, other): v for k, v in KEYS.items()}
    b_keys[f"corpus/{other}/155_Data_Privacy/Spain__172575/scorecard.json"] = json.dumps({
        "gate": "fail", "worst_score": 41.0, "weakest_dimension": "placement",
        "dimensions": {"placement": {"label": "Placement", "score": 41.0, "critical": True}},
        "pages": {"total": 99, "counts": {"silent": 7, "flagged": 8}},
        "headings": {"matched": 3, "total": 40},
        "tables": [{"bucket": "failed"}],
        "active_finding_count": 11,
    }).encode()
    monkeypatch.setitem(globals(), "KEYS", b_keys)
    b = G.gallery_tree("all", other)

    def spain(tree):
        return next(d for f in tree["folders"] for j in f["jurisdictions"]
                    for d in j["documents"] if d["doc_id"] == "172575")

    assert spain(a)["pages"] == 24 and spain(a)["findings"] == 2
    assert spain(a)["weakest"] == "Completeness"
    assert spain(b)["pages"] == 99, "run B must not be served run A's cached summary"
    assert spain(b)["findings"] == 11 and spain(b)["weakest"] == "Placement"


# ---------------------------------------------------------------- stage 4/5 (post-AI)

def test_a_stage4_post_ai_scorecard_overrides_the_frozen_stage3_verdict(monkeypatch):
    """scorecard.json is frozen at stage 3 (the --resume completion marker), but when
    stage 4/5 ran, scorecard_post_ai.json is the tree that actually ships — the same rule
    extraction_monitor.job_detail and run_corpus._final_verdict already apply. Before this,
    the Doc Gallery's run preview (and, downstream, the published gallery) never even
    looked for that file, so a document reviewers saw "AI stage 4" complete for in the
    pipeline monitor showed its stale pre-AI verdict here instead."""
    run = "2026-08-24-05"
    keys = {k.replace(RUN, run): v for k, v in KEYS.items()}
    keys[f"corpus/{run}/155_Data_Privacy/Spain__172575/scorecard_post_ai.json"] = json.dumps({
        "gate": "fail", "worst_score": 41.0, "weakest_dimension": "fidelity",
        "dimensions": {"fidelity": {"label": "Fidelity", "score": 41.0, "critical": True}},
        "pages": {"total": 24, "counts": {"silent": 0, "flagged": 1}},
        "headings": {"matched": 30, "total": 31},
        "tables": [{"bucket": "failed"}, {"bucket": "failed"}],
        "active_finding_count": 5,
    }).encode()
    monkeypatch.setitem(globals(), "KEYS", keys)

    tree = G.gallery_tree("all", run)
    spain = next(d for f in tree["folders"] for j in f["jurisdictions"]
                for d in j["documents"] if d["doc_id"] == "172575")
    assert spain["gate"] == "fail" and spain["worst_score"] == 41.0, \
        "the table must show the post-AI verdict, not the frozen stage-3 pass"
    assert spain["findings"] == 5

    man = {d["slug"]: d for d in G.store(run).manifest()}
    entry = man["155_Data_Privacy__Spain__172575"]
    assert entry["scorecard"].endswith("scorecard_post_ai.json")
    assert G.scorecard_json("155_Data_Privacy__Spain__172575", run)["gate"] == "fail"


def test_a_document_with_no_post_ai_scorecard_is_unaffected(monkeypatch):
    """The majority of documents never ran stage 4/5 — no second GET, no behaviour change."""
    run = "2026-08-24-06"
    keys = {k.replace(RUN, run): v for k, v in KEYS.items()}
    monkeypatch.setitem(globals(), "KEYS", keys)

    st = G.store(run)
    st.manifest()
    assert not any(k.endswith("scorecard_post_ai.json") for k in st.s3.asked_keys)
    man = {d["slug"]: d for d in st.manifest()}
    assert man["155_Data_Privacy__Spain__172575"]["scorecard"] == \
        "155_Data_Privacy/Spain__172575/scorecard.json"
    assert man["155_Data_Privacy__Spain__172575"]["gate"] == "pass"
