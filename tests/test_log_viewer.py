"""
Unit tests for :mod:`ui.log_viewer_tab`.

Design of this module
---------------------
* **Worker threads are driven synchronously.**  ``LogLoaderThread``,
  ``RealTimeFilterThread`` and ``HistoricalFilterThread`` are ``QThread``
  subclasses whose whole behaviour lives in ``run()``.  Calling ``run()`` on the
  main thread makes every signal a direct connection, so the emissions are
  delivered immediately and the assertions stay deterministic.  Only a couple of
  tests marked ``integration`` really ``start()`` a thread, and each of those is
  bounded by ``wait()`` with a timeout.
* **Interruption** is simulated by shadowing ``isInterruptionRequested`` on the
  instance: ``QThread::requestInterruption()`` is a documented no-op on a thread
  that is not running, so it cannot be used to test ``run()`` synchronously.
* **No modal window ever opens.**  Every test that builds a ``LogViewerTab``
  goes through the ``tab`` fixture, which pulls in the ``dialogs`` recorder, so
  a stray ``QMessageBox`` is recorded instead of blocking the suite.
* **No real user data.**  ``isolated_logging_config`` redirects the log folder
  into ``tmp_path``; the root logger is silenced for the duration of every test
  so the viewer's own ``logger.info()`` calls cannot feed records back into the
  widget under test and make the assertions non-deterministic.
* **The widget is never shown.**  ``QTextEdit`` lays its document out lazily;
  showing the tab would turn the line-by-line ``insertHtml()`` in
  ``append_colored_line`` into seconds of work per hundred lines.

Tests marked ``bug`` + ``xfail`` document defects found in the production code;
they assert the *correct* behaviour and are expected to fail until the defect is
fixed.
"""

import logging
import time
from datetime import date, datetime

import pytest
from PyQt6.QtCore import QDate, Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QListWidgetItem, QMessageBox

import ui.log_viewer_tab as lvt
from ui.log_viewer_tab import (
    MAX_DISPLAYED_LOG_LINES,
    MAX_LOG_FILE_SIZE_MB,
    MAX_REAL_TIME_LOG_LINES,
    HistoricalFilterThread,
    LogLoaderThread,
    LogViewerTab,
    RealTimeFilterThread,
    RealTimeLogHandler,
)
from utils.logging_config import LOG_COLORS, MB_TO_BYTES

WORKER_CLASSES = (LogLoaderThread, RealTimeFilterThread, HistoricalFilterThread)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def quiet_root_logger():
    """
    Silence the root logger for the duration of a test.

    ``LogViewerTab`` installs a handler on the *root* logger, so every
    ``logger.info()`` executed by the production code - including the ones the
    viewer itself emits while clearing or loading - would land back in
    ``real_time_logs`` and change the counters under test.
    """
    root = logging.getLogger()
    original_level = root.level
    root.setLevel(logging.CRITICAL + 1)
    yield root
    root.setLevel(original_level)


@pytest.fixture
def tab(qapp, isolated_logging_config, quiet_root_logger, dialogs):
    """A ``LogViewerTab`` on an isolated log folder, closed down afterwards."""
    widget = LogViewerTab()
    widget.real_time_logs.clear()
    widget.real_time_text.clear()
    dialogs.clear()
    yield widget
    widget.closeEvent(QCloseEvent())
    qapp.processEvents()


@pytest.fixture
def no_start(monkeypatch):
    """
    Replace the three worker classes by subclasses whose ``start()`` is a no-op.

    The widget then builds and wires a *real* thread object - which the tests
    inspect - without any concurrency.
    """
    stubs = {}
    for name in ("RealTimeFilterThread", "HistoricalFilterThread", "LogLoaderThread"):
        base = getattr(lvt, name)

        class Stub(base):
            started = False

            def start(self, *args, **kwargs):
                self.started = True

        Stub.__name__ = f"NoStart{name}"
        monkeypatch.setattr(lvt, name, Stub)
        stubs[name] = Stub
    return stubs


class FakeThread:
    """Stand-in for a running worker thread; records what ``closeEvent`` does."""

    def __init__(self, running=True):
        self._running = running
        self.interruptions = 0
        self.waits = []

    def isRunning(self):
        return self._running

    def requestInterruption(self):
        self.interruptions += 1

    def wait(self, msecs=None):
        self.waits.append(msecs)
        return True


def collect(signal):
    """Record every emission of *signal* as a tuple of its arguments."""
    seen = []
    signal.connect(lambda *args: seen.append(args))
    return seen


def interrupt_from(thread, call_index):
    """Make ``isInterruptionRequested`` return True from *call_index* onwards."""
    state = {"calls": 0}

    def fake():
        state["calls"] += 1
        return state["calls"] > call_index

    thread.isInterruptionRequested = fake
    return state


def entry(msg, level=logging.INFO):
    """One real-time log record as the viewer stores it."""
    return {"msg": msg, "level": level}


def list_item(data):
    """A ``QListWidgetItem`` carrying *data* in the user role."""
    item = QListWidgetItem("entry")
    item.setData(Qt.ItemDataRole.UserRole, data)
    return item


def log_file_entry(path, size=None, name=None, is_current=True):
    """The dictionary ``refresh_log_files`` stores on every list item."""
    return {
        "path": str(path),
        "name": name if name is not None else path.name,
        "size": path.stat().st_size if size is None else size,
        "modified": datetime(2024, 3, 1, 12, 0, 0),
        "is_current": is_current,
    }


def type_search(line_edit, timer, text):
    """Fill a search field without letting its 300 ms debounce timer fire."""
    line_edit.setText(text)
    timer.stop()


def shown_lines(text_edit):
    """The non-empty lines currently visible in a log view."""
    return [line for line in text_edit.toPlainText().split("\n") if line]


# ===========================================================================
# Signal naming - the custom signals must not shadow QThread.finished
# ===========================================================================

@pytest.mark.parametrize("worker", WORKER_CLASSES, ids=lambda c: c.__name__)
def test_worker_threads_do_not_redefine_qthreads_finished_signal(worker):
    """A worker may not declare its own ``finished`` - QThread already has one."""
    assert "finished" not in vars(worker)
    own_signals = [name for name, value in vars(worker).items()
                   if type(value).__name__ == "pyqtSignal"]
    assert own_signals, f"{worker.__name__} declares no completion signal"
    assert "finished" not in own_signals


@pytest.mark.integration
def test_log_loader_emits_its_own_signal_and_qthreads_finished(qapp, tmp_path):
    """Both the inherited ``finished`` and ``load_finished`` fire for one run."""
    path = tmp_path / "app.log"
    path.write_text("first line\n", encoding="utf-8")
    data = log_file_entry(path)

    thread = LogLoaderThread(str(path), data)
    loaded = collect(thread.load_finished)
    done = collect(thread.finished)

    thread.start()
    assert thread.wait(5000)
    qapp.processEvents()

    assert loaded == [("first line\n", data)]
    assert done == [()]


# ===========================================================================
# LogLoaderThread
# ===========================================================================

def test_log_loader_emits_the_file_content_and_the_original_metadata(tmp_path):
    """The happy path hands back the whole text plus the untouched dictionary."""
    path = tmp_path / "app.log"
    path.write_text("line 1\nline 2\n", encoding="utf-8")
    data = log_file_entry(path)

    thread = LogLoaderThread(str(path), data)
    loaded = collect(thread.load_finished)
    errors = collect(thread.error)
    thread.run()

    assert loaded == [("line 1\nline 2\n", data)]
    assert errors == []


def test_log_loader_keeps_diacritics_and_replaces_undecodable_bytes(tmp_path):
    """UTF-8 is preserved; broken bytes become U+FFFD instead of an exception."""
    path = tmp_path / "app.log"
    path.write_bytes("Žluťoučký kůň\n".encode("utf-8") + b"\xff\xfe\n")

    thread = LogLoaderThread(str(path), {})
    loaded = collect(thread.load_finished)
    thread.run()

    content = loaded[0][0]
    assert content.startswith("Žluťoučký kůň\n")
    assert "�" in content


