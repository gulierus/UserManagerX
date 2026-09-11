"""
Unit tests for the data-source widgets and the EduPage loading machinery.

Covered modules
---------------
* :mod:`sources.file_source`      - encrypted JSON import widget,
* :mod:`sources.edupage_source`   - EduPage credential/mode widget,
* :mod:`sources.ad_source`        - Active Directory import widget,
* :mod:`utils.edupage_tasks`      - the worker tasks,
* :mod:`utils.edupage_coordinator`- the dialog/task orchestration.

Safety rules obeyed by every test in this file
----------------------------------------------
* **No network.** ``edupage_api`` is not installed at all, so a fake package is
  injected into ``sys.modules``; the AD tests drive the *real* ``ldap3`` through
  its in-memory ``MOCK_SYNC`` strategy, which never opens a socket.
* **No modal dialogs.** Everything that could block asks for the ``dialogs`` /
  ``accept_dialogs`` fixtures.
* **No threads.** The task bodies are executed directly on the calling thread;
  the blocking "wait for the user" loops are driven by replacing the
  instance-level ``msleep`` with a scripted callback, so a cancellation can be
  injected deterministically instead of being raced for.

Tests marked ``bug`` + ``xfail`` assert the *correct* behaviour and document a
defect that is still present in the production code.
"""

import json
import sys
import types

import pytest

from PyQt6.QtWidgets import (
    QDialog, QInputDialog, QMessageBox, QWidget,
)

from models import Class, Person, Source, SourceManager
from utils.progress_tasks import LogLevel, TaskCancelledException


# ===========================================================================
# Shared helpers
# ===========================================================================

def queue_text_answers(monkeypatch, answers):
    """
    Feed ``QInputDialog.getText`` a scripted sequence of ``(text, ok)`` answers.

    The shared ``dialogs`` fixture always returns the *same* answer, which turns
    every "ask again" branch into an endless loop.  Returns the list of
    positional argument tuples the production code passed in, so the default
    name can be asserted.
    """
    calls = []
    remaining = list(answers)

    def fake_get_text(*args, **kwargs):
        calls.append(args)
        assert remaining, "QInputDialog.getText was called more often than scripted"
        return remaining.pop(0)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
    return calls


def source_payload(classes=None, **extra):
    """Build the plain dictionary an encrypted export file contains."""
    data = {
        "format_version": 2,
        "name": "Exported",
        "source_type": "file",
        "classes": [],
    }
    for class_name, people in (classes or []):
        data["classes"].append({
            "name": class_name,
            "metadata": {},
            "persons": [
                {"first_name": first, "last_name": last, "class_name": class_name}
                for first, last in people
            ],
        })
    data.update(extra)
    return data


class StubTask:
    """Minimal stand-in for an EduPage task, as the coordinator uses one."""

    def __init__(self, source=None, error=None, cancelled=False):
        self._source = source
        self._error = error
        self._cancelled = cancelled
        self.selected_classes = None
        self.code = None
        self.cancel_calls = 0
        self.result_names = []
        self.twofa_login = None

    # -- the API the coordinator relies on ------------------------------
    def request_cancel(self):
        self.cancel_calls += 1
        self._cancelled = True

    def is_cancelled(self):
        return self._cancelled

    def set_selected_classes(self, names):
        self.selected_classes = list(names)

    def set_2fa_code(self, code):
        self.code = code

    def get_result_source(self, name):
        self.result_names.append(name)
        if self._error is not None:
            raise self._error
        source = self._source or Source(name=name, source_type="edupage", readonly=True)
        source.name = name
        return source


class StubDialog:
    """Stand-in for :class:`ProgressDialog` in the cancellation queries."""

    def __init__(self, cancelled=False, error=None):
        self._cancelled = cancelled
        self._error = error

    def was_cancelled(self):
        if self._error is not None:
            raise self._error
        return self._cancelled


# ===========================================================================
# Fake edupage_api package (the real one is not installed)
# ===========================================================================

class FakeEduClass:
    """What ``Edupage.get_classes()`` returns."""

    def __init__(self, class_id, name):
        self.class_id = class_id
        self.name = name


class FakeEduStudent:
    """What ``Edupage.get_all_students()`` returns."""

    def __init__(self, person_id, class_id, name_short):
        self.person_id = person_id
        self.class_id = class_id
        self.name_short = name_short


class EduPagePlan:
    """Scripted behaviour of the fake EduPage server."""

    def __init__(self):
        self.classes = []
        self.students = []
        self.full_names = {}
        self.name_errors = set()
        self.needs_2fa = False
        self.bad_credentials = False
        self.login_succeeds = True
        self.code_accepted = True
        self.has_logout = True
        self.logout_error = None
        self.login_calls = []
        self.codes = []
        self.resend_calls = 0
        self.logouts = 0
        self.dbi_targets = []
        self.edupage = None
        self.exceptions = None

    def add_class(self, class_id, name):
        self.classes.append(FakeEduClass(class_id, name))

    def add_student(self, person_id, class_id, name_short, full_name=None):
        self.students.append(FakeEduStudent(person_id, class_id, name_short))
        if full_name is not None:
            self.full_names[person_id] = full_name


@pytest.fixture
def edupage(monkeypatch):
    """
    Install a fake ``edupage_api`` package and return the behaviour plan.

    ``edupage_api`` is deliberately absent from this environment; the tasks
    import it lazily inside their methods, so putting the fake modules into
    ``sys.modules`` is enough and nothing ever reaches the network.
    """
    plan = EduPagePlan()

    package = types.ModuleType("edupage_api")
    exceptions = types.ModuleType("edupage_api.exceptions")
    dbi = types.ModuleType("edupage_api.dbi")

    class BadCredentialsException(Exception):
        pass

    class SecondFactorFailedException(Exception):
        pass

    exceptions.BadCredentialsException = BadCredentialsException
    exceptions.SecondFactorFailedException = SecondFactorFailedException
    plan.exceptions = exceptions

    class TwoFactorLogin:
        def __init__(self, edupage_obj):
            self._edupage = edupage_obj

        def finish_with_code(self, code):
            plan.codes.append(code)
            if not plan.code_accepted:
                raise SecondFactorFailedException("bad code")
            self._edupage.is_logged_in = True

        def resend_notifications(self):
            plan.resend_calls += 1

    class Edupage:
        def __init__(self):
            plan.edupage = self
            self.is_logged_in = False
            if plan.has_logout:
                self.logout = self._logout

        def _logout(self):
            plan.logouts += 1
            if plan.logout_error is not None:
                raise plan.logout_error

        def login(self, username, password, subdomain):
            plan.login_calls.append((username, password, subdomain))
            if plan.bad_credentials:
                raise BadCredentialsException("nope")
            if plan.needs_2fa:
                return TwoFactorLogin(self)
            self.is_logged_in = plan.login_succeeds
            return None

        def get_classes(self):
            return list(plan.classes)

        def get_all_students(self):
            return list(plan.students)

    class DbiHelper:
        def __init__(self, edupage_obj):
            plan.dbi_targets.append(edupage_obj)

        def fetch_student_name(self, person_id):
            if person_id in plan.name_errors:
                raise RuntimeError(f"no such person {person_id}")
            return plan.full_names.get(person_id)

    package.Edupage = Edupage
    package.exceptions = exceptions
    package.dbi = dbi
    dbi.DbiHelper = DbiHelper

    monkeypatch.setitem(sys.modules, "edupage_api", package)
    monkeypatch.setitem(sys.modules, "edupage_api.exceptions", exceptions)
    monkeypatch.setitem(sys.modules, "edupage_api.dbi", dbi)
    return plan


def script_waiting_loop(task, actions, limit=40):
    """
    Drive a task's blocking "wait for the user" loop.

    ``msleep`` is replaced on the instance, so every turn of the loop runs the
    next callback from *actions* instead of really sleeping.  The limit turns a
    loop that never ends into a failing test rather than a frozen suite.
    """
    state = {"turns": 0}

    def fake_msleep(milliseconds):
        state["turns"] += 1
        assert state["turns"] <= limit, "waiting loop never finished"
        if actions:
            actions.pop(0)()

    task.msleep = fake_msleep
    return state


def collect_logs(task):
    """Record every ``(message, level)`` the task logs."""
    entries = []
    task.log_message.connect(lambda msg, level, stamp: entries.append((msg, level)))
    return entries


# ===========================================================================
# Widget fixtures
# ===========================================================================

@pytest.fixture
def file_widget(qapp, source_manager, monkeypatch):
    """A FileSourceWidget with both encryption backends reported available."""
    import sources.file_source as fs

    monkeypatch.setattr(fs, "get_available_methods",
                        lambda: {"gpg": True, "aes-gcm": True})
    widget = fs.FileSourceWidget(source_manager)
    yield widget
    widget.deleteLater()


@pytest.fixture
def edupage_widget(qapp, source_manager):
    """An EdupageSourceWidget whose coordinator calls are recorded, not run."""
    from sources.edupage_source import EdupageSourceWidget

    widget = EdupageSourceWidget(source_manager)
    calls = []

    def recorder(name):
        def record(*args, **kwargs):
            calls.append((name, args, kwargs))
        return record

    for method in ("execute_mode1_single_phase", "execute_mode1_two_phase",
                   "execute_mode2_load"):
        setattr(widget.coordinator, method, recorder(method))

    widget.coordinator_calls = calls
    yield widget
    widget.deleteLater()


@pytest.fixture
def coordinator(qapp, source_manager):
    """An EduPageLoadCoordinator attached to a throw-away parent widget."""
    from utils.edupage_coordinator import EduPageLoadCoordinator

    parent = QWidget()
    coord = EduPageLoadCoordinator(parent, source_manager)
    coord._test_parent = parent          # keep the parent alive
    yield coord
    parent.deleteLater()


