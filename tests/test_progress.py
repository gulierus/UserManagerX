"""
Unit tests for :mod:`utils.progress_tasks` and :mod:`utils.progress_dialog`.

The task base class is a ``QThread``.  Most tests therefore call ``run()``
*directly* on the main thread: the signals then use direct connections, every
emission is delivered synchronously and the assertions stay deterministic.
Only the handful of tests marked ``integration``/``slow`` start a real worker
thread, and those are bounded by the ``threaded`` fixture, which resumes,
cancels and - as a last resort - terminates every task it started, so a
misbehaving task can never hang the suite.

``ProgressDialog`` is a ``QDialog``; the ``accept_dialogs`` fixture neutralises
``QDialog.exec`` (and with it every ``QMessageBox``) so no test can ever block
on a modal window.

Tests marked ``bug`` + ``xfail`` document defects found in the production code;
they assert the *correct* behaviour and are expected to fail until the defect
is fixed.
"""

import logging
import time

import pytest
from PyQt6.QtCore import QDateTime

from utils.progress_dialog import ProgressDialog
from utils.progress_tasks import (
    AbstractProgressTask,
    LogLevel,
    TaskCancelledException,
)

TASKS_LOGGER = "utils.progress_tasks"
DIALOG_LOGGER = "utils.progress_dialog"


# ---------------------------------------------------------------------------
# Concrete tasks used by the tests
# ---------------------------------------------------------------------------

class SucceedingTask(AbstractProgressTask):
    """Reports a few progress steps and returns a result message."""

    def __init__(self, name="Import žáků", result="42 záznamů",
                 is_deterministic=True, can_pause=False, steps=(0, 50, 100)):
        super().__init__(name, is_deterministic, can_pause)
        self.result = result
        self.steps = steps
        self.cleanup_calls = 0
        self.executed = 0

    def execute(self):
        self.executed += 1
        for step in self.steps:
            self.emit_progress(step, f"Krok {step}")
        return self.result

    def cleanup(self):
        self.cleanup_calls += 1


class FailingTask(AbstractProgressTask):
    """Raises the exception it was built with, half way through."""

    def __init__(self, error=None, name="Rozbitý úkol", **kwargs):
        super().__init__(name, kwargs.pop("is_deterministic", True),
                         kwargs.pop("can_pause", False))
        self.error = error or RuntimeError("boom")
        self.cleanup_calls = 0

    def execute(self):
        self.emit_progress(25, "Napůl hotovo")
        raise self.error

    def cleanup(self):
        self.cleanup_calls += 1


class CheckCancelTask(AbstractProgressTask):
    """Calls ``check_cancelled()`` before doing anything else."""

    def __init__(self, name="Zrušitelný", can_pause=False):
        super().__init__(name, True, can_pause)
        self.cleanup_calls = 0
        self.reached_end = False

    def execute(self):
        self.check_cancelled()
        self.reached_end = True
        return "hotovo"

    def cleanup(self):
        self.cleanup_calls += 1


class SelfCancellingTask(AbstractProgressTask):
    """Requests its own cancellation but returns from ``execute()`` normally."""

    def __init__(self, name="Sám sebe ruší"):
        super().__init__(name, True, False)
        self.cleanup_calls = 0

    def execute(self):
        self.request_cancel()
        return "tenhle výsledek nikdo neuvidí"

    def cleanup(self):
        self.cleanup_calls += 1


class CleanupFailingTask(AbstractProgressTask):
    """``cleanup()`` explodes; ``execute()`` optionally explodes first."""

    def __init__(self, cleanup_error=None, execute_error=None,
                 name="Úklid selže"):
        super().__init__(name, True, False)
        self.cleanup_error = cleanup_error or OSError("soubor je zamčený")
        self.execute_error = execute_error
        self.cleanup_calls = 0

    def execute(self):
        if self.execute_error is not None:
            raise self.execute_error
        return "ok"

    def cleanup(self):
        self.cleanup_calls += 1
        raise self.cleanup_error


class NoneReturningTask(AbstractProgressTask):
    """A subclass that forgets to return a result message."""

    def execute(self):
        return None

    def cleanup(self):
        pass


class LoopTask(AbstractProgressTask):
    """Counts in a real worker thread until cancelled or the limit is hit."""

    def __init__(self, name="Smyčka", can_pause=False, limit=200, sleep_ms=5):
        super().__init__(name, True, can_pause)
        self.limit = limit
        self.sleep_ms = sleep_ms
        self.iterations = 0
        self.cleanup_calls = 0

    def execute(self):
        while self.iterations < self.limit:
            self.check_cancelled()
            self.wait_if_paused()
            self.iterations += 1
            self.emit_progress(min(self.iterations, 100), f"Krok {self.iterations}")
            self.msleep(self.sleep_ms)
        return f"{self.iterations} kroků"

    def cleanup(self):
        self.cleanup_calls += 1


class StubbornTask(SucceedingTask):
    """Never really runs, always claims to be running and ignores ``wait()``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.start_calls = 0
        self.pretend_running = False
        self.wait_calls = []
        self.terminate_calls = 0

    def start(self, *args, **kwargs):            # noqa: D102 - test double
        self.start_calls += 1

    def isRunning(self):                         # noqa: N802 - Qt spelling
        return self.pretend_running

    def wait(self, *args):                       # noqa: D102 - test double
        self.wait_calls.append(args[0] if args else None)
        return False

    def terminate(self):                         # noqa: D102 - test double
        self.terminate_calls += 1
        self.pretend_running = False


class NonStartingTask(SucceedingTask):
    """Records ``start()`` calls instead of really launching a thread."""

    def __init__(self, pretend_running=False, **kwargs):
        super().__init__(**kwargs)
        self.start_calls = 0
        self.pretend_running = pretend_running

    def start(self, *args, **kwargs):          # noqa: D102 - test double
        self.start_calls += 1

    def isRunning(self):                        # noqa: N802 - Qt spelling
        return self.pretend_running


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class SignalLog:
    """Records every signal a task emits, in emission order."""

    def __init__(self, task):
        self.events = []
        task.task_started.connect(lambda dt: self.events.append(("started", dt)))
        task.progress_changed.connect(
            lambda value, msg: self.events.append(("progress", value, msg)))
        task.log_message.connect(
            lambda msg, level, ts: self.events.append(("log", msg, level, ts)))
        task.task_finished.connect(
            lambda ok, dt, msg: self.events.append(("finished", ok, msg, dt)))
        task.task_cancelled.connect(lambda: self.events.append(("cancelled",)))
        task.task_paused.connect(lambda: self.events.append(("paused",)))
        task.task_resumed.connect(lambda: self.events.append(("resumed",)))
        task.task_error.connect(lambda msg: self.events.append(("error", msg)))

    def kinds(self):
        """Every signal name, in order, ``log``/``progress`` included."""
        return [event[0] for event in self.events]

    def flow(self):
        """Signal names without the noisy ``log`` and ``progress`` entries."""
        return [k for k in self.kinds() if k not in ("log", "progress")]

    def of(self, kind):
        """All recorded events of one kind."""
        return [event for event in self.events if event[0] == kind]

    def logs(self):
        """``(message, level)`` pairs of every log signal."""
        return [(event[1], event[2]) for event in self.events if event[0] == "log"]

    def log_texts(self):
        """Only the log message texts."""
        return [event[1] for event in self.events if event[0] == "log"]


def spin(qapp, predicate, timeout_ms=2000):
    """Pump the Qt event loop until *predicate* holds or the timeout expires."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    qapp.processEvents()
    return bool(predicate())