def test_log_loader_reports_a_missing_file_without_raising(tmp_path):
    """A file that disappeared between listing and loading is reported."""
    thread = LogLoaderThread(str(tmp_path / "gone.log"), {})
    loaded = collect(thread.load_finished)
    errors = collect(thread.error)
    thread.run()

    assert errors == [("File was deleted or moved",)]
    assert loaded == []


def test_log_loader_reports_a_directory_as_an_os_error(tmp_path):
    """Opening a directory is an OSError and keeps the system message."""
    thread = LogLoaderThread(str(tmp_path), {})
    errors = collect(thread.error)
    thread.run()

    assert errors == [("Could not read the file: Is a directory",)]


@pytest.mark.parametrize("error,expected", [
    (PermissionError(13, "Permission denied"), "Permission denied to read file"),
    (MemoryError(), "File too large to load into memory"),
    (OSError(5, "Input/output error"), "Could not read the file: Input/output error"),
    (OSError("plain failure"), "Could not read the file: plain failure"),
    (ValueError("boom"), "Unexpected error: boom"),
    (RuntimeError(), "Unexpected error: "),
])
def test_log_loader_maps_every_failure_to_its_own_message(monkeypatch, error, expected):
    """Each failure class gets its own wording and nothing escapes ``run()``."""
    def exploding_open(*args, **kwargs):
        raise error

    monkeypatch.setattr("builtins.open", exploding_open)

    thread = LogLoaderThread("/any/path.log", {})
    loaded = collect(thread.load_finished)
    errors = collect(thread.error)
    thread.run()

    assert errors == [(expected,)]
    assert loaded == []


def test_log_loader_drops_the_content_when_it_was_interrupted(tmp_path):
    """An interrupted load emits neither the content nor an error."""
    path = tmp_path / "app.log"
    path.write_text("content\n", encoding="utf-8")

    thread = LogLoaderThread(str(path), {})
    loaded = collect(thread.load_finished)
    errors = collect(thread.error)
    interrupt_from(thread, 0)
    thread.run()

    assert loaded == []
    assert errors == []


# ===========================================================================
# RealTimeFilterThread
# ===========================================================================

@pytest.mark.parametrize("level", [
    logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL,
])
def test_real_time_filter_accepts_every_level_when_the_filter_is_all(level):
    """``ALL`` switches the level check off completely."""
    thread = RealTimeFilterThread([], "ALL", "")
    assert thread._matches_filter(entry("whatever", level)) is True


@pytest.mark.parametrize("level,level_filter,expected", [
    (logging.ERROR, "ERROR", True),
    (logging.ERROR, "WARNING", False),
    (logging.CRITICAL, "CRITICAL", True),
    (logging.DEBUG, "DEBUG", True),
    (logging.WARNING, "WARN", False),          # the short alias is not the name
    (25, "INFO", False),                        # custom level -> "Level 25"
    (25, "Level 25", True),
])
def test_real_time_filter_compares_the_level_name_of_the_record(level, level_filter,
                                                               expected):
    """The record's numeric level is translated with ``logging.getLevelName``."""
    thread = RealTimeFilterThread([], level_filter, "")
    assert thread._matches_filter(entry("msg", level)) is expected


@pytest.mark.parametrize("needle,message,expected", [
    ("boom", "ERROR - boom happened", True),
    ("BOOM", "ERROR - boom happened", True),          # the needle is lower-cased
    ("boom", "ERROR - BOOM happened", True),          # so is the message
    ("žák", "INFO - Žák byl vytvořen", True),         # diacritics survive lower()
    ("nothing", "ERROR - boom happened", False),
    ("", "anything at all", True),                    # empty search matches all
    ("  ", "anything at all", False),                 # whitespace is a real needle
])
def test_real_time_filter_text_search_is_case_insensitive(needle, message, expected):
    """The text filter is a case-insensitive substring test on the message."""
    thread = RealTimeFilterThread([], "ALL", needle)
    assert thread._matches_filter(entry(message)) is expected


def test_real_time_filter_applies_level_and_text_together():
    """Both filters must match, not either of them."""
    thread = RealTimeFilterThread([], "ERROR", "disk")
    assert thread._matches_filter(entry("ERROR - disk full", logging.ERROR)) is True
    assert thread._matches_filter(entry("ERROR - net down", logging.ERROR)) is False
    assert thread._matches_filter(entry("INFO - disk full", logging.INFO)) is False


def test_real_time_filter_run_emits_the_matches_and_the_untouched_total():
    """``filter_finished`` carries the kept entries plus the input size."""
    logs = [entry("INFO - a"), entry("ERROR - b", logging.ERROR), entry("INFO - c")]
    thread = RealTimeFilterThread(logs, "ERROR", "")
    finished = collect(thread.filter_finished)

    thread.run()

    assert finished == [([entry("ERROR - b", logging.ERROR)], 3)]


def test_real_time_filter_run_reports_progress_every_hundred_entries():
    """Progress is sampled, not emitted per entry, and always names the total."""
    thread = RealTimeFilterThread([entry(f"m{i}") for i in range(250)], "ALL", "")
    progress = collect(thread.progress)

    thread.run()

    assert progress == [(0, 250), (100, 250), (200, 250)]


def test_real_time_filter_run_on_an_empty_list_emits_an_empty_result():
    """No entries still produces a result so the view can be emptied."""
    thread = RealTimeFilterThread([], "ALL", "")
    finished = collect(thread.filter_finished)
    progress = collect(thread.progress)

    thread.run()

    assert finished == [([], 0)]
    assert progress == []


def test_real_time_filter_run_discards_partial_results_when_interrupted():
    """An interruption in the middle of the loop throws the partial list away."""
    thread = RealTimeFilterThread([entry(f"m{i}") for i in range(50)], "ALL", "")
    finished = collect(thread.filter_finished)
    interrupt_from(thread, 10)

    thread.run()

    assert finished == []


def test_real_time_filter_run_checks_interruption_once_more_after_the_loop():
    """A cancellation that arrives after the last entry still suppresses the result."""
    logs = [entry(f"m{i}") for i in range(3)]
    thread = RealTimeFilterThread(logs, "ALL", "")
    finished = collect(thread.filter_finished)
    interrupt_from(thread, len(logs))   # False during the loop, True at the end

    thread.run()

    assert finished == []


def test_real_time_filter_run_swallows_a_malformed_entry(quiet_root_logger):
    """A record without ``msg`` is logged, not raised out of the thread."""
    thread = RealTimeFilterThread([{"level": logging.INFO}], "ALL", "")
    finished = collect(thread.filter_finished)

    thread.run()          # must not raise

    assert finished == []


def test_real_time_filter_rejects_a_non_string_search_text():
    """``None`` is not a search term - the constructor fails loudly."""
    with pytest.raises(AttributeError):
        RealTimeFilterThread([], "ALL", None)


# ===========================================================================
# HistoricalFilterThread
# ===========================================================================

@pytest.mark.parametrize("line", ["", "   ", "\t", "\n"])
def test_historical_filter_always_drops_blank_lines(line):
    """Blank lines are noise and never reach the view, whatever the filters."""
    thread = HistoricalFilterThread([], "ALL", None, False, "")
    assert thread._line_matches_filters(line) is False


@pytest.mark.parametrize("line,level_filter,expected", [
    ("2024-03-01 10:00:00 - app - ERROR - boom", "ERROR", True),
    ("2024-03-01 10:00:00 - app - INFO - hello", "ERROR", False),
    ("2024-03-01 10:00:00 - app - INFO - hello", "ALL", True),
    ("2024-03-01 10:00:00 - app - info - hello", "INFO", False),   # case sensitive
    # The level filter is a plain substring test over the whole line, so a level
    # name inside the *message* counts as a match.
    ("2024-03-01 10:00:00 - app - INFO - retrying after ERROR", "ERROR", True),
])
def test_historical_filter_level_is_a_substring_test_over_the_line(line, level_filter,
                                                                  expected):
    """A text log has no structured level, so the whole line is searched."""
    thread = HistoricalFilterThread([], level_filter, None, False, "")
    assert thread._line_matches_filters(line) is expected