# ===========================================================================
# FileSourceWidget.parse_json_to_source
# ===========================================================================

def test_parse_json_rebuilds_every_class_and_person(file_widget):
    """A well formed export is rebuilt class by class and person by person."""
    data = source_payload([("6.A", [("Jan", "Novák"), ("Eva", "Dvořáková")]),
                           ("7.B", [("Petr", "Černý")])])

    source = file_widget.parse_json_to_source(data, "ignored-filename")

    assert source.name == "Exported"
    assert [cls.name for cls in source.classes] == ["6.A", "7.B"]
    assert [(p.first_name, p.last_name) for p in source.classes[0].persons] == [
        ("Jan", "Novák"), ("Eva", "Dvořáková"),
    ]
    assert len(source.get_all_persons()) == 3


def test_parse_json_marks_the_imported_source_readonly(file_widget):
    """Data that came from a file may never be edited in place."""
    source = file_widget.parse_json_to_source(source_payload([("6.A", [])]), "f")
    assert source.readonly is True


def test_parse_json_falls_back_to_the_file_name_when_none_is_stored(file_widget):
    """Without a stored name the file stem names the source."""
    data = source_payload([("6.A", [])])
    del data["name"]

    assert file_widget.parse_json_to_source(data, "trida-export").name == "trida-export"


def test_parse_json_keeps_an_empty_stored_name_from_shadowing_the_file_name(file_widget):
    """An empty stored name is not a name - the file stem wins."""
    data = source_payload([], name="")
    assert file_widget.parse_json_to_source(data, "backup").name == "backup"


def test_parse_json_accepts_a_file_without_a_single_class(file_widget):
    """An export of an empty source is valid and yields zero classes."""
    source = file_widget.parse_json_to_source(source_payload([]), "empty")
    assert source.classes == []


def test_parse_json_preserves_diacritics_and_unusual_class_names(file_widget):
    """Unicode names survive the import byte for byte."""
    data = source_payload([("Třída Ⅷ.b", [("Žofie", "Škrháková")])])

    source = file_widget.parse_json_to_source(data, "f")

    assert source.classes[0].name == "Třída Ⅷ.b"
    person = source.classes[0].persons[0]
    assert (person.first_name, person.last_name) == ("Žofie", "Škrháková")
    assert person.class_name == "Třída Ⅷ.b"


def test_parse_json_restores_source_info_and_metadata(file_widget):
    """Everything the exporter wrote about the source comes back."""
    data = source_payload([], source_info={"subdomain": "skola"},
                          metadata={"origin": "edupage"})

    source = file_widget.parse_json_to_source(data, "f")

    assert source.get_source_info("subdomain") == "skola"
    assert source.metadata["origin"] == "edupage"


@pytest.mark.parametrize("payload", [
    [],
    ["not", "an", "object"],
    "a string",
    42,
    None,
    True,
], ids=["empty-list", "list", "string", "int", "none", "bool"])
def test_parse_json_rejects_payloads_that_are_not_json_objects(file_widget, payload):
    """Anything but a JSON object is a ValueError, never a crash."""
    with pytest.raises(ValueError):
        file_widget.parse_json_to_source(payload, "f")


@pytest.mark.parametrize("payload, needle", [
    ({"classes": "6.A"}, "list"),
    ({"classes": [["6.A"]]}, "dictionary"),
    ({"classes": [{"persons": []}]}, "name"),
    ({"classes": [{"name": "6.A", "persons": {"a": 1}}]}, "list"),
    ({"classes": [{"name": "6.A", "persons": ["Jan"]}]}, "dictionary"),
    ({"classes": [{"name": "6.A", "persons": [{"first_name": "Jan"}]}]}, "last_name"),
    ({"classes": [{"name": "6.A", "persons": [
        {"first_name": "Jan", "last_name": None, "class_name": "6.A"}]}]}, "last_name"),
], ids=["classes-not-list", "class-not-dict", "class-without-name",
        "persons-not-list", "person-not-dict", "person-missing-field",
        "person-null-field"])
def test_parse_json_rejects_broken_structures_with_a_helpful_value_error(
        file_widget, payload, needle):
    """Every structural defect raises ValueError naming the offending part."""
    with pytest.raises(ValueError) as excinfo:
        file_widget.parse_json_to_source(payload, "f")
    assert needle in str(excinfo.value)


def test_parse_json_wraps_unexpected_errors_in_a_value_error(file_widget, monkeypatch):
    """A non-ValueError failure is translated, so on_load can report it."""
    import sources.file_source as fs

    def explode(*args, **kwargs):
        raise TypeError("unsupported operand")

    monkeypatch.setattr(fs, "source_from_dict", explode)

    with pytest.raises(ValueError) as excinfo:
        file_widget.parse_json_to_source({"classes": []}, "f")
    assert "Failed to parse JSON data" in str(excinfo.value)
    assert "unsupported operand" in str(excinfo.value)


# ===========================================================================
# FileSourceWidget.on_load - guards
# ===========================================================================

def test_on_load_without_a_file_warns_and_decrypts_nothing(file_widget, dialogs,
                                                           monkeypatch):
    """No file selected - the user is warned before any crypto runs."""
    import sources.file_source as fs

    monkeypatch.setattr(fs, "decrypt_file", lambda *a, **k:
                        pytest.fail("decrypt_file must not be called"))
    file_widget.password_input.setText("secret-password")

    file_widget.on_load()

    assert dialogs.kinds() == ["warning"]
    assert dialogs.titles() == ["No File"]


def test_on_load_treats_a_whitespace_only_path_as_no_file(file_widget, dialogs):
    """A path of blanks is not a path."""
    file_widget.file_input.setText("   ")
    file_widget.password_input.setText("secret-password")

    file_widget.on_load()

    assert dialogs.titles() == ["No File"]


def test_on_load_without_a_password_warns_and_decrypts_nothing(file_widget, dialogs,
                                                               tmp_path, monkeypatch):
    """An empty password is refused before the file is touched."""
    import sources.file_source as fs

    monkeypatch.setattr(fs, "decrypt_file", lambda *a, **k:
                        pytest.fail("decrypt_file must not be called"))
    target = tmp_path / "data.aes"
    target.write_text("{}", encoding="utf-8")
    file_widget.file_input.setText(str(target))

    file_widget.on_load()

    assert dialogs.titles()[-1] == "No Password"


@pytest.mark.parametrize("method, hint", [
    ("gpg", "GnuPG"),
    ("aes-gcm", "PyCryptodome"),
])
def test_on_load_refuses_an_unavailable_method_and_names_the_missing_library(
        file_widget, dialogs, tmp_path, monkeypatch, method, hint):
    """Picking a backend that is not installed explains what to install."""
    import sources.file_source as fs

    monkeypatch.setattr(fs, "get_available_methods",
                        lambda: {"gpg": False, "aes-gcm": False})
    monkeypatch.setattr(fs, "decrypt_file", lambda *a, **k:
                        pytest.fail("decrypt_file must not be called"))

    target = tmp_path / "data.enc"
    target.write_text("x", encoding="utf-8")
    file_widget.file_input.setText(str(target))
    file_widget.password_input.setText("secret-password")
    file_widget.method_combo.setCurrentIndex(
        file_widget.method_combo.findData(method))

    file_widget.on_load()

    critical = [c for c in dialogs.calls if c[0] == "critical"]
    assert critical and critical[-1][1] == "Method Not Available"
    assert hint in critical[-1][2]


def test_on_load_auto_detect_lets_the_backend_decide_the_method(
        file_widget, dialogs, tmp_path, monkeypatch):
    """Auto-detect never pre-checks availability - it passes method=None."""
    import sources.file_source as fs

    seen = {}

    def fake_decrypt(path, password, method=None):
        seen["method"] = method
        return json.dumps(source_payload([("6.A", [])]))

    monkeypatch.setattr(fs, "get_available_methods",
                        lambda: {"gpg": False, "aes-gcm": False})
    monkeypatch.setattr(fs, "decrypt_file", fake_decrypt)
    queue_text_answers(monkeypatch, [("Auto", True)])

    target = tmp_path / "data.enc"
    target.write_text("x", encoding="utf-8")
    file_widget.file_input.setText(str(target))
    file_widget.password_input.setText("secret-password")

    file_widget.on_load()

    assert seen["method"] is None
    assert file_widget.source_manager.get_source_names() == ["Auto"]


# ===========================================================================
# FileSourceWidget.on_load - failure branches
# ===========================================================================

@pytest.mark.parametrize("error, title, needle", [
    (FileNotFoundError("gone"), "File Not Found", "data.enc"),
    (ValueError("Cannot detect encryption method"), "Invalid File",
     "Cannot detect encryption method"),
    (RuntimeError("AES-GCM decryption failed - incorrect password or corrupted file"),
     "Incorrect Password", "password"),
    (RuntimeError("GPG decryption is not available. Install GnuPG."),
     "Encryption Method Not Available", "Install GnuPG"),
    (RuntimeError("something exploded"), "Decryption Failed", "something exploded"),
    (OSError("disk on fire"), "Error", "disk on fire"),
    (KeyError("surprise"), "Error", "surprise"),
], ids=["missing-file", "value-error", "wrong-password", "method-missing",
        "other-runtime", "os-error", "unexpected"])
def test_on_load_maps_each_decryption_failure_to_its_own_message(
        file_widget, dialogs, tmp_path, monkeypatch, error, title, needle):
    """Every failure class gets the message written for it - and no source."""
    import sources.file_source as fs

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(fs, "decrypt_file", boom)
    target = tmp_path / "data.enc"
    target.write_text("x", encoding="utf-8")
    file_widget.file_input.setText(str(target))
    file_widget.password_input.setText("secret-password")

    file_widget.on_load()

    critical = [c for c in dialogs.calls if c[0] == "critical"]
    assert critical, f"no critical dialog for {error!r}"
    assert critical[-1][1] == title
    assert needle in critical[-1][2]
    assert file_widget.source_manager.sources == []


