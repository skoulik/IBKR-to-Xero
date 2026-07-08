"""GUI smoke test: drive the example statement through the worker thread and
assert the window reaches the Done state.

Skipped when PySide6 or pytest-qt is missing (the GUI is an optional extra),
and when the examples/ folder is absent (same pattern as the other tests).
Runs headless via the offscreen Qt platform.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from ibkr_to_xero.gui import MainWindow  # noqa: E402


def test_gui_conversion_reaches_done(statement_csv, tmp_path, qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    # Select the statement and target a scratch output dir (not examples/).
    window._load_statement(statement_csv)
    window._output_edit.setText(str(tmp_path))
    assert window._convert_button.isEnabled()

    window._start_conversion()
    # Busy immediately, then Done once the worker finishes on its thread.
    qtbot.waitUntil(
        lambda: window._stack.currentIndex() == 2 and window._result is not None,
        timeout=15000,
    )

    assert (tmp_path / "AUD.csv").exists()
    assert (tmp_path / "USD.csv").exists()
    assert "account U" in window._report_view.toPlainText()
