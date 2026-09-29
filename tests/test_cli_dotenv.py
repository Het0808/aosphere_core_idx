"""_load_dotenv strips a trailing " # comment" instead of keeping it as part of the value.

.env.example's own style is "KEY=value  # explanation" on one line throughout, and a value
this reads unquoted (ACI_DATA_DIR, ACI_AUTH_ENABLED, ...) previously ended up as
"data-local  # isolated build", not "data-local" -- a wrong path / never-equal-to-"0" flag
that fails silently rather than raising, since nothing here parses as a type error.
"""
import os

from aosphere_core_index.cli import _load_dotenv


def test_trailing_comment_is_not_part_of_the_value(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "ACI_DATA_DIR=data-local          # isolated local build\n"
        "ACI_AUTH_ENABLED=0              # force-disabled for local run\n"
    )
    for k in ("ACI_DATA_DIR", "ACI_AUTH_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    _load_dotenv()
    assert os.environ["ACI_DATA_DIR"] == "data-local"
    assert os.environ["ACI_AUTH_ENABLED"] == "0"


def test_a_value_with_no_comment_is_unaffected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ACI_LOG_LEVEL=INFO\n")
    monkeypatch.delenv("ACI_LOG_LEVEL", raising=False)
    _load_dotenv()
    assert os.environ["ACI_LOG_LEVEL"] == "INFO"


def test_existing_environment_still_wins_over_the_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ACI_LOG_LEVEL=DEBUG  # comment\n")
    monkeypatch.setenv("ACI_LOG_LEVEL", "WARNING")
    _load_dotenv()
    assert os.environ["ACI_LOG_LEVEL"] == "WARNING"