def test_on_load_recognises_the_incorrect_password_hint_case_insensitively(
        file_widget, dialogs, tmp_path, monkeypatch):
    """The password branch matches regardless of how the backend capitalises."""
    import sources.file_source as fs

    def boom(*args, **kwargs):
        raise RuntimeError("Decryption failed: INCORRECT PASSWORD supplied")

    monkeypatch.setattr(fs, "decrypt_file", boom)
    target = tmp_path / "data.enc"
    target.write_text("x", encoding="utf-8")
    file_widget.file_input.setText(str(target))
    file_widget.password_input.setText("secret-password")

    file_widget.on_load()

    assert dialogs.calls[-1][1] == "Incorrect Password"


def test_on_load_reports_non_json_plaintext_as_invalid_data(
        file_widget, dialogs, tmp_path, monkeypatch):
    """Decryption can succeed and still yield something that is not an export."""
    import sources.file_source as fs

    monkeypatch.setattr(fs, "decrypt_file", lambda *a, **k: "not json at all")
    target = tmp_path / "data.enc"
    target.write_text("x", encoding="utf-8")
    file_widget.file_input.setText(str(target))
    file_widget.password_input.setText("secret-password")

    file_widget.on_load()

    assert dialogs.calls[-1][:2] == ("critical", "Invalid Data")
    assert file_widget.source_manager.sources == []


def test_on_load_reports_a_structurally_invalid_export_as_an_invalid_file(
        file_widget, dialogs, tmp_path, monkeypatch):
    """Valid JSON with a broken structure lands in the ValueError branch."""
    import sources.file_source as fs

    monkeypatch.setattr(fs, "decrypt_file",
                        lambda *a, **k: json.dumps({"classes": "6.A"}))
    target = tmp_path / "data.enc"
    target.write_text("x", encoding="utf-8")
    file_widget.file_input.setText(str(target))
    file_widget.password_input.setText("secret-password")

    file_widget.on_load()

    assert dialogs.calls[-1][:2] == ("critical", "Invalid File")


# ===========================================================================
# FileSourceWidget.on_load - naming and the duplicate flow
# ===========================================================================

@pytest.fixture
def loadable_file(file_widget, tmp_path, monkeypatch):
    """Point the widget at a file whose decryption yields one class."""
    import sources.file_source as fs

    monkeypatch.setattr(
        fs, "decrypt_file",
        lambda *a, **k: json.dumps(source_payload([("6.A", [("Jan", "Novák")])])))
    target = tmp_path / "export.enc"
    target.write_text("x", encoding="utf-8")
    file_widget.file_input.setText(str(target))
    file_widget.password_input.setText("secret-password")
    return file_widget


def test_on_load_stores_the_source_under_the_trimmed_name(loadable_file, dialogs,
                                                          monkeypatch):
    """Surrounding blanks in the chosen name are removed."""
    queue_text_answers(monkeypatch, [("   Moje data   ", True)])

    loadable_file.on_load()

    assert loadable_file.source_manager.get_source_names() == ["Moje data"]
    assert dialogs.calls[-1][:2] == ("information", "Success")


def test_on_load_offers_the_file_stem_as_the_default_name(loadable_file, dialogs,
                                                          monkeypatch):
    """The name dialog is pre-filled with the name parsed from the payload."""
    calls = queue_text_answers(monkeypatch, [("Whatever", True)])

    loadable_file.on_load()

    # QInputDialog.getText(parent, title, label, echo_mode, default_text)
    assert calls[0][4] == "Exported"


def test_on_load_replaces_the_existing_source_when_the_user_confirms(
        loadable_file, dialogs, monkeypatch):
    """'Replace it?' really replaces - one source, and it is the new one."""
    manager = loadable_file.source_manager
    stale = Source(name="Class data", source_type="manual")
    manager.add_source(stale)
    removed = []
    manager.source_removed.connect(removed.append)

    dialogs.question_answer = QMessageBox.StandardButton.Yes
    queue_text_answers(monkeypatch, [("Class data", True)])

    loadable_file.on_load()

    assert manager.get_source_names() == ["Class data"]
    fresh = manager.get_source_by_name("Class data")
    assert fresh is not stale
    assert len(fresh.get_all_persons()) == 1
    assert removed == ["Class data"]


def test_on_load_keeps_both_sources_when_the_user_declines_and_renames(
        loadable_file, dialogs, monkeypatch):
    """Declining the replacement asks again and leaves the old source alone."""
    manager = loadable_file.source_manager
    stale = Source(name="Class data", source_type="manual")
    manager.add_source(stale)

    dialogs.question_answer = QMessageBox.StandardButton.No
    calls = queue_text_answers(monkeypatch,
                               [("Class data", True), ("Class data 2", True)])

    loadable_file.on_load()

    assert manager.get_source_names() == ["Class data", "Class data 2"]
    assert manager.get_source_by_name("Class data") is stale
    # The rejected name is offered again as the default of the second prompt.
    assert calls[1][4] == "Class data"


def test_on_load_asks_again_after_a_whitespace_only_name(loadable_file, dialogs,
                                                         monkeypatch):
    """A name made of blanks is refused, and the dialog comes back."""
    calls = queue_text_answers(monkeypatch, [("    ", True), ("Real name", True)])

    loadable_file.on_load()

    assert len(calls) == 2
    assert dialogs.saw("Source name cannot be empty")
    assert loadable_file.source_manager.get_source_names() == ["Real name"]


@pytest.mark.parametrize("answer", [("", True), ("Name", False), ("", False)],
                         ids=["empty-text", "cancelled", "both"])
def test_on_load_adds_nothing_when_the_name_dialog_is_dismissed(
        loadable_file, dialogs, monkeypatch, answer):
    """Cancelling (or clearing) the name aborts the whole import."""
    queue_text_answers(monkeypatch, [answer])

    loadable_file.on_load()

    assert loadable_file.source_manager.sources == []
    assert not dialogs.saw("Success")


def test_on_load_clears_the_password_field_after_a_successful_import(
        loadable_file, dialogs, monkeypatch):
    """The decryption password is not left lying around in the widget."""
    queue_text_answers(monkeypatch, [("Imported", True)])

    loadable_file.on_load()

    assert loadable_file.password_input.text() == ""


def test_on_load_keeps_the_password_when_the_import_failed(loadable_file, dialogs,
                                                           monkeypatch):
    """A failed attempt keeps the password so the user can retry."""
    queue_text_answers(monkeypatch, [("Imported", False)])

    loadable_file.on_load()

    assert loadable_file.password_input.text() == "secret-password"


def test_on_load_reports_the_counted_students_and_classes(loadable_file, dialogs,
                                                          monkeypatch):
    """The success message counts what was actually imported."""
    queue_text_answers(monkeypatch, [("Imported", True)])

    loadable_file.on_load()

    text = dialogs.calls[-1][2]
    assert "1 student(s)" in text
    assert "1 class(es)" in text


@pytest.mark.integration
def test_on_load_decrypts_a_real_aes_gcm_file_end_to_end(file_widget, dialogs,
                                                         tmp_path, monkeypatch):
    """A genuinely encrypted export is decrypted, parsed and stored."""
    from utils.encryption import encrypt_file

    payload = source_payload([("6.A", [("Žofie", "Škrháková")])], name="Škola")
    target = tmp_path / "export.aes"
    encrypt_file(json.dumps(payload), str(target), "correct-horse", method="aes-gcm")

    queue_text_answers(monkeypatch, [("Škola", True)])
    file_widget.file_input.setText(str(target))
    file_widget.password_input.setText("correct-horse")

    file_widget.on_load()

    source = file_widget.source_manager.get_source_by_name("Škola")
    assert source is not None
    assert source.readonly is True
    assert [p.last_name for p in source.get_all_persons()] == ["Škrháková"]


@pytest.mark.integration
def test_on_load_reports_a_wrong_password_for_a_real_aes_gcm_file(
        file_widget, dialogs, tmp_path):
    """The real backend's failure text is routed to the password message."""
    from utils.encryption import encrypt_file

    target = tmp_path / "export.aes"
    encrypt_file(json.dumps(source_payload([])), str(target), "correct-horse")

    file_widget.file_input.setText(str(target))
    file_widget.password_input.setText("wrong-password")

    file_widget.on_load()

    assert dialogs.calls[-1][:2] == ("critical", "Incorrect Password")
    assert file_widget.source_manager.sources == []


# ===========================================================================
# EdupageSourceWidget.on_load_clicked
# ===========================================================================

@pytest.mark.gui
@pytest.mark.parametrize("subdomain, username, password", [
    ("", "user@x.cz", "pw"),
    ("skola", "", "pw"),
    ("skola", "user@x.cz", ""),
    ("   ", "user@x.cz", "pw"),
    ("skola", "  ", "pw"),
    ("", "", ""),
], ids=["no-subdomain", "no-username", "no-password", "blank-subdomain",
        "blank-username", "nothing"])
def test_on_load_clicked_requires_every_credential_field(
        edupage_widget, dialogs, subdomain, username, password):
    """Any missing credential stops the run before the coordinator is asked."""
    edupage_widget.subdomain_input.setText(subdomain)
    edupage_widget.username_input.setText(username)
    edupage_widget.password_input.setText(password)

    edupage_widget.on_load_clicked()

    assert dialogs.titles() == ["Missing Information"]
    assert edupage_widget.coordinator_calls == []