def drain(qapp, rounds=5):
    """Deliver the signals a finished worker thread left queued."""
    for _ in range(rounds):
        qapp.processEvents()


@pytest.fixture
def threaded(qapp):
    """Start tasks in real threads and guarantee every one of them is stopped."""
    started = []

    def _start(task, start=True):
        """Register *task* for the guaranteed shutdown, optionally starting it."""
        started.append(task)
        if start:
            task.start()
        return task

    yield _start

    for task in started:
        task.request_resume()      # unblock a paused worker
        task.request_cancel()      # ask it to stop
        if not task.wait(2000):
            task.terminate()
            task.wait(500)
    qapp.processEvents()


@pytest.fixture
def make_dialog(qapp, accept_dialogs):
    """Build ProgressDialogs and tear them down without ever blocking."""
    created = []

    def _make(task, **kwargs):
        dialog = ProgressDialog(task, **kwargs)
        created.append(dialog)
        return dialog

    yield _make

    for dialog in created:
        task = dialog.task
        if task.isRunning():
            task.request_resume()
            task.request_cancel()
            if not task.wait(2000):
                task.terminate()
                task.wait(500)
        dialog.close()
    qapp.processEvents()


# ===========================================================================
# AbstractProgressTask - construction and properties
# ===========================================================================

def test_abstract_task_cannot_be_instantiated_without_execute_and_cleanup(qapp):
    """The ABC contract survives the QThread/ABC metaclass combination."""
    with pytest.raises(TypeError) as excinfo:
        AbstractProgressTask("nikdy")

    message = str(excinfo.value)
    assert "execute" in message and "cleanup" in message


@pytest.mark.parametrize("is_deterministic, can_pause", [
    (True, False), (True, True), (False, False), (False, True),
])
def test_task_properties_mirror_the_constructor_arguments(qapp, is_deterministic,
                                                          can_pause):
    """name/is_deterministic/can_pause are exposed read-only exactly as given."""
    task = SucceedingTask(name="Přenos dat", is_deterministic=is_deterministic,
                          can_pause=can_pause)

    assert task.task_name == "Přenos dat"
    assert task.is_deterministic is is_deterministic
    assert task.can_pause is can_pause


def test_a_fresh_task_has_no_timestamps_and_is_neither_cancelled_nor_paused(qapp):
    """Nothing is set before the task has run."""
    task = SucceedingTask()

    assert task.start_time is None
    assert task.end_time is None
    assert task.is_cancelled() is False
    assert task.is_paused() is False


# ===========================================================================
# AbstractProgressTask - the happy path
# ===========================================================================

def test_successful_run_emits_started_then_finished_true(qapp):
    """The happy path is exactly task_started -> ... -> task_finished(True)."""
    task = SucceedingTask(result="42 záznamů")
    log = SignalLog(task)

    task.run()

    assert log.flow() == ["started", "finished"]
    kind, success, message, end_time = log.of("finished")[0]
    assert success is True
    assert message == "42 záznamů"
    assert end_time == task.end_time


def test_successful_run_records_start_and_end_time_in_order(qapp):
    """Both timestamps are filled in and the end is not before the start."""
    task = SucceedingTask()

    task.run()

    assert task.start_time is not None and task.start_time.isValid()
    assert task.end_time is not None and task.end_time.isValid()
    assert task.start_time.msecsTo(task.end_time) >= 0


def test_successful_run_calls_cleanup_exactly_once(qapp):
    """cleanup() runs once, in the finally block, after a successful execute()."""
    task = SucceedingTask()

    task.run()

    assert task.executed == 1
    assert task.cleanup_calls == 1


def test_successful_run_logs_the_start_and_the_success_message(qapp):
    """The first log is INFO 'started', the last is SUCCESS with the result."""
    task = SucceedingTask(name="Export", result="3 soubory")
    log = SignalLog(task)

    task.run()

    logs = log.logs()
    assert logs[0] == ("Task 'Export' started", LogLevel.INFO)
    assert logs[-1] == ("Task completed successfully: 3 soubory", LogLevel.SUCCESS)


def test_progress_updates_are_forwarded_in_order_with_their_messages(qapp):
    """emit_progress() delivers (value, message) verbatim and in order."""
    task = SucceedingTask(steps=(0, 33, 100))
    log = SignalLog(task)

    task.run()

    assert [(e[1], e[2]) for e in log.of("progress")] == [
        (0, "Krok 0"), (33, "Krok 33"), (100, "Krok 100"),
    ]


def test_task_finished_is_emitted_before_cleanup_runs(qapp):
    """Documented order: listeners hear 'finished' first, cleanup happens after."""
    task = CleanupFailingTask(cleanup_error=RuntimeError("pozdě"))
    log = SignalLog(task)

    task.run()

    kinds = log.kinds()
    assert kinds.index("finished") < len(kinds) - 1
    assert log.log_texts()[-1] == "Cleanup failed: pozdě"


def test_execute_returning_none_is_still_reported_as_success(qapp):
    """A subclass that forgets the result message succeeds with an empty one."""
    task = NoneReturningTask("Bez výsledku")
    log = SignalLog(task)

    task.run()

    assert log.flow() == ["started", "finished"]
    assert log.of("finished")[0][1] is True
    assert log.of("finished")[0][2] == ""      # PyQt maps None onto an empty str


# ===========================================================================
# AbstractProgressTask - failures
# ===========================================================================

def test_failing_task_emits_error_then_finished_false(qapp):
    """A raising execute() produces task_error followed by task_finished(False)."""
    task = FailingTask(RuntimeError("disk je plný"))
    log = SignalLog(task)

    task.run()

    assert log.flow() == ["started", "error", "finished"]
    assert log.of("error")[0][1] == "disk je plný"
    kind, success, message, _end = log.of("finished")[0]
    assert success is False
    assert message == "Error: disk je plný"


def test_failing_task_still_runs_cleanup(qapp):
    """The finally block releases resources even when execute() blew up."""
    task = FailingTask(ValueError("špatný formát"))

    task.run()

    assert task.cleanup_calls == 1


@pytest.mark.parametrize("error", [
    ValueError("neplatná hodnota"),
    KeyError("chybí klíč"),
    ZeroDivisionError("division by zero"),
    OSError("[Errno 2] soubor nenalezen"),
    RuntimeError(""),
])
def test_every_exception_type_is_reported_with_its_str_value(qapp, error):
    """The payload is str(exception) - the type is not part of the message."""
    task = FailingTask(error)
    log = SignalLog(task)

    task.run()

    assert log.of("error")[0][1] == str(error)
    assert log.of("finished")[0][2] == f"Error: {error}"