@pytest.mark.parametrize("line,expected", [
    ("2024-03-01 10:00:00 - app - INFO - hit", True),
    ("2024-03-02 10:00:00 - app - INFO - other day", False),
    ("   2024-03-01 10:00:00 - app - INFO - indented", False),   # must be at the start
    ("app - INFO - no date at all", False),
    ("2024-02-30 10:00:00 - app - INFO - impossible date", False),
    ("2024-03-1 10:00:00 - app - INFO - not zero padded", False),
])
def test_historical_filter_keeps_only_lines_stamped_with_the_chosen_day(line, expected):
    """The date must be a valid ISO date at the very start of the line."""
    thread = HistoricalFilterThread([], "ALL", date(2024, 3, 1), True, "")
    assert thread._line_matches_filters(line) is expected


def test_historical_filter_ignores_the_date_when_no_date_was_given():
    """The checkbox alone does not filter - an unset date disables the check."""
    thread = HistoricalFilterThread([], "ALL", None, True, "")
    assert thread._line_matches_filters("no date here at all") is True


def test_historical_filter_ignores_the_date_when_the_checkbox_is_off():
    """A stored date is only applied while the checkbox is ticked."""
    thread = HistoricalFilterThread([], "ALL", date(2024, 3, 1), False, "")
    assert thread._line_matches_filters("2024-12-24 - app - INFO - x") is True


@pytest.mark.parametrize("needle,line,expected", [
    ("kůň", "2024-03-01 - app - INFO - Žluťoučký KŮŇ", True),
    ("KŮŇ", "2024-03-01 - app - INFO - žluťoučký kůň", True),
    ("missing", "2024-03-01 - app - INFO - žluťoučký kůň", False),
])
def test_historical_filter_text_search_is_case_insensitive(needle, line, expected):
    """Searching a historical line ignores case on both sides, diacritics included."""
    thread = HistoricalFilterThread([], "ALL", None, False, needle)
    assert thread._line_matches_filters(line) is expected


def test_historical_filter_requires_every_active_filter_to_match():
    """Level, date and text are combined with AND."""
    thread = HistoricalFilterThread([], "ERROR", date(2024, 3, 1), True, "disk")
    assert thread._line_matches_filters("2024-03-01 - app - ERROR - disk full") is True
    assert thread._line_matches_filters("2024-03-02 - app - ERROR - disk full") is False
    assert thread._line_matches_filters("2024-03-01 - app - INFO - disk full") is False
    assert thread._line_matches_filters("2024-03-01 - app - ERROR - net down") is False


def test_historical_filter_run_emits_the_kept_lines_in_input_order():
    """Ordering is preserved and blank lines are dropped on the way."""
    lines = ["2024-03-01 - ERROR - a", "", "2024-03-01 - INFO - b",
             "2024-03-01 - ERROR - c"]
    thread = HistoricalFilterThread(lines, "ERROR", None, False, "")
    finished = collect(thread.filter_finished)

    thread.run()

    assert finished == [(["2024-03-01 - ERROR - a", "2024-03-01 - ERROR - c"],)]


def test_historical_filter_run_reports_progress_every_five_hundred_lines():
    """The historical sampling interval is coarser than the real-time one."""
    thread = HistoricalFilterThread(["INFO - x"] * 1200, "ALL", None, False, "")
    progress = collect(thread.progress)

    thread.run()

    assert progress == [(0, 1200), (500, 1200), (1000, 1200)]


def test_historical_filter_run_emits_nothing_when_interrupted():
    """Cancelling a historical filter discards everything collected so far."""
    thread = HistoricalFilterThread(["INFO - x"] * 20, "ALL", None, False, "")
    finished = collect(thread.filter_finished)
    interrupt_from(thread, 5)

    thread.run()

    assert finished == []


# ===========================================================================
# RealTimeLogHandler
# ===========================================================================

def test_real_time_handler_forwards_the_formatted_message_and_the_level():
    """The callback receives the formatted text and the numeric level."""
    seen = []
    handler = RealTimeLogHandler(lambda msg, level: seen.append((msg, level)))
    handler.setFormatter(logging.Formatter("%(levelname)s|%(message)s"))
    record = logging.LogRecord("ui.test", logging.WARNING, __file__, 10,
                               "disk %s", ("full",), None)

    handler.emit(record)

    assert seen == [("WARNING|disk full", logging.WARNING)]


def test_real_time_handler_routes_a_failing_callback_to_handle_error():
    """An exception in the GUI callback must not break the logging call."""
    handled = []
    handler = RealTimeLogHandler(lambda msg, level: 1 / 0)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.handleError = handled.append
    record = logging.LogRecord("ui.test", logging.INFO, __file__, 10, "x", (), None)

    handler.emit(record)          # must not raise

    assert handled == [record]


# ===========================================================================
# _line_color
# ===========================================================================

@pytest.mark.parametrize("line,key", [
    ("2024-03-01 - app - ERROR - boom", "ERROR"),
    ("2024-03-01 - app - CRITICAL - boom", "CRITICAL"),
    ("2024-03-01 - app - WARNING - hmm", "WARNING"),
    ("2024-03-01 - app - INFO - hello", "INFO"),
    ("2024-03-01 - app - DEBUG - detail", "DEBUG"),
    ("a line without any level", "DEFAULT"),
    ("", "DEFAULT"),
])
def test_line_color_matches_the_shared_colour_table(line, key):
    """The historical view uses exactly the colours of ``LOG_COLORS``."""
    assert LogViewerTab._line_color(line) == LOG_COLORS[key]


@pytest.mark.parametrize("line,key", [
    ("INFO - failed with ERROR code", "ERROR"),
    ("WARNING - and also ERROR", "ERROR"),
    ("DEBUG - and WARNING", "WARNING"),
    ("DEBUG - and INFO", "INFO"),
])
def test_line_color_gives_the_most_severe_level_on_the_line(line, key):
    """A line naming two levels is coloured after the more severe one."""
    assert LogViewerTab._line_color(line) == LOG_COLORS[key]


@pytest.mark.parametrize("line", ["error - lower case", "warning", "info", "debug"])
def test_line_color_only_recognises_upper_case_level_names(line):
    """Levels are written upper case by the formatter, so the match is exact."""
    assert LogViewerTab._line_color(line) == LOG_COLORS["DEFAULT"]


# ===========================================================================
# display_colored_log
# ===========================================================================

@pytest.mark.gui
def test_display_colored_log_builds_the_document_in_a_single_call(tab):
    """The whole view is one ``setHtml`` - never one ``insertHtml`` per line."""
    calls = {"set_html": 0, "insert_html": 0}
    original = tab.historical_text.setHtml

    def counting_set_html(html):
        calls["set_html"] += 1
        original(html)

    tab.historical_text.setHtml = counting_set_html
    tab.historical_text.insertHtml = lambda html: calls.__setitem__(
        "insert_html", calls["insert_html"] + 1)

    tab.display_colored_log([f"2024-03-01 - app - INFO - line {i}" for i in range(200)])

    assert calls == {"set_html": 1, "insert_html": 0}
    assert len(shown_lines(tab.historical_text)) == 200


@pytest.mark.gui
@pytest.mark.slow
def test_display_colored_log_renders_twenty_thousand_lines_quickly(tab):
    """20 000 lines must render in one pass - the old per-line version took minutes."""
    lines = [f"2024-03-01 10:00:00 - app - INFO - message {i}" for i in range(20000)]

    started = time.perf_counter()
    tab.display_colored_log(lines)
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"rendering took {elapsed:.2f}s"
    assert len(shown_lines(tab.historical_text)) == MAX_DISPLAYED_LOG_LINES + 1