@pytest.mark.gui
def test_on_load_clicked_trims_the_names_but_keeps_the_password_verbatim(
        edupage_widget, dialogs):
    """Only subdomain and username are trimmed - a password may contain blanks."""
    edupage_widget.subdomain_input.setText("  skola  ")
    edupage_widget.username_input.setText("  ucitel@skola.cz ")
    edupage_widget.password_input.setText("  pass word  ")

    edupage_widget.on_load_clicked()

    name, args, _ = edupage_widget.coordinator_calls[0]
    assert name == "execute_mode1_single_phase"
    assert args == ("ucitel@skola.cz", "  pass word  ", "skola")


@pytest.mark.gui
@pytest.mark.parametrize("mode, phase, expected", [
    (1, 1, "execute_mode1_single_phase"),
    (1, 2, "execute_mode1_two_phase"),
    (2, 1, "execute_mode2_load"),
    (2, 2, "execute_mode2_load"),
], ids=["mode1-single", "mode1-two", "mode2-single", "mode2-two"])
def test_on_load_clicked_routes_to_the_selected_mode_and_phase(
        edupage_widget, dialogs, mode, phase, expected):
    """The radio buttons decide which coordinator entry point runs."""
    edupage_widget.subdomain_input.setText("skola")
    edupage_widget.username_input.setText("ucitel")
    edupage_widget.password_input.setText("pw")
    (edupage_widget.mode1_radio if mode == 1 else edupage_widget.mode2_radio).setChecked(True)
    (edupage_widget.single_phase_radio if phase == 1
     else edupage_widget.two_phase_radio).setChecked(True)

    edupage_widget.on_load_clicked()

    assert [c[0] for c in edupage_widget.coordinator_calls] == [expected]
    assert dialogs.calls == []


@pytest.mark.gui
def test_on_load_clicked_warns_when_no_mode_is_selected(edupage_widget, dialogs):
    """Without a checked mode the widget asks for one instead of guessing."""
    edupage_widget.mode_button_group.setExclusive(False)
    edupage_widget.mode1_radio.setChecked(False)
    edupage_widget.mode2_radio.setChecked(False)
    edupage_widget.subdomain_input.setText("skola")
    edupage_widget.username_input.setText("ucitel")
    edupage_widget.password_input.setText("pw")

    edupage_widget.on_load_clicked()

    assert dialogs.titles() == ["Invalid Mode"]
    assert edupage_widget.coordinator_calls == []


@pytest.mark.gui
def test_on_load_clicked_turns_a_coordinator_error_into_a_message_box(
        edupage_widget, dialogs):
    """A crash while starting is reported, not propagated out of the slot."""
    def explode(*args, **kwargs):
        raise RuntimeError("task factory broke")

    edupage_widget.coordinator.execute_mode1_single_phase = explode
    edupage_widget.subdomain_input.setText("skola")
    edupage_widget.username_input.setText("ucitel")
    edupage_widget.password_input.setText("pw")

    edupage_widget.on_load_clicked()

    assert dialogs.calls[-1][:2] == ("critical", "Error")
    assert "task factory broke" in dialogs.calls[-1][2]


@pytest.mark.gui
def test_on_loading_complete_reports_the_loaded_amounts(edupage_widget, dialogs,
                                                        make_source):
    """The success dialog names the source and counts classes and students."""
    source = make_source("EduPage-skola", [("6.A", 2), ("7.B", 1)])

    edupage_widget.on_loading_complete(source)

    kind, title, text = dialogs.calls[-1]
    assert (kind, title) == ("information", "Success")
    assert "EduPage-skola" in text
    assert "Classes: 2" in text
    assert "Students: 3" in text


@pytest.mark.gui
def test_on_loading_failed_shows_the_reason(edupage_widget, dialogs):
    """A failure signal is shown verbatim to the user."""
    edupage_widget.on_loading_failed("Login failed")

    assert dialogs.calls[-1][:2] == ("critical", "Loading Failed")
    assert "Login failed" in dialogs.calls[-1][2]


# ===========================================================================
# EduPage tasks - single phase
# ===========================================================================

def make_single_phase(selection=None):
    """Build a single-phase task that answers the class-selection request."""
    from utils.edupage_tasks import EduPageMode1SinglePhaseTask

    task = EduPageMode1SinglePhaseTask("ucitel", "pw", "skola")
    if selection is not None:
        task.class_selection_needed.connect(
            lambda names: task.set_selected_classes(list(selection)))
    return task


def test_single_phase_builds_one_person_per_student_with_edupage_metadata(edupage):
    """The happy path turns every filtered student into a Person."""
    edupage.add_class(10, "6.A")
    edupage.add_class(20, "7.B")
    edupage.add_student(1, 10, "novakj", "Jan Novák")
    edupage.add_student(2, 10, "dvorake", "Eva Dvořáková")
    edupage.add_student(3, 20, "cernyp", "Petr Černý")

    task = make_single_phase(["6.A"])
    result = task.execute()

    assert edupage.login_calls == [("ucitel", "pw", "skola")]
    assert list(task.result_data) == ["6.A"]
    people = task.result_data["6.A"]
    assert [(p.first_name, p.last_name) for p in people] == [
        ("Jan", "Novák"), ("Eva", "Dvořáková"),
    ]
    assert people[0].metadata == {"edupage_id": 1, "name_short": "novakj"}
    assert result == "Loaded 2 students from 1 classes"
    assert edupage.logouts == 1


@pytest.mark.parametrize("class_name", [
    "6.a", " 7.B ", "IX.C", "prima", "9,A", "Třída Ⅷ.b", "6 - A", "VIII.tr",
], ids=["lower", "padded", "roman", "word", "comma", "unicode", "spaced", "suffix"])
def test_single_phase_keeps_the_edupage_class_name_byte_for_byte(edupage, class_name):
    """Documented guarantee: EduPage class names are never normalised."""
    edupage.add_class(10, class_name)
    edupage.add_student(1, 10, "novakj", "Jan Novák")

    task = make_single_phase([class_name])
    task.execute()
    source = task.get_result_source("EduPage")

    assert list(task.result_data) == [class_name]
    assert [cls.name for cls in source.classes] == [class_name]
    assert source.classes[0].persons[0].class_name == class_name


def test_single_phase_keeps_the_edupage_class_order_not_the_selection_order(edupage):
    """Classes follow the order EduPage listed them in, not the user's clicks."""
    for index, name in enumerate(["6.A", "7.B", "8.C"]):
        edupage.add_class(10 + index, name)
        edupage.add_student(index, 10 + index, f"s{index}", f"First{index} Last{index}")

    task = make_single_phase(["8.C", "6.A", "7.B"])
    task.execute()

    assert [cls.name for cls in task.get_result_source("S").classes] == \
        ["6.A", "7.B", "8.C"]


def test_single_phase_falls_back_to_the_short_name_when_no_full_name_exists(edupage):
    """A student without a stored full name keeps the short name."""
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "novakj")          # no full name in the DBI

    task = make_single_phase(["6.A"])
    task.execute()

    person = task.result_data["6.A"][0]
    assert (person.first_name, person.last_name) == ("novakj", "")


def test_single_phase_puts_a_one_word_full_name_into_the_first_name_only(edupage):
    """A single-word name yields first_name=<name> and an empty last name."""
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "cher", "Cher")

    task = make_single_phase(["6.A"])
    task.execute()

    person = task.result_data["6.A"][0]
    assert person.first_name == "Cher"
    assert person.last_name == ""
    assert person.class_name == "6.A"


def test_single_phase_splits_a_three_word_name_after_the_first_space(edupage):
    """Everything after the first blank belongs to the last name."""
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "novakj", "Jan Karel Novák")

    task = make_single_phase(["6.A"])
    task.execute()

    person = task.result_data["6.A"][0]
    assert (person.first_name, person.last_name) == ("Jan", "Karel Novák")


def test_single_phase_skips_a_student_whose_name_lookup_fails(edupage):
    """One broken record must not lose the rest of the class."""
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "novakj", "Jan Novák")
    edupage.add_student(2, 10, "brokene", "Eva Rozbitá")
    edupage.name_errors.add(2)

    task = make_single_phase(["6.A"])
    logs = collect_logs(task)
    task.execute()

    assert [p.last_name for p in task.result_data["6.A"]] == ["Novák"]
    assert any(level is LogLevel.WARNING and "brokene" in message
               for message, level in logs)


def test_single_phase_ignores_students_of_classes_that_were_not_selected(edupage):
    """Filtering is by class id, and unselected classes stay out of the result."""
    edupage.add_class(10, "6.A")
    edupage.add_class(20, "7.B")
    edupage.add_student(1, 10, "a", "Jan Novák")
    edupage.add_student(2, 20, "b", "Eva Dvořáková")
    edupage.add_student(3, 99, "c", "Bez Třídy")

    task = make_single_phase(["7.B"])
    task.execute()

    assert list(task.result_data) == ["7.B"]
    assert [p.last_name for p in task.result_data["7.B"]] == ["Dvořáková"]


def test_single_phase_selecting_an_unknown_class_yields_an_empty_result(edupage):
    """A selection that matches nothing is not an error, just no data."""
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "a", "Jan Novák")

    task = make_single_phase(["does-not-exist"])
    result = task.execute()

    assert task.result_data == {}
    assert result == "Loaded 0 students from 0 classes"


@pytest.mark.parametrize("classes, students, message", [
    ([], [(1, 10, "a")], "No classes found"),
    ([(10, "6.A")], [], "No students found"),
], ids=["no-classes", "no-students"])
def test_single_phase_fails_loudly_when_edupage_returns_nothing(
        edupage, classes, students, message):
    """An empty school is reported as an error message, not as an empty source."""
    for class_id, name in classes:
        edupage.add_class(class_id, name)
    for person_id, class_id, short in students:
        edupage.add_student(person_id, class_id, short, "Jan Novák")

    task = make_single_phase(["6.A"])
    with pytest.raises(Exception) as excinfo:
        task.execute()
    assert message in str(excinfo.value)


