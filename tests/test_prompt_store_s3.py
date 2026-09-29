"""The S3 prompt store (AOSNG-3442).

Tested against a fake S3 client rather than a bucket, so the KEY LAYOUT, the caching and the
failure behaviour are pinned without credentials. The weighting reflects what would actually
hurt: a read failure reported as "no override" (the service answers with the default while the
UI shows a saved prompt), two products colliding onto one key, and an S3 GET landing on the
answer path of every question.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aosphere_core_index.llm import prompt_store as PS  # noqa: E402
from aosphere_core_index.llm.prompt_store_s3 import (  # noqa: E402
    S3Backend,
    from_env,
    key_for,
    parse_uri,
)

MRAM = "Marketing Restrictions - Asset Management"


class _Err(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    """Enough S3 to exercise the backend, and it counts calls."""

    def __init__(self):
        self.objects, self.gets, self.puts, self.deletes, self.lists = {}, 0, 0, 0, 0
        self.fail_get = None

    def get_object(self, Bucket, Key):  # noqa: N803
        self.gets += 1
        if self.fail_get:
            raise _Err(self.fail_get)
        if Key not in self.objects:
            raise _Err("NoSuchKey")
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.puts += 1
        self.objects[Key] = Body

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deletes += 1
        self.objects.pop(Key, None)

    def get_paginator(self, _name):
        return self

    def paginate(self, Bucket, Prefix):  # noqa: N803
        self.lists += 1
        yield {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}


class _Body:
    def __init__(self, b): self._b = b
    def read(self): return self._b


@pytest.fixture
def fake():
    return _FakeS3()


@pytest.fixture
def be(fake):
    return S3Backend("a-bucket", "prompts/", client=fake)


# ------------------------------------------------------------------ the key layout

def test_the_prefix_is_outside_any_index_version():
    """Prompts survive a reindex (AOSNG-3442 decision 4). Under index/<version>/ they would
    either be stranded at cutover or need a copy step that could silently drop one."""
    bucket, prefix = parse_uri("s3://aosphere-tenant-dev1-core-index/prompts/")
    assert bucket == "aosphere-tenant-dev1-core-index" and prefix == "prompts/"
    assert not prefix.startswith("index/")
    assert key_for(prefix, MRAM).startswith("prompts/")


@pytest.mark.parametrize("uri,want", [
    ("s3://b/prompts/", ("b", "prompts/")),
    ("s3://b/prompts", ("b", "prompts/")),      # a missing slash must not make prompts<slug>
    ("s3://b/", ("b", "")),
    ("s3://b", ("b", "")),
    ("  s3://b/a/b/  ", ("b", "a/b/")),
])
def test_uri_parsing(uri, want):
    assert parse_uri(uri) == want


@pytest.mark.parametrize("bad", ["", "prompts/", "https://b/p", "s3:///p"])
def test_a_malformed_uri_is_rejected_loudly(bad):
    """Misconfiguration must fail at startup, not resolve to a surprising bucket."""
    with pytest.raises(ValueError):
        parse_uri(bad)


def test_the_key_is_readable_but_unique():
    """The slug is for a human reading the bucket; the hash is what prevents a collision.
    Product names carry ampersands, parentheses, commas and accents."""
    k = key_for("prompts/", MRAM)
    assert k.startswith("prompts/marketing-restrictions-asset-management-") and k.endswith(".json")
    # two names that slug identically must NOT share a key
    a, b = key_for("prompts/", "A & B"), key_for("prompts/", "A / B".replace("/", "&"))
    assert key_for("prompts/", "Foo Bar") != key_for("prompts/", "Foo  Bar") or a == b
    seen = {key_for("prompts/", n) for n in
            ["Data Privacy", "Data  Privacy", "data privacy", "Data-Privacy", MRAM]}
    assert len(seen) == 5, "distinct product names must not collide onto one prompt"


def test_the_key_is_stable_across_calls():
    """It is the storage address. If it drifted, a save would land somewhere a read never
    looks and the prompt would appear to vanish."""
    assert key_for("prompts/", MRAM) == key_for("prompts/", MRAM)


# ------------------------------------------------------------------ round trip

def test_a_saved_prompt_reads_back_with_its_audit_fields(be, fake):
    be.put(MRAM, "MRAM PROMPT", "sme@aosphere.com")
    assert fake.puts == 1
    rec = be.get(MRAM)
    assert rec.prompt == "MRAM PROMPT" and rec.updated_by == "sme@aosphere.com"
    assert rec.updated_at > 0


def test_overwriting_replaces_in_place(be, fake):
    be.put(MRAM, "FIRST", "a")
    be.put(MRAM, "SECOND", "b")
    assert len(fake.objects) == 1, "one object per product"
    assert be.get(MRAM).prompt == "SECOND"


def test_delete_restores_the_default(be):
    be.put(MRAM, "X", None)
    be.delete(MRAM)
    assert be.get(MRAM) is None


def test_a_missing_object_is_absence_not_an_error(be):
    assert be.get(MRAM) is None


def test_the_listing_reports_product_names_and_modes_not_keys(be):
    """A key holds a slug and a hash; "Marketing Restrictions - Asset Management" cannot be
    recovered from it, so the names must come from inside the objects. The modes come with
    them, because the screen shows which of the two answers each product has customised."""
    be.put(MRAM, "A", None, "summary")
    be.put(MRAM, "B", None, "explain")
    be.put("Data Privacy", "C", None, "explain")
    assert be.overrides() == {MRAM: {"summary", "explain"}, "Data Privacy": {"explain"}}


# ------------------------------------------------------------------ failure behaviour

def test_a_read_failure_refuses_rather_than_reporting_no_override(be, fake):
    """THE one that matters. AccessDenied reported as "no override" would answer every
    question with the default while the Prompts screen showed a saved prompt — and nothing
    anywhere would say why."""
    be.put(MRAM, "GOOD", None)
    be._cache.clear()
    fake.fail_get = "AccessDenied"
    with pytest.raises(PS.PromptStoreUnavailable):
        be.get(MRAM)


def test_a_failed_read_still_leaves_ai_mode_answering():
    """resolve() must absorb the refusal, fall back to the default, and put the reason in
    `source` — a broken prompt store cannot take AI Mode down."""
    from aosphere_core_index.llm.product_prompt import resolve

    class _Broken:
        def get(self, product):
            raise PS.PromptStoreUnavailable("AccessDenied")

    r = resolve([MRAM], _Broken())
    assert r.is_default and "unavailable" in r.source


def test_corrupt_json_refuses(be, fake):
    fake.objects[key_for("prompts/", MRAM)] = b"{not json"
    with pytest.raises(PS.PromptStoreUnavailable):
        be.get(MRAM)


def test_one_unreadable_object_does_not_hide_the_listing(be, fake):
    be.put(MRAM, "GOOD", None)
    fake.objects["prompts/junk.json"] = b"{{{"
    assert be.overrides() == {MRAM: {"summary"}}


def test_an_empty_prompt_is_refused(be):
    with pytest.raises(PS.PromptStoreUnavailable):
        be.put(MRAM, "  \n ", None)


def test_an_object_with_no_prompt_reads_as_absent(be, fake):
    fake.objects[key_for("prompts/", MRAM)] = json.dumps({"product": MRAM}).encode()
    assert be.get(MRAM) is None


# ------------------------------------------------------------------ caching

def test_reads_are_cached_so_s3_is_not_on_the_answer_path(be, fake):
    """resolve() runs on EVERY AI Mode question. An uncached backend would add an S3 GET to
    every answer — latency, cost, and a hard dependency on S3 for a feature that is supposed
    to degrade gracefully to the default."""
    be.put(MRAM, "P", None)
    before = fake.gets
    for _ in range(20):
        assert be.get(MRAM).prompt == "P"
    assert fake.gets == before, "20 questions must not be 20 GETs"


def test_a_save_is_visible_to_this_process_at_once(be):
    """Otherwise an SME saves a prompt, asks a question on the same pod, gets the old one for
    up to a minute, and reasonably concludes the feature is broken."""
    be.put(MRAM, "FIRST", None)
    assert be.get(MRAM).prompt == "FIRST"
    be.put(MRAM, "SECOND", None)
    assert be.get(MRAM).prompt == "SECOND", "a write must invalidate its own cache entry"
    be.delete(MRAM)
    assert be.get(MRAM) is None


def test_a_write_invalidates_the_listing_too(be):
    assert be.overrides() == {}
    be.put(MRAM, "P", None)
    assert be.overrides() == {MRAM: {"summary"}}, "the screen must not show stale state"


def test_absence_is_cached_as_well(be, fake):
    """A product on the default is the common case; it must not cost a GET per question."""
    assert be.get(MRAM) is None
    before = fake.gets
    for _ in range(10):
        assert be.get(MRAM) is None
    assert fake.gets == before


def test_the_ttl_is_bounded_so_another_pods_save_is_picked_up():
    """The cache is per-process. A save on one pod must reach the others without a restart."""
    from aosphere_core_index.llm import prompt_store_s3 as S3
    assert 0 < S3._TTL <= 300, "a long TTL would strand a prompt saved elsewhere"


# ------------------------------------------------------------------ configuration

def test_the_store_is_off_unless_configured(monkeypatch):
    monkeypatch.delenv("ACI_PROMPT_STORE_S3", raising=False)
    assert from_env() is None


def test_s3_wins_over_a_local_directory(monkeypatch, tmp_path):
    """An environment given a bucket meant to use it; falling back to a container-local
    directory would look like it worked and lose the prompts on restart."""
    monkeypatch.setenv("ACI_PROMPT_STORE_S3", "s3://b/prompts/")
    monkeypatch.setenv("ACI_PROMPT_STORE_DIR", str(tmp_path))
    monkeypatch.setattr(PS, "_backend", None)
    try:
        assert isinstance(PS.backend(), S3Backend)
    finally:
        PS.set_backend(PS.NullBackend())


# ------------------------------------------------------------------ logging

def test_a_read_failure_is_logged_with_the_aws_error_code(be, fake, caplog):
    """The fallback is otherwise SILENT server-side: the question still gets answered, nothing
    500s, and an operator sees a healthy service quietly ignoring every prompt an SME wrote.

    The AWS CODE is the point. AccessDenied, NoSuchBucket and ExpiredToken are three different
    operator actions, and the exception class — ClientError for all three — separates none of
    them, which is exactly the debugging dead end this avoids.
    """
    fake.fail_get = "AccessDenied"
    with caplog.at_level("WARNING"), pytest.raises(PS.PromptStoreUnavailable):
        be.get(MRAM)
    msg = caplog.text
    assert "AccessDenied" in msg
    assert "s3:GetObject" in msg, "the log must name the permission to grant"
    assert "DEFAULT" in msg, "it must say what the service is doing instead"
    assert MRAM in msg


def test_a_broken_store_does_not_log_once_per_question(be, fake, caplog):
    """resolve() runs on every AI Mode question. Unthrottled, a bad IAM grant would write one
    warning per question for as long as the outage lasted."""
    fake.fail_get = "AccessDenied"
    with caplog.at_level("WARNING"):
        for _ in range(50):
            with pytest.raises(PS.PromptStoreUnavailable):
                be.get(MRAM)
    hits = [r for r in caplog.records if "could not read" in r.message]
    assert len(hits) == 1, f"50 failed questions produced {len(hits)} log lines"


def test_a_failed_save_is_logged_at_error_and_never_throttled(be, fake, caplog):
    """A save is a deliberate human action that just failed in front of someone; there is one
    line per attempt, not per question, so throttling would only hide it."""
    def boom(**kw):
        raise _Err("AccessDenied")
    fake.put_object = boom
    with caplog.at_level("WARNING"):
        for _ in range(3):
            with pytest.raises(PS.PromptStoreUnavailable):
                be.put(MRAM, "P", "sme@aosphere.com")
    errs = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errs) == 3, "every failed save must be visible"
    assert "s3:PutObject" in caplog.text and "sme@aosphere.com" in caplog.text


def test_a_failed_delete_says_the_override_is_still_in_force(be, fake, caplog):
    """The UI reports a failure, but the operator needs to know the OLD prompt is still
    answering questions — that is the part with a consequence."""
    be.put(MRAM, "P", None)

    def boom(**kw):
        raise _Err("AccessDenied")
    fake.delete_object = boom
    with caplog.at_level("WARNING"), pytest.raises(PS.PromptStoreUnavailable):
        be.delete(MRAM)
    assert "still in force" in caplog.text and "s3:DeleteObject" in caplog.text


def test_a_listing_failure_names_the_symptom_the_operator_will_see(be, fake, caplog):
    def boom(**kw):
        raise _Err("AccessDenied")
    fake.paginate = boom
    with caplog.at_level("WARNING"), pytest.raises(PS.PromptStoreUnavailable):
        be.overrides()
    assert "s3:ListBucket" in caplog.text
    assert "on the default" in caplog.text, "connect the cause to the visible symptom"


def test_the_configured_store_is_logged_at_startup(monkeypatch, caplog):
    """So a pod's logs answer "which store did this one pick?" without a shell on it."""
    monkeypatch.setenv("ACI_PROMPT_STORE_S3", "s3://a-bucket/prompts/")
    with caplog.at_level("INFO"):
        assert from_env() is not None
    assert "a-bucket" in caplog.text and "prompts/" in caplog.text