def test_failing_task_logs_the_error_at_error_level_before_the_signal(qapp):
    """The ERROR log message precedes task_error so the panel shows it first."""
    task = FailingTask(RuntimeError("selhalo"))
    log = SignalLog(task)

    task.run()

    assert ("Task failed with error: selhalo", LogLevel.ERROR) in log.logs()
    assert log.kinds().index("log") < log.kinds().index("error")


def test_failure_sets_the_end_time(qapp):
    """A failed run is timed just like a successful one."""
    task = FailingTask(RuntimeError("x"))

    task.run()

    assert task.end_time is not None and task.end_time.isValid()


def test_progress_reported_before_the_exception_is_not_rolled_back(qapp):
    """Everything emitted before the failure still reached the listeners."""
    task = FailingTask(RuntimeError("x"))
    log = SignalLog(task)

    task.run()

    assert [(e[1], e[2]) for e in log.of("progress")] == [(25, "Napůl hotovo")]


# ===========================================================================
# AbstractProgressTask - cancellation
# ===========================================================================

def test_check_cancelled_does_nothing_until_cancellation_is_requested(qapp):
    """check_cancelled() is a no-op on a task nobody cancelled."""
    task = CheckCancelTask()

    assert task.check_cancelled() is None


def test_check_cancelled_raises_task_cancelled_exception(qapp):
    """After request_cancel() the convenience check raises, with a reason."""
    task = CheckCancelTask()
    task.request_cancel()

    with pytest.raises(TaskCancelledException, match="cancelled by user"):
        task.check_cancelled()


def test_cancelled_run_emits_cancelled_then_finished_false(qapp):
    """TaskCancelledException produces task_cancelled + task_finished(False)."""
    task = CheckCancelTask()
    log = SignalLog(task)
    task.request_cancel()

    task.run()

    assert log.flow() == ["started", "cancelled", "finished"]
    kind, success, message, _end = log.of("finished")[0]
    assert success is False
    assert message == "Cancelled by user"
    assert task.reached_end is False


def test_cancellation_is_not_reported_as_an_error(qapp):
    """A user cancellation must never reach the task_error channel."""
    task = CheckCancelTask()
    log = SignalLog(task)
    task.request_cancel()

    task.run()

    assert log.of("error") == []


def test_cancelled_run_still_calls_cleanup(qapp):
    """Resources are released on the cancellation path too."""
    task = CheckCancelTask()
    task.request_cancel()

    task.run()

    assert task.cleanup_calls == 1


def test_cancellation_noticed_after_execute_returned_wins_over_the_result(qapp):
    """is_cancelled() is re-checked after execute(): the result is discarded."""
    task = SelfCancellingTask()
    log = SignalLog(task)

    task.run()

    assert log.flow() == ["started", "cancelled", "finished"]
    assert log.of("finished")[0][2] == "Cancelled by user"
    assert task.cleanup_calls == 1


def test_cancelled_run_logs_a_warning_and_sets_the_end_time(qapp):
    """The cancellation path logs at WARNING level and stamps the end time."""
    task = CheckCancelTask()
    log = SignalLog(task)
    task.request_cancel()

    task.run()

    assert ("Task cancelled by user", LogLevel.WARNING) in log.logs()
    assert task.end_time is not None and task.end_time.isValid()


def test_request_cancel_flips_is_cancelled_and_logs_a_warning(qapp):
    """The flag is thread-safe state; the request itself is announced."""
    task = SucceedingTask()
    log = SignalLog(task)

    assert task.is_cancelled() is False
    task.request_cancel()

    assert task.is_cancelled() is True
    assert ("Cancellation requested", LogLevel.WARNING) in log.logs()


def test_request_cancel_is_idempotent(qapp):
    """Cancelling twice leaves the task cancelled - it never toggles back."""
    task = SucceedingTask()

    task.request_cancel()
    task.request_cancel()

    assert task.is_cancelled() is True


def test_cancellation_survives_a_completed_run(qapp):
    """The flag is not reset by run(); tasks are single-use by design."""
    task = SelfCancellingTask()

    task.run()

    assert task.is_cancelled() is True


# ===========================================================================
# AbstractProgressTask - pause / resume
# ===========================================================================

def test_request_pause_on_a_pausable_task_emits_task_paused(qapp):
    """A pausable task records the request and announces it."""
    task = SucceedingTask(can_pause=True)
    log = SignalLog(task)

    task.request_pause()

    assert task.is_paused() is True
    assert log.flow() == ["paused"]
    assert ("Pause requested", LogLevel.INFO) in log.logs()


def test_request_pause_on_a_non_pausable_task_is_ignored(qapp, caplog):
    """No state change, no signal - only a warning in the log."""
    caplog.set_level(logging.DEBUG, logger=TASKS_LOGGER)
    task = SucceedingTask(name="Nelze pozastavit", can_pause=False)
    log = SignalLog(task)

    task.request_pause()

    assert task.is_paused() is False
    assert log.events == []
    assert "does not support pausing" in caplog.text


def test_request_resume_clears_the_pause_and_emits_task_resumed(qapp):
    """Resume is the exact counterpart of pause."""
    task = SucceedingTask(can_pause=True)
    log = SignalLog(task)
    task.request_pause()

    task.request_resume()

    assert task.is_paused() is False
    assert log.flow() == ["paused", "resumed"]
    assert ("Resuming task", LogLevel.INFO) in log.logs()


def test_request_resume_on_a_non_pausable_task_is_ignored(qapp):
    """A task that cannot pause cannot resume either - silently."""
    task = SucceedingTask(can_pause=False)
    log = SignalLog(task)

    task.request_resume()

    assert log.events == []


def test_pause_resume_pause_is_repeatable(qapp):
    """The pause flag follows every request; both signals fire each time."""
    task = SucceedingTask(can_pause=True)
    log = SignalLog(task)

    task.request_pause()
    task.request_resume()
    task.request_pause()

    assert task.is_paused() is True
    assert log.flow() == ["paused", "resumed", "paused"]


def test_request_cancel_clears_a_pending_pause(qapp):
    """Cancelling a paused task must not leave it paused, or it would deadlock."""
    task = SucceedingTask(can_pause=True)
    task.request_pause()

    task.request_cancel()

    assert task.is_paused() is False
    assert task.is_cancelled() is True


def test_wait_if_paused_returns_immediately_when_nothing_is_paused(qapp):
    """The common case must not touch the wait condition at all."""
    task = SucceedingTask(can_pause=True)

    started = time.monotonic()
    task.wait_if_paused()

    assert time.monotonic() - started < 0.5


def test_wait_if_paused_ignores_the_flag_on_a_non_pausable_task(qapp):
    """The can_pause guard runs first, so a stray flag can never block."""
    task = SucceedingTask(can_pause=False)
    task._pause_requested = True

    started = time.monotonic()
    task.wait_if_paused()

    assert time.monotonic() - started < 0.5


