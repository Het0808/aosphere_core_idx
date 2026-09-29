"""A Bedrock failure has to say WHICH failure it was.

Every modelled Bedrock error arrives as one Python class -- ClientError -- so `error.type`
read "ClientError" for a throttle, a model timeout, an access denial and a validation error
alike. Those need opposite responses: a throttle means slow down and the call will work,
an access denial means this role cannot call Bedrock at all and every retry is wasted. The
code that separates them was in the response the whole time and only the retry hook looked
at it, and only for attempts that were retried rather than for the failure that ended the
call.

Kibana has to be able to answer "are we being throttled?" with one filter. That is
aci.aws_error_code, and this file pins it for each shape of failure:

    ThrottlingException    modelled, retryable      -> code
    ModelTimeoutException  modelled, Bedrock's end  -> code
    ReadTimeoutError       socket, no response      -> error.type (there IS no code)
    ConnectTimeoutError    socket, no response      -> error.type
"""
import io
import json
import sys
import time
import types
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from aosphere_core_index.extract import ai_postprocess as A  # noqa: E402


def _client_error(code, status=400, request_id="req-abc-123"):
    return ClientError({"Error": {"Code": code, "Message": f"{code}: the long AWS text"},
                        "ResponseMetadata": {"RequestId": request_id,
                                             "HTTPStatusCode": status}}, "Converse")


def _raising(exc):
    class _C:
        def converse(self, **_kw):
            raise exc
    return _C()


class _Ok:
    def converse(self, **_kw):
        return {"output": {"message": {"content": [{"text": "<tr><td>x</td></tr>"}]}},
                "usage": {"inputTokens": 1200, "outputTokens": 340},
                "stopReason": "end_turn",
                "ResponseMetadata": {"RequestId": "req-ok-1"}}


def _batch():
    return types.SimpleNamespace(header="", html="<tr><td>x</td></tr>", rows=["x"])