@pytest.mark.gui
def test_display_colored_log_caps_the_view_and_keeps_the_newest_lines(tab):
    """Above the cap the oldest lines go and a notice explains the trimming."""
    lines = [f"line {i}" for i in range(MAX_DISPLAYED_LOG_LINES + 3)]

    tab.display_colored_log(lines)

    visible = shown_lines(tab.historical_text)
    assert len(visible) == MAX_DISPLAYED_LOG_LINES + 1
    assert visible[0] == (f"— showing the last {MAX_DISPLAYED_LOG_LINES} of "
                          f"{len(lines)} lines. Use the filters above to narrow "
                          "the result. —")
    assert visible[1] == "line 3"                       # the three oldest are gone
    assert visible[-1] == f"line {MAX_DISPLAYED_LOG_LINES + 2}"


@pytest.mark.gui
def test_display_colored_log_shows_no_notice_exactly_at_the_cap(tab):
    """The notice appears only when something was actually dropped."""
    lines = [f"line {i}" for i in range(MAX_DISPLAYED_LOG_LINES)]

    tab.display_colored_log(lines)

    visible = shown_lines(tab.historical_text)
    assert len(visible) == MAX_DISPLAYED_LOG_LINES
    assert visible[0] == "line 0"
    assert "showing the last" not in tab.historical_text.toPlainText()


@pytest.mark.gui
def test_display_colored_log_escapes_html_instead_of_rendering_it(tab):
    """A log line containing markup is shown literally, not interpreted."""
    tab.display_colored_log(['<script>alert("x")</script> & <b>bold</b>'])

    assert tab.historical_text.toPlainText().startswith(
        '<script>alert("x")</script> & <b>bold</b>')
    assert "&lt;script&gt;" in tab.historical_text.toHtml()


@pytest.mark.gui
def test_display_colored_log_colours_each_line_by_its_level(tab):
    """Both colours of a mixed block end up in the document."""
    tab.display_colored_log(["app - ERROR - boom", "app - WARNING - hmm"])

    html = tab.historical_text.toHtml().lower()
    assert LOG_COLORS["ERROR"].lower().lstrip("#") in html
    assert LOG_COLORS["WARNING"].lower().lstrip("#") in html


@pytest.mark.gui
def test_display_colored_log_empties_the_view_for_an_empty_result(tab):
    """Filtering everything away leaves a clean view and no notice."""
    tab.display_colored_log(["something"])

    tab.display_colored_log([])

    assert shown_lines(tab.historical_text) == []


# ===========================================================================
# append_colored_line
# ===========================================================================

@pytest.mark.gui
def test_append_colored_line_escapes_html(tab):
    """Markup in a live log message is displayed, not executed."""
    tab.append_colored_line("<b>&amp;</b> <i>tag</i>")

    assert shown_lines(tab.real_time_text) == ["<b>&amp;</b> <i>tag</i>"]


@pytest.mark.gui
@pytest.mark.parametrize("line,key", [
    ("10:00:00 - app - ERROR - boom", "ERROR"),
    ("10:00:00 - app - CRITICAL - boom", "CRITICAL"),
    ("10:00:00 - app - WARNING - hmm", "WARNING"),
    ("10:00:00 - app - INFO - hi", "INFO"),
    ("10:00:00 - app - DEBUG - d", "DEBUG"),
    ("no level here", "DEFAULT"),
])
def test_append_colored_line_uses_the_same_colours_as_the_historical_view(tab, line, key):
    """Both views must colour an identical line identically."""
    tab.append_colored_line(line)

    assert LOG_COLORS[key] == LogViewerTab._line_color(line)
    assert LOG_COLORS[key].lower().lstrip("#") in tab.real_time_text.toHtml().lower()


@pytest.mark.gui
def test_append_colored_line_keeps_the_messages_in_arrival_order(tab):
    """Lines are appended, never prepended."""
    for index in range(5):
        tab.append_colored_line(f"line {index}")

    assert shown_lines(tab.real_time_text) == [f"line {i}" for i in range(5)]


@pytest.mark.gui
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: real-time view is never trimmed, lineCount() stays 1",
                   strict=False)
def test_append_colored_line_trims_the_view_to_the_line_cap(tab, monkeypatch):
    """The real-time view must never hold more than the configured line cap."""
    monkeypatch.setattr(lvt, "MAX_REAL_TIME_LOG_LINES", 5)

    for index in range(9):
        tab.append_colored_line(f"line {index}")

    visible = shown_lines(tab.real_time_text)
    assert len(visible) <= 5
    assert visible[-1] == "line 8"


@pytest.mark.gui
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: every message lands in one paragraph, so the view "
                          "cannot be trimmed and each append re-lays it out",
                   strict=False)
def test_append_colored_line_starts_a_new_paragraph_per_message(tab):
    """
    Each message must be its own block in the document.

    ``insertHtml('...<br>')`` keeps all of them inside a single paragraph: that
    is why ``document().lineCount()`` never grows (so the line cap above never
    trims anything) and why every append re-lays out all previous messages -
    with a realised layout, appending 1 200 lines costs about 11 s of blocked
    GUI time, and the cost per line keeps growing.
    """
    for index in range(20):
        tab.append_colored_line(f"2024-03-01 10:00:00 - app - INFO - line {index}")

    document = tab.real_time_text.document()
    assert document.blockCount() == 20
    assert document.lineCount() == 20


def test_the_line_caps_have_their_documented_values():
    """The two caps are part of the module contract."""
    assert MAX_REAL_TIME_LOG_LINES == 10000
    assert MAX_DISPLAYED_LOG_LINES == 5000
    assert MAX_LOG_FILE_SIZE_MB == 10


# ===========================================================================
# add_real_time_log
# ===========================================================================

@pytest.mark.gui
def test_add_real_time_log_stores_displays_and_counts_the_message(tab):
    """A matching record is remembered, shown and counted in the stats label."""
    tab.add_real_time_log("10:00:00 - app - ERROR - boom", logging.ERROR)

    assert tab.real_time_logs == [entry("10:00:00 - app - ERROR - boom", logging.ERROR)]
    assert shown_lines(tab.real_time_text) == ["10:00:00 - app - ERROR - boom"]
    assert tab.rt_stats_label.text() == "Lines: 1 / 1"


@pytest.mark.gui
def test_add_real_time_log_counts_visible_against_stored_messages(tab):
    """The stats label is ``visible / stored``, not ``stored / stored``."""
    tab.rt_level_combo.setCurrentText("ERROR")
    tab.add_real_time_log("a", logging.ERROR)
    tab.add_real_time_log("b", logging.INFO)
    tab.add_real_time_log("c", logging.ERROR)

    assert len(tab.real_time_logs) == 3
    assert tab.rt_stats_label.text() == "Lines: 2 / 3"


@pytest.mark.gui
def test_add_real_time_log_stores_but_hides_a_message_the_filter_rejects(tab):
    """A filtered-out record is kept for later filtering but not displayed."""
    tab.rt_level_combo.setCurrentText("ERROR")

    tab.add_real_time_log("10:00:00 - app - INFO - hidden", logging.INFO)

    assert tab.real_time_logs == [entry("10:00:00 - app - INFO - hidden", logging.INFO)]
    assert shown_lines(tab.real_time_text) == []
    assert tab.rt_stats_label.text() == "Lines: 0"      # left over from the filter


@pytest.mark.gui
def test_add_real_time_log_honours_the_text_filter_with_diacritics(tab):
    """The live search matches case-insensitively on accented text too."""
    type_search(tab.rt_search_input, tab.rt_search_debounce_timer, "ŽÁK")

    tab.add_real_time_log("INFO - žák byl vytvořen", logging.INFO)
    tab.add_real_time_log("INFO - something else", logging.INFO)

    assert shown_lines(tab.real_time_text) == ["INFO - žák byl vytvořen"]
    assert len(tab.real_time_logs) == 2


@pytest.mark.gui
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: the stats counter is not updated for hidden messages",
                   strict=False)