def test_wait_if_paused_does_not_block_a_cancelled_task(qapp):
    """cancel beats pause: a cancelled worker walks straight through."""
    task = SucceedingTask(can_pause=True)
    task.request_pause()
    task._pause_requested = True          # cancel would normally clear it
    task._cancel_requested = True

    started = time.monotonic()
    task.wait_if_paused()

    assert time.monotonic() - started < 0.5


# ===========================================================================
# AbstractProgressTask - emit_progress / emit_log
# ===========================================================================

@pytest.mark.parametrize("value, message", [
    (0, ""),
    (100, "Hotovo"),
    (-1, "Neurčitý průběh"),
    (50, "Načítám třídu 6.A - žáci s diakritikou: Řehoř"),
    (12345, "mimo rozsah"),
])
def test_emit_progress_forwards_its_arguments_unchanged(qapp, value, message):
    """The task does not validate or clamp - the UI decides what to do."""
    task = SucceedingTask()
    log = SignalLog(task)

    task.emit_progress(value, message)

    assert log.of("progress") == [("progress", value, message)]


def test_emit_progress_defaults_to_an_empty_message(qapp):
    """The message argument is optional."""
    task = SucceedingTask()
    log = SignalLog(task)

    task.emit_progress(7)

    assert log.of("progress") == [("progress", 7, "")]


def test_emit_log_defaults_to_info_and_stamps_the_current_time(qapp):
    """The timestamp travels with the message so the UI need not guess."""
    task = SucceedingTask()
    log = SignalLog(task)
    before = QDateTime.currentDateTime()

    task.emit_log("Připojeno")

    kind, message, level, timestamp = log.of("log")[0]
    assert (message, level) == ("Připojeno", LogLevel.INFO)
    assert timestamp.isValid()
    assert abs(before.msecsTo(timestamp)) < 5000


@pytest.mark.parametrize("level, expected_levelno", [
    (LogLevel.DEBUG, logging.DEBUG),
    (LogLevel.INFO, logging.INFO),
    (LogLevel.WARNING, logging.WARNING),
    (LogLevel.ERROR, logging.ERROR),
    (LogLevel.SUCCESS, logging.INFO),      # the stdlib logger has no SUCCESS
])
def test_emit_log_maps_every_level_onto_the_python_logger(qapp, caplog, level,
                                                          expected_levelno):
    """SUCCESS degrades to INFO; every other level maps one to one."""
    caplog.set_level(logging.DEBUG, logger=TASKS_LOGGER)
    task = SucceedingTask(name="Úloha")

    task.emit_log("zpráva", level)

    records = [r for r in caplog.records if r.name == TASKS_LOGGER
               and r.message == "[Úloha] zpráva"]
    assert len(records) == 1
    assert records[0].levelno == expected_levelno


def test_emit_log_prefixes_the_python_log_with_the_task_name(qapp, caplog):
    """Several tasks logging at once stay distinguishable, diacritics included."""
    caplog.set_level(logging.DEBUG, logger=TASKS_LOGGER)
    task = SucceedingTask(name="Přenos dat 6.A")

    task.emit_log("start", LogLevel.WARNING)

    assert "[Přenos dat 6.A] start" in caplog.text


def test_emit_log_rejects_a_level_that_is_not_a_LogLevel(qapp):
    """The signal is typed: passing the raw string value is a TypeError."""
    task = SucceedingTask()

    with pytest.raises(TypeError):
        task.emit_log("zpráva", "error")


def test_log_level_values_are_the_documented_lowercase_names(qapp):
    """The enum values are part of the public contract."""
    assert [level.value for level in LogLevel] == [
        "debug", "info", "warning", "error", "success",
    ]


# ===========================================================================
# AbstractProgressTask - cleanup that itself fails
# ===========================================================================

def test_cleanup_failure_is_swallowed_after_a_successful_run(qapp):
    """A broken cleanup() must not turn a successful task into a failure."""
    task = CleanupFailingTask(cleanup_error=OSError("soubor je zamčený"))
    log = SignalLog(task)

    task.run()

    assert log.flow() == ["started", "finished"]
    assert log.of("finished")[0][1] is True
    assert task.cleanup_calls == 1


def test_cleanup_failure_is_reported_on_the_log_channel(qapp):
    """The user still gets to see that the cleanup went wrong."""
    task = CleanupFailingTask(cleanup_error=OSError("soubor je zamčený"))
    log = SignalLog(task)

    task.run()

    assert ("Cleanup failed: soubor je zamčený", LogLevel.ERROR) in log.logs()


def test_cleanup_failure_is_logged_with_a_traceback(qapp, caplog):
    """logger.exception keeps the stack trace for the bug report."""
    caplog.set_level(logging.DEBUG, logger=TASKS_LOGGER)
    task = CleanupFailingTask(cleanup_error=OSError("soubor je zamčený"),
                              name="Úklid")

    task.run()

    failures = [r for r in caplog.records
                if r.levelno == logging.ERROR and "cleanup of task" in r.message]
    assert len(failures) == 1
    assert failures[0].exc_info is not None


def test_cleanup_failure_does_not_hide_the_original_error(qapp):
    """The error the task really failed with still reaches the listeners."""
    task = CleanupFailingTask(cleanup_error=OSError("zamčeno"),
                              execute_error=ValueError("neplatný soubor"))
    log = SignalLog(task)

    task.run()

    assert log.of("error") == [("error", "neplatný soubor")]
    assert log.of("finished")[0][2] == "Error: neplatný soubor"
    assert ("Cleanup failed: zamčeno", LogLevel.ERROR) in log.logs()


# ===========================================================================
# ProgressDialog - construction
# ===========================================================================

@pytest.mark.gui
def test_dialog_starts_in_the_ready_state(make_dialog):
    """Nothing runs before start_task(): the dialog only announces itself."""
    dialog = make_dialog(SucceedingTask(name="Import žáků"))

    assert dialog.windowTitle() == "Progress: Import žáků"
    assert dialog.status_label.text() == "Ready"
    assert dialog.operation_label.text() == "Waiting to start..."
    assert dialog.cancel_button.isEnabled() is True
    assert dialog.close_button.isEnabled() is False
    assert dialog.stats_panel.isHidden() is True
    assert dialog.log_text.toPlainText() == ""


@pytest.mark.gui
@pytest.mark.parametrize("can_pause", [True, False])
def test_pause_button_exists_only_for_a_pausable_task(make_dialog, can_pause):
    """A task that cannot pause must not offer a pause button at all."""
    dialog = make_dialog(SucceedingTask(can_pause=can_pause))

    assert (dialog.pause_resume_button is not None) is can_pause


@pytest.mark.gui
def test_deterministic_task_gets_a_percentage_progress_bar(make_dialog):
    """0-100 with the Qt default percentage format."""
    dialog = make_dialog(SucceedingTask(is_deterministic=True))

    assert (dialog.progress_bar.minimum(), dialog.progress_bar.maximum()) == (0, 100)
    assert dialog.progress_bar.value() == 0


