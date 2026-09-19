"""
Shared pytest configuration and fixtures for the UserManagerX test suite.

Design rules
------------
* **Headless.** ``QT_QPA_PLATFORM=offscreen`` is set before PyQt6 is imported,
  so the suite runs on a build server without a display.
* **One QApplication.** Qt allows exactly one per process; it is created once
  per session and reused.
* **No modal dialogs.** Every blocking dialog entry point is replaced by a
  recorder, so a test can assert *which* message the user would have seen
  instead of hanging forever waiting for a click.
* **No side effects on the developer's machine.** The settings file, the
  logging configuration and the password policy are redirected into ``tmp_path``
  and reset between tests, so running the suite never touches real user data.
"""

import os
import sys
from pathlib import Path

# Must happen before the first PyQt6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox,
)

# QtWebEngine (used by the PDF preview) demands this before the QApplication
# exists; main.py does the same.
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)


# ---------------------------------------------------------------------------
# Qt application
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    """The single QApplication instance shared by the whole session."""
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture(autouse=True)
def _qt_ready(qapp):
    """Make sure every test has the QApplication available."""
    yield
    qapp.processEvents()


# ---------------------------------------------------------------------------
# Modal dialog recorder
# ---------------------------------------------------------------------------

class DialogRecorder:
    """
    Records the modal dialogs a widget tried to show.

    Attributes:
        calls: list of ``(kind, title, text)`` tuples in call order.

    The answers the stubs give can be steered per test::

        dialogs.question_answer = QMessageBox.StandardButton.No
        dialogs.text_answer = ("New name", True)
        dialogs.exec_result = QDialog.DialogCode.Rejected
    """

    def __init__(self):
        self.calls = []
        self.question_answer = QMessageBox.StandardButton.Yes
        self.warning_answer = QMessageBox.StandardButton.Yes
        self.text_answer = ("test-value", True)
        self.item_answer = ("test-item", True)
        self.open_file_answer = ("", "")
        self.save_file_answer = ("", "")
        self.directory_answer = ""
        self.exec_result = QDialog.DialogCode.Accepted
        #: Buttons added with QMessageBox.addButton(); the stub clicks this index
        self.clicked_button_index = 0
        #: Default text each QInputDialog.getText() was pre-filled with, in
        #: call order - that is the name the application *suggested*.
        self.text_defaults = []

    # -- queries --------------------------------------------------------

    def kinds(self):
        """Return only the kinds, in order."""
        return [call[0] for call in self.calls]

    def titles(self):
        """Return only the window titles, in order."""
        return [call[1] for call in self.calls]

    def texts(self):
        """Return only the message bodies, in order."""
        return [call[2] for call in self.calls]

    def find(self, needle):
        """Return every recorded call whose title or text contains *needle*."""
        needle = needle.lower()
        return [c for c in self.calls
                if needle in (c[1] or "").lower() or needle in (c[2] or "").lower()]

    def saw(self, needle):
        """True when any recorded dialog mentions *needle*."""
        return bool(self.find(needle))

    def clear(self):
        """Forget everything recorded so far."""
        self.calls.clear()
        self.text_defaults.clear()


@pytest.fixture
def dialogs(monkeypatch):
    """
    Replace every modal dialog with a recorder.

    Without this, a test that triggers ``QMessageBox.warning(...)`` would block
    the suite forever.
    """
    recorder = DialogRecorder()

    def _record(kind, default_getter=None):
        def stub(*args, **kwargs):
            title = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
            text = args[2] if len(args) > 2 and isinstance(args[2], str) else ""
            recorder.calls.append((kind, title, text))
            if kind == "question":
                return recorder.question_answer
            if kind == "warning":
                return recorder.warning_answer
            return QMessageBox.StandardButton.Ok
        return staticmethod(stub)

    monkeypatch.setattr(QMessageBox, "information", _record("information"))
    monkeypatch.setattr(QMessageBox, "warning", _record("warning"))
    monkeypatch.setattr(QMessageBox, "critical", _record("critical"))
    monkeypatch.setattr(QMessageBox, "question", _record("question"))
    monkeypatch.setattr(QMessageBox, "about", _record("about"))

    def _box_exec(self):
        recorder.calls.append(("box_exec", self.windowTitle(), self.text()))
        buttons = self.buttons()
        if buttons:
            index = min(recorder.clicked_button_index, len(buttons) - 1)
            self.setDefaultButton(buttons[index])
            # QMessageBox.clickedButton() reads an internal field; emulate it
            self._test_clicked = buttons[index]
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "exec", _box_exec)
    monkeypatch.setattr(
        QMessageBox, "clickedButton",
        lambda self: getattr(self, "_test_clicked", None),
    )

    def _get_text(*args, **kwargs):
        title = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
        label = args[2] if len(args) > 2 and isinstance(args[2], str) else ""
        default = args[4] if len(args) > 4 else kwargs.get("text", "")
        recorder.calls.append(("get_text", title, label))
        recorder.text_defaults.append(default if isinstance(default, str) else "")
        return recorder.text_answer

    def _get_item(*args, **kwargs):
        recorder.calls.append(("get_item", "", ""))
        return recorder.item_answer

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(_get_text))
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(_get_item))

    monkeypatch.setattr(
        QFileDialog, "getOpenFileName",
        staticmethod(lambda *a, **k: (recorder.calls.append(("open_file", "", "")),
                                      recorder.open_file_answer)[1]))
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        staticmethod(lambda *a, **k: (recorder.calls.append(("save_file", "", "")),
                                      recorder.save_file_answer)[1]))
    monkeypatch.setattr(
        QFileDialog, "getExistingDirectory",
        staticmethod(lambda *a, **k: (recorder.calls.append(("directory", "", "")),
                                      recorder.directory_answer)[1]))

    return recorder