def test_add_real_time_log_keeps_the_stored_counter_up_to_date(tab):
    """The ``visible / stored`` counter must follow messages the filter hides too."""
    tab.add_real_time_log("INFO - alpha", logging.INFO)
    type_search(tab.rt_search_input, tab.rt_search_debounce_timer, "alpha")

    tab.add_real_time_log("INFO - beta", logging.INFO)

    assert len(tab.real_time_logs) == 2
    assert tab.rt_stats_label.text() == "Lines: 1 / 2"


@pytest.mark.gui
def test_add_real_time_log_drops_the_oldest_records_above_the_cap(tab, monkeypatch):
    """The stored list is a ring buffer of the newest ``MAX`` messages."""
    monkeypatch.setattr(lvt, "MAX_REAL_TIME_LOG_LINES", 4)
    tab.rt_level_combo.setCurrentText("CRITICAL")      # nothing is rendered

    for index in range(7):
        tab.add_real_time_log(f"m{index}", logging.INFO)

    assert [record["msg"] for record in tab.real_time_logs] == ["m3", "m4", "m5", "m6"]


@pytest.mark.gui
def test_log_matches_filter_follows_the_live_filter_widgets(tab):
    """The helper reads the current combo and search box, not a snapshot."""
    record = entry("10:00:00 - app - INFO - hello", logging.INFO)
    assert tab._log_matches_filter(record) is True

    tab.rt_level_combo.setCurrentText("ERROR")
    assert tab._log_matches_filter(record) is False

    tab.rt_level_combo.setCurrentText("INFO")
    type_search(tab.rt_search_input, tab.rt_search_debounce_timer, "nope")
    assert tab._log_matches_filter(record) is False


# ===========================================================================
# clear_real_time_log
# ===========================================================================

@pytest.mark.gui
def test_clear_real_time_log_asks_before_throwing_anything_away(tab, dialogs):
    """The user is asked with a question dialog naming what will be cleared."""
    dialogs.question_answer = QMessageBox.StandardButton.No
    tab.add_real_time_log("keep me", logging.INFO)

    tab.clear_real_time_log()

    assert dialogs.kinds() == ["question"]
    assert dialogs.titles() == ["Clear Logs"]
    assert tab.real_time_logs == [entry("keep me")]
    assert shown_lines(tab.real_time_text) == ["keep me"]


@pytest.mark.gui
def test_clear_real_time_log_empties_view_list_and_counter_on_yes(tab, dialogs):
    """Confirming wipes the display, the stored records and the counter."""
    dialogs.question_answer = QMessageBox.StandardButton.Yes
    tab.add_real_time_log("a", logging.INFO)
    tab.add_real_time_log("b", logging.INFO)

    tab.clear_real_time_log()

    assert tab.real_time_logs == []
    assert shown_lines(tab.real_time_text) == []
    assert tab.rt_stats_label.text() == "Lines: 0"


@pytest.mark.gui
def test_clear_real_time_log_is_idempotent(tab, dialogs):
    """Clearing an already empty view changes nothing and does not raise."""
    dialogs.question_answer = QMessageBox.StandardButton.Yes
    tab.add_real_time_log("a", logging.INFO)

    tab.clear_real_time_log()
    tab.clear_real_time_log()

    assert tab.real_time_logs == []
    assert tab.rt_stats_label.text() == "Lines: 0"
    assert dialogs.kinds() == ["question", "question"]


# ===========================================================================
# update_real_time_filter and its result slot
# ===========================================================================

@pytest.mark.gui
def test_update_real_time_filter_does_nothing_without_stored_messages(tab, no_start):
    """An empty buffer is reported as ``Lines: 0`` and starts no thread."""
    tab.update_real_time_filter()

    assert tab.rt_stats_label.text() == "Lines: 0"
    assert tab.rt_filter_thread is None


@pytest.mark.gui
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: the early return leaves the UI in the filtering state",
                   strict=False)
def test_update_real_time_filter_re_enables_the_ui_when_there_is_nothing_to_do(tab):
    """
    Bailing out early must still release the UI.

    Reproduction: start a filter (the level combo is disabled), let it be
    interrupted by the next keystroke, clear the log in the meantime - the
    debounced filter then returns early and the combo stays disabled forever.
    """
    tab._set_filtering_state(True, "real-time")

    tab.update_real_time_filter()

    assert tab._is_filtering is False
    assert tab.rt_level_combo.isEnabled() is True


@pytest.mark.gui
def test_update_real_time_filter_passes_a_snapshot_of_the_buffer(tab, no_start):
    """The thread works on a copy, so new records cannot corrupt the filtering."""
    tab.real_time_logs.append(entry("INFO - first"))
    type_search(tab.rt_search_input, tab.rt_search_debounce_timer, "First")
    tab.rt_level_combo.setCurrentText("INFO")

    tab.update_real_time_filter()
    thread = tab.rt_filter_thread
    tab.real_time_logs.append(entry("INFO - second"))

    assert thread.started is True
    assert thread.logs == [entry("INFO - first")]
    assert thread.level_filter == "INFO"
    assert thread.search_text == "first"            # lower-cased by the thread
    assert tab._is_filtering is True
    assert tab.rt_level_combo.isEnabled() is False


@pytest.mark.gui
def test_rt_filter_result_replaces_the_view_and_releases_the_ui(tab, no_start):
    """The current thread's result is rendered and the controls come back."""
    tab.real_time_logs.append(entry("INFO - first"))
    tab.append_colored_line("stale content")
    tab.update_real_time_filter()

    tab.rt_filter_thread.filter_finished.emit([entry("INFO - kept")], 7)

    assert shown_lines(tab.real_time_text) == ["INFO - kept"]
    assert tab.rt_stats_label.text() == "Lines: 1 / 7"
    assert tab._is_filtering is False
    assert tab.rt_level_combo.isEnabled() is True


@pytest.mark.gui
def test_rt_filter_result_of_a_superseded_thread_is_ignored(tab, no_start):
    """A late result from an abandoned thread must not overwrite the view."""
    tab.real_time_logs.append(entry("INFO - first"))
    tab.append_colored_line("INFO - already on screen")
    tab.update_real_time_filter()
    superseded = tab.rt_filter_thread
    tab.update_real_time_filter()               # a second, newer thread

    assert superseded is not tab.rt_filter_thread
    superseded.filter_finished.emit([entry("INFO - stale")], 99)

    assert shown_lines(tab.real_time_text) == ["INFO - already on screen"]
    assert tab.rt_stats_label.text() != "Lines: 1 / 99"
    assert tab.rt_level_combo.isEnabled() is False      # the newer run still owns it


@pytest.mark.gui
def test_rt_filter_progress_shows_the_current_position(tab):
    """Progress overwrites the counter instead of appending to it."""
    tab._on_rt_filter_progress(100, 4000)
    tab._on_rt_filter_progress(200, 4000)

    assert tab.rt_stats_label.text() == "Filtering... 200/4000"


@pytest.mark.gui
def test_typing_in_the_search_box_only_arms_the_debounce_timer(tab):
    """Every keystroke restarts a 300 ms timer instead of filtering at once."""
    tab.real_time_logs.append(entry("INFO - x"))

    tab.rt_search_input.setText("a")

    assert tab.rt_search_debounce_timer.isActive() is True
    assert tab.rt_search_debounce_timer.isSingleShot() is True
    assert tab.rt_search_debounce_timer.interval() == 300
    assert tab.rt_filter_thread is None
    tab.rt_search_debounce_timer.stop()


# ===========================================================================
# load_log_file
# ===========================================================================

@pytest.mark.gui
@pytest.mark.parametrize("data", [None, "not a dictionary", ["path"], 42, {}])
def test_load_log_file_rejects_item_data_that_is_not_a_log_entry(tab, dialogs, data):
    """Anything but a non-empty dictionary is refused with a warning."""
    tab.load_log_file(list_item(data))

    assert dialogs.kinds() == ["warning"]
    assert dialogs.texts() == ["Invalid log file data"]
    assert tab.loader_thread is None