def test_single_phase_reports_a_failed_login_as_an_error(edupage):
    """Wrong credentials end in a plain failure, not a cancellation."""
    edupage.bad_credentials = True
    task = make_single_phase(["6.A"])
    logs = collect_logs(task)

    with pytest.raises(Exception) as excinfo:
        task.execute()

    assert "Login failed" in str(excinfo.value)
    assert not isinstance(excinfo.value, TaskCancelledException)
    assert any("Invalid username or password" in message for message, _ in logs)


def test_single_phase_reports_a_login_that_never_completes_as_an_error(edupage):
    """``is_logged_in`` staying False is a login failure."""
    edupage.login_succeeds = False
    task = make_single_phase(["6.A"])

    with pytest.raises(Exception) as excinfo:
        task.execute()
    assert "Login failed" in str(excinfo.value)


def test_single_phase_cancelling_the_class_selection_cancels_the_task(edupage):
    """Closing the class dialog must end as 'cancelled', not as 'failed'."""
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "a", "Jan Novák")

    task = make_single_phase()               # nobody answers the request
    script_waiting_loop(task, [task.request_cancel])

    with pytest.raises(TaskCancelledException):
        task.execute()


def test_single_phase_waits_until_the_selection_arrives(edupage):
    """The loop spins until ``set_selected_classes`` is called."""
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "a", "Jan Novák")

    task = make_single_phase()
    state = script_waiting_loop(
        task, [lambda: None, lambda: None,
               lambda: task.set_selected_classes(["6.A"])])

    task.execute()

    assert state["turns"] == 3
    assert [p.last_name for p in task.result_data["6.A"]] == ["Novák"]


# ===========================================================================
# EduPage tasks - two factor authentication
# ===========================================================================

def test_2fa_code_is_handed_to_finish_with_code_and_completes_the_login(edupage):
    """The code the user typed is what finishes the second factor."""
    edupage.needs_2fa = True
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "a", "Jan Novák")

    task = make_single_phase(["6.A"])
    logged_in = []
    task.login_successful.connect(lambda: logged_in.append(True))
    task.twofa_code_needed.connect(lambda: task.set_2fa_code("123456"))
    script_waiting_loop(task, [])

    task.execute()

    assert edupage.codes == ["123456"]
    assert logged_in == [True]
    assert [p.last_name for p in task.result_data["6.A"]] == ["Novák"]


def test_a_rejected_2fa_code_fails_the_login(edupage):
    """``SecondFactorFailedException`` ends the run with a login failure."""
    edupage.needs_2fa = True
    edupage.code_accepted = False
    edupage.add_class(10, "6.A")

    task = make_single_phase(["6.A"])
    logs = collect_logs(task)
    task.twofa_code_needed.connect(lambda: task.set_2fa_code("000000"))

    with pytest.raises(Exception) as excinfo:
        task.execute()

    assert "Login failed" in str(excinfo.value)
    assert any("Invalid 2FA code" in message for message, _ in logs)


def test_cancelling_while_waiting_for_the_2fa_code_cancels_the_task(edupage):
    """Closing the 2FA dialog is a cancellation, so run() reports 'Cancelled'."""
    edupage.needs_2fa = True
    edupage.add_class(10, "6.A")

    task = make_single_phase(["6.A"])
    script_waiting_loop(task, [task.request_cancel])

    with pytest.raises(TaskCancelledException):
        task.execute()
    assert edupage.codes == []


@pytest.mark.bug
def test_cancelling_the_2fa_dialog_is_not_logged_as_a_login_error(edupage):
    """A cancellation is a user decision - it must not be logged as an ERROR."""
    edupage.needs_2fa = True
    edupage.add_class(10, "6.A")

    task = make_single_phase(["6.A"])
    logs = collect_logs(task)
    script_waiting_loop(task, [task.request_cancel])

    with pytest.raises(TaskCancelledException):
        task.execute()

    assert not [message for message, level in logs
                if level is LogLevel.ERROR and "Login error" in message]


# ===========================================================================
# EduPage tasks - logout, cleanup and Source building
# ===========================================================================

def test_logout_is_skipped_when_no_session_was_ever_opened(edupage):
    """Without an Edupage object there is nothing to log out from."""
    task = make_single_phase()
    task.cleanup()
    assert edupage.logouts == 0


def test_cleanup_logs_out_of_an_open_session(edupage):
    """``cleanup`` always closes the session."""
    from edupage_api import Edupage

    task = make_single_phase()
    task.edupage = Edupage()
    task.cleanup()

    assert edupage.logouts == 1


def test_logout_is_tolerated_when_the_api_has_no_logout_method(edupage):
    """Older API builds have no logout(); that is logged, not raised."""
    edupage.has_logout = False
    from edupage_api import Edupage

    task = make_single_phase()
    task.edupage = Edupage()
    logs = collect_logs(task)
    task.cleanup()

    assert any("Logout method not available" in message for message, _ in logs)


def test_a_failing_logout_is_only_a_warning(edupage):
    """A logout error must never mask the loaded data."""
    edupage.logout_error = RuntimeError("session already gone")
    from edupage_api import Edupage

    task = make_single_phase()
    task.edupage = Edupage()
    logs = collect_logs(task)
    task.cleanup()

    assert any(level is LogLevel.WARNING and "session already gone" in message
               for message, level in logs)


def test_get_result_source_marks_the_source_readonly_and_records_the_subdomain(edupage):
    """The built source is read-only, typed 'edupage' and knows its school."""
    task = make_single_phase()
    task.result_data = {"6.A": [Person("Jan", "Novák", "6.A")]}

    source = task.get_result_source("EduPage-skola")

    assert source.name == "EduPage-skola"
    assert source.source_type == "edupage"
    assert source.readonly is True
    assert source.get_source_info("subdomain") == "skola"


def test_get_result_source_without_any_data_yields_an_empty_source(edupage):
    """No loaded class means a source with no classes, not a crash."""
    task = make_single_phase()
    source = task.get_result_source("Empty")
    assert source.classes == []
    assert source.get_all_persons() == []


def test_get_result_source_keeps_empty_classes(edupage):
    """A selected class without students is still part of the result."""
    task = make_single_phase()
    task.result_data = {"6.A": [], "7.B": [Person("Jan", "Novák", "7.B")]}

    source = task.get_result_source("S")

    assert [(cls.name, len(cls.persons)) for cls in source.classes] == \
        [("6.A", 0), ("7.B", 1)]


def test_get_result_source_builds_a_fresh_source_on_every_call(edupage):
    """Calling it twice must not hand out the same containers."""
    task = make_single_phase()
    task.result_data = {"6.A": [Person("Jan", "Novák", "6.A")]}

    first = task.get_result_source("A")
    second = task.get_result_source("B")

    assert first is not second
    assert first.classes[0] is not second.classes[0]
    assert first.name == "A" and second.name == "B"


# ===========================================================================
# EduPage tasks - two phase
# ===========================================================================

def make_two_phase(selection=None):
    """Build a two-phase task for the requested phase."""
    from utils.edupage_tasks import EduPageMode1TwoPhaseTask

    return EduPageMode1TwoPhaseTask("ucitel", "pw", "skola",
                                    selected_class_names=selection)


def test_get_class_names_is_empty_before_the_first_phase_ran(edupage):
    """Nothing was fetched yet, so there is nothing to offer."""
    assert make_two_phase().get_class_names() == []


def test_phase1_returns_the_class_names_verbatim_and_in_server_order(edupage):
    """Phase 1 hands the raw EduPage names to the selection dialog."""
    for index, name in enumerate([" 9.C ", "prima", "6.a"]):
        edupage.add_class(index, name)

    task = make_two_phase()
    message = task.execute()

    assert task.get_class_names() == [" 9.C ", "prima", "6.a"]
    assert message == "Found 3 classes"
    assert edupage.logouts == 1


def test_phase1_fails_when_the_school_has_no_classes(edupage):
    """An empty class list is an error the coordinator must report."""
    task = make_two_phase()
    with pytest.raises(Exception) as excinfo:
        task.execute()
    assert "No classes found" in str(excinfo.value)


def test_phase2_loads_only_the_selected_classes(edupage):
    """With a selection the task logs in again and loads those classes."""
    edupage.add_class(10, "6.A")
    edupage.add_class(20, "7.B")
    edupage.add_student(1, 10, "a", "Jan Novák")
    edupage.add_student(2, 20, "b", "Eva Dvořáková")

    task = make_two_phase(["7.B"])
    result = task.execute()

    assert list(task.result_data) == ["7.B"]
    assert [p.first_name for p in task.result_data["7.B"]] == ["Eva"]
    assert result == "Loaded 1 students from 1 classes"
    source = task.get_result_source("Zdroj")
    assert [cls.name for cls in source.classes] == ["7.B"]


def test_phase2_with_an_empty_selection_loads_nothing_but_still_succeeds(edupage):
    """``selected_class_names=[]`` routes to phase 2, which finds no class."""
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "a", "Jan Novák")

    task = make_two_phase([])
    result = task.execute()

    assert task.result_data == {}
    assert result == "Loaded 0 students from 0 classes"


def test_phase2_keeps_class_names_verbatim(edupage):
    """The verbatim guarantee holds for the two-phase task as well."""
    edupage.add_class(10, "  IX. b  ")
    edupage.add_student(1, 10, "a", "Žofie Škrháková")

    task = make_two_phase(["  IX. b  "])
    task.execute()

    source = task.get_result_source("S")
    assert [cls.name for cls in source.classes] == ["  IX. b  "]
    assert source.classes[0].persons[0].class_name == "  IX. b  "


