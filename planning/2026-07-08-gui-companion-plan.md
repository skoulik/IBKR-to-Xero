# GUI companion — implementation plan

*Drafted 2026-07-08. A one-shot desktop GUI for the converter: drop (or open) a
statement, get the report and links to the output files. Same contract as the
CLI: reconcile or write nothing.*

## Decisions

### D1 — Common engine, not a CLI wrapper

Both front-ends call a shared library entry point; the GUI does **not** shell
out to `ibkr2xero`.

Rationale:

- A subprocess wrapper communicates through argv, exit codes and stdout text —
  the GUI would have to re-parse the report and error messages it wants to
  display structurally (links, per-currency summaries, error styling).
- The core is already a pure library (`parser → convert → reconcile → writer`);
  CLAUDE.md's stated design intent is that it back other front-ends (TODO M4
  web/Telegram, M5 Xero API). The engine extraction is required for those
  anyway; the GUI is just its first consumer.
- The only logic currently trapped in `cli.py` is: the output-dir default rule,
  zero-transaction filtering, report-text assembly, and the error/exit-code
  mapping. All of it is front-end-agnostic.

### D2 — Toolkit: PySide6 (Qt 6)

Chosen for native drag-and-drop, `QTextBrowser` (rich report text + clickable
file links for free), proper HiDPI, and threading helpers. The weight is
acceptable: only `PySide6-Essentials` is needed (QtCore/QtGui/QtWidgets,
~70 MB download / ~150 MB installed in the venv), and it is an **optional**
dependency — CLI-only installs don't pull it. Note the pip wheels bundle their
own private Qt runtime; they cannot link against a locally installed C++ Qt,
and don't need to.

Considered: Tkinter + tkinterdnd2 (lightest, but dated look, manual hyperlink
plumbing, third-party DnD binaries); pywebview (modern look and M4 synergy, but
a two-language codebase and file-path-from-drop quirks).

### D3 — Entry point: `ibkr2xero-gui`

A separate `[project.gui-scripts]` entry (`ibkr_to_xero.gui:main`). On Windows,
`gui-scripts` produces a console-less launcher. The CLI keeps its one-shot
contract untouched.

## Step 1 — Engine extraction (pure refactor)

New module `ibkr_to_xero/engine.py`:

```python
@dataclass
class RunOptions:
    output_dir: Path | None = None          # None → default rule (statement stem)
    force_overwrite: bool = False
    skip_zero_transactions: bool = False
    report_name: str | None = "ibkr2xero_report.txt"   # None → don't save
    accept_unattributed_gst: bool = False

@dataclass
class RunResult:
    account_line: str                        # "account U…, period … to …"
    output_dir: Path
    files: list[tuple[Path, str]]            # (csv path, per-currency summary line)
    notes: list[tuple[Path, list[str]]]      # notes per file (or fold into files)
    report_text: str                         # canonical report (bare filenames)
    report_path: Path | None                 # None when saving disabled

def run(statement_path: Path, options: RunOptions) -> RunResult: ...
```

- `run()` performs parse → convert → reconcile → zero-filter → resolve output
  dir → write → assemble report. It **never prints**; errors propagate as the
  existing typed exceptions (`StatementError`, `ReconciliationError`,
  `FileExistsError` from the overwrite check).
- Report text is assembled once, with **bare filenames** (that is the saved
  form); front-ends that want full paths on the console (the CLI does) print
  from `RunResult.files`.
- `cli.py` shrinks to: argparse → `RunOptions` → `run()` → print report /
  map exceptions to stderr + exit 2. **Observable CLI behaviour must not
  change**; the existing test suite is the regression harness, plus a
  before/after byte-compare of console output and report file on the example
  statement.

## Step 2 — GUI (`ibkr_to_xero/gui.py`)

One window, four states:

1. **Idle** — large drop zone: *"Drop an IB Activity Statement CSV here"*, plus
   an **Open…** button (QFileDialog) for the no-mouse-gymnastics path. Below,
   the options row:
   - ☐ Skip zero-amount transactions
   - ☐ Accept unattributed GST
   - ☑ Save report
   - Output folder: `<default from statement>` **[Browse…]** — shows the
     default-rule result as placeholder once a file is chosen; editable.
2. **Busy** — controls disabled, indeterminate progress bar. Conversion runs on
   a worker thread (`QThread` / signal back to UI) so the window never freezes.
3. **Done** — the drop zone becomes a `QTextBrowser` showing the report text,
   followed by clickable links: one per output CSV and one for the saved report
   (open via `QDesktopServices.openUrl` → default app, i.e. Excel), and an
   **Open folder** button (`explorer /select` on the report, plain open
   otherwise). A **Start over** button returns to Idle; dropping a new file
   onto the results view also restarts directly.
4. **Error** — same area shows the exception text styled as an error, with the
   explicit line *"No output files were written."* and Start over. This is a
   first-class state, not a message box: rejection reports can be long
   (reconciliation details) and should be selectable/copyable.

Interaction details:

- **Overwrite conflict**: `run()` raises `FileExistsError` → modal question
  *"Output files already exist in \<dir\>. Overwrite?"* → on Yes, re-run with
  `force_overwrite=True` (the pipeline is pure and fast; re-running is simpler
  than a two-phase engine API).
- **Drop validation**: exactly one local file; multiple files or a folder →
  friendly error (multi-statement runs are out of scope, per TODO). No
  extension gate — the parser's `StatementError` is the authority on what a
  statement is.
- **Stateless** (v1): options reset to defaults on every launch, matching the
  one-shot CLI philosophy. Revisit only if it annoys in practice.
- Window title: `ibkr2xero <version>`. Version from package metadata.

## Step 3 — Packaging & docs

- `pyproject.toml`: `[project.optional-dependencies] gui = ["PySide6-Essentials>=6.7"]`,
  `[project.gui-scripts] ibkr2xero-gui = "ibkr_to_xero.gui:main"`.
  `gui.py` fails at import of PySide6 with a clear message naming
  `pip install ibkr-to-xero[gui]`.
- README: short GUI section with a screenshot placeholder.
- TODO.md: record this as its own milestone (GUI companion) alongside M4 —
  M4's web front-end will reuse the same engine.

## Testing

- Engine: the existing CLI tests largely become engine tests; keep thin
  CLI-level tests for argv mapping and exit codes.
- GUI: keep `gui.py` a thin view over the engine so there is little to test;
  one optional `pytest-qt` smoke test (construct window, feed a path through
  the worker, assert Done state) — skipped when PySide6 isn't installed, same
  pattern as the examples-folder skip.
- Manual pass on the real example statement: success path, rejection path
  (statement with unverifiable GST, without the checkbox), overwrite dialog.

## Out of scope (v1)

- Multi-statement / batch drops (dropped from the CLI roadmap too, M3.2).
- Drag-and-drop of non-file payloads (e.g. an attachment dragged straight from
  an email client without a real file path). Not to be confused with
  non-*statement* files, which are in scope: any real file is accepted and the
  parser's rejection surfaces in the Error state.
- Persisted settings, recent-files list.
- Standalone `.exe` (PyInstaller) — belongs to M6 distribution; the engine/GUI
  split keeps it possible.

## Resolved questions (2026-07-08)

- Report file name is **not** exposed in the GUI; the "Save report" checkbox is
  the only report control, and the default name is used. Renaming stays a
  CLI-only affordance (`-r/--report-name`).
- The Done state renders the report **verbatim in monospace** — it is the same
  artifact the CLI prints and the report file stores; the GUI's added value is
  the links row underneath, not reformatting.