@pytest.mark.gui
def test_indeterminate_task_gets_a_busy_progress_bar(make_dialog):
    """min == max == 0 is Qt's busy indicator; the text says 'Processing...'."""
    dialog = make_dialog(SucceedingTask(is_deterministic=False))

    assert (dialog.progress_bar.minimum(), dialog.progress_bar.maximum()) == (0, 0)
    assert dialog.progress_bar.format() == "Processing..."


# ===========================================================================
# ProgressDialog - start_task
# ===========================================================================

@pytest.mark.gui
def test_start_task_starts_the_thread_and_switches_to_running(make_dialog):
    """The single legitimate start hands control to the task."""
    task = NonStartingTask()
    dialog = make_dialog(task)

    dialog.start_task()

    assert task.start_calls == 1
    assert dialog.status_label.text() == "Running"
    assert "#4CAF50" in dialog.status_label.styleSheet()


@pytest.mark.gui
def test_start_task_refuses_a_second_start(make_dialog, caplog):
    """Double-clicking the caller's button must not start the task twice."""
    caplog.set_level(logging.DEBUG, logger=DIALOG_LOGGER)
    task = NonStartingTask()
    dialog = make_dialog(task)

    dialog.start_task()
    dialog.start_task()

    assert task.start_calls == 1
    assert "Task is already running" in caplog.text


@pytest.mark.gui
def test_start_task_refuses_a_task_whose_thread_already_runs(make_dialog, caplog):
    """A task started elsewhere is detected through QThread.isRunning()."""
    caplog.set_level(logging.DEBUG, logger=DIALOG_LOGGER)
    task = NonStartingTask(pretend_running=True)
    dialog = make_dialog(task)

    dialog.start_task()

    assert task.start_calls == 0
    assert dialog.status_label.text() == "Ready"
    assert "Task thread is already running" in caplog.text


@pytest.mark.gui
def test_on_task_started_signal_switches_the_status_to_running(make_dialog):
    """The signal alone is enough - the dialog does not need start_task()."""
    dialog = make_dialog(SucceedingTask())

    dialog.on_task_started(QDateTime.currentDateTime())

    assert dialog.status_label.text() == "Running"


# ===========================================================================
# ProgressDialog - on_progress_changed
# ===========================================================================

@pytest.mark.gui
@pytest.mark.parametrize("value, message, expected_format", [
    (0, "Začínám", "0% - Začínám"),
    (42, "Načítám třídu 6.A", "42% - Načítám třídu 6.A"),
    (100, "Hotovo", "100% - Hotovo"),
    (7, "", "7%"),
])
def test_deterministic_progress_is_rendered_as_percent_and_message(
        make_dialog, value, message, expected_format):
    """Value drives the bar, the message is appended to the bar text."""
    dialog = make_dialog(SucceedingTask(is_deterministic=True))

    dialog.on_progress_changed(value, message)

    assert dialog.progress_bar.value() == value
    assert dialog.progress_bar.format() == expected_format


@pytest.mark.gui
def test_deterministic_progress_mirrors_the_message_in_the_operation_label(make_dialog):
    """The operation label repeats the last non-empty message."""
    dialog = make_dialog(SucceedingTask(is_deterministic=True))

    dialog.on_progress_changed(10, "Připojuji se k EduPage")

    assert dialog.operation_label.text() == "Připojuji se k EduPage"


@pytest.mark.gui
def test_an_empty_message_does_not_wipe_the_operation_label(make_dialog):
    """A value-only update keeps the last thing the user was told."""
    dialog = make_dialog(SucceedingTask(is_deterministic=True))
    dialog.on_progress_changed(10, "Načítám třídy")

    dialog.on_progress_changed(20, "")

    assert dialog.operation_label.text() == "Načítám třídy"
    assert dialog.progress_bar.format() == "20%"


@pytest.mark.gui
def test_a_none_message_is_treated_like_an_empty_one(make_dialog):
    """A slot called with None must not render the string 'None'."""
    dialog = make_dialog(SucceedingTask(is_deterministic=True))

    dialog.on_progress_changed(55, None)

    assert dialog.progress_bar.value() == 55
    assert dialog.progress_bar.format() == "55%"
    assert dialog.operation_label.text() == "Waiting to start..."


@pytest.mark.gui
def test_indeterminate_progress_shows_only_the_message(make_dialog):
    """A busy bar has no meaningful value, so only the text is updated."""
    dialog = make_dialog(SucceedingTask(is_deterministic=False))

    dialog.on_progress_changed(60, "Stahuji data")

    assert dialog.progress_bar.format() == "Stahuji data"
    assert dialog.progress_bar.maximum() == 0
    assert dialog.operation_label.text() == "Stahuji data"


@pytest.mark.gui
def test_indeterminate_progress_without_a_message_keeps_the_default_text(make_dialog):
    """An empty message must not blank out the busy indicator's caption."""
    dialog = make_dialog(SucceedingTask(is_deterministic=False))

    dialog.on_progress_changed(60, "")

    assert dialog.progress_bar.format() == "Processing..."


@pytest.mark.gui
def test_a_negative_value_on_a_deterministic_task_shows_only_the_message(make_dialog):
    """-1 means 'no measurable progress' even for a deterministic bar."""
    dialog = make_dialog(SucceedingTask(is_deterministic=True))
    dialog.on_progress_changed(30, "Načítám")

    dialog.on_progress_changed(-1, "Nevím, jak dlouho to potrvá")

    assert dialog.progress_bar.value() == 30
    assert dialog.progress_bar.format() == "Nevím, jak dlouho to potrvá"


@pytest.mark.gui
def test_progress_updates_after_the_cancelled_signal_are_ignored(make_dialog):
    """A worker that reports once more must not undo the cancelled state."""
    dialog = make_dialog(SucceedingTask(is_deterministic=True))
    dialog.on_progress_changed(25, "Načítám třídy")
    dialog.on_task_cancelled()

    dialog.on_progress_changed(80, "Načítám žáky")

    assert dialog.progress_bar.value() == 25
    assert dialog.progress_bar.format() == "25% - Cancelled"
    assert dialog.operation_label.text() == "Task cancelled by user"


# ===========================================================================
# ProgressDialog - the cancelled state
# ===========================================================================

@pytest.mark.gui
def test_on_task_cancelled_puts_every_widget_into_the_cancelled_state(make_dialog):
    """Status, operation label and bar text all say 'cancelled'."""
    dialog = make_dialog(SucceedingTask(is_deterministic=True))
    dialog.on_progress_changed(25, "Načítám třídy")

    dialog.on_task_cancelled()

    assert dialog.status_label.text() == "Cancelled"
    assert "#FF9800" in dialog.status_label.styleSheet()
    assert dialog.operation_label.text() == "Task cancelled by user"
    assert dialog.progress_bar.format() == "25% - Cancelled"
    assert dialog.was_cancelled() is True