def test_phase2_single_word_names_end_up_in_the_first_name(edupage):
    """Same name splitting rule as the single-phase task."""
    edupage.add_class(10, "6.A")
    edupage.add_student(1, 10, "cher", "Cher")

    task = make_two_phase(["6.A"])
    task.execute()

    person = task.result_data["6.A"][0]
    assert (person.first_name, person.last_name) == ("Cher", "")


def test_phase2_cancels_instead_of_failing_when_the_user_stops_during_2fa(edupage):
    """Cancelling the second login is still a cancellation."""
    edupage.needs_2fa = True
    edupage.add_class(10, "6.A")

    task = make_two_phase(["6.A"])
    script_waiting_loop(task, [task.request_cancel])

    with pytest.raises(TaskCancelledException):
        task.execute()


# ===========================================================================
# EduPage tasks - mode 2 placeholder
# ===========================================================================

def test_mode2_execute_announces_that_it_is_not_implemented(edupage):
    """Mode 2 raises NotImplementedError with a helpful message."""
    from utils.edupage_tasks import EduPageMode2Task

    task = EduPageMode2Task("u", "p", "skola")
    with pytest.raises(NotImplementedError) as excinfo:
        task.execute()
    assert "not yet implemented" in str(excinfo.value)


def test_mode2_get_result_source_still_returns_a_usable_empty_source(edupage):
    """Even the placeholder builds a correctly tagged source."""
    from utils.edupage_tasks import EduPageMode2Task

    source = EduPageMode2Task("u", "p", "skola").get_result_source("X")

    assert (source.name, source.source_type, source.readonly) == ("X", "edupage", True)
    assert source.get_source_info("subdomain") == "skola"
    assert source.classes == []


# ===========================================================================
# EduPageLoadCoordinator - cancellation bookkeeping
# ===========================================================================

@pytest.mark.parametrize("dialog, task, expected", [
    (None, None, False),
    (StubDialog(cancelled=True), None, True),
    (StubDialog(cancelled=False), None, False),
    (None, StubTask(cancelled=True), True),
    (None, StubTask(cancelled=False), False),
    (StubDialog(cancelled=False), StubTask(cancelled=True), True),
    (StubDialog(error=RuntimeError("deleted")), StubTask(cancelled=True), True),
    (StubDialog(error=AttributeError("gone")), StubTask(cancelled=False), False),
], ids=["nothing", "dialog-cancelled", "dialog-running", "task-cancelled",
        "task-running", "task-wins", "dead-dialog", "dead-dialog-running"])
def test_was_cancelled_asks_the_dialog_first_and_survives_dead_objects(
        coordinator, dialog, task, expected):
    """The cancellation query never raises, whatever state the objects are in."""
    coordinator.current_dialog = dialog
    coordinator.current_task = task

    assert coordinator._was_cancelled() is expected


def test_was_cancelled_is_false_when_the_task_object_is_already_destroyed(coordinator):
    """A deleted C++ task cannot be cancelled - it must not blow up either."""
    class DeadTask:
        def is_cancelled(self):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    coordinator.current_dialog = None
    coordinator.current_task = DeadTask()

    assert coordinator._was_cancelled() is False


def test_report_failure_emits_loading_failed_for_a_real_error(coordinator):
    """A genuine failure reaches the widget."""
    received = []
    coordinator.loading_failed.connect(received.append)

    assert coordinator._report_failure("Login failed") is True
    assert received == ["Login failed"]


def test_report_failure_stays_silent_after_a_cancellation(coordinator):
    """The user already knows they cancelled - no error window on top."""
    coordinator.current_dialog = StubDialog(cancelled=True)
    received = []
    coordinator.loading_failed.connect(received.append)

    assert coordinator._report_failure("Cancelled by user") is False
    assert received == []


# ===========================================================================
# EduPageLoadCoordinator - source naming
# ===========================================================================

def test_ask_for_source_name_returns_the_trimmed_answer(coordinator, dialogs,
                                                        monkeypatch):
    """Blanks around the typed name are removed."""
    calls = queue_text_answers(monkeypatch, [("  EduPage-skola  ", True)])

    assert coordinator._ask_for_source_name("EduPage-skola") == "EduPage-skola"
    assert calls[0][4] == "EduPage-skola"          # offered as the default


@pytest.mark.parametrize("answer", [("EduPage", False), ("", True)],
                         ids=["cancelled", "cleared"])
def test_ask_for_source_name_returns_none_when_the_dialog_is_dismissed(
        coordinator, dialogs, monkeypatch, answer):
    """Cancelling the name dialog aborts the whole import."""
    queue_text_answers(monkeypatch, [answer])
    assert coordinator._ask_for_source_name("EduPage") is None


def test_ask_for_source_name_rejects_a_blank_name_and_asks_again(coordinator, dialogs,
                                                                 monkeypatch):
    """A name of blanks is refused with a warning and the dialog reopens."""
    calls = queue_text_answers(monkeypatch, [("   ", True), ("Dobré jméno", True)])

    assert coordinator._ask_for_source_name("EduPage") == "Dobré jméno"
    assert len(calls) == 2
    assert dialogs.saw("Source name cannot be empty")


def test_ask_for_source_name_accepts_a_duplicate_after_confirmation(
        coordinator, dialogs, monkeypatch, make_source):
    """Answering 'Yes' to the duplicate question keeps the typed name."""
    coordinator.source_manager.add_source(make_source("EduPage", []))
    dialogs.question_answer = QMessageBox.StandardButton.Yes
    queue_text_answers(monkeypatch, [("EduPage", True)])

    assert coordinator._ask_for_source_name("EduPage") == "EduPage"
    assert dialogs.saw("already exists")


def test_ask_for_source_name_asks_again_when_the_duplicate_is_declined(
        coordinator, dialogs, monkeypatch, make_source):
    """Answering 'No' reopens the dialog instead of returning the name."""
    coordinator.source_manager.add_source(make_source("EduPage", []))
    dialogs.question_answer = QMessageBox.StandardButton.No
    calls = queue_text_answers(monkeypatch, [("EduPage", True), ("EduPage 2", True)])

    assert coordinator._ask_for_source_name("EduPage") == "EduPage 2"
    assert len(calls) == 2


# ===========================================================================
# EduPageLoadCoordinator - class selection
# ===========================================================================

class FakeSelectionDialog:
    """Stand-in for ClassSelectionDialog with a scripted outcome."""

    instances = []

    accepted = True
    selection = ["6.A"]

    def __init__(self, class_names, parent=None):
        self.class_names = list(class_names)
        self.selected_classes = list(type(self).selection)
        type(self).instances.append(self)

    def exec(self):
        return (QDialog.DialogCode.Accepted if type(self).accepted
                else QDialog.DialogCode.Rejected)


@pytest.fixture
def fake_selection_dialog(monkeypatch):
    """Replace ClassSelectionDialog so no real modal window is built."""
    import utils.edupage_coordinator as ec

    class Dialog(FakeSelectionDialog):
        instances = []
        accepted = True
        selection = ["6.A"]

    monkeypatch.setattr(ec, "ClassSelectionDialog", Dialog)
    return Dialog


def test_handle_class_selection_forwards_the_choice_to_the_task(
        coordinator, fake_selection_dialog):
    """An accepted selection is handed to the waiting task unchanged."""
    fake_selection_dialog.selection = ["7.B", "6.A"]
    task = StubTask()
    coordinator.current_task = task

    coordinator._handle_class_selection(["6.A", "7.B", "8.C"])

    assert task.selected_classes == ["7.B", "6.A"]
    assert task.cancel_calls == 0
    assert fake_selection_dialog.instances[0].class_names == ["6.A", "7.B", "8.C"]


def test_handle_class_selection_cancels_the_task_when_the_dialog_is_rejected(
        coordinator, fake_selection_dialog):
    """Closing the dialog stops the waiting task instead of leaving it hanging."""
    fake_selection_dialog.accepted = False
    task = StubTask()
    coordinator.current_task = task

    coordinator._handle_class_selection(["6.A"])

    assert task.cancel_calls == 1
    assert task.selected_classes is None


def test_handle_class_selection_cancels_the_task_on_an_empty_selection(
        coordinator, fake_selection_dialog):
    """Accepting without a single class also cancels - there is nothing to load."""
    fake_selection_dialog.selection = []
    task = StubTask()
    coordinator.current_task = task

    coordinator._handle_class_selection(["6.A"])

    assert task.cancel_calls == 1
    assert task.selected_classes is None


@pytest.mark.gui
def test_class_selection_dialog_lists_the_classes_sorted(qapp):
    """The real dialog sorts the names so long lists stay findable."""
    from utils.edupage_coordinator import ClassSelectionDialog

    dialog = ClassSelectionDialog(["9.C", "6.A", "7.B"])
    try:
        shown = [dialog.class_list.item(i).text()
                 for i in range(dialog.class_list.count())]
        assert shown == ["6.A", "7.B", "9.C"]
    finally:
        dialog.deleteLater()


@pytest.mark.gui
def test_class_selection_dialog_returns_every_selected_class(qapp, dialogs):
    """Select all, confirm - every class comes back."""
    from utils.edupage_coordinator import ClassSelectionDialog

    dialog = ClassSelectionDialog(["9.C", "6.A"])
    try:
        dialog.select_all()
        dialog.on_ok()
        assert sorted(dialog.selected_classes) == ["6.A", "9.C"]
        assert dialog.result() == int(QDialog.DialogCode.Accepted)
    finally:
        dialog.deleteLater()


@pytest.mark.gui
def test_class_selection_dialog_refuses_to_close_without_a_selection(qapp, dialogs):
    """Confirming an empty selection warns and keeps the dialog open."""
    from utils.edupage_coordinator import ClassSelectionDialog

    dialog = ClassSelectionDialog(["6.A", "7.B"])
    try:
        dialog.select_all()
        dialog.select_none()
        dialog.on_ok()
        assert dialog.selected_classes == []
        assert dialogs.titles() == ["No Selection"]
    finally:
        dialog.deleteLater()