@pytest.mark.gui
def test_load_log_file_refuses_an_entry_without_a_path(tab, dialogs):
    """A dictionary without ``path`` cannot be opened and says so."""
    tab.load_log_file(list_item({"name": "app.log", "size": 10}))

    assert dialogs.kinds() == ["warning"]
    assert dialogs.texts() == ["Log file path not found"]
    assert tab.loader_thread is None


@pytest.mark.gui
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: a log entry without 'size' raises KeyError in the slot",
                   strict=False)
def test_load_log_file_refuses_an_entry_without_a_size(tab, dialogs, tmp_path):
    """Item data is validated, so a truncated entry must warn, not crash."""
    path = tmp_path / "app.log"
    path.write_text("x\n", encoding="utf-8")

    tab.load_log_file(list_item({"path": str(path), "name": "app.log"}))

    assert dialogs.kinds() == ["warning"]
    assert tab.loader_thread is None


@pytest.mark.gui
def test_load_log_file_asks_before_opening_a_large_file_and_obeys_no(tab, dialogs,
                                                                    tmp_path, no_start):
    """Above 10 MB the user decides; ``No`` stops the load completely."""
    path = tmp_path / "big.log"
    path.write_text("x\n", encoding="utf-8")
    dialogs.question_answer = QMessageBox.StandardButton.No
    huge = log_file_entry(path, size=int(12.5 * MB_TO_BYTES))

    tab.load_log_file(list_item(huge))

    assert dialogs.kinds() == ["question"]
    assert dialogs.titles() == ["Large File"]
    assert "12.50 MB" in dialogs.texts()[0]
    assert tab.loader_thread is None
    assert tab.historical_text.toPlainText() == ""


@pytest.mark.gui
def test_load_log_file_starts_the_loader_when_the_large_file_is_confirmed(tab, dialogs,
                                                                         tmp_path,
                                                                         no_start):
    """``Yes`` goes on and hands the path to the loader thread."""
    path = tmp_path / "big.log"
    path.write_text("x\n", encoding="utf-8")
    dialogs.question_answer = QMessageBox.StandardButton.Yes
    huge = log_file_entry(path, size=11 * MB_TO_BYTES)

    tab.load_log_file(list_item(huge))

    assert dialogs.kinds() == ["question"]
    assert tab.loader_thread.started is True
    assert tab.loader_thread.file_path == str(path)
    assert tab.historical_text.toPlainText() == "Loading..."
    assert tab.hist_stats_label.text() == "Loading file..."


@pytest.mark.gui
def test_load_log_file_does_not_ask_about_a_small_file(tab, dialogs, tmp_path, no_start):
    """A file at the limit is opened straight away."""
    path = tmp_path / "app.log"
    path.write_text("x\n", encoding="utf-8")

    tab.load_log_file(list_item(log_file_entry(path, size=MAX_LOG_FILE_SIZE_MB
                                               * MB_TO_BYTES)))

    assert dialogs.calls == []
    assert tab.loader_thread.started is True


@pytest.mark.gui
def test_load_log_file_abandons_a_previous_loader(tab, dialogs, tmp_path, no_start):
    """A second click disconnects and interrupts the loader that is still busy."""
    path = tmp_path / "app.log"
    path.write_text("x\n", encoding="utf-8")
    item = list_item(log_file_entry(path))

    tab.load_log_file(item)
    first = tab.loader_thread
    first.isRunning = lambda: True
    interruptions = []
    first.requestInterruption = lambda: interruptions.append(True)
    first.wait = lambda msecs=None: True

    tab.load_log_file(item)

    assert interruptions == [True]
    assert tab.loader_thread is not first


@pytest.mark.gui
@pytest.mark.integration
def test_load_log_file_displays_the_file_through_the_real_threads(tab, qapp, tmp_path):
    """End to end: loader thread, filter thread and the coloured view."""
    path = tmp_path / "app.log"
    path.write_text("2024-03-01 10:00:00 - app - INFO - start\n"
                    "2024-03-01 10:00:01 - app - ERROR - boom\n", encoding="utf-8")

    tab.load_log_file(list_item(log_file_entry(path)))

    assert tab.loader_thread.wait(5000)
    qapp.processEvents()
    assert tab.hist_filter_thread.wait(5000)
    qapp.processEvents()

    assert shown_lines(tab.historical_text) == [
        "2024-03-01 10:00:00 - app - INFO - start",
        "2024-03-01 10:00:01 - app - ERROR - boom",
    ]
    assert "Lines: 3" in tab.hist_stats_label.text()
    assert tab.hist_stats_label.text().endswith("| Filtered: 2 lines")


# ===========================================================================
# on_log_file_loaded / on_log_file_error
# ===========================================================================

@pytest.mark.gui
def test_on_log_file_loaded_keeps_the_content_and_describes_the_file(tab, no_start,
                                                                    tmp_path):
    """The raw text is stored for re-filtering and the stats line is filled in."""
    data = {"name": "app.log", "size": int(0.5 * MB_TO_BYTES)}

    tab.on_log_file_loaded("a\nb\n", data)

    assert tab.current_log_content == "a\nb\n"
    # ``split('\n')`` counts the empty string after the trailing newline as a line
    assert tab.hist_stats_label.text() == "File: app.log | Size: 0.50 MB | Lines: 3"


@pytest.mark.gui
def test_on_log_file_loaded_with_empty_content_skips_the_filtering(tab, no_start):
    """An empty file is described but never handed to a filter thread."""
    tab.on_log_file_loaded("", {"name": "empty.log", "size": 0})

    assert tab.current_log_content == ""
    assert tab.hist_filter_thread is None
    assert tab.hist_stats_label.text() == "File: empty.log | Size: 0.00 MB | Lines: 1"


@pytest.mark.gui
def test_on_log_file_loaded_reports_a_broken_entry_instead_of_raising(tab, dialogs,
                                                                     no_start):
    """A metadata dictionary without ``size`` ends in a dialog, not a traceback."""
    tab.on_log_file_loaded("a\n", {"name": "app.log"})

    assert dialogs.kinds() == ["critical"]
    assert dialogs.texts()[0].startswith("Failed to process log file:")


@pytest.mark.gui
def test_on_log_file_error_clears_the_view_and_tells_the_user(tab, dialogs):
    """An error empties the view and surfaces the message in a critical box."""
    tab.historical_text.setPlainText("Loading...")

    tab.on_log_file_error("Permission denied to read file")

    assert tab.historical_text.toPlainText() == ""
    assert tab.hist_stats_label.text() == "Error loading file"
    assert dialogs.kinds() == ["critical"]
    assert "Permission denied to read file" in dialogs.texts()[0]


@pytest.mark.gui
@pytest.mark.parametrize("message,refreshes", [
    ("File was deleted or moved", 1),
    ("file was DELETED", 1),
    ("Permission denied to read file", 0),
    ("Could not read the file: Is a directory", 0),
])
def test_on_log_file_error_refreshes_the_list_only_for_a_vanished_file(tab, dialogs,
                                                                      message,
                                                                      refreshes):
    """Only a deleted file makes the file list stale, so only then is it rebuilt."""
    calls = []
    tab.refresh_log_files = lambda: calls.append(True)

    tab.on_log_file_error(message)

    assert len(calls) == refreshes


# ===========================================================================
# apply_historical_filters and its result slot
# ===========================================================================

@pytest.mark.gui
def test_apply_historical_filters_does_nothing_without_a_loaded_file(tab, no_start):
    """Without content there is nothing to filter and no thread is created."""
    tab.apply_historical_filters()

    assert tab.hist_filter_thread is None