@pytest.mark.gui
def test_the_cancelled_state_survives_the_task_finished_false_that_follows(make_dialog):
    """A cancelled run is never re-labelled 'Failed' by the finish handler."""
    task = SucceedingTask(is_deterministic=True)
    task._start_time = QDateTime.currentDateTime()
    task._end_time = task._start_time.addMSecs(1500)
    dialog = make_dialog(task)
    dialog.on_progress_changed(25, "Načítám třídy")

    dialog.on_task_cancelled()
    dialog.on_task_finished(False, task.end_time, "Cancelled by user")

    assert dialog.status_label.text() == "Cancelled"
    assert dialog.progress_bar.format() == "25% - Cancelled"
    assert dialog.operation_label.text() == "Task cancelled by user"
    assert dialog.was_cancelled() is True
    assert dialog.stats_panel.isHidden() is False


@pytest.mark.gui
def test_cancelling_an_indeterminate_task_stops_the_busy_animation(make_dialog):
    """The bar leaves busy mode, otherwise it would keep sweeping forever."""
    dialog = make_dialog(SucceedingTask(is_deterministic=False))
    dialog.on_progress_changed(0, "Stahuji data")

    dialog.on_task_cancelled()

    assert dialog.progress_bar.maximum() == 100
    assert dialog.progress_bar.value() == 0
    assert dialog.progress_bar.format() == "Cancelled"


@pytest.mark.gui
def test_was_cancelled_follows_the_task_flag_even_without_any_signal(make_dialog):
    """Callers may ask before the cancelled signal has been delivered."""
    task = SucceedingTask()
    dialog = make_dialog(task)
    assert dialog.was_cancelled() is False

    task.request_cancel()

    assert dialog.was_cancelled() is True


# ===========================================================================
# ProgressDialog - the failed and finished states
# ===========================================================================

@pytest.mark.gui
def test_a_failed_task_is_labelled_failed(make_dialog):
    """No cancellation anywhere means task_finished(False) is a failure."""
    task = SucceedingTask(is_deterministic=True)
    task._start_time = QDateTime.currentDateTime()
    task._end_time = task._start_time.addMSecs(2500)
    dialog = make_dialog(task)
    dialog.on_progress_changed(40, "Zapisuji")

    dialog.on_task_finished(False, task.end_time, "Error: disk je plný")

    assert dialog.status_label.text() == "Failed"
    assert "#EF5350" in dialog.status_label.styleSheet()
    assert dialog.progress_bar.format() == "40% - Failed"
    assert "disk je plný" in dialog.result_label.text()


@pytest.mark.gui
def test_a_failed_indeterminate_task_leaves_the_busy_mode(make_dialog):
    """The animation stops on failure too."""
    dialog = make_dialog(SucceedingTask(is_deterministic=False))

    dialog.on_task_finished(False, QDateTime.currentDateTime(), "Error: x")

    assert dialog.progress_bar.maximum() == 100
    assert dialog.progress_bar.format() == "Failed"


@pytest.mark.gui
def test_a_successful_deterministic_task_ends_at_one_hundred_percent(make_dialog):
    """Even a task that never reported 100% is completed when it succeeds."""
    dialog = make_dialog(SucceedingTask(is_deterministic=True))
    dialog.on_progress_changed(40, "Zapisuji")

    dialog.on_task_finished(True, QDateTime.currentDateTime(), "42 záznamů")

    assert dialog.status_label.text() == "Completed Successfully"
    assert dialog.progress_bar.value() == 100
    assert dialog.progress_bar.format() == "100% - Complete"


@pytest.mark.gui
@pytest.mark.parametrize("can_pause", [True, False])
def test_finishing_disables_the_controls_and_enables_close(make_dialog, can_pause):
    """After the task ends only the Close button stays usable."""
    dialog = make_dialog(SucceedingTask(can_pause=can_pause))

    dialog.on_task_finished(True, QDateTime.currentDateTime(), "hotovo")

    assert dialog.cancel_button.isEnabled() is False
    assert dialog.close_button.isEnabled() is True
    if can_pause:
        assert dialog.pause_resume_button.isEnabled() is False


@pytest.mark.gui
def test_on_task_error_only_logs_and_never_opens_a_dialog(make_dialog, dialogs, caplog):
    """Errors are reported through the log panel, not through a message box."""
    caplog.set_level(logging.DEBUG, logger=DIALOG_LOGGER)
    dialog = make_dialog(SucceedingTask())

    dialog.on_task_error("disk je plný")

    assert "disk je plný" in caplog.text
    assert dialogs.calls == []


# ===========================================================================
# ProgressDialog - statistics
# ===========================================================================

def _timed_dialog(make_dialog, duration_ms, is_deterministic=True):
    """Build a dialog whose task pretends to have run for *duration_ms*."""
    task = SucceedingTask(is_deterministic=is_deterministic)
    base = QDateTime.currentDateTime()
    task._start_time = base
    task._end_time = base.addMSecs(duration_ms)
    return make_dialog(task)


@pytest.mark.gui
@pytest.mark.parametrize("duration_ms, expected", [
    (0, "0.0 seconds"),
    (1500, "1.5 seconds"),
    (59000, "59.0 seconds"),
    (60000, "1 min 0 sec"),
    (90000, "1 min 30 sec"),
    (3599000, "59 min 59 sec"),
    (3600000, "1 hr 0 min"),
    (7530000, "2 hr 5 min"),
])
def test_duration_is_formatted_by_magnitude(make_dialog, duration_ms, expected):
    """Seconds below a minute, minutes below an hour, hours above."""
    dialog = _timed_dialog(make_dialog, duration_ms)

    dialog.on_task_finished(True, dialog.task.end_time, "hotovo")

    assert dialog.duration_label.text() == f"<b>Duration:</b> {expected}"


@pytest.mark.gui
def test_statistics_show_both_timestamps_and_the_result(make_dialog):
    """The panel becomes visible and repeats the result message."""
    dialog = _timed_dialog(make_dialog, 2000)
    start, end = dialog.task.start_time, dialog.task.end_time

    dialog.on_task_finished(True, end, "42 záznamů")

    assert dialog.stats_panel.isHidden() is False
    assert start.toString("yyyy-MM-dd hh:mm:ss") in dialog.start_time_label.text()
    assert end.toString("yyyy-MM-dd hh:mm:ss") in dialog.end_time_label.text()
    assert "42 záznamů" in dialog.result_label.text()


@pytest.mark.gui
@pytest.mark.parametrize("success, colour", [(True, "#4CAF50"), (False, "#EF5350")])
def test_the_result_line_is_coloured_by_the_outcome(make_dialog, success, colour):
    """Green for success, red for anything else."""
    dialog = _timed_dialog(make_dialog, 1000)

    dialog.on_task_finished(success, dialog.task.end_time, "výsledek")

    assert colour in dialog.result_label.text()