# ===========================================================================
# EduPageLoadCoordinator - two factor dialog handling
# ===========================================================================

class FakeTwoFADialog:
    """Stand-in for TwoFADialog with a scripted outcome."""

    accepted = True
    code = "123456"

    def __init__(self, parent=None):
        self.code = type(self).code
        self.resend_requested = _FakeSignal()
        self.resend_results = []
        type(self).instance = self

    def exec(self):
        return (QDialog.DialogCode.Accepted if type(self).accepted
                else QDialog.DialogCode.Rejected)

    def on_resend_complete(self, success):
        self.resend_results.append(success)


class _FakeSignal:
    """The two methods the coordinator uses on a pyqtSignal."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


@pytest.fixture
def fake_2fa_dialog(monkeypatch):
    """Replace TwoFADialog so nothing modal is ever shown."""
    import utils.edupage_coordinator as ec

    class Dialog(FakeTwoFADialog):
        accepted = True
        code = "123456"

    monkeypatch.setattr(ec, "TwoFADialog", Dialog)
    return Dialog


def test_handle_2fa_request_gives_the_typed_code_to_the_task(coordinator,
                                                             fake_2fa_dialog):
    """The code travels from the dialog into the waiting task."""
    task = StubTask()
    coordinator.current_task = task

    coordinator._handle_2fa_code_request()

    assert task.code == "123456"
    assert task.cancel_calls == 0
    assert coordinator.current_2fa_dialog is None


def test_handle_2fa_request_cancels_the_task_when_the_dialog_is_rejected(
        coordinator, fake_2fa_dialog):
    """Closing the 2FA dialog stops the task instead of waiting forever."""
    fake_2fa_dialog.accepted = False
    task = StubTask()
    coordinator.current_task = task

    coordinator._handle_2fa_code_request()

    assert task.code is None
    assert task.cancel_calls == 1


def test_2fa_resend_asks_the_login_object_and_reports_success(coordinator,
                                                              fake_2fa_dialog):
    """A successful resend is confirmed in the dialog."""
    class Login:
        def __init__(self):
            self.calls = 0

        def resend_notifications(self):
            self.calls += 1

    task = StubTask()
    task.twofa_login = Login()
    coordinator.current_task = task
    coordinator._handle_2fa_code_request()          # creates the dialog instance
    dialog = fake_2fa_dialog.instance
    coordinator.current_2fa_dialog = dialog

    coordinator._handle_2fa_resend()

    assert task.twofa_login.calls == 1
    assert dialog.resend_results == [True]


def test_2fa_resend_reports_failure_when_there_is_no_login_object(coordinator,
                                                                  fake_2fa_dialog):
    """Without a TwoFactorLogin the dialog is told the resend failed."""
    coordinator.current_task = StubTask()
    coordinator._handle_2fa_code_request()
    dialog = fake_2fa_dialog.instance
    coordinator.current_2fa_dialog = dialog

    coordinator._handle_2fa_resend()

    assert dialog.resend_results == [False]


def test_2fa_resend_reports_failure_when_the_api_raises(coordinator, fake_2fa_dialog):
    """An exception from the API is swallowed and shown as a failed resend."""
    class Login:
        def resend_notifications(self):
            raise RuntimeError("rate limited")

    task = StubTask()
    task.twofa_login = Login()
    coordinator.current_task = task
    coordinator._handle_2fa_code_request()
    dialog = fake_2fa_dialog.instance
    coordinator.current_2fa_dialog = dialog

    coordinator._handle_2fa_resend()

    assert dialog.resend_results == [False]


@pytest.mark.gui
def test_twofa_dialog_refuses_an_empty_code(qapp, dialogs):
    """Verifying without a code warns instead of accepting."""
    from utils.edupage_coordinator import TwoFADialog

    dialog = TwoFADialog()
    try:
        dialog.code_input.setText("   ")
        dialog.on_verify()
        assert dialog.code is None
        assert dialogs.titles() == ["Invalid Code"]
    finally:
        dialog.deleteLater()


@pytest.mark.gui
def test_twofa_dialog_trims_and_keeps_the_entered_code(qapp, dialogs):
    """A typed code is stored trimmed and accepts the dialog."""
    from utils.edupage_coordinator import TwoFADialog

    dialog = TwoFADialog()
    try:
        dialog.code_input.setText(" 12345 ")
        dialog.on_verify()
        assert dialog.code == "12345"
        assert dialog.result() == int(QDialog.DialogCode.Accepted)
    finally:
        dialog.deleteLater()


# ===========================================================================
# EduPageLoadCoordinator - finishing a run
# ===========================================================================

def test_single_phase_finish_reports_a_failure(coordinator, dialogs, monkeypatch):
    """A failed task is reported through loading_failed."""
    failures = []
    coordinator.loading_failed.connect(failures.append)
    coordinator.current_task = StubTask()

    coordinator._on_single_phase_finished(False, "Error: Login failed", "skola")

    assert failures == ["Error: Login failed"]
    assert coordinator.source_manager.sources == []


def test_single_phase_finish_stays_silent_after_a_cancellation(coordinator, dialogs):
    """A cancelled run shows no error window."""
    failures = []
    coordinator.loading_failed.connect(failures.append)
    coordinator.current_task = StubTask(cancelled=True)

    coordinator._on_single_phase_finished(False, "Cancelled by user", "skola")

    assert failures == []


def test_single_phase_finish_adds_the_source_and_announces_it(coordinator, dialogs,
                                                              monkeypatch):
    """The happy path stores the source and emits loading_complete once."""
    completed = []
    coordinator.loading_complete.connect(completed.append)
    task = StubTask(source=Source(name="tmp", source_type="edupage", readonly=True))
    coordinator.current_task = task
    calls = queue_text_answers(monkeypatch, [("Moje EduPage", True)])

    coordinator._on_single_phase_finished(True, "Loaded 3 students", "skola")

    assert calls[0][4] == "EduPage-skola"           # default offered
    assert task.result_names == ["Moje EduPage"]
    assert coordinator.source_manager.get_source_names() == ["Moje EduPage"]
    assert len(completed) == 1
    assert completed[0] is coordinator.source_manager.sources[0]


def test_single_phase_finish_adds_nothing_when_the_name_is_cancelled(
        coordinator, dialogs, monkeypatch):
    """No name, no source - and no signal either."""
    completed, failures = [], []
    coordinator.loading_complete.connect(completed.append)
    coordinator.loading_failed.connect(failures.append)
    task = StubTask()
    coordinator.current_task = task
    queue_text_answers(monkeypatch, [("Whatever", False)])

    coordinator._on_single_phase_finished(True, "done", "skola")

    assert task.result_names == []
    assert coordinator.source_manager.sources == []
    assert completed == [] and failures == []


def test_single_phase_finish_reports_a_broken_result_source(coordinator, dialogs,
                                                            monkeypatch):
    """If the source cannot be built the user is told why, and nothing is added."""
    completed, failures = [], []
    coordinator.loading_complete.connect(completed.append)
    coordinator.loading_failed.connect(failures.append)
    coordinator.current_task = StubTask(error=KeyError("class_id"))
    queue_text_answers(monkeypatch, [("Moje EduPage", True)])

    coordinator._on_single_phase_finished(True, "done", "skola")

    assert len(failures) == 1
    assert "Could not build the source" in failures[0]
    assert "class_id" in failures[0]
    assert coordinator.source_manager.sources == []
    assert completed == []


def test_phase1_finish_reports_a_school_without_classes(coordinator, dialogs,
                                                        fake_selection_dialog):
    """No class names - warn the user and report the failure."""
    class EmptyTask(StubTask):
        def get_class_names(self):
            return []

    failures = []
    coordinator.loading_failed.connect(failures.append)
    coordinator.current_task = EmptyTask()

    coordinator._on_mode1_phase1_finished(True, "Found 0 classes")

    assert dialogs.titles() == ["No Classes"]
    assert failures == ["No classes found"]
    assert fake_selection_dialog.instances == []


def test_phase1_finish_starts_phase2_with_the_selected_classes(
        coordinator, dialogs, fake_selection_dialog):
    """An accepted selection moves the workflow on to phase 2."""
    class Task(StubTask):
        def get_class_names(self):
            return ["6.A", "7.B"]

    fake_selection_dialog.selection = ["7.B"]
    coordinator.current_task = Task()
    started = []
    coordinator._execute_mode1_phase2 = started.append

    coordinator._on_mode1_phase1_finished(True, "Found 2 classes")

    assert started == [["7.B"]]


@pytest.mark.parametrize("accepted, selection", [(False, ["6.A"]), (True, [])],
                         ids=["dialog-rejected", "nothing-selected"])
def test_phase1_finish_stops_quietly_when_no_class_is_chosen(
        coordinator, dialogs, fake_selection_dialog, accepted, selection):
    """Cancelling the selection ends the workflow without an error window."""
    class Task(StubTask):
        def get_class_names(self):
            return ["6.A"]

    fake_selection_dialog.accepted = accepted
    fake_selection_dialog.selection = selection
    coordinator.current_task = Task()
    started, failures = [], []
    coordinator._execute_mode1_phase2 = started.append
    coordinator.loading_failed.connect(failures.append)

    coordinator._on_mode1_phase1_finished(True, "Found 1 classes")

    assert started == []
    assert failures == []


def test_phase1_finish_reports_a_failed_first_phase(coordinator, dialogs):
    """A failed phase 1 is reported like any other failure."""
    failures = []
    coordinator.loading_failed.connect(failures.append)
    coordinator.current_task = StubTask()

    coordinator._on_mode1_phase1_finished(False, "Error: Login failed")

    assert failures == ["Error: Login failed"]


def test_phase2_finish_uses_the_cached_subdomain_for_the_default_name(
        coordinator, dialogs, monkeypatch):
    """Phase 2 offers 'EduPage-<subdomain>' from the cached credentials."""
    coordinator.cached_subdomain = "gymnazium"
    coordinator.current_task = StubTask()
    completed = []
    coordinator.loading_complete.connect(completed.append)
    calls = queue_text_answers(monkeypatch, [("EduPage-gymnazium", True)])

    coordinator._on_mode1_phase2_finished(True, "Loaded 10 students")

    assert calls[0][4] == "EduPage-gymnazium"
    assert coordinator.source_manager.get_source_names() == ["EduPage-gymnazium"]
    assert len(completed) == 1


def test_phase2_finish_suppresses_the_error_after_a_cancellation(coordinator, dialogs):
    """Cancelling during phase 2 shows no failure dialog."""
    failures = []
    coordinator.loading_failed.connect(failures.append)
    coordinator.current_dialog = StubDialog(cancelled=True)
    coordinator.current_task = StubTask()

    coordinator._on_mode1_phase2_finished(False, "Cancelled by user")

    assert failures == []


def test_mode2_load_only_explains_that_it_is_not_implemented(coordinator, dialogs):
    """Mode 2 shows an information dialog and starts nothing."""
    coordinator.execute_mode2_load("u", "p", "skola")

    assert dialogs.kinds() == ["information"]
    assert "not yet implemented" in dialogs.texts()[0]
    assert coordinator.current_task is None
    assert coordinator.source_manager.sources == []


# ===========================================================================
# ActiveDirectorySourceWidget
# ===========================================================================

class ADDirectory:
    """A tiny in-memory directory served by ldap3's MOCK_SYNC strategy."""

    base_dn = "dc=example,dc=com"
    admin_dn = "cn=admin,dc=example,dc=com"
    admin_password = "secret-password"

    def __init__(self):
        self.entries = [
            (self.admin_dn, {"objectClass": ["top", "person"],
                             "sn": "admin", "userPassword": self.admin_password}),
        ]
        self.searches = []

    def add_ou(self, name):
        dn = f"ou={name},{self.base_dn}"
        self.entries.append((dn, {
            "objectClass": ["top", "organizationalUnit"],
            "ou": name,
            # A real directory returns distinguishedName when it is requested.
            "distinguishedName": dn,
        }))
        return dn

    def add_user(self, ou_name, cn, **attributes):
        dn = f"cn={cn},ou={ou_name},{self.base_dn}"
        entry = {"objectClass": ["top", "person", "user"], "distinguishedName": dn}
        entry.update({key: value for key, value in attributes.items()
                      if value is not None})
        self.entries.append((dn, entry))
        return dn