@pytest.mark.gui
def test_apply_historical_filters_hands_the_current_widget_values_to_the_thread(
        tab, no_start):
    """Level, date and search box are read at start time, not at emit time."""
    tab.current_log_content = "2024-03-01 - app - ERROR - boom\n2024-03-02 - INFO - x"
    tab.hist_level_combo.setCurrentText("ERROR")
    tab.hist_date_check.setChecked(True)
    tab.hist_date_filter.setDate(QDate(2024, 3, 1))
    type_search(tab.hist_search_input, tab.hist_search_debounce_timer, "BOOM")

    tab.apply_historical_filters()

    thread = tab.hist_filter_thread
    assert thread.started is True
    assert thread.lines == ["2024-03-01 - app - ERROR - boom", "2024-03-02 - INFO - x"]
    assert thread.level_filter == "ERROR"
    assert thread.date_filter == date(2024, 3, 1)
    assert thread.date_filter_enabled is True
    assert thread.search_text == "boom"
    assert tab.hist_level_combo.isEnabled() is False
    assert tab.hist_date_filter.isEnabled() is False


@pytest.mark.gui
def test_apply_historical_filters_leaves_the_date_unset_while_the_box_is_unticked(
        tab, no_start):
    """An unticked checkbox means no date is passed on at all."""
    tab.current_log_content = "2024-03-01 - INFO - x"
    tab.hist_date_check.setChecked(False)
    tab.hist_date_filter.setDate(QDate(2024, 3, 1))

    tab.apply_historical_filters()

    assert tab.hist_filter_thread.date_filter is None
    assert tab.hist_filter_thread.date_filter_enabled is False


@pytest.mark.gui
def test_hist_filter_result_renders_the_lines_and_releases_the_ui(tab, no_start):
    """The current thread's result is displayed and the stats line completed."""
    tab.current_log_content = "2024-03-01 - INFO - x"
    tab.hist_stats_label.setText("File: app.log | Size: 0.01 MB | Lines: 2")
    tab.apply_historical_filters()

    tab.hist_filter_thread.filter_finished.emit(["app - ERROR - boom", "app - INFO - x"])

    assert shown_lines(tab.historical_text) == ["app - ERROR - boom", "app - INFO - x"]
    assert tab.hist_stats_label.text() == (
        "File: app.log | Size: 0.01 MB | Lines: 2 | Filtered: 2 lines")
    assert tab._is_filtering is False
    assert tab.hist_level_combo.isEnabled() is True


@pytest.mark.gui
def test_hist_filter_result_replaces_a_previous_filtered_counter(tab, no_start):
    """Filtering twice does not stack two ``Filtered:`` suffixes."""
    tab.current_log_content = "2024-03-01 - INFO - x"
    tab.hist_stats_label.setText("File: app.log | Lines: 2")

    tab.apply_historical_filters()
    tab.hist_filter_thread.filter_finished.emit(["a", "b"])
    tab.apply_historical_filters()
    tab.hist_filter_thread.filter_finished.emit(["a"])

    assert tab.hist_stats_label.text() == "File: app.log | Lines: 2 | Filtered: 1 lines"


@pytest.mark.gui
def test_hist_filter_result_of_a_superseded_thread_is_ignored(tab, no_start):
    """The result of an abandoned filter never reaches the view or the label."""
    tab.current_log_content = "2024-03-01 - INFO - x"
    tab.hist_stats_label.setText("File: app.log")
    tab.apply_historical_filters()
    superseded = tab.hist_filter_thread
    tab.apply_historical_filters()

    assert superseded is not tab.hist_filter_thread
    superseded.filter_finished.emit(["stale line"])

    assert shown_lines(tab.historical_text) == []
    assert tab.hist_stats_label.text() == "File: app.log"
    assert tab._is_filtering is True
    assert tab.hist_level_combo.isEnabled() is False


@pytest.mark.gui
def test_hist_filter_progress_appends_the_counter_to_the_file_description(tab):
    """The first progress update extends the file line with the position."""
    tab.hist_stats_label.setText("File: app.log | Size: 0.01 MB | Lines: 12")

    tab._on_hist_filter_progress(0, 1000)

    assert tab.hist_stats_label.text() == (
        "File: app.log | Size: 0.01 MB | Lines: 12 | Filtering... 0/1000")


@pytest.mark.gui
def test_hist_filter_progress_replaces_a_finished_counter(tab):
    """A new run drops the ``Filtered:`` suffix of the previous one."""
    tab.hist_stats_label.setText("File: app.log | Filtered: 9 lines")

    tab._on_hist_filter_progress(500, 1000)

    assert tab.hist_stats_label.text() == "File: app.log | Filtering... 500/1000"


@pytest.mark.gui
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: consecutive progress updates append instead of replace",
                   strict=False)
def test_hist_filter_progress_does_not_pile_up_counters(tab):
    """Every progress update must replace the previous one, not extend it."""
    tab.hist_stats_label.setText("File: app.log | Lines: 12")

    tab._on_hist_filter_progress(0, 1000)
    tab._on_hist_filter_progress(500, 1000)
    tab._on_hist_filter_progress(1000, 1000)

    assert tab.hist_stats_label.text().count("Filtering...") == 1
    assert tab.hist_stats_label.text() == "File: app.log | Lines: 12 | Filtering... 1000/1000"


# ===========================================================================
# refresh_log_files
# ===========================================================================

@pytest.mark.gui
def test_refresh_log_files_does_nothing_while_the_tab_is_hidden(tab,
                                                               isolated_logging_config):
    """The 5 s auto-refresh must not work when nobody can see the list."""
    log_dir = isolated_logging_config.get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / isolated_logging_config.config["log_file"]).write_text("x\n")

    tab.refresh_log_files()

    assert tab.file_list.count() == 0


@pytest.mark.gui
def test_refresh_log_files_lists_current_and_rotated_files(tab, isolated_logging_config,
                                                           monkeypatch):
    """Every log file becomes one item carrying its own metadata dictionary."""
    monkeypatch.setattr(tab, "isVisible", lambda: True)
    log_dir = isolated_logging_config.get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    name = isolated_logging_config.config["log_file"]
    (log_dir / name).write_text("current\n", encoding="utf-8")
    (log_dir / f"{name}.1").write_text("older\n", encoding="utf-8")

    tab.refresh_log_files()

    assert tab.file_list.count() == 2
    texts = [tab.file_list.item(i).text() for i in range(2)]
    assert any(text.startswith("🟢") and "(Current)" in text for text in texts)
    assert any(text.startswith("📄") for text in texts)
    for index in range(2):
        data = tab.file_list.item(index).data(Qt.ItemDataRole.UserRole)
        assert set(data) >= {"path", "name", "size", "modified", "is_current"}
        assert "Modified:" in tab.file_list.item(index).text()


@pytest.mark.gui
def test_refresh_log_files_keeps_the_selected_file_selected(tab,
                                                            isolated_logging_config,
                                                            monkeypatch):
    """Rebuilding the list must not throw the user out of the file they read."""
    monkeypatch.setattr(tab, "isVisible", lambda: True)
    log_dir = isolated_logging_config.get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    name = isolated_logging_config.config["log_file"]
    (log_dir / name).write_text("current\n", encoding="utf-8")
    (log_dir / f"{name}.1").write_text("older\n", encoding="utf-8")
    tab.refresh_log_files()
    wanted = next(tab.file_list.item(i) for i in range(tab.file_list.count())
                  if tab.file_list.item(i).data(
                      Qt.ItemDataRole.UserRole)["name"].endswith(".1"))
    tab.file_list.setCurrentItem(wanted)

    tab.refresh_log_files()

    current = tab.file_list.currentItem()
    assert current is not None
    assert current.data(Qt.ItemDataRole.UserRole)["name"] == f"{name}.1"


@pytest.mark.gui
def test_refresh_log_files_survives_a_failing_log_directory(tab, isolated_logging_config,
                                                            monkeypatch):
    """An unreadable log folder empties the list instead of killing the tab."""
    monkeypatch.setattr(tab, "isVisible", lambda: True)

    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(isolated_logging_config, "get_log_files", boom)

    tab.refresh_log_files()          # must not raise

    assert tab.file_list.count() == 0


# ===========================================================================
# open_log_folder / open_external_folder / show_config
# ===========================================================================