@pytest.fixture
def accept_dialogs(monkeypatch, dialogs):
    """Make every ``QDialog.exec()`` return Accepted without showing anything."""
    monkeypatch.setattr(QDialog, "exec", lambda self: dialogs.exec_result)
    return dialogs


# ---------------------------------------------------------------------------
# Isolated application state
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """
    Point the SettingsManager singleton at a throw-away file.

    Returns the fresh :class:`~utils.settings_manager.SettingsManager`.
    """
    import utils.settings_manager as sm

    settings_file = tmp_path / "settings.json"

    monkeypatch.setattr(
        sm.SettingsManager, "_get_settings_file", lambda self: settings_file
    )
    monkeypatch.setattr(sm, "_settings_instance", None, raising=False)

    instance = sm.get_settings()
    yield instance
    monkeypatch.setattr(sm, "_settings_instance", None, raising=False)


@pytest.fixture(autouse=True)
def _reset_password_policy():
    """Undo password-policy changes so tests cannot influence each other."""
    import utils.password_policy as pp

    original = pp._active_policy
    pp._active_policy = None
    yield
    pp._active_policy = original


@pytest.fixture(autouse=True)
def _no_network_year_lookup():
    """
    Pre-resolve the current year so no test ever asks the internet.

    The main window warms the year cache in a background thread at start-up,
    and loading a source needs the year too. Without this fixture every test
    that builds a window or adds a source would make a real HTTP request.
    """
    import utils.enrollment_service as service
    from utils.school_year import YearResolution, get_system_year

    original = service._cached_resolution
    system_year = get_system_year()
    service._cached_resolution = YearResolution(
        system_year=system_year, internet_year=system_year, year=system_year,
    )
    yield service._cached_resolution
    service._cached_resolution = original


@pytest.fixture
def isolated_logging_config(tmp_path, monkeypatch):
    """Give LoggingConfig its own config file and log directory."""
    import utils.logging_config as lc

    config_file = tmp_path / "logging_config.json"
    monkeypatch.setattr(lc, "_logging_config", None, raising=False)
    config = lc.LoggingConfig(config_file=str(config_file))
    config.config["log_dir"] = str(tmp_path / "logs")
    monkeypatch.setattr(lc, "_logging_config", config, raising=False)
    yield config
    monkeypatch.setattr(lc, "_logging_config", None, raising=False)


# ---------------------------------------------------------------------------
# Model factories
# ---------------------------------------------------------------------------

@pytest.fixture
def make_person():
    """Factory: ``make_person("Jan", "Novák", "6.A", ad_username="novakjan")``."""
    from models import Person

    def _make(first_name="Jan", last_name="Novák", class_name="6.A", **kwargs):
        return Person(first_name=first_name, last_name=last_name,
                      class_name=class_name, **kwargs)

    return _make


@pytest.fixture
def make_class(make_person):
    """Factory: ``make_class("6.A", 3)`` builds a class with 3 students."""
    from models import Class

    def _make(name="6.A", student_count=0, persons=None):
        cls = Class(name=name)
        if persons:
            for person in persons:
                cls.add_person(person)
        else:
            for index in range(student_count):
                cls.add_person(make_person(
                    first_name=f"First{index}", last_name=f"Last{index}",
                    class_name=name,
                ))
        return cls

    return _make


@pytest.fixture
def make_source(make_class):
    """
    Factory for a whole source.

    ``make_source("EduPage", [("6.A", 2), ("IX.", 3)], readonly=True)``
    """
    from models import Source

    def _make(name="TestSource", classes=None, source_type="manual",
              readonly=False):
        source = Source(name=name, source_type=source_type, readonly=readonly)
        for entry in (classes or []):
            if isinstance(entry, tuple):
                source.add_class(make_class(entry[0], entry[1]))
            else:
                source.add_class(make_class(entry, 0))
        return source

    return _make


@pytest.fixture
def source_manager():
    """A fresh, empty SourceManager."""
    from models import SourceManager
    return SourceManager()