def _end_event(fn):
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        fn()
    except BaseException:                                    # noqa: BLE001
        pass
    finally:
        sys.stdout = old
    events = [json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")]
    ends = [e for e in events if e["event.action"] == "bedrock.call.end"]
    assert ends, "the call emitted no end event"
    return ends[0]


@pytest.mark.parametrize("code", ["ThrottlingException", "ModelTimeoutException",
                                  "AccessDeniedException", "ValidationException",
                                  "ServiceQuotaExceededException"])
def test_a_modelled_failure_logs_the_AWS_CODE_not_just_ClientError(code):
    """The one filter that answers "are we being throttled". Without it every one of these
    is the same record and the question cannot be asked at all."""
    e = _end_event(lambda: A.call_model(_raising(_client_error(code)), "sonnet", _batch()))
    assert e["aci.aws_error_code"] == code
    assert e["error.type"] == "ClientError"          # kept: the Python class is still true
    assert code in e["message"], "the code belongs in the message a person reads first"
    # the handle AWS support and CloudWatch start from -- previously logged on SUCCESS and
    # dropped on failure, which is backwards
    assert e["aci.aws_request_id"] == "req-abc-123"
    assert e["aci.http_status"] == 400


@pytest.mark.parametrize("exc", [ReadTimeoutError(endpoint_url="https://bedrock.x"),
                                 ConnectTimeoutError(endpoint_url="https://bedrock.x")])
def test_a_socket_failure_is_named_by_its_class_and_claims_no_AWS_code(exc):
    """These never reached AWS, so there is no response and no code to report. Inventing one
    would be worse than the gap: the class name already says which half of the call failed."""
    e = _end_event(lambda: A.call_model(_raising(exc), "sonnet", _batch()))
    assert e["error.type"] == type(exc).__name__
    assert "aci.aws_error_code" not in e
    assert type(exc).__name__ in e["message"]


def test_every_call_records_when_it_started_and_what_it_cost_in_attempts():
    """So one record answers "when did this begin, how long did it take, how many attempts"
    without joining the start event to the end event in Kibana."""
    ok = _end_event(lambda: A.call_model(_Ok(), "sonnet", _batch()))
    assert ok["event.outcome"] == "success"
    assert isinstance(ok["aci.started_at"], (int, float))
    assert ok["aci.retry_count"] == 0                # a clean first attempt
    assert ok["event.duration_s"] >= 0
    assert ok["aci.tokens_in"] == 1200 and ok["aci.tokens_out"] == 340

    failed = _end_event(lambda: A.call_model(
        _raising(_client_error("ThrottlingException")), "sonnet", _batch()))
    assert isinstance(failed["aci.started_at"], (int, float))
    assert "aci.retry_count" in failed               # present on the failure path too


def test_the_retry_count_reports_what_botocore_actually_did(monkeypatch):
    """botocore retries INSIDE converse(), so a call that finally raises may have been three
    round trips -- and from outside, one slow attempt and three throttled ones look the same.
    The hook records the attempt; the call's own record carries it."""
    A._attempts.n = 0

    class _Events:
        def __init__(self):
            self.fn = None

        def register(self, name, fn):
            if "needs-retry" in name:
                self.fn = fn

    events = _Events()

    class _Session:
        def client(self, *_a, **_kw):
            return types.SimpleNamespace(meta=types.SimpleNamespace(events=events))

    import boto3
    orig = boto3.Session
    boto3.Session = lambda *a, **kw: _Session()
    try:
        A.bedrock_client()
    finally:
        boto3.Session = orig

    # The hook fires INSIDE converse, after call_model has reset the count for this call —
    # so the client has to retry the way botocore does rather than the test poking the
    # counter beforehand, which would be measuring the previous call.
    class _ThrottledThenGivesUp:
        def converse(self, **_kw):
            for attempt in (1, 2, 3):
                events.fn(response=(types.SimpleNamespace(status_code=429),
                                    {"Error": {"Code": "ThrottlingException",
                                               "Message": "slow down"},
                                     "ResponseMetadata": {"RequestId": "r-1"}}),
                          attempts=attempt)
            raise _client_error("ThrottlingException")

    e = _end_event(lambda: A.call_model(_ThrottledThenGivesUp(), "sonnet", _batch()))
    assert e["aci.retry_count"] == 3, "the call did not report what the retries cost"
    assert e["aci.aws_error_code"] == "ThrottlingException"

    # and the NEXT call starts from zero — a count that leaked between calls would make a
    # clean call inherit the previous one's throttling
    clean = _end_event(lambda: A.call_model(_Ok(), "sonnet", _batch()))
    assert clean["aci.retry_count"] == 0


def test_a_section_call_reports_the_same_way_as_a_table_call():
    """Two call sites, one contract. The section path is the one that runs six-wide, so it is
    the one whose failures get counted -- reporting differently there would make a Kibana
    count of throttles depend on which kind of call happened to hit the limit."""
    e = _end_event(lambda: A.repair_section(
        _raising(_client_error("ThrottlingException")), "# 1 Head\n\ntext", [],
        Path("/tmp"), "haiku"))
    assert e["aci.aws_error_code"] == "ThrottlingException"
    assert e["aci.aws_request_id"] == "req-abc-123"
    assert "aci.started_at" in e and "aci.retry_count" in e
    assert e["aci.purpose"] == "section_repair"


def test_the_extractor_cannot_itself_fail_while_reporting_a_failure():
    """It runs INSIDE the handler that is reporting the error, so anything it raises replaces
    a useful Bedrock reason with an unrelated exception and loses the original. Three real
    shapes did exactly that before it was guarded: Error arriving as a string, a non-numeric
    HTTPStatusCode, and a metadata block that is not a dict."""
    class _Odd(Exception):
        pass

    shapes = [
        "a string response",
        {"Error": "boom"},
        {"Error": {"Code": "X"}, "ResponseMetadata": "boom"},
        {"Error": {"Code": "X"}, "ResponseMetadata": {"HTTPStatusCode": "not-a-number"}},
        {},
        None,
    ]
    for shape in shapes:
        e = _Odd("x")
        e.response = shape
        out = A._aws_error(e)                       # must not raise, whatever it is handed
        assert isinstance(out, dict)

    # and the good shape still yields everything
    full = A._aws_error(_client_error("ThrottlingException", status=429))
    assert full == {"aci.aws_error_code": "ThrottlingException",
                    "aci.aws_error_message": "ThrottlingException: the long AWS text",
                    "aci.aws_request_id": "req-abc-123",
                    "aci.http_status": 429}


def test_all_three_bedrock_call_sites_report_a_throttle_the_same_way():
    """Three call sites: table repair, section repair, and the summary route's single call
    carrying every page image. If one reports differently, a Kibana count of throttles
    depends on which kind of call happened to hit the limit -- and the summary route is the
    likeliest of the three to hit it, being one call with the whole document in it."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import summary_ai_extract as S

    throttle = _client_error("ThrottlingException", status=429, request_id="req-7")
    client = _raising(throttle)
    sites = {
        "table": lambda: A.call_model(client, "sonnet", _batch()),
        "section": lambda: A.repair_section(client, "# 1 H\n\ntext", [], Path("/tmp"), "haiku"),
        "summary": lambda: S.transcribe(client, "sonnet", [(1, b"x")], "do it", 4096),
    }
    for name, call in sites.items():
        e = _end_event(call)
        assert e["aci.aws_error_code"] == "ThrottlingException", f"{name} lost the AWS code"
        assert e["aci.aws_request_id"] == "req-7", f"{name} lost the request id"
        assert e["aci.http_status"] == 429, f"{name} lost the status"
        assert "aci.retry_count" in e and "aci.started_at" in e, f"{name} is missing fields"
        assert "ThrottlingException" in e["message"], f"{name} does not name it in the message"


def test_each_concurrent_call_reports_its_OWN_retry_count():
    """Six sections run at once. A counter shared between them would credit a clean call with
    another call's throttling, which is the number people would act on."""
    import concurrent.futures

    class _RetriesThenFails:
        def __init__(self, n):
            self.n = n

        def converse(self, **_kw):
            for i in range(1, self.n + 1):
                A._attempts.n = i                   # what botocore's hook does, on this thread
                time.sleep(0.001)
            raise _client_error("ThrottlingException")

    def one(n):
        try:
            A.call_model(_RetriesThenFails(n), f"model-{n}", _batch())
        except BaseException:                                # noqa: BLE001
            pass

    # ONE capture around the whole block: six threads each swapping sys.stdout would
    # clobber each other's, which is a bug in the test rather than in the counter.
    buf = io.StringIO()
    old_stdout, sys.stdout = sys.stdout, buf
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(one, [1, 2, 3, 4, 5, 6]))
    finally:
        sys.stdout = old_stdout

    ends = [json.loads(line) for line in buf.getvalue().splitlines()
            if line.startswith("{") and '"bedrock.call.end"' in line]
    got = {e["aci.model"]: e["aci.retry_count"] for e in ends}
    assert got == {f"model-{n}": n for n in (1, 2, 3, 4, 5, 6)}


