"""A vector store that mentions content the mount no longer has must not 500 the search.

The two are loaded by DIFFERENT steps of a deploy — publish the index, flip the pointer,
reindex in-pod — so during any cutover the store can hold a region the mount does not. That
happened moving this corpus to PDF-only extraction: "European Union", "Hong Kong" and
"Turkey" were renamed, "United States - States and Territories" went away, and every query
that touched one of them returned 500 to every user, with a traceback that ended at io.open
and never said which jurisdiction was missing.

A stale region should cost its own hit and log loudly. These tests pin both halves: results
still come back, and the failure is attributed.
"""

import json

import pytest

from aosphere_core_index.service import registry as R


def _content(keys=("A1.1",)):
    return {"doc": {"jurisdiction": "Testland", "product": "Data Privacy"},
            "sections": [{"key": k, "title": f"Clause {k}", "level": 2, "parent_key": "A1",
                          "elements": [{"kind": "answer", "text": "Text of " + k}]}
                         for k in keys],
            "answers_by_clause": {}, "guidance_by_id": {}, "alerts_by_id": {},
            "alerts": [], "alerts_by_clause": {}, "cited_by": {}}


def test_a_missing_content_file_names_the_region(tmp_path, monkeypatch):
    """The diagnostic half: the error must say WHICH region and WHICH path."""
    monkeypatch.setattr(R, "_content_path", lambda region: tmp_path / f"{region}.content.json")
    with pytest.raises(R.BundleUnavailable) as e:
        R._bundle_from_content("European Union")
    assert "European Union" in str(e.value)
    assert "content.json" in str(e.value)


def test_content_is_read_as_utf8_whatever_the_locale(tmp_path, monkeypatch):
    """These files carry Türkiye, Curaçao, em-dashes and curly quotes. read_text() with no
    encoding follows the process locale, so a container started with a non-UTF-8 locale
    turned them into UnicodeDecodeError."""
    p = tmp_path / "Türkiye.content.json"
    doc = _content()
    doc["sections"][0]["elements"][0]["text"] = "Türkiye — “quoted” Curaçao"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(R, "_content_path", lambda region: p)
    bundle = R._bundle_from_content("Türkiye")
    assert "Türkiye — “quoted”" in bundle.content["sections"][0]["elements"][0]["text"]


def test_a_corrupt_content_file_is_reported_not_raised_as_json_error(tmp_path, monkeypatch):
    p = tmp_path / "Broken.content.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(R, "_content_path", lambda region: p)
    with pytest.raises(R.BundleUnavailable):
        R._bundle_from_content("Broken")


def test_search_drops_the_stale_hit_and_keeps_the_rest(monkeypatch, caplog):
    """The outage half, end to end through search_all: two hits, one region unreadable."""
    good, gone = "Testland", "European Union"
    bundle = R.RegionBundle(region=good, doc=None, content=_content(),
                            sections_by_key={s["key"]: s for s in _content()["sections"]},
                            index=None)

    def fake_get_bundle(region):
        if region == gone:
            raise R.BundleUnavailable(f"{region}: cannot load /data/...{region}.content.json")
        return bundle

    hits = [{"jurisdiction": good, "key": "A1.1", "score": 0.9, "kind": "clause"},
            {"jurisdiction": gone, "key": "A1.1", "score": 0.8, "kind": "clause"}]
    monkeypatch.setattr(R, "get_bundle", fake_get_bundle)
    monkeypatch.setattr(R, "retrieve", lambda *a, **k: list(hits))
    monkeypatch.setattr(R, "get_multi",
                        lambda *a, **k: type("MI", (), {"regions": [good, gone]})())
    # search_all embeds the query itself; a unit test must not call Bedrock
    monkeypatch.setattr(R, "embedder", lambda: type("E", (), {
        "embed": staticmethod(lambda texts: [[0.0, 0.0]]), "name": "stub"})())
    monkeypatch.setattr("aosphere_core_index.embeddings.reranker.enabled", lambda: False)

    with caplog.at_level("WARNING"):
        out = R.search_all("controller to processor agreement", k=5)

    js = [h["jurisdiction"] for h in out]
    assert good in js, "a readable region must still be returned"
    assert gone not in js, "the unreadable region's hit must be dropped, not raised"
    assert any(gone in r.getMessage() for r in caplog.records), \
        "the drop must be logged with the region named"