@pytest.fixture
def fake_open(monkeypatch):
    """Record (and neutralise) the file-manager call ``open_log_folder`` makes."""
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return Result()

    monkeypatch.setattr(lvt.subprocess, "run", runner)
    monkeypatch.setattr(lvt.platform, "system", lambda: "Darwin")
    return calls, Result


@pytest.mark.gui
def test_open_log_folder_warns_when_the_folder_does_not_exist_yet(tab, dialogs,
                                                                  fake_open):
    """Nothing is launched before the first log file was ever written."""
    calls, _ = fake_open

    tab.open_log_folder()

    assert dialogs.kinds() == ["warning"]
    assert dialogs.titles() == ["Not Found"]
    assert calls == []


@pytest.mark.gui
def test_open_log_folder_launches_the_file_manager_with_a_timeout(tab, dialogs,
                                                                  fake_open,
                                                                  isolated_logging_config):
    """The folder is handed to the platform opener and the call is bounded."""
    calls, _ = fake_open
    log_dir = isolated_logging_config.get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    tab.open_log_folder()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ["open", str(log_dir)]
    assert kwargs["timeout"] == lvt.SUBPROCESS_TIMEOUT
    assert dialogs.calls == []


@pytest.mark.gui
def test_open_log_folder_reports_a_failing_file_manager(tab, dialogs, fake_open,
                                                        isolated_logging_config):
    """A non-zero exit code becomes a critical dialog carrying the stderr."""
    calls, result = fake_open
    result.returncode = 1
    result.stderr = "no such thing"
    isolated_logging_config.get_log_dir().mkdir(parents=True, exist_ok=True)

    tab.open_log_folder()

    assert dialogs.kinds() == ["critical"]
    assert "Failed to open folder" in dialogs.texts()[0]
    assert "no such thing" in dialogs.texts()[0]


@pytest.mark.gui
@pytest.mark.parametrize("error,kind,title", [
    (lvt.subprocess.TimeoutExpired("open", 5), "warning", "Timeout"),
    (FileNotFoundError("no opener"), "critical", "Error"),
    (RuntimeError("something else"), "critical", "Error"),
])
def test_open_log_folder_turns_every_launch_failure_into_a_dialog(tab, dialogs,
                                                                  monkeypatch,
                                                                  isolated_logging_config,
                                                                  error, kind, title):
    """No exception from the file manager may escape into the event loop."""
    isolated_logging_config.get_log_dir().mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lvt.platform, "system", lambda: "Linux")

    def exploding_run(*args, **kwargs):
        raise error

    monkeypatch.setattr(lvt.subprocess, "run", exploding_run)

    tab.open_log_folder()

    assert dialogs.kinds() == [kind]
    assert dialogs.titles() == [title]


@pytest.mark.gui
def test_open_external_folder_restores_the_configured_log_dir(tab, dialogs,
                                                              isolated_logging_config,
                                                              tmp_path, monkeypatch):
    """The external folder is only borrowed - the configuration is put back."""
    monkeypatch.setattr(tab, "isVisible", lambda: True)
    external = tmp_path / "elsewhere"
    external.mkdir()
    (external / isolated_logging_config.config["log_file"]).write_text("x\n")
    original = isolated_logging_config.config["log_dir"]
    dialogs.directory_answer = str(external)

    tab.open_external_folder()

    assert isolated_logging_config.config["log_dir"] == original
    assert tab.file_list.count() == 1
    assert dialogs.kinds() == ["directory", "information"]
    assert str(external) in dialogs.texts()[1]


@pytest.mark.gui
def test_open_external_folder_restores_the_log_dir_even_when_refreshing_fails(
        tab, dialogs, isolated_logging_config, tmp_path):
    """A failure half way through may not leave the configuration pointing away."""
    external = tmp_path / "elsewhere"
    external.mkdir()
    original = isolated_logging_config.config["log_dir"]
    dialogs.directory_answer = str(external)

    def boom():
        raise RuntimeError("refresh exploded")

    tab.refresh_log_files = boom

    with pytest.raises(RuntimeError):
        tab.open_external_folder()

    assert isolated_logging_config.config["log_dir"] == original


@pytest.mark.gui
def test_open_external_folder_does_nothing_when_the_dialog_is_cancelled(
        tab, dialogs, isolated_logging_config):
    """Cancelling the folder chooser leaves the configuration untouched."""
    original = isolated_logging_config.config["log_dir"]
    dialogs.directory_answer = ""
    calls = []
    tab.refresh_log_files = lambda: calls.append(True)

    tab.open_external_folder()

    assert isolated_logging_config.config["log_dir"] == original
    assert calls == []
    assert dialogs.kinds() == ["directory"]


@pytest.mark.gui
def test_show_config_points_at_the_settings_tab(tab, dialogs):
    """The old configuration dialog was replaced by a hint, not by a dialog."""
    tab.show_config()

    assert dialogs.kinds() == ["information"]
    assert "Settings" in dialogs.texts()[0]


# ===========================================================================
# closeEvent
# ===========================================================================

@pytest.mark.gui
def test_close_event_stops_every_timer_and_accepts(tab):
    """All three timers are stopped and the close is not vetoed."""
    tab.rt_search_debounce_timer.start(300)
    tab.hist_search_debounce_timer.start(300)
    assert tab.refresh_timer.isActive() is True
    event = QCloseEvent()
    event.ignore()

    tab.closeEvent(event)

    assert tab.refresh_timer.isActive() is False
    assert tab.rt_search_debounce_timer.isActive() is False
    assert tab.hist_search_debounce_timer.isActive() is False
    assert event.isAccepted() is True


@pytest.mark.gui
def test_close_event_removes_the_handler_from_the_root_logger(tab, quiet_root_logger):
    """The viewer must not keep receiving records once it is closed."""
    handler = tab.real_time_handler
    assert handler in quiet_root_logger.handlers

    tab.closeEvent(QCloseEvent())

    assert handler not in quiet_root_logger.handlers


@pytest.mark.gui
def test_close_event_can_be_called_twice(tab, quiet_root_logger):
    """A second close finds nothing to remove and still accepts."""
    tab.closeEvent(QCloseEvent())
    event = QCloseEvent()

    tab.closeEvent(event)

    assert event.isAccepted() is True
    assert tab.real_time_handler not in quiet_root_logger.handlers


@pytest.mark.gui
def test_close_event_interrupts_and_waits_for_the_worker_threads(tab):
    """Running filters are asked to stop and given a bounded grace period."""
    tab.rt_filter_thread = FakeThread()
    tab.hist_filter_thread = FakeThread()
    tab.loader_thread = FakeThread()

    tab.closeEvent(QCloseEvent())

    assert tab.rt_filter_thread.interruptions == 1
    assert tab.rt_filter_thread.waits == [1000]
    assert tab.hist_filter_thread.interruptions == 1
    assert tab.hist_filter_thread.waits == [1000]
    assert tab.loader_thread.waits == [1000]

    tab.rt_filter_thread = None
    tab.hist_filter_thread = None
    tab.loader_thread = None


@pytest.mark.gui
def test_close_event_ignores_threads_that_already_stopped(tab):
    """A finished thread is left alone."""
    tab.rt_filter_thread = FakeThread(running=False)
    tab.loader_thread = FakeThread(running=False)

    tab.closeEvent(QCloseEvent())

    assert tab.rt_filter_thread.interruptions == 0
    assert tab.rt_filter_thread.waits == []
    assert tab.loader_thread.waits == []

    tab.rt_filter_thread = None
    tab.loader_thread = None


@pytest.mark.gui
def test_close_event_survives_a_broken_timer(tab, quiet_root_logger):
    """One failing clean-up step does not stop the remaining ones."""
    class ExplodingTimer:
        def stop(self):
            raise RuntimeError("timer is gone")

    tab.refresh_timer = ExplodingTimer()
    event = QCloseEvent()
    event.ignore()

    tab.closeEvent(event)

    assert event.isAccepted() is True
    assert tab.real_time_handler not in quiet_root_logger.handlers