# --------------------------------------------------------- the fourth call site: stage 5


def test_stage_five_subchunking_used_to_be_the_one_bedrock_call_with_no_events_at_all():
    """Found on a full audit of every `except` wrapping `.converse(`. Three call sites logged
    start/end and the AWS code; this one — find_subsections, stage 5's boundary detection —
    caught every exception and `continue`d with nothing emitted: no start, no end, no error
    code, nothing. "no sub-chunking is a fine outcome" is true when the model found no
    boundaries and was never true when Bedrock throttled or denied the call, and from
    outside — a report, a log, Kibana — the two looked identical."""
    e = _end_event(lambda: A.find_subsections(
        _raising(_client_error("ThrottlingException", status=429, request_id="req-sub")),
        "<table><tr><td>6.2 Funds</td></tr></table>"))
    assert e["aci.aws_error_code"] == "ThrottlingException"
    assert e["aci.aws_request_id"] == "req-sub"
    assert e["aci.purpose"] == "subchunk_boundaries"
    assert "aci.retry_count" in e and "aci.started_at" in e


def test_stage_five_still_degrades_quietly_to_the_CALLER_no_events_change_that():
    """The events are new; the CONTRACT is not. A document with no sub-chunkable boundaries
    is a normal outcome and must not become a pipeline failure just because it is now
    logged -- find_subsections still returns empty and the caller still proceeds."""
    bounds, usage = A.find_subsections(
        _raising(_client_error("ThrottlingException")),
        "<table><tr><td>6.2 Funds</td></tr></table>")
    assert bounds == [] and usage == []


def test_stage_five_reports_a_clean_success_too():
    class _Ok2:
        def converse(self, **_kw):
            return {"output": {"message": {"content": [{"text": "[]"}]}},
                    "usage": {"inputTokens": 50, "outputTokens": 5}}

    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        A.find_subsections(_Ok2(), "<table><tr><td>6.2 Funds</td></tr></table>")
    finally:
        sys.stdout = old
    ev = [json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")]
    ends = [e for e in ev if e["event.action"] == "bedrock.call.end"]
    assert ends and ends[0]["event.outcome"] == "success"
