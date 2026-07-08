"""Desktop GUI companion for the converter.

A one-shot window over the same engine the CLI uses: choose (or drop) a
statement, get the report and links to the output files. Same contract as the
CLI — reconcile or write nothing. This is a thin view; all logic lives in
``engine.run``.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
    from PySide6.QtGui import (
        QDesktopServices,
        QDragEnterEvent,
        QDropEvent,
        QFontDatabase,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMenu,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QStackedWidget,
        QTextBrowser,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - exercised only without PySide6
    raise ImportError(
        "The ibkr2xero GUI requires PySide6. Install it with: "
        "pip install ibkr-to-xero[gui]"
    ) from exc

from .engine import RunOptions, RunResult, resolve_output_dir, run
from .model import ReconciliationError, StatementError


def _app_version() -> str:
    try:
        return version("ibkr-to-xero")
    except PackageNotFoundError:  # running from a source tree without metadata
        return "dev"


class ConversionWorker(QObject):
    """Runs one conversion off the UI thread and reports back via signals."""

    succeeded = Signal(object)  # RunResult
    overwrite_conflict = Signal(str)  # str(FileExistsError)
    failed = Signal(str)  # error text for the Error state
    finished = Signal()

    def __init__(self, statement_path: Path, options: RunOptions) -> None:
        super().__init__()
        self._statement_path = statement_path
        self._options = options

    @Slot()
    def run(self) -> None:
        try:
            result = run(self._statement_path, self._options)
        except FileExistsError as exc:
            self.overwrite_conflict.emit(str(exc))
        except (StatementError, ReconciliationError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # unexpected: still surface it, write nothing
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)
        finally:
            self.finished.emit()


class MainWindow(QWidget):
    """Single window with four states: Idle, Busy, Done, Error."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"ibkr2xero {_app_version()}")
        self.setAcceptDrops(True)
        self.resize(640, 480)

        self._statement_path: Path | None = None
        self._thread: QThread | None = None
        self._worker: ConversionWorker | None = None

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_idle_page())
        self._stack.addWidget(self._build_busy_page())
        self._stack.addWidget(self._build_result_page())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

        self.show_idle()

    # ------------------------------------------------------------------ pages

    def _build_idle_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        self._drop_zone = QLabel(self._DROP_PROMPT)
        self._drop_zone.setAlignment(Qt.AlignCenter)
        self._drop_zone.setFrameShape(QFrame.StyledPanel)
        self._drop_zone.setMinimumHeight(160)
        self._drop_zone.setToolTip(
            "Drop an Activity Statement CSV exported from Interactive Brokers\n"
            "(or right-click anywhere to browse for one)."
        )
        self._style_drop_zone(selected=False)
        layout.addWidget(self._drop_zone, stretch=1)

        self._skip_zero_check = QCheckBox("Skip zero-amount transactions")
        self._skip_zero_check.setToolTip(
            "Omit zero-amount transactions (e.g. option expiries) from the output;\n"
            "they carry no cash and Xero discards them on import anyway."
        )
        self._accept_gst_check = QCheckBox("Accept unattributed GST")
        self._accept_gst_check.setToolTip(
            "Accept a GST amount that cannot be verified as 10% GST on specific\n"
            "fee rows, as a single lump-sum line. When unticked, such a statement\n"
            "is rejected and nothing is written."
        )
        self._save_report_check = QCheckBox("Save report")
        self._save_report_check.setChecked(True)
        self._save_report_check.setToolTip(
            "Save the conversion report (the text shown after converting) as\n"
            "ibkr2xero_report.txt alongside the output CSVs."
        )
        checks = QGridLayout()
        checks.addWidget(self._skip_zero_check, 0, 0)
        checks.addWidget(self._accept_gst_check, 0, 1)
        checks.addWidget(self._save_report_check, 0, 2)
        checks.setColumnStretch(3, 1)  # keep the checkboxes packed to the left
        layout.addLayout(checks)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Output folder:"))
        self._output_edit = QLineEdit()
        self._output_edit.setPlaceholderText("(choose a statement first)")
        self._output_edit.setToolTip(
            "Where the per-currency CSVs (and the report) are written.\n"
            "Defaults to a subfolder next to the statement, named after it."
        )
        folder_row.addWidget(self._output_edit, stretch=1)
        browse_button = QPushButton("Browse…")
        browse_button.setToolTip("Choose a different output folder.")
        browse_button.clicked.connect(self._on_browse_clicked)
        folder_row.addWidget(browse_button)
        layout.addLayout(folder_row)

        self._convert_button = QPushButton("Convert")
        self._convert_button.setEnabled(False)
        self._convert_button.setStyleSheet(
            "QPushButton { font-size: 16px; font-weight: bold; padding: 8px; }"
        )
        self._convert_button.setToolTip(
            "Convert the selected statement into per-currency Xero import CSVs.\n"
            "Nothing is written unless every currency reconciles."
        )
        self._convert_button.clicked.connect(self._start_conversion)
        layout.addWidget(self._convert_button)

        return page

    _DROP_PROMPT = (
        "Drop an IB Activity Statement CSV here\nor right-click to open a file"
    )

    def _style_drop_zone(self, *, selected: bool) -> None:
        # Dim text for the prompt; regular text once a file is selected.
        color = "palette(window-text)" if selected else "palette(mid)"
        self._drop_zone.setStyleSheet(
            "QLabel { border: 2px dashed palette(mid); border-radius: 8px; "
            f"color: {color}; font-size: 15px; }}"
        )

    def _build_busy_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        layout.addWidget(QLabel("Converting…", alignment=Qt.AlignCenter))
        progress = QProgressBar()
        progress.setRange(0, 0)  # indeterminate
        layout.addWidget(progress)
        layout.addStretch(1)
        return page

    def _build_result_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        self._report_view = QTextBrowser()
        self._report_view.setOpenExternalLinks(False)
        self._report_view.setLineWrapMode(QTextBrowser.NoWrap)
        # "monospace" is not a real family on Windows (it silently falls back
        # to a proportional font); ask the system for its fixed-pitch font.
        self._report_view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        layout.addWidget(self._report_view, stretch=1)

        # Links row (one button per output file / report), rebuilt per run.
        self._links_row = QHBoxLayout()
        layout.addLayout(self._links_row)

        button_row = QHBoxLayout()
        self._open_folder_button = QPushButton("Open folder")
        self._open_folder_button.setToolTip("Show the output folder in Explorer.")
        self._open_folder_button.clicked.connect(self._on_open_folder_clicked)
        button_row.addWidget(self._open_folder_button)
        button_row.addStretch(1)
        start_over_button = QPushButton("Start over")
        start_over_button.setToolTip("Back to the drop zone for another statement.")
        start_over_button.clicked.connect(self.show_idle)
        button_row.addWidget(start_over_button)
        layout.addLayout(button_row)

        self._result: RunResult | None = None
        return page

    # ----------------------------------------------------------- state switch

    def show_idle(self) -> None:
        self._statement_path = None
        self._result = None
        self._drop_zone.setText(self._DROP_PROMPT)
        self._style_drop_zone(selected=False)
        self._output_edit.clear()
        self._output_edit.setPlaceholderText("(choose a statement first)")
        self._convert_button.setEnabled(False)
        self._stack.setCurrentIndex(0)

    def _show_busy(self) -> None:
        self._stack.setCurrentIndex(1)

    def _show_done(self, result: RunResult) -> None:
        self._result = result
        self._report_view.setStyleSheet("")
        self._report_view.setPlainText(result.report_text)
        self._rebuild_links(result)
        self._open_folder_button.setVisible(True)
        self._stack.setCurrentIndex(2)

    def _show_error(self, message: str) -> None:
        self._result = None
        # Explicit red: palette(bright-text) is white on light themes, which
        # made the error text invisible on the white report background.
        self._report_view.setStyleSheet("QTextBrowser { color: #d32f2f; }")
        self._report_view.setPlainText(f"{message}\n\nNo output files were written.")
        self._clear_links()
        self._open_folder_button.setVisible(False)
        self._stack.setCurrentIndex(2)

    # --------------------------------------------------------------- handlers

    def _on_open_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open IB Activity Statement", "", "CSV files (*.csv);;All files (*)"
        )
        if path:
            self._load_statement(Path(path))

    def _on_browse_clicked(self) -> None:
        start = self._output_edit.text() or (
            str(self._statement_path.parent) if self._statement_path else ""
        )
        directory = QFileDialog.getExistingDirectory(self, "Choose output folder", start)
        if directory:
            self._output_edit.setText(directory)

    def _on_open_folder_clicked(self) -> None:
        if self._result is None:
            return
        report = self._result.report_path
        if sys.platform == "win32" and report is not None:
            # Open the folder with the report preselected. Explorer wants the
            # switch and path as one token; a list would quote them apart and
            # break on paths with spaces.
            subprocess.run(f'explorer /select,"{report}"')
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result.output_dir)))

    def _load_statement(self, path: Path) -> None:
        """Select a statement in the Idle state and preview its output dir."""
        self._statement_path = path
        self._drop_zone.setText(f"Selected: {path.name}\n\nDrop another file to replace")
        self._style_drop_zone(selected=True)
        default_dir = resolve_output_dir(path, None)
        self._output_edit.setText(str(default_dir))
        self._convert_button.setEnabled(True)
        self._stack.setCurrentIndex(0)

    def _current_options(self) -> RunOptions:
        text = self._output_edit.text().strip()
        return RunOptions(
            output_dir=Path(text) if text else None,
            force_overwrite=False,
            skip_zero_transactions=self._skip_zero_check.isChecked(),
            report_name=(
                "ibkr2xero_report.txt" if self._save_report_check.isChecked() else None
            ),
            accept_unattributed_gst=self._accept_gst_check.isChecked(),
        )

    def _start_conversion(self, *, force_overwrite: bool = False) -> None:
        if self._statement_path is None:
            return
        options = self._current_options()
        if force_overwrite:
            options.force_overwrite = True
        self._show_busy()
        self._run_worker(self._statement_path, options)

    # ----------------------------------------------------------- worker glue

    def _run_worker(self, statement_path: Path, options: RunOptions) -> None:
        thread = QThread(self)
        worker = ConversionWorker(statement_path, options)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(self._on_succeeded)
        worker.overwrite_conflict.connect(self._on_overwrite_conflict)
        worker.failed.connect(self._show_error)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        # Keep references alive until the thread finishes.
        self._thread = thread
        self._worker = worker
        thread.finished.connect(self._clear_worker)
        thread.start()

    def _clear_worker(self) -> None:
        self._thread = None
        self._worker = None

    @Slot(object)
    def _on_succeeded(self, result: RunResult) -> None:
        self._show_done(result)

    @Slot(str)
    def _on_overwrite_conflict(self, _message: str) -> None:
        out_dir = resolve_output_dir(self._statement_path, self._current_options().output_dir)
        answer = QMessageBox.question(
            self,
            "Overwrite output files?",
            f"Output files already exist in {out_dir}. Overwrite?",
        )
        if answer == QMessageBox.Yes:
            self._start_conversion(force_overwrite=True)
        else:
            self.show_idle()

    # ---------------------------------------------------------------- links

    def _clear_links(self) -> None:
        while self._links_row.count():
            item = self._links_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild_links(self, result: RunResult) -> None:
        self._clear_links()
        for file in result.files:
            self._links_row.addWidget(self._file_button(file.path))
        if result.report_path is not None:
            self._links_row.addWidget(self._file_button(result.report_path))
        self._links_row.addStretch(1)

    def _file_button(self, path: Path) -> QPushButton:
        button = QPushButton(path.name)
        button.setToolTip(f"Open {path}")
        button.setFlat(True)
        button.setStyleSheet("QPushButton { color: palette(link); text-decoration: underline; }")
        button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        return button

    # --------------------------------------------------------- drag and drop

    def _busy(self) -> bool:
        """A conversion is in flight (worker alive or Busy page showing)."""
        return self._thread is not None or self._stack.currentIndex() == 1

    def contextMenuEvent(self, event) -> None:
        if self._busy():
            return
        menu = QMenu(self)
        menu.addAction("Open…", self._on_open_clicked)
        menu.exec(event.globalPos())

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self._busy() and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        if self._busy():  # don't disturb a running conversion
            event.ignore()
            return
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if len(paths) != 1 or not paths[0].is_file():
            self._show_error(
                "Please drop exactly one statement file. "
                "Folders and multiple files are not supported."
            )
            event.ignore()
            return
        event.acceptProposedAction()
        self._load_statement(paths[0])


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
