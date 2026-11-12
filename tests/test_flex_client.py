"""Flex Web Service client against canned IBKR responses: no network, a fake clock."""

import datetime as dt
import urllib.error
import urllib.parse

import pytest

from ibkr_to_xero.flex_client import (
    FlexClient,
    FlexError,
    FlexQueryError,
    FlexTokenError,
    FlexUnavailableError,
)

TOKEN = "123456789012345678901234"
JUN1, JUN30 = dt.date(2026, 6, 1), dt.date(2026, 6, 30)
REPORT = b'<FlexQueryResponse queryName="q" type="AF"><FlexStatements count="1"/></FlexQueryResponse>'


def _status(status, code=None, message=None, reference=None):
    parts = [f"<Status>{status}</Status>"]
    if reference:
        parts.append(f"<ReferenceCode>{reference}</ReferenceCode>")
    if code:
        parts.append(f"<ErrorCode>{code}</ErrorCode><ErrorMessage>{message}</ErrorMessage>")
    return f"<FlexStatementResponse timestamp='x'>{''.join(parts)}</FlexStatementResponse>".encode()


SENT = _status("Success", reference="REF42")
IN_PROGRESS = _status("Warn", 1019, "Statement generation in progress. Please try again shortly.")


class _Response:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeIBKR:
    """Plays back responses (bytes, or an exception to raise) and records requests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []  # (endpoint, params, user agent)
        self.now = 0.0
        self.sleeps = []

    def open(self, request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        params = dict(urllib.parse.parse_qsl(url.query))
        self.requests.append((url.path.rsplit("/", 1)[1], params, request.get_header("User-agent")))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _Response(response)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def client(self):
        return FlexClient(TOKEN, opener=self.open, sleep=self.sleep, monotonic=lambda: self.now)


def test_fetch_polls_until_the_report_is_ready():
    ibkr = FakeIBKR(SENT, IN_PROGRESS, IN_PROGRESS, REPORT)
    progress = []
    assert ibkr.client().fetch("1705", JUN1, JUN30, progress=progress.append) == REPORT
    (send, send_params, agent), *gets = ibkr.requests
    assert send == "SendRequest"
    assert send_params == {"t": TOKEN, "q": "1705", "fd": "20260601", "td": "20260630", "v": "3"}
    assert agent.startswith("ibkr2xero")  # a generic client name, nothing personal
    assert [g[0] for g in gets] == ["GetStatement"] * 3
    assert all(g[1] == {"t": TOKEN, "q": "REF42", "v": "3"} for g in gets)
    assert ibkr.sleeps == [5.0, 5.0, 10.0]  # first wait, then back-off doubling
    assert progress == ["Statement generation in progress. Please try again shortly."] * 2


def test_rate_limit_is_waited_out():
    too_many = _status("Warn", 1018, "Too many requests have been made from this token.")
    ibkr = FakeIBKR(too_many, SENT, REPORT)
    assert ibkr.client().fetch("1705", JUN1, JUN30) == REPORT
    assert [r[0] for r in ibkr.requests] == ["SendRequest", "SendRequest", "GetStatement"]


@pytest.mark.parametrize("code, message", [(1012, "Token has expired."), (1015, "Token is invalid.")])
def test_token_problems_say_what_to_do(code, message):
    ibkr = FakeIBKR(_status("Fail", code, message))
    with pytest.raises(FlexTokenError, match="make a new token") as excinfo:
        ibkr.client().fetch("1705", JUN1, JUN30)
    assert excinfo.value.code == code
    assert TOKEN not in str(excinfo.value)


def test_invalid_query_rejected():
    ibkr = FakeIBKR(_status("Fail", 1014, "Query is invalid."))
    with pytest.raises(FlexQueryError, match="check the query ID"):
        ibkr.client().fetch("1705", JUN1, JUN30)


def test_unknown_error_code_ends_the_fetch():
    ibkr = FakeIBKR(SENT, _status("Fail", 1017, "Reference code is invalid."))
    with pytest.raises(FlexError, match="Reference code is invalid") as excinfo:
        ibkr.client().fetch("1705", JUN1, JUN30)
    assert excinfo.value.code == 1017


def test_gives_up_after_max_wait():
    ibkr = FakeIBKR(SENT, *[IN_PROGRESS] * 10)
    with pytest.raises(FlexUnavailableError, match="gave up waiting"):
        ibkr.client().fetch("1705", JUN1, JUN30, max_wait=60)
    assert ibkr.now <= 60


@pytest.mark.parametrize("failure", [
    urllib.error.HTTPError(f"https://x/?t={TOKEN}", 503, "Service Unavailable", {}, None),
    urllib.error.URLError(ConnectionRefusedError()),
    TimeoutError(),
])
def test_network_failures_never_leak_the_token(failure):
    ibkr = FakeIBKR(failure)
    with pytest.raises(FlexUnavailableError) as excinfo:
        ibkr.client().fetch("1705", JUN1, JUN30)
    assert TOKEN not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


def test_unreadable_response_rejected():
    ibkr = FakeIBKR(b"<html>maintenance</html")
    with pytest.raises(FlexError, match="unreadable response"):
        ibkr.client().fetch("1705", JUN1, JUN30)


def test_date_ranges_ibkr_would_refuse():
    client = FakeIBKR().client()
    with pytest.raises(ValueError, match="at most 365 days"):
        client.fetch("1705", dt.date(2024, 7, 1), dt.date(2025, 7, 1))
    with pytest.raises(ValueError, match="ends before it starts"):
        client.fetch("1705", JUN30, JUN1)


def test_empty_token_rejected():
    with pytest.raises(ValueError, match="empty Flex token"):
        FlexClient("  ")
