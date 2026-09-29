"""The file-backed prompt store (AOSNG-3442).

It exists so the override path can be exercised end to end before S3 lands. The tests are
weighted to the ways a prompt store can lie: reporting an unreadable prompt as "no override"
(the service would answer with the default while the UI showed a saved prompt), and leaving a
half-written file where a system prompt should be.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aosphere_core_index.llm import prompt_store as PS  # noqa: E402
from aosphere_core_index.llm.prompt_store_local import LocalFileBackend, from_env  # noqa: E402

PRODUCT = "Marketing Restrictions - Asset Management"


@pytest.fixture
def be(tmp_path):
    return LocalFileBackend(tmp_path / "prompts")


def test_a_saved_prompt_comes_back_with_its_audit_fields(be):
    be.put(PRODUCT, "MRAM PROMPT", "sme@aosphere.com")
    rec = be.get(PRODUCT)
    assert rec.prompt == "MRAM PROMPT"
    assert rec.updated_by == "sme@aosphere.com" and rec.updated_at > 0


def test_overwriting_replaces_rather_than_accumulates(be):
    be.put(PRODUCT, "FIRST", "a")
    be.put(PRODUCT, "SECOND", "b")
    assert be.get(PRODUCT).prompt == "SECOND"
    assert be.get(PRODUCT).updated_by == "b"
    assert len(list((be.dir).glob("*.prompt.json"))) == 1, "one file per product, not a pile"


def test_delete_restores_the_default_and_is_idempotent(be):
    be.put(PRODUCT, "X", None)
    be.delete(PRODUCT)
    assert be.get(PRODUCT) is None
    be.delete(PRODUCT)          # deleting what is not there must not raise


def test_the_listing_reports_real_names_and_which_modes(be):
    """Files are named by a hash of the product plus the mode, so the listing has to read
    them. It reports the MODES too, because the screen has to show which of the two answers
    a product has customised."""
    be.put(PRODUCT, "A", None, "summary")
    be.put(PRODUCT, "B", None, "explain")
    be.put("Data Privacy", "C", None, "explain")
    assert be.overrides() == {PRODUCT: {"summary", "explain"}, "Data Privacy": {"explain"}}


def test_awkward_product_names_round_trip(be):
    """Real names carry ampersands, parentheses, commas and accents — and macOS is
    case-insensitive where Linux is not, which is why files are named by hash."""
    for name in ["160_Bank_Confidentiality_&_Outsourcing",
                 "Canada (Alberta, British Columbia, Ontario and Quebec)",
                 "Aruba, Curaçao and St. Maarten"]:
        be.put(name, f"P for {name}", None)
        assert be.get(name).prompt == f"P for {name}"
    assert len(be.overrides()) == 3


def test_an_unreadable_prompt_refuses_rather_than_reporting_no_override(be):
    """THE one that matters. Reporting a corrupt file as "no override" would answer with the
    default while the UI showed a saved prompt — the SME would have no way to tell."""
    be.put(PRODUCT, "GOOD", None)
    f = next(be.dir.glob("*.prompt.json"))
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(PS.PromptStoreUnavailable):
        be.get(PRODUCT)


def test_an_empty_prompt_is_refused_even_at_this_layer(be):
    """The API turns an empty box into a DELETE, but an empty system prompt would strip the
    grounding and citation rules, so the store refuses one too."""
    with pytest.raises(PS.PromptStoreUnavailable):
        be.put(PRODUCT, "   \n ", None)
    assert be.get(PRODUCT) is None


def test_a_stored_file_that_is_valid_json_but_has_no_prompt_reads_as_absent(be):
    be.dir.mkdir(parents=True)
    (be.dir / "deadbeef.prompt.json").write_text(json.dumps({"product": PRODUCT}))
    assert be.get(PRODUCT) is None
    assert be.overrides() == {}


def test_a_broken_file_does_not_break_the_listing(be):
    be.put(PRODUCT, "GOOD", None)
    be.dir.joinpath("junk.prompt.json").write_text("{{{")
    assert be.overrides() == {PRODUCT: {"summary"}}, "one bad file must not hide the others"


def test_reading_before_anything_is_written_is_not_an_error(be):
    assert be.get(PRODUCT) is None
    assert be.overrides() == {}


def test_no_temp_files_are_left_behind(be):
    be.put(PRODUCT, "X", None)
    assert not list(be.dir.glob("*.tmp")), "an atomic write must clean up after itself"


# ------------------------------------------------------------------ configuration

def test_the_store_is_off_unless_the_directory_is_configured(monkeypatch):
    """The default must stay a refusal. A dev machine that silently persisted prompts to a
    container-local directory would show them as saved and lose them with the container."""
    monkeypatch.delenv("ACI_PROMPT_STORE_DIR", raising=False)
    assert from_env() is None
    monkeypatch.setattr(PS, "_backend", None)
    assert isinstance(PS.backend(), PS.NullBackend)


def test_setting_the_directory_installs_the_file_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("ACI_PROMPT_STORE_DIR", str(tmp_path))
    monkeypatch.setattr(PS, "_backend", None)
    b = PS.backend()
    assert isinstance(b, LocalFileBackend) and b.dir == tmp_path
    assert PS.backend() is b, "the backend is resolved once, not per call"


def test_an_explicitly_set_backend_still_wins(monkeypatch, tmp_path):
    """Tests and future configuration set the backend directly; the env var must not
    override an explicit choice."""
    monkeypatch.setenv("ACI_PROMPT_STORE_DIR", str(tmp_path))
    monkeypatch.setattr(PS, "_backend", None)
    null = PS.NullBackend()
    PS.set_backend(null)
    try:
        assert PS.backend() is null
    finally:
        PS.set_backend(PS.NullBackend())