@pytest.mark.gui
def test_a_negative_duration_is_reported_instead_of_a_nonsense_number(make_dialog):
    """The system clock moved backwards - say so rather than print '-60.0 s'."""
    dialog = _timed_dialog(make_dialog, -60000)

    dialog.on_task_finished(True, dialog.task.end_time, "hotovo")

    assert dialog.duration_label.text() == (
        "<b>Duration:</b> Invalid duration (system time changed)")
    assert dialog.stats_panel.isHidden() is False


@pytest.mark.gui
@pytest.mark.parametrize("start_ms, end_ms", [
    (None, None),      # nothing ran at all
    (0, None),         # started, never finished
    (None, 0),         # finished without a start time
])
def test_statistics_stay_hidden_when_a_timestamp_is_missing(make_dialog, caplog,
                                                            start_ms, end_ms):
    """A half-timed task must not crash the finish handler."""
    caplog.set_level(logging.DEBUG, logger=DIALOG_LOGGER)
    task = SucceedingTask()
    base = QDateTime.currentDateTime()
    task._start_time = None if start_ms is None else base.addMSecs(start_ms)
    task._end_time = None if end_ms is None else base.addMSecs(end_ms)
    dialog = make_dialog(task)

    dialog.on_task_finished(True, base, "hotovo")

    assert dialog.stats_panel.isHidden() is True
    assert dialog.duration_label.text() == ""
    assert "Cannot show statistics" in caplog.text
    assert dialog.close_button.isEnabled() is True


@pytest.mark.gui
def test_statistics_stay_hidden_for_an_invalid_null_timestamp(make_dialog):
    """A default-constructed QDateTime counts as 'no time at all'."""
    task = SucceedingTask()
    task._start_time = QDateTime()
    task._end_time = QDateTime.currentDateTime()
    dialog = make_dialog(task)

    dialog.on_task_finished(True, task.end_time, "hotovo")

    assert dialog.stats_panel.isHidden() is True


# ===========================================================================
# ProgressDialog - log panel
# ===========================================================================

@pytest.mark.gui
@pytest.mark.parametrize("level, colour", [
    (LogLevel.DEBUG, "#888"),
    (LogLevel.INFO, "#e0e0e0"),
    (LogLevel.WARNING, "#ffa726"),
    (LogLevel.ERROR, "#ef5350"),
    (LogLevel.SUCCESS, "#4caf50"),
])
def test_each_log_level_gets_its_own_colour(make_dialog, level, colour):
    """The severity has to be recognisable at a glance."""
    dialog = make_dialog(SucceedingTask())

    dialog.on_log_message("zpráva", level, QDateTime.currentDateTime())

    assert colour in dialog.log_text.toHtml().lower()


@pytest.mark.gui
def test_a_log_line_carries_the_timestamp_and_the_text(make_dialog):
    """[hh:mm:ss] plus the message, diacritics intact."""
    stamp = QDateTime.currentDateTime()
    dialog = make_dialog(SucceedingTask())

    dialog.on_log_message("Načítám třídu 6.A", LogLevel.INFO, stamp)

    plain = dialog.log_text.toPlainText()
    assert plain == f"[{stamp.toString('hh:mm:ss')}] Načítám třídu 6.A"


@pytest.mark.gui
def test_log_lines_are_appended_in_arrival_order(make_dialog):
    """The panel is a transcript, so order matters."""
    stamp = QDateTime.currentDateTime()
    dialog = make_dialog(SucceedingTask())

    for text in ("první", "druhá", "třetí"):
        dialog.on_log_message(text, LogLevel.INFO, stamp)

    assert dialog.log_text.toPlainText().splitlines() == [
        f"[{stamp.toString('hh:mm:ss')}] {text}"
        for text in ("první", "druhá", "třetí")
    ]


@pytest.mark.gui
def test_an_unknown_log_level_falls_back_to_the_neutral_colour(make_dialog):
    """A level the colour table does not know must not raise."""
    dialog = make_dialog(SucceedingTask())

    dialog.on_log_message("zpráva", None, QDateTime.currentDateTime())

    assert "#e0e0e0" in dialog.log_text.toHtml().lower()


# ===========================================================================
# ProgressDialog - buttons
# ===========================================================================

@pytest.mark.gui
def test_the_cancel_button_asks_the_task_to_stop(make_dialog):
    """One click disables the button and requests cancellation exactly once."""
    task = NonStartingTask()
    dialog = make_dialog(task)
    dialog.start_task()

    dialog.cancel_button.click()

    assert task.is_cancelled() is True
    assert dialog.cancel_button.isEnabled() is False
    assert dialog.cancel_button.text() == "Cancelling..."
    assert dialog.operation_label.text() == "Cancelling task..."


@pytest.mark.gui
def test_cancelling_before_the_task_started_does_nothing(make_dialog, caplog):
    """The guard keeps a stray click from cancelling a task that never ran."""
    caplog.set_level(logging.DEBUG, logger=DIALOG_LOGGER)
    task = NonStartingTask()
    dialog = make_dialog(task)

    dialog.on_cancel_clicked()

    assert task.is_cancelled() is False
    assert dialog.cancel_button.isEnabled() is True
    assert "task is not running" in caplog.text


@pytest.mark.gui
def test_cancelling_after_the_task_finished_does_nothing(make_dialog):
    """task_finished resets the running flag, so late clicks are ignored."""
    task = NonStartingTask()
    dialog = make_dialog(task)
    dialog.start_task()
    dialog.on_task_finished(True, QDateTime.currentDateTime(), "hotovo")

    dialog.on_cancel_clicked()

    assert task.is_cancelled() is False


@pytest.mark.gui
def test_the_pause_button_toggles_between_pause_and_resume(make_dialog):
    """Click pauses, the next click resumes; the caption follows the state."""
    task = NonStartingTask(can_pause=True)
    dialog = make_dialog(task)
    dialog.start_task()

    dialog.pause_resume_button.click()

    assert task.is_paused() is True
    assert dialog.status_label.text() == "Paused"
    assert "Resume" in dialog.pause_resume_button.text()

    dialog.pause_resume_button.click()

    assert task.is_paused() is False
    assert dialog.status_label.text() == "Running"
    assert "Pause" in dialog.pause_resume_button.text()


@pytest.mark.gui
def test_pausing_before_the_task_started_does_nothing(make_dialog, caplog):
    """No task, nothing to pause - and the status must not lie."""
    caplog.set_level(logging.DEBUG, logger=DIALOG_LOGGER)
    task = NonStartingTask(can_pause=True)
    dialog = make_dialog(task)

    dialog.pause_resume_button.click()

    assert task.is_paused() is False
    assert dialog.status_label.text() == "Ready"
    assert "task is not running" in caplog.text


# ===========================================================================
# ProgressDialog - closeEvent
# ===========================================================================

@pytest.mark.gui
def test_closing_a_running_dialog_cancels_the_task(make_dialog):
    """Closing the window is an implicit cancel, not a silent abandon."""
    task = NonStartingTask()
    dialog = make_dialog(task)
    dialog.start_task()

    dialog.close()

    assert task.is_cancelled() is True