def test_a_malformed_uri_fails_at_startup_not_mid_request(monkeypatch):
    """Better a pod that will not start than one that 503s on the first save."""
    monkeypatch.setenv("ACI_PROMPT_STORE_S3", "not-an-s3-uri")
    with pytest.raises(ValueError):
        from_env()


# ------------------------------------------------------------------ the zero-config default

@pytest.fixture
def clean_env(monkeypatch):
    for v in ("ACI_PROMPT_STORE_S3", "ACI_PROMPT_STORE_DIR", "ACI_DOC_GALLERY_BUCKET",
              "ACI_EXTRACTION_BUCKET", "ACI_DOC_GALLERY_REGION", "ACI_EXTRACTION_REGION"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(PS, "_backend", None)
    yield monkeypatch
    PS.set_backend(PS.NullBackend())


def test_the_default_needs_no_configuration_beyond_the_environments_own_bucket(clean_env):
    """No prompt-store setting at all: an environment that has a gallery bucket has a prompt
    store, at prompts/ in it."""
    clean_env.setenv("ACI_DOC_GALLERY_BUCKET", "aosphere-tenant-dev1-core-index")
    b = PS.backend()
    assert isinstance(b, S3Backend)
    assert b.bucket == "aosphere-tenant-dev1-core-index" and b.prefix == "prompts/"


def test_the_default_never_uses_the_read_only_ingest_bucket(clean_env):
    """The trap this avoids. `settings.source_bucket` looks like the obvious default, but it
    is the READ-ONLY ingest bucket, it defaults to a PRODUCTION bucket, and nothing in the
    deployment sets it — so defaulting to it would have had dev1 writing prompts into prod.
    """
    from aosphere_core_index.config import settings
    from aosphere_core_index.llm.prompt_store_s3 import default

    clean_env.setenv("ACI_DOC_GALLERY_BUCKET", "a-dev-bucket")
    assert "prod" in settings.source_bucket, "if this changed, re-read the reasoning above"
    assert default().bucket != settings.source_bucket


def test_the_extraction_bucket_is_accepted_as_well(clean_env):
    """extraction_target() honours both names; the prompt store must not disagree with it."""
    clean_env.setenv("ACI_EXTRACTION_BUCKET", "extract-bucket")
    assert PS.backend().bucket == "extract-bucket"


def test_with_no_bucket_anywhere_there_is_nothing_to_guess(clean_env):
    """A laptop. Better to refuse writes with a reason than to invent a bucket."""
    from aosphere_core_index.llm.prompt_store_s3 import default
    assert default() is None
    assert isinstance(PS.backend(), PS.NullBackend)


def test_an_explicit_uri_outranks_the_default(clean_env):
    clean_env.setenv("ACI_DOC_GALLERY_BUCKET", "default-bucket")
    clean_env.setenv("ACI_PROMPT_STORE_S3", "s3://chosen-bucket/elsewhere/")
    b = PS.backend()
    assert b.bucket == "chosen-bucket" and b.prefix == "elsewhere/"


def test_an_explicit_local_directory_outranks_the_default(clean_env, tmp_path):
    """Explicit beats implicit: a developer who asked for a directory must not silently be
    writing to a bucket."""
    from aosphere_core_index.llm.prompt_store_local import LocalFileBackend

    clean_env.setenv("ACI_DOC_GALLERY_BUCKET", "default-bucket")
    clean_env.setenv("ACI_PROMPT_STORE_DIR", str(tmp_path))
    assert isinstance(PS.backend(), LocalFileBackend)


def test_the_default_prefix_is_a_constant_not_derived_from_the_index_version(clean_env):
    """Prompts survive a reindex. A prefix that moved with the index version would strand
    every override at the next cutover."""
    from aosphere_core_index.llm.prompt_store_s3 import DEFAULT_PREFIX
    assert DEFAULT_PREFIX == "prompts/"
    assert "index" not in DEFAULT_PREFIX and "{" not in DEFAULT_PREFIX


# ------------------------------------------------------------ prompts saved before modes

LEGACY = b'{"product": "Marketing Restrictions - Asset Management", "prompt": "OLD PROMPT", \
"updated_by": "someone@aosphere.com"}'


def test_a_prompt_saved_before_modes_is_still_found(be, fake):
    """A colleague's 13.6KB prompt was live on dev under the pre-mode key when modes were
    added. Changing the key layout without reading it would have orphaned it and the service
    would have answered every question with the default — silently."""
    from aosphere_core_index.llm.prompt_store_s3 import legacy_key_for

    fake.objects[legacy_key_for("prompts/", MRAM)] = LEGACY
    for mode in ("summary", "explain"):
        rec = be.get(MRAM, mode)
        assert rec is not None, f"the pre-mode prompt is not being read for {mode}"
        assert rec.prompt == "OLD PROMPT"
        assert rec.legacy is True, "it must be marked, so assembly keeps its old shape"
        be._cache.clear()


def test_a_pre_mode_prompt_keeps_the_answer_format_it_had(be, fake):
    """It was the product half WITH the built-in format block appended. Reading it as a mode
    override would drop that block and silently change how it answers."""
    from aosphere_core_index.llm.agent import _ANSWER_BRIEF, instructions_for
    from aosphere_core_index.llm.prompt_store import ResolverAdapter
    from aosphere_core_index.llm.prompt_store_s3 import legacy_key_for
    from aosphere_core_index.llm.product_prompt import resolve

    fake.objects[legacy_key_for("prompts/", MRAM)] = LEGACY
    r = resolve([MRAM], ResolverAdapter(be), "summary")
    assert r.prompt == "OLD PROMPT" and r.owns_format is False
    assert "pre-mode" in r.source, "the trace should say why this one is different"
    assert instructions_for(False, r.prompt, r.owns_format) == "OLD PROMPT" + _ANSWER_BRIEF


def test_saving_a_mode_takes_over_from_the_pre_mode_prompt(be, fake):
    """The migration path, and it needs no script: saving either mode writes a per-mode key,
    which is preferred from then on. The other mode keeps using the old prompt until it too
    is saved."""
    from aosphere_core_index.llm.prompt_store_s3 import legacy_key_for

    fake.objects[legacy_key_for("prompts/", MRAM)] = LEGACY
    be.put(MRAM, "NEW SUMMARY", "sme@aosphere.com", "summary")
    be._cache.clear()
    s, e = be.get(MRAM, "summary"), be.get(MRAM, "explain")
    assert s.prompt == "NEW SUMMARY" and s.legacy is False
    assert e.prompt == "OLD PROMPT" and e.legacy is True


def test_the_pre_mode_prompt_is_never_written_only_read(be, fake):
    """Read-only compatibility. Writing to the old key would resurrect a layout nothing else
    reads and leave two sources of truth."""
    be.put(MRAM, "P", None, "summary")
    be.put(MRAM, "Q", None, "explain")
    assert all("-summary.json" in k or "-explain.json" in k for k in fake.objects), \
        f"a write used the pre-mode key: {sorted(fake.objects)}"


def test_a_pre_mode_prompt_shows_against_both_modes_in_the_listing(be, fake):
    """It answers both, so a screen showing either as "default" would be lying about what is
    in force."""
    from aosphere_core_index.llm.prompt_store_s3 import legacy_key_for

    fake.objects[legacy_key_for("prompts/", MRAM)] = LEGACY
    assert be.overrides() == {MRAM: {"summary", "explain"}}
