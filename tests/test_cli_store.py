"""The CLI's commands over the store, end to end on a temporary store, with the terminal's
answers scripted and a fake Flex Web Service. Skips where DuckDB lacks our build (D65).

Implements: AD-129, AD-135, AD-136, AD-137
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from ibkr_to_xero import cli, store_cli, vault
from ibkr_to_xero.store import Store
from ibkr_to_xero.store_cli import Terminal

from .flex_fake import FakeFlex
from .test_database import _httpfs_linked

pytestmark = pytest.mark.skipif(
    not _httpfs_linked(),
    reason="this DuckDB has no httpfs linked in (PyPI's?): the store needs our build",
)

D = dt.date
SATURDAY = dt.datetime(2026, 11, 21, 2, 49, tzinfo=dt.timezone.utc)


class Script(Terminal):
    """Answers questions from a list, in order, and keeps everything shown."""

    def __init__(self, *answers, interactive=True):
        self.answers = list(answers)
        self.shown: list[str] = []
        self._interactive = interactive

    @property
    def interactive(self):
        return self._interactive

    def ask(self, prompt):
        self.shown.append(prompt)
        assert self.answers, f"no answer scripted for {prompt!r}"
        return self.answers.pop(0)

    secret = ask

    def out(self, text=""):
        self.shown.append(text)

    err = out

    @property
    def text(self):
        return "\n".join(self.shown)


@pytest.fixture(autouse=True)
def _cheap(memory_keyring, monkeypatch):
    monkeypatch.setattr(store_cli, "STORE_COST", vault.Cost(n=2**4, r=8, p=1))
    return memory_keyring


@pytest.fixture
def fake():
    flex = FakeFlex(closed=D(2026, 11, 19))
    flex.fee_every_weekday(D(2025, 1, 1), D(2026, 12, 31))
    return flex


@pytest.fixture
def run(tmp_path, fake):
    folder = tmp_path / "store"

    def run(*argv, answers=(), interactive=True):
        term = Script(*answers, interactive=interactive)
        code = store_cli.main([*argv, "--store", str(folder)], terminal=term, client_factory=fake,
                              now=lambda: SATURDAY, exports_root=tmp_path / "exports")
        assert not term.answers, f"answers left over: {term.answers}"
        return code, term.text

    run.folder = folder
    return run


def _add(run, remember="y"):
    return run("account", "add", "My IBKR",
               answers=["pw", "pw", remember, "1234", "TOKEN"])


def test_the_first_account_add_makes_the_store(run, memory_keyring):
    code, text = _add(run)
    assert code == 0
    assert "Making a store" in text and "keep it safe" in text
    store = Store.open(run.folder)  # remembered: no password
    assert [e.name for e in store.ledgers()] == ["My IBKR"]
    settings = store.find_ledger("My IBKR").settings()
    assert (settings.flex_query_id, settings.flex_token) == ("1234", "TOKEN")
    assert "TOKEN" not in text  # never echoed


def test_a_store_not_remembered_asks_for_the_password(run):
    _add(run, remember="n")
    code, text = run("status", answers=["wrong", "pw"])
    assert code == 0 and "The password is wrong." in text and "My IBKR" in text
    code, text = run("status", interactive=False)
    assert code == 2 and "password is needed" in text


def test_the_first_fetch_asks_the_xero_dates_and_fetches_back(run, fake):
    _add(run)
    code, text = run("fetch", answers=["", "2025-06-30"])  # one by one; USD
    assert code == 0, text
    assert "fetched 2025-11-20 to 2026-11-19" in text
    assert "fetched 2025-07-01 to 2025-11-19" in text
    code, text = run("status")
    assert "USD: in Xero up to 2025-06-30" in text and "2025-07-01 to 2026-11-19" in text


def test_nothing_in_xero_yet_takes_a_start_date(run):
    _add(run)
    code, text = run("fetch", answers=["", "none", "2026-07-01"])
    assert code == 0, text
    code, text = run("status")
    assert "USD: in Xero up to 2026-06-30" in text


def test_without_a_terminal_the_dates_wait(run):
    _add(run)
    code, text = run("fetch", interactive=False)
    assert code == 0 and "Not asked, with no terminal" in text
    code, text = run("export", interactive=False)
    assert code == 2 and "no terminal" in text.lower()


def test_export_then_confirm(run, tmp_path):
    _add(run)
    run("fetch", answers=["2026-11-13"])
    code, text = run("export")
    assert code == 0, text
    folder = tmp_path / "exports" / "My IBKR"
    (export,) = folder.iterdir()
    assert (export / "My IBKR USD.csv").exists() and "My IBKR USD.csv: 4 transactions" in text
    code, text = run("export", interactive=False)  # a pending file needs an answer
    assert code == 2 and "ibkr2xero confirm" in text and "No output files were written." in text
    code, text = run("confirm", answers=["y"])
    assert code == 0 and "imported into Xero?" in text
    code, text = run("export")
    assert code == 0 and "Nothing to export" in text


def test_an_export_asks_about_pending_files_first(run, tmp_path):
    _add(run)
    run("fetch", answers=["2026-11-13"])
    run("export")
    code, text = run("export", answers=["n"])
    assert code == 0 and "Before exporting: 1 exported file(s)" in text
    assert len(list((tmp_path / "exports" / "My IBKR").iterdir())) == 2


def test_a_refetch_is_kept_on_request(run, fake):
    _add(run)
    run("fetch", answers=["2026-11-13"])
    run("export")
    run("confirm", answers=["y"])
    next(e for e in fake.events if e.date == D(2026, 11, 18)).amount = Decimal("-2")
    code, text = run("refetch", "--from", "2026-11-18", "--to", "2026-11-19", answers=["y"])
    assert code == 0, text
    assert "changed USD 2026-11-18 -1.5 FEE 20261118 -> 2026-11-18 -2 FEE 20261118" in text
    assert "Already exported, so correct these in Xero by hand" in text


def test_a_refetch_with_nothing_changed_keeps_nothing(run):
    _add(run)
    run("fetch", answers=["2026-11-13"])
    code, text = run("refetch", "--from", "2026-11-18", "--to", "2026-11-19")
    assert code == 0 and "Nothing has changed at IBKR; nothing kept." in text


def test_re_export_and_its_arguments(run):
    _add(run)
    run("fetch", answers=["2026-11-13"])
    code, text = run("export", "--again", "--from", "2026-11-02", "--to", "2026-11-06")
    assert code == 0 and "5 transactions" in text
    code, text = run("export", "--again", "--from", "2026-11-02")
    assert code == 2 and "--from and --to" in text


def test_accounts_rename_remove_and_choose(run):
    _add(run)
    code, _ = run("account", "add", "Second", answers=["99", "TOKEN"])
    assert code == 0
    code, text = run("status")
    assert code == 2 and "name one with -a" in text
    assert run("status", "-a", "second")[0] == 0
    assert run("account", "rename", "Second", "Other")[0] == 0
    code, text = run("account", "remove", "Other", answers=["nope"])
    assert "Not removed." in text
    code, text = run("account", "remove", "Other", answers=["other"])
    assert "Removed." in text
    code, text = run("account", "list")
    assert text.strip() == "My IBKR: not fetched yet"


def test_password_change_remember_and_forget(run, memory_keyring):
    _add(run, remember="n")
    assert run("password", "--remember", answers=["pw"])[0] == 0
    assert memory_keyring.entries
    assert run("password", answers=["new", "new"])[0] == 0
    assert run("password", "--forget")[0] == 0
    assert not memory_keyring.entries
    code, text = run("status", answers=["new"])
    assert code == 0


def test_start_over_sets_the_store_aside(run, tmp_path):
    _add(run)
    code, text = run("start-over", answers=["y", "pw2", "pw2", "n"])
    assert code == 0, text
    aside = [p.name for p in tmp_path.iterdir() if "password forgotten" in p.name]
    assert aside == [f"store (password forgotten {dt.date.today()})"]


def test_a_command_word_goes_to_the_store_and_anything_else_is_a_statement(monkeypatch):
    seen = []
    monkeypatch.setattr(store_cli, "main", lambda argv: seen.append(argv) or 0)
    assert cli.main(["fetch", "-a", "x"]) == 0 and seen == [["fetch", "-a", "x"]]
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    commands = store_cli._parser()._subparsers._group_actions[0].choices
    assert tuple(commands) == cli.STORE_COMMANDS
