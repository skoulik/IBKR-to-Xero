"""Client for IBKR's Flex Web Service: run an Activity Flex Query, get its report.

Two calls (plan Background): SendRequest (token, query ID, date range) returns
a reference code; GetStatement (token, reference code) returns the report once
IBKR has generated it — seconds for a month, many minutes for a year — so it
is polled with back-off. The token is a parameter: where it is stored is the
caller's business (plan D9), and it never appears in an error message.

No I/O besides HTTPS to IBKR; the HTTP opener and the clock are injectable so
tests run without the network.
"""

from __future__ import annotations

import datetime as dt
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from importlib import metadata
from typing import Callable

BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"

# One request covers at most 365 days (IBKR; integrators report the same for
# explicit date ranges). Longer histories are fetched in chunks by the caller.
MAX_DAYS = 365

# IBKR error codes (Flex Web Service docs, "Error Codes"). Transient ones are
# retried until the deadline; the rest end the fetch.
_TRANSIENT = {
    1001,  # statement could not be generated at this time
    1004,  # statement is incomplete at this time
    1005,  # settlement data is not ready
    1006,  # FIFO P/L data is not ready
    1007,  # MTM P/L data is not ready
    1008,  # MTM and FIFO P/L data is not ready
    1009,  # server under heavy load
    1018,  # too many requests (1 per second, 10 per minute per token)
    1019,  # statement generation in progress
    1021,  # statement could not be retrieved at this time
}
_TOKEN = {
    1011,  # service account is inactive
    1012,  # token has expired
    1013,  # IP restriction
    1015,  # token is invalid
    1025,  # too many failed attempts
}
_QUERY = {
    1010,  # legacy Flex Queries are no longer supported
    1014,  # query is invalid
    1016,  # account is invalid
    1020,  # invalid request or unable to validate request
}

_TOKEN_HINT = (
    "make a new token in Client Portal (Settings > Account Reports > Flex Web Service); "
    "a token can only run Flex reports"
)
_QUERY_HINT = "check the query ID and that it is an Activity Flex Query"

_FIRST_POLL = 5.0  # seconds after SendRequest before the first GetStatement
_MAX_POLL = 30.0
_DEFAULT_MAX_WAIT = 3600.0  # a year's report has taken over 20 minutes


class FlexError(Exception):
    """The Flex Web Service did not deliver a report.

    code: IBKR's error code, when it gave one.
    """

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class FlexTokenError(FlexError):
    """The token is expired, invalid or restricted: the user must act."""


class FlexQueryError(FlexError):
    """The query ID or the request is not usable."""


class FlexUnavailableError(FlexError):
    """IBKR could not be reached, or did not deliver in time; trying later may work."""


def _user_agent() -> str:
    try:
        return f"ibkr2xero/{metadata.version('ibkr-to-xero')}"
    except metadata.PackageNotFoundError:
        return "ibkr2xero"


class FlexClient:
    """Runs Activity Flex Queries with one token.

    opener(request, timeout) -> response with .read(), like urlopen; sleep and
    monotonic are the clock. progress, if given, is called with a short status
    line while a report is being generated.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        timeout: float = 120.0,
        opener: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not token.strip():
            raise ValueError("empty Flex token")
        self._token = token.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep
        self._monotonic = monotonic

    def fetch(
        self,
        query_id: str,
        from_date: dt.date,
        to_date: dt.date,
        *,
        max_wait: float = _DEFAULT_MAX_WAIT,
        progress: Callable[[str], None] | None = None,
    ) -> bytes:
        """Run the query for from_date..to_date (inclusive) and return the report.

        Raises FlexTokenError, FlexQueryError, FlexUnavailableError (incl. not
        delivered within max_wait seconds) or FlexError; ValueError for a
        range IBKR would refuse.
        """
        if to_date < from_date:
            raise ValueError(f"date range ends before it starts: {from_date} to {to_date}")
        days = (to_date - from_date).days + 1
        if days > MAX_DAYS:
            raise ValueError(f"a Flex request covers at most {MAX_DAYS} days, not {days}")
        deadline = self._monotonic() + max_wait
        reference = self._send_request(
            str(query_id).strip(), from_date, to_date, deadline, progress
        )
        return self._get_statement(reference, deadline, progress)

    # --- the two calls -------------------------------------------------------

    def _send_request(self, query_id, from_date, to_date, deadline, progress) -> str:
        params = {
            "t": self._token,
            "q": query_id,
            "fd": from_date.strftime("%Y%m%d"),
            "td": to_date.strftime("%Y%m%d"),
            "v": "3",
        }
        delay = _FIRST_POLL
        while True:
            response = _status(self._get("SendRequest", params))
            if response is None:
                raise FlexError("SendRequest: unexpected response from IBKR")
            status, code, message, reference = response
            if status == "Success" and reference:
                return reference
            delay = self._wait_or_raise("SendRequest", code, message, delay, deadline, progress)

    def _get_statement(self, reference, deadline, progress) -> bytes:
        params = {"t": self._token, "q": reference, "v": "3"}
        delay = _FIRST_POLL
        self._sleep(delay)  # generation takes at least a few seconds
        while True:
            body = self._get("GetStatement", params)
            response = _status(body)
            if response is None:
                return body  # the report itself
            _, code, message, _ = response
            delay = self._wait_or_raise("GetStatement", code, message, delay, deadline, progress)

    def _wait_or_raise(self, endpoint, code, message, delay, deadline, progress) -> float:
        """Sleep before retrying a transient error and return the next delay; raise otherwise."""
        number = int(code) if code and code.isdigit() else None
        text = f"{endpoint}: {message or 'no message'}" + (f" (error {number})" if number else "")
        if number in _TOKEN:
            raise FlexTokenError(f"{text}; {_TOKEN_HINT}", number)
        if number in _QUERY:
            raise FlexQueryError(f"{text}; {_QUERY_HINT}", number)
        if number not in _TRANSIENT:
            raise FlexError(text, number)
        if self._monotonic() + delay > deadline:
            raise FlexUnavailableError(f"{text}; gave up waiting", number)
        if progress:
            progress(message or "waiting for IBKR")
        self._sleep(delay)
        return min(delay * 2, _MAX_POLL)

    def _get(self, endpoint: str, params: dict) -> bytes:
        url = f"{self._base_url}/{endpoint}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
        # Never let the URL (it carries the token) into an error message.
        try:
            with self._opener(request, timeout=self._timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise FlexUnavailableError(f"{endpoint}: IBKR answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise FlexUnavailableError(
                f"{endpoint}: cannot reach IBKR ({type(reason).__name__})"
            ) from None


def _status(body: bytes) -> tuple[str, str, str, str] | None:
    """(Status, ErrorCode, ErrorMessage, ReferenceCode) of a service response;
    None when the body is something else (a report)."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise FlexError("unexpected, unreadable response from IBKR") from None
    if root.tag != "FlexStatementResponse":
        return None

    def text(tag):
        return (root.findtext(tag) or "").strip()

    return text("Status"), text("ErrorCode"), text("ErrorMessage"), text("ReferenceCode")