@pytest.mark.gui
def test_closing_a_finished_dialog_does_not_cancel_anything(make_dialog):
    """A completed task must not be marked as cancelled on the way out."""
    task = NonStartingTask()
    dialog = make_dialog(task)
    dialog.start_task()
    dialog.on_task_finished(True, QDateTime.currentDateTime(), "hotovo")

    dialog.close()

    assert task.is_cancelled() is False


@pytest.mark.gui
def test_closing_a_dialog_that_never_started_does_not_cancel_anything(make_dialog):
    """start_task() was never called, so there is nothing to stop."""
    task = NonStartingTask()
    dialog = make_dialog(task)

    dialog.close()

    assert task.is_cancelled() is False


@pytest.mark.gui
def test_closing_force_terminates_a_task_that_ignores_the_cancel_request(make_dialog):
    """After a bounded wait the dialog kills the thread instead of hanging."""
    task = StubbornTask()
    dialog = make_dialog(task)
    dialog.start_task()
    task.pretend_running = True

    dialog.close()

    assert task.is_cancelled() is True
    assert task.wait_calls[0] == 5000
    assert task.terminate_calls == 1


@pytest.mark.gui
def test_a_failure_after_a_direct_cancel_request_is_still_shown_as_cancelled(make_dialog):
    """was_cancelled() consults the task, so the cancelled signal is optional."""
    task = SucceedingTask(is_deterministic=True)
    task._start_time = QDateTime.currentDateTime()
    task._end_time = task._start_time.addMSecs(500)
    dialog = make_dialog(task)
    dialog.on_progress_changed(15, "Načítám")
    task.request_cancel()

    dialog.on_task_finished(False, task.end_time, "Cancelled by user")

    assert dialog.status_label.text() == "Cancelled"
    assert dialog.progress_bar.format() == "15% - Cancelled"


# ===========================================================================
# Real worker threads
# ===========================================================================

@pytest.mark.gui
@pytest.mark.slow
@pytest.mark.integration
def test_a_real_run_drives_the_dialog_to_the_completed_state(qapp, make_dialog,
                                                             threaded):
    """End to end: queued signals from the worker thread update every widget."""
    task = SucceedingTask(name="Vlákno", result="42 záznamů", steps=(10, 60, 100))
    dialog = make_dialog(task)

    threaded(task, start=False)

    dialog.start_task()
    assert spin(qapp, lambda: not task.isRunning(), 2000)
    drain(qapp)

    assert dialog.status_label.text() == "Completed Successfully"
    assert dialog.progress_bar.value() == 100
    assert "42 záznamů" in dialog.result_label.text()
    assert "Task 'Vlákno' started" in dialog.log_text.toPlainText()
    assert task.cleanup_calls == 1


@pytest.mark.gui
@pytest.mark.slow
@pytest.mark.integration
def test_cancelling_a_real_worker_ends_in_the_cancelled_state(qapp, make_dialog,
                                                              threaded):
    """The whole cancel chain: button -> flag -> exception -> cancelled state."""
    task = LoopTask(limit=200, sleep_ms=5)
    dialog = make_dialog(task)
    threaded(task, start=False)
    dialog.start_task()
    assert spin(qapp, lambda: task.iterations >= 2, 2000)

    dialog.cancel_button.click()
    assert spin(qapp, lambda: not task.isRunning(), 2000)
    drain(qapp)

    assert task.iterations < task.limit
    assert dialog.status_label.text() == "Cancelled"
    assert dialog.was_cancelled() is True
    assert dialog.progress_bar.format().endswith("- Cancelled")
    assert dialog.operation_label.text() == "Task cancelled by user"
    assert task.cleanup_calls == 1


@pytest.mark.gui
@pytest.mark.slow
@pytest.mark.integration
def test_pausing_a_real_worker_really_stops_it_until_it_is_resumed(qapp, make_dialog,
                                                                   threaded):
    """wait_if_paused() blocks the thread and request_resume() releases it."""
    task = LoopTask(name="Pauzovatelná", can_pause=True, limit=400, sleep_ms=5)
    dialog = make_dialog(task)
    threaded(task, start=False)
    dialog.start_task()
    assert spin(qapp, lambda: task.iterations >= 2, 2000)

    dialog.pause_resume_button.click()
    time.sleep(0.08)                       # let the worker reach the wait point
    qapp.processEvents()
    frozen_at = task.iterations
    time.sleep(0.08)

    assert task.iterations == frozen_at
    assert dialog.status_label.text() == "Paused"

    dialog.pause_resume_button.click()

    assert spin(qapp, lambda: task.iterations > frozen_at, 2000)
    assert dialog.status_label.text() == "Running"


@pytest.mark.slow
@pytest.mark.integration
def test_cancelling_a_paused_worker_wakes_it_up_and_finishes_it(qapp, threaded):
    """A paused thread must still react to cancel, or the dialog would hang."""
    task = LoopTask(can_pause=True, limit=400, sleep_ms=5)
    log = SignalLog(task)
    threaded(task)
    assert spin(qapp, lambda: task.iterations >= 2, 2000)

    task.request_pause()
    time.sleep(0.08)
    task.request_cancel()

    assert task.wait(2000) is True
    drain(qapp)
    assert log.flow()[-2:] == ["cancelled", "finished"]
    assert task.cleanup_calls == 1


# ===========================================================================
# Defects found in the production code
# ===========================================================================

@pytest.mark.gui
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: a successful indeterminate task leaves the "
                          "progress bar sweeping forever", strict=False)
def test_a_successful_indeterminate_task_stops_the_busy_animation(make_dialog):
    """Success has to leave busy mode, exactly like Cancelled and Failed do."""
    dialog = make_dialog(SucceedingTask(is_deterministic=False))
    dialog.on_progress_changed(0, "Stahuji data")

    dialog.on_task_finished(True, QDateTime.currentDateTime(), "hotovo")

    assert dialog.progress_bar.maximum() == 100


@pytest.mark.gui
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: durations are rendered as '1 min 60 sec' because "
                          "the seconds are rounded independently", strict=False)
def test_a_duration_is_never_rendered_with_sixty_seconds(make_dialog):
    """119.6 s is 2 minutes, not '1 min 60 sec'."""
    dialog = _timed_dialog(make_dialog, 119600)

    dialog.on_task_finished(True, dialog.task.end_time, "hotovo")

    assert "60 sec" not in dialog.duration_label.text()


@pytest.mark.gui
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: progress updates between the cancel click and "
                          "task_cancelled overwrite the 'Cancelling task...' "
                          "feedback", strict=False)
def test_progress_after_the_cancel_click_keeps_the_cancelling_feedback(make_dialog):
    """Once the user pressed Cancel the dialog must stop showing progress."""
    task = NonStartingTask(is_deterministic=True)
    dialog = make_dialog(task)
    dialog.start_task()
    dialog.on_progress_changed(25, "Načítám třídy")
    dialog.cancel_button.click()
    assert dialog.was_cancelled() is True

    dialog.on_progress_changed(30, "Načítám žáky")     # late update from the worker

    assert dialog.operation_label.text() == "Cancelling task..."