@pytest.fixture
def ad_directory(monkeypatch):
    """
    Serve ``sources.ad_source`` from ldap3's in-memory mock server.

    The production code imports ldap3 inside ``on_load``, so patching the
    module attributes is enough; MOCK_SYNC never opens a socket.
    """
    import ldap3

    directory = ADDirectory()
    real_server, real_connection = ldap3.Server, ldap3.Connection

    def fake_server(name, get_info=None):
        directory.server_name = name
        return real_server(name, get_info=ldap3.OFFLINE_AD_2012_R2)

    def fake_connection(server, user=None, password=None, auto_bind=False, **kwargs):
        conn = real_connection(server, user=user, password=password,
                               client_strategy=ldap3.MOCK_SYNC)
        for dn, attributes in directory.entries:
            conn.strategy.add_entry(dn, dict(attributes))
        if auto_bind and not conn.bind():
            raise ldap3.core.exceptions.LDAPBindError("invalid credentials")

        real_search = conn.search

        def spying_search(**kwargs):
            directory.searches.append(kwargs)
            return real_search(**kwargs)

        conn.search = spying_search
        return conn

    monkeypatch.setattr(ldap3, "Server", fake_server)
    monkeypatch.setattr(ldap3, "Connection", fake_connection)
    return directory


@pytest.fixture
def ad_widget(qapp, source_manager):
    """An AD widget filled with credentials the mock directory accepts."""
    from sources.ad_source import ActiveDirectorySourceWidget

    widget = ActiveDirectorySourceWidget(source_manager)
    widget.server_input.setText("ldap://dc.example.com")
    widget.base_dn_input.setText(ADDirectory.base_dn)
    widget.username_input.setText(ADDirectory.admin_dn)
    widget.password_input.setText(ADDirectory.admin_password)
    yield widget
    widget.deleteLater()


@pytest.mark.gui
@pytest.mark.parametrize("field", ["server_input", "base_dn_input",
                                   "username_input", "password_input"])
def test_ad_load_requires_every_field(ad_widget, dialogs, field):
    """Any empty field stops the run before ldap3 is even imported."""
    getattr(ad_widget, field).setText("")

    ad_widget.on_load()

    assert dialogs.titles() == ["Missing Information"]
    assert ad_widget.source_manager.sources == []


@pytest.mark.gui
def test_ad_load_warns_when_no_class_ou_matches(ad_widget, ad_directory, dialogs):
    """A directory without Trida-* units produces a warning, not an empty source."""
    ad_directory.add_ou("Ucitele")

    ad_widget.on_load()

    assert dialogs.titles()[-1] == "No Data"
    assert ad_widget.source_manager.sources == []


@pytest.mark.gui
def test_ad_load_strips_the_trida_prefix_from_the_class_name(ad_widget, ad_directory,
                                                             dialogs):
    """'Trida-6A' becomes the class '6A'."""
    ad_directory.add_ou("Trida-6A")

    ad_widget.on_load()

    source = ad_widget.source_manager.sources[0]
    assert [cls.name for cls in source.classes] == ["6A"]
    assert source.readonly is True
    assert source.source_type == "active_directory"
    assert source.name == "AD-ldap://dc.example.com"


@pytest.mark.gui
@pytest.mark.bug
def test_ad_load_skips_accounts_without_a_first_or_last_name(ad_widget, ad_directory,
                                                             dialogs):
    """A user without givenName or sn is not a student record."""
    ad_directory.add_ou("Trida-6A")
    ad_directory.add_user("Trida-6A", "nogiven", sn="Novák")
    ad_directory.add_user("Trida-6A", "nosn", givenName="Jan")

    ad_widget.on_load()

    assert ad_widget.source_manager.sources, "the class OU itself must still load"
    source = ad_widget.source_manager.sources[0]
    assert [cls.name for cls in source.classes] == ["6A"]
    assert source.get_all_persons() == []


@pytest.mark.gui
def test_ad_load_reports_a_rejected_bind(ad_widget, ad_directory, dialogs):
    """Wrong credentials end in the connection error dialog."""
    ad_widget.password_input.setText("wrong-password")

    ad_widget.on_load()

    assert dialogs.calls[-1][0] == "critical"
    assert "Failed to connect" in dialogs.calls[-1][2]
    assert ad_widget.source_manager.sources == []


@pytest.mark.gui
@pytest.mark.bug
def test_ad_load_imports_the_students_of_every_class_ou(ad_widget, ad_directory,
                                                        dialogs):
    """Students of every Trida-* unit become persons of that class."""
    ad_directory.add_ou("Trida-6A")
    ad_directory.add_user("Trida-6A", "novakjan", givenName="Jan", sn="Novák",
                          sAMAccountName="novakjan", displayName="Jan Novák",
                          mail="jan@skola.cz")
    ad_directory.add_ou("Trida-7B")
    ad_directory.add_user("Trida-7B", "dvorakeva", givenName="Eva", sn="Dvořáková",
                          sAMAccountName="dvorakeva")

    ad_widget.on_load()

    source = ad_widget.source_manager.sources[0]
    by_class = {cls.name: cls for cls in source.classes}
    assert sorted(by_class) == ["6A", "7B"]
    jan = by_class["6A"].persons[0]
    assert (jan.first_name, jan.last_name) == ("Jan", "Novák")
    assert jan.ad_username == "novakjan"
    assert jan.ad_email == "jan@skola.cz"
    assert jan.metadata["ad_dn"] == f"cn=novakjan,ou=Trida-6A,{ADDirectory.base_dn}"


@pytest.mark.gui
@pytest.mark.bug
def test_ad_load_accepts_a_class_ou_written_in_lower_case(ad_widget, ad_directory,
                                                          dialogs):
    """LDAP matches 'trida-7b' case-insensitively, so the class must be loaded."""
    ad_directory.add_ou("trida-7b")

    ad_widget.on_load()

    assert ad_widget.source_manager.sources, "the lower-case class OU was dropped"
    assert [cls.name for cls in ad_widget.source_manager.sources[0].classes] == ["7b"]


@pytest.mark.gui
@pytest.mark.bug
def test_ad_load_honours_the_selected_search_format(ad_widget, ad_directory, dialogs):
    """The chosen 'Search Format' must influence the OU search filter."""
    ad_directory.add_ou("Trida-6A")

    ad_widget.format_combo.setCurrentIndex(
        ad_widget.format_combo.findData("uppercase"))
    ad_widget.on_load()
    uppercase_filter = ad_directory.searches[0]["search_filter"]

    ad_directory.searches.clear()
    ad_widget.format_combo.setCurrentIndex(
        ad_widget.format_combo.findData("lowercase"))
    ad_widget.on_load()
    lowercase_filter = ad_directory.searches[0]["search_filter"]

    assert uppercase_filter != lowercase_filter


@pytest.mark.gui
@pytest.mark.bug
def test_ad_load_reports_a_missing_ldap3_package(ad_widget, dialogs, monkeypatch):
    """Without ldap3 the user gets a message - not a NameError."""
    monkeypatch.setitem(sys.modules, "ldap3", None)

    ad_widget.on_load()

    assert dialogs.calls[-1][0] == "critical"
    assert "ldap3" in dialogs.calls[-1][2]
