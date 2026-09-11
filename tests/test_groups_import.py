"""
Unit tests for the group management / import-export corner of the UI:

* :mod:`ui.import_dialog`               - method combo, availability, validation
* :mod:`ui.export_dialog`               - destination/password validation
* :mod:`ui.group_management_dialog`     - group list, templates, import handling
* :mod:`ui.duplicate_comparison_dialog` - per-duplicate resolutions
* :mod:`utils.group_tasks`              - encrypted export/import round trips

Design rules of this module
---------------------------
* **Nothing may block.**  ``QMessageBox`` and the ``QInputDialog`` /
  ``QFileDialog`` statics are neutralised by the shared ``dialogs`` fixture;
  ``QDialog.exec`` is replaced by the local ``dialog_driver`` fixture, which
  dispatches to a per-class handler and *rejects* every dialog nobody
  registered a handler for.  A forgotten stub therefore closes the dialog
  instead of hanging the suite.
* **Tasks run on the main thread.**  ``AbstractProgressTask`` is a ``QThread``;
  the tests call ``execute()`` directly so every signal is delivered
  synchronously and no worker thread can outlive a test.
* **Real crypto, throw-away files.**  The round-trip tests encrypt into
  ``tmp_path`` with the real AES-GCM implementation (~50 ms per file); nothing
  is written outside it and no network is touched.

Tests marked ``bug`` + ``xfail`` assert the *correct* behaviour and document a
defect in the production code.
"""

import json

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QTextEdit,
)

import ui.export_dialog as export_dialog_module
import ui.import_dialog as import_dialog_module
from models import ADGroup, GroupTemplate, VerificationStatus
from ui.duplicate_comparison_dialog import (
    DuplicateGroupComparisonDialog,
    DuplicateResolution,
    DuplicateTemplateComparisonDialog,
)
from ui.export_dialog import ExportDialog
from ui.group_management_dialog import GroupManagementDialog
from ui.import_dialog import ImportDialog
from utils.encryption import (
    encrypt_file_aes_gcm, encrypt_file_with_format, get_available_methods,
)
from utils.group_tasks import (
    GroupExportTask, GroupImportTask, TemplateExportTask, TemplateImportTask,
)
from utils.progress_tasks import TaskCancelledException

PASSWORD = "Tajné-Heslo-2024"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def group(name="Studenti", dn=None, **kwargs):
    """Build an ADGroup; the DN defaults to ``CN=<name>,OU=Groups,DC=skola,DC=local``."""
    return ADGroup(name=name,
                   dn=dn or f"CN={name},OU=Groups,DC=skola,DC=local",
                   **kwargs)


def template(name="Šablona", groups=None, **kwargs):
    """Build a GroupTemplate with the given groups."""
    return GroupTemplate(name=name, groups=list(groups or []), **kwargs)


def write_usrx(path, payload, password=PASSWORD, method="aes-gcm"):
    """Encrypt *payload* (str or JSON-able object) into a USRX container."""
    data = payload if isinstance(payload, str) else json.dumps(payload,
                                                               ensure_ascii=False)
    encrypt_file_with_format(data=data, output_path=str(path), password=password,
                             encryption_method=method, file_format="usrx",
                             metadata={"content_type": "test"})
    return str(path)


def attach(task):
    """Connect recorders to a task's signals; returns ``(progress, logs)``."""
    progress, logs = [], []
    task.progress_changed.connect(lambda value, message: progress.append((value, message)))
    task.log_message.connect(lambda message, level, stamp: logs.append((level, message)))
    return progress, logs


def select_rows(list_widget, *rows):
    """Select the given rows of a QListWidget and return the selected items."""
    list_widget.clearSelection()
    for row in rows:
        list_widget.item(row).setSelected(True)
    return list_widget.selectedItems()


class TemplateForm:
    """Accessor for the widgets of the inline *Create Template* dialog."""

    def __init__(self, dialog):
        self.dialog = dialog
        self.name = dialog.findChildren(QLineEdit)[0]
        texts = dialog.findChildren(QTextEdit)
        self.description, self.validation = texts[0], texts[1]
        self.groups = dialog.findChildren(QListWidget)[0]
        self.buttons = dialog.findChild(QDialogButtonBox)
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)

    def check(self, *rows):
        for row in rows:
            self.groups.item(row).setCheckState(Qt.CheckState.Checked)

    def submit(self):
        """Trigger acceptance the way the OK button would - even when disabled."""
        self.buttons.accepted.emit()


def fill_template_form(name=None, rows=(), description=None, submit=True,
                       states=None):
    """
    Build a ``dialog_driver`` handler that fills the create-template form.

    ``states`` - optional list that collects ``(stage, ok_enabled, validation)``
    so a test can assert on the inline validation without reaching into the
    dialog after it was destroyed.
    """
    def _handler(dialog):
        form = TemplateForm(dialog)
        if states is not None:
            states.append(("initial", form.ok.isEnabled(),
                           form.validation.toPlainText()))
        if name is not None:
            form.name.setText(name)
        if description is not None:
            form.description.setPlainText(description)
        form.check(*rows)
        if states is not None:
            states.append(("filled", form.ok.isEnabled(),
                           form.validation.toPlainText()))
        if submit:
            form.submit()
        return dialog.result()
    return _handler


def bulk(action):
    """Handler that clicks the bulk button of a duplicate dialog and applies."""
    wanted = "Replace" if action is DuplicateResolution.REPLACE else "Keep"

    def _handler(dialog):
        button = next(b for b in dialog.findChildren(QPushButton)
                      if wanted in b.text())
        button.click()
        return QDialog.DialogCode.Accepted
    return _handler


def accepted(_dialog):
    """Handler: accept the dialog without touching it."""
    return QDialog.DialogCode.Accepted


def rejected(_dialog):
    """Handler: cancel the dialog."""
    return QDialog.DialogCode.Rejected


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class Driver:
    """Dispatch table replacing ``QDialog.exec``."""

    def __init__(self):
        self._handlers = []
        self.seen = []

    def on(self, cls, handler):
        """Register *handler* for dialogs of type *cls* (latest wins)."""
        self._handlers.insert(0, (cls, handler))
        return self

    def run(self, dialog):
        self.seen.append(type(dialog).__name__)
        for cls, handler in self._handlers:
            if isinstance(dialog, cls):
                return handler(dialog)
        return QDialog.DialogCode.Rejected

    def opened(self, cls):
        """True when a dialog of that class was exec'd."""
        return cls.__name__ in self.seen


@pytest.fixture
def dialog_driver(monkeypatch, dialogs):
    """Replace ``QDialog.exec`` with a dispatch table; unknown dialogs cancel."""
    driver = Driver()
    monkeypatch.setattr(QDialog, "exec", lambda self: driver.run(self))
    return driver


@pytest.fixture
def make_import_dialog(qapp, dialogs, monkeypatch):
    """Factory for an ImportDialog with a controlled method-availability map."""
    created = []

    def _make(available=None, title="Import"):
        if available is not None:
            query = available if callable(available) else (lambda: available)
            monkeypatch.setattr(import_dialog_module, "get_available_methods", query)
        dialog = ImportDialog(title)
        created.append(dialog)
        return dialog

    yield _make
    for dialog in created:
        dialog.deleteLater()


@pytest.fixture
def make_export_dialog(qapp, dialogs, monkeypatch):
    """Factory for an ExportDialog with a controlled method-availability map."""
    created = []

    def _make(title="Export", available=None):
        if available is not None:
            query = available if callable(available) else (lambda: available)
            monkeypatch.setattr(export_dialog_module, "get_available_methods", query)
        dialog = ExportDialog(title)
        created.append(dialog)
        return dialog

    yield _make
    for dialog in created:
        dialog.deleteLater()


@pytest.fixture
def make_gm_dialog(qapp, dialogs):
    """Factory for a GroupManagementDialog (offline unless credentials asked)."""
    created = []

    def _make(groups=None, templates=None, credentials=False):
        extra = dict(ad_server="ldaps://dc.skola.local", ad_username="admin",
                     ad_password="secret") if credentials else {}
        dialog = GroupManagementDialog(initial_groups=groups,
                                       initial_templates=templates, **extra)
        created.append(dialog)
        return dialog

    yield _make
    for dialog in created:
        dialog.close()
        dialog.deleteLater()


ALL_AVAILABLE = {"gpg": True, "aes-gcm": True}
NO_GPG = {"gpg": False, "aes-gcm": True}


# ===========================================================================
# ImportDialog - method combo
# ===========================================================================

@pytest.mark.gui
class TestImportDialogMethods:
    """The decryption-method combo and its availability handling."""

    def test_combo_offers_auto_detect_first_then_the_two_backends(self, make_import_dialog):
        """Auto-detect is the pre-selected first entry, followed by gpg and aes-gcm."""
        dialog = make_import_dialog(ALL_AVAILABLE)
        combo = dialog._method_combo
        assert [combo.itemData(i) for i in range(combo.count())] == \
            ["auto", "gpg", "aes-gcm"]
        assert combo.currentData() == "auto"
        assert dialog.get_method() is None

    def test_unavailable_backend_is_disabled_and_labelled(self, make_import_dialog):
        """A missing backend is greyed out and its label says so."""
        dialog = make_import_dialog(NO_GPG)
        combo = dialog._method_combo
        model = combo.model()
        assert combo.itemText(1).endswith("- not available")
        assert model.item(1).isEnabled() is False
        assert combo.itemText(2) == "AES-GCM"
        assert model.item(2).isEnabled() is True

    def test_auto_detect_is_never_disabled(self, make_import_dialog):
        """Auto-detect stays selectable even when no backend is installed."""
        dialog = make_import_dialog({"gpg": False, "aes-gcm": False})
        assert dialog._method_combo.model().item(0).isEnabled() is True
        assert dialog._method_combo.itemText(0) == "Auto-detect (recommended)"

    def test_status_line_reports_both_backends_but_not_auto_detect(self, make_import_dialog):
        """The status label summarises every concrete method's availability."""
        dialog = make_import_dialog(NO_GPG)
        status = dialog._method_status.text()
        assert "GPG (OpenPGP): not available" in status
        assert "AES-GCM: available" in status
        assert "Auto-detect" not in status

    def test_failing_availability_query_disables_every_backend(self, make_import_dialog):
        """When the backend query explodes, nothing is offered as available."""
        def boom():
            raise OSError("registry unreadable")

        dialog = make_import_dialog(boom)
        model = dialog._method_combo.model()
        assert model.item(1).isEnabled() is False
        assert model.item(2).isEnabled() is False
        assert "not available" in dialog._method_status.text()

    def test_availability_refresh_is_idempotent(self, make_import_dialog):
        """Re-querying availability does not append the suffix twice."""
        dialog = make_import_dialog(NO_GPG)
        dialog._update_method_availability()
        dialog._update_method_availability()
        assert dialog._method_combo.itemText(1) == "GPG (OpenPGP) - not available"


# ===========================================================================
# ImportDialog - validation of _on_accept
# ===========================================================================

@pytest.mark.gui
class TestImportDialogAccept:
    """``_on_accept`` refuses incomplete input and reports the right values."""

    @pytest.mark.parametrize("path", ["", "   ", "\t"])
    def test_missing_file_is_refused(self, make_import_dialog, dialogs, path):
        """A blank (or whitespace-only) path never produces a result."""
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._file_input.setText(path)
        dialog._pass_input.setText(PASSWORD)
        dialog._on_accept()
        assert dialogs.titles() == ["Missing File"]
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert dialog.get_values() == ("", "")

    def test_missing_password_is_refused(self, make_import_dialog, dialogs):
        """A selected file without a password is rejected."""
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._file_input.setText("/tmp/groups.usrx")
        dialog._on_accept()
        assert dialogs.titles() == ["Missing Password"]
        assert dialog.get_full_values() == ("", "", None)

    def test_unavailable_method_is_refused_and_named(self, make_import_dialog, dialogs):
        """Choosing a backend that is not installed explains which one failed."""
        dialog = make_import_dialog(NO_GPG)
        dialog._file_input.setText("/tmp/groups.usrx")
        dialog._pass_input.setText(PASSWORD)
        dialog._method_combo.setCurrentIndex(1)
        dialog._on_accept()
        assert dialogs.titles() == ["Method Not Available"]
        assert "gpg" in dialogs.texts()[0]
        assert dialog.get_values() == ("", "")

    def test_unavailable_method_is_reported_before_the_missing_password(
            self, make_import_dialog, dialogs):
        """The method problem is the first thing the user is told about."""
        dialog = make_import_dialog(NO_GPG)
        dialog._file_input.setText("/tmp/groups.usrx")
        dialog._method_combo.setCurrentIndex(1)
        dialog._on_accept()
        assert dialogs.titles() == ["Method Not Available"]

    def test_auto_detect_reports_none_as_the_method(self, make_import_dialog, dialogs):
        """Auto-detect hands the decryption helper ``None``, not the string 'auto'."""
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._file_input.setText("  /tmp/groups.usrx  ")
        dialog._pass_input.setText(PASSWORD)
        dialog._on_accept()
        assert dialogs.calls == []
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.get_full_values() == ("/tmp/groups.usrx", PASSWORD, None)
        assert dialog.get_method() is None

    @pytest.mark.parametrize("index, method", [(1, "gpg"), (2, "aes-gcm")])
    def test_explicit_method_is_reported_verbatim(self, make_import_dialog,
                                                  dialogs, index, method):
        """An explicitly chosen, available method is returned as its value."""
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._file_input.setText("/tmp/groups.usrx")
        dialog._pass_input.setText(PASSWORD)
        dialog._method_combo.setCurrentIndex(index)
        dialog._on_accept()
        assert dialog.get_method() == method
        assert dialog.get_full_values() == ("/tmp/groups.usrx", PASSWORD, method)

    def test_password_is_taken_verbatim_including_padding(self, make_import_dialog):
        """Only the path is stripped - a password may legitimately contain spaces."""
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._file_input.setText("/tmp/groups.usrx")
        dialog._pass_input.setText("  mezery uvnitř  ")
        dialog._on_accept()
        assert dialog.get_values()[1] == "  mezery uvnitř  "


# ===========================================================================
# ImportDialog - file description
# ===========================================================================

@pytest.mark.gui
class TestImportDialogDescribeFile:
    """``_describe_file`` tells the user what the selected file looks like."""

    def test_usrx_container_reports_the_method_stored_in_the_header(
            self, make_import_dialog, tmp_path):
        """A USRX file is recognised and its header method is shown upper-cased."""
        path = write_usrx(tmp_path / "groups.usrx", {"export_type": "groups"})
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._describe_file(path)
        info = dialog._file_info.text()
        assert "USRX container" in info
        assert "AES-GCM" in info

    def test_standard_aes_file_reports_the_detected_method(
            self, make_import_dialog, tmp_path):
        """A plain AES-GCM JSON envelope is detected by content."""
        path = tmp_path / "groups.enc"
        encrypt_file_aes_gcm('{"export_type": "groups"}', str(path), PASSWORD)
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._describe_file(str(path))
        assert dialog._file_info.text() == "Detected encryption: AES-GCM"

    @pytest.mark.parametrize("name, payload", [
        ("plain.txt", b"just some text\n"),
        ("empty.bin", b""),
        ("binary.bin", b"\x00\x01\x02\xff\xfe"),
        ("truncated.usrx", b"USRX\x01"),
    ])
    def test_unreadable_file_asks_for_a_manual_choice(
            self, make_import_dialog, tmp_path, name, payload):
        """Anything the app cannot classify falls back to the manual hint."""
        path = tmp_path / name
        path.write_bytes(payload)
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._describe_file(str(path))
        assert "could not be detected" in dialog._file_info.text()
        assert "select it manually" in dialog._file_info.text()

    def test_missing_file_asks_for_a_manual_choice(self, make_import_dialog, tmp_path):
        """A path that does not exist is reported, not raised."""
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._describe_file(str(tmp_path / "gone.usrx"))
        assert "could not be detected" in dialog._file_info.text()

    def test_empty_path_clears_the_info_label(self, make_import_dialog, tmp_path):
        """Clearing the selection clears the description."""
        path = write_usrx(tmp_path / "groups.usrx", {"export_type": "groups"})
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._describe_file(path)
        assert dialog._file_info.text()
        dialog._describe_file("")
        assert dialog._file_info.text() == ""

    def test_browsing_stores_the_chosen_file_and_describes_it(
            self, make_import_dialog, dialogs, tmp_path):
        """Picking a file in the file dialog fills the (read-only) path field."""
        path = write_usrx(tmp_path / "groups.usrx", {"export_type": "groups"})
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialogs.open_file_answer = (path, "USRX Files (*.usrx)")
        dialog._browse()
        assert dialog._file_input.text() == path
        assert "USRX container" in dialog._file_info.text()

    def test_cancelled_browsing_keeps_the_previous_selection(
            self, make_import_dialog, dialogs, tmp_path):
        """Cancelling the file dialog must not wipe what was already chosen."""
        path = write_usrx(tmp_path / "groups.usrx", {"export_type": "groups"})
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialogs.open_file_answer = (path, "")
        dialog._browse()
        dialogs.open_file_answer = ("", "")
        dialog._browse()
        assert dialog._file_input.text() == path

    def test_changing_the_method_re_describes_the_selected_file(
            self, make_import_dialog, tmp_path):
        """Switching the combo refreshes the hint for the file already chosen."""
        path = write_usrx(tmp_path / "groups.usrx", {"export_type": "groups"})
        dialog = make_import_dialog(ALL_AVAILABLE)
        dialog._file_input.setText(path)
        assert dialog._file_info.text() == ""
        dialog._method_combo.setCurrentIndex(2)
        assert "USRX container" in dialog._file_info.text()


# ===========================================================================
# ExportDialog
# ===========================================================================

@pytest.mark.gui
class TestExportDialog:
    """Destination/password validation of the export dialog."""

    def test_defaults_to_aes_before_anything_is_accepted(self, make_export_dialog):
        """Nothing is reported until the user completed the form."""
        dialog = make_export_dialog()
        assert dialog.get_values() == ("", "aes-gcm", "")

    @pytest.mark.parametrize("path", ["", "   "])
    def test_missing_destination_is_refused(self, make_export_dialog, dialogs, path):
        """A blank destination never produces a result."""
        dialog = make_export_dialog()
        dialog._file_input.setText(path)
        dialog._pass_input.setText(PASSWORD)
        dialog._pass_confirm.setText(PASSWORD)
        dialog._on_accept()
        assert dialogs.titles() == ["Missing File"]
        assert dialog.get_values() == ("", "aes-gcm", "")

    def test_missing_password_is_refused(self, make_export_dialog, dialogs):
        """An empty password is rejected before the confirmation is compared."""
        dialog = make_export_dialog()
        dialog._file_input.setText("/tmp/groups.usrx")
        dialog._pass_confirm.setText(PASSWORD)
        dialog._on_accept()
        assert dialogs.titles() == ["Missing Password"]

    def test_mismatched_confirmation_is_refused(self, make_export_dialog, dialogs):
        """Password and confirmation must match exactly."""
        dialog = make_export_dialog()
        dialog._file_input.setText("/tmp/groups.usrx")
        dialog._pass_input.setText(PASSWORD)
        dialog._pass_confirm.setText(PASSWORD + " ")
        dialog._on_accept()
        assert dialogs.titles() == ["Password Mismatch"]
        assert dialog.result() != QDialog.DialogCode.Accepted

    @pytest.mark.parametrize("index, method", [(0, "aes-gcm"), (1, "gpg")])
    def test_accepted_form_reports_path_method_password(
            self, make_export_dialog, dialogs, index, method):
        """A complete form reports the trimmed path, the method and the password."""
        # Declare both backends installed: this test is about what a completed
        # form reports, not about what happens to be available on this machine
        # (the dialog refuses a method whose backend is missing - see
        # test_method_without_a_backend_is_refused).
        dialog = make_export_dialog(available=ALL_AVAILABLE)
        dialog._file_input.setText("  /tmp/skupiny.usrx ")
        dialog._pass_input.setText("Ďábelské heslo")
        dialog._pass_confirm.setText("Ďábelské heslo")
        dialog._method_combo.setCurrentIndex(index)
        dialog._on_accept()
        assert dialogs.calls == []
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.get_values() == ("/tmp/skupiny.usrx", method, "Ďábelské heslo")

    def test_browsing_fills_the_destination_field(self, make_export_dialog,
                                                  dialogs, tmp_path):
        """The save dialog feeds the read-only destination field."""
        dialog = make_export_dialog()
        target = str(tmp_path / "skupiny.usrx")
        dialogs.save_file_answer = (target, "USRX Files (*.usrx)")
        dialog._browse()
        assert dialog._file_input.text() == target
        dialogs.save_file_answer = ("", "")
        dialog._browse()
        assert dialog._file_input.text() == target

    @pytest.mark.bug
    def test_method_without_a_backend_is_refused(self, make_export_dialog, dialogs):
        """Picking an uninstalled backend must be caught here, like on import."""
        # The missing backend is declared instead of being taken from this
        # machine: the check under test must fire on every machine, also on one
        # where GPG happens to be installed (the test used to skip itself
        # there, which hid the defect).
        dialog = make_export_dialog(available=NO_GPG)
        dialog._file_input.setText("/tmp/groups.usrx")
        dialog._pass_input.setText(PASSWORD)
        dialog._pass_confirm.setText(PASSWORD)
        dialog._method_combo.setCurrentIndex(1)          # GPG / OpenPGP
        dialog._on_accept()
        assert dialogs.saw("not available")
        assert dialog.result() != QDialog.DialogCode.Accepted


# ===========================================================================
# GroupManagementDialog - manual group entry
# ===========================================================================

@pytest.mark.gui
class TestAddGroupManually:
    """Adding groups by DN in offline mode."""

    def test_missing_dn_adds_nothing(self, make_gm_dialog, dialogs):
        """An empty DN is refused with a warning."""
        dialog = make_gm_dialog()
        dialog.group_dn_input.setText("   ")
        dialog.add_group_manually()
        assert dialogs.titles() == ["Missing Input"]
        assert dialog.groups == []

    def test_name_is_extracted_from_the_dn_and_group_starts_unverified(
            self, make_gm_dialog, dialogs):
        """Without an explicit name the CN is used; the group is not verified."""
        dialog = make_gm_dialog()
        dialog.group_dn_input.setText(" CN=Učitelé,OU=Groups,DC=skola,DC=local ")
        dialog.add_group_manually()
        added = dialog.groups[0]
        assert added.name == "Učitelé"
        assert added.dn == "CN=Učitelé,OU=Groups,DC=skola,DC=local"
        assert added.verification_status is VerificationStatus.NOT_VERIFIED
        assert added.is_verified() is False
        assert dialogs.titles() == ["Group Added"]
        assert dialog.group_dn_input.text() == ""
        assert dialog.group_name_input.text() == ""
        assert dialog.group_list.count() == 1

    def test_explicit_name_wins_over_the_dn(self, make_gm_dialog):
        """A typed name is used verbatim (stripped) instead of the CN."""
        dialog = make_gm_dialog()
        dialog.group_dn_input.setText("CN=Students,OU=Groups,DC=skola,DC=local")
        dialog.group_name_input.setText("  Žáci školy  ")
        dialog.add_group_manually()
        assert dialog.groups[0].name == "Žáci školy"

    def test_duplicate_dn_is_refused_case_insensitively(self, make_gm_dialog, dialogs):
        """The same DN in different case is still the same group."""
        existing = group(name="Students", dn="CN=Students,OU=Groups,DC=skola,DC=local")
        dialog = make_gm_dialog(groups=[existing])
        dialog.group_dn_input.setText("cn=students,ou=groups,dc=skola,dc=local")
        dialog.add_group_manually()
        assert dialogs.titles() == ["Duplicate"]
        assert dialog.groups == [existing]
        assert dialog.group_dn_input.text() == "cn=students,ou=groups,dc=skola,dc=local"


class TestExtractCnFromDn:
    """The DN -> CN helper used for auto-naming manually added groups."""

    @pytest.mark.parametrize("dn, expected", [
        ("CN=Students,OU=Groups,DC=skola,DC=local", "Students"),
        ("cn=students,ou=groups", "students"),
        ("  CN=Padded  ,OU=Groups", "Padded"),
        ("OU=Groups,DC=skola,DC=local", "OU=Groups"),
        ("OU=Groups,CN=Late,DC=local", "Late"),
        ("CN=Učitelé 2. stupně,OU=Groups", "Učitelé 2. stupně"),
        ("CN=", ""),
    ])
    def test_first_cn_component_is_returned(self, make_gm_dialog, dn, expected):
        """The first CN= component wins; otherwise the first RDN is used."""
        dialog = make_gm_dialog()
        assert dialog._extract_cn_from_dn(dn) == expected

    def test_non_string_dn_falls_back_to_the_placeholder(self, make_gm_dialog):
        """A ``None`` DN is caught and reported as 'Unknown Group'."""
        dialog = make_gm_dialog()
        assert dialog._extract_cn_from_dn(None) == "Unknown Group"

    @pytest.mark.bug
    @pytest.mark.parametrize("dn", ["", ",", "   "])
    def test_dn_without_any_rdn_falls_back_to_a_usable_name(self, make_gm_dialog, dn):
        """A group must never end up with an empty display name."""
        dialog = make_gm_dialog()
        assert dialog._extract_cn_from_dn(dn) == "Unknown Group"

    @pytest.mark.bug
    def test_escaped_comma_stays_part_of_the_cn(self, make_gm_dialog):
        """``CN=Sales\\, EU`` is one RDN - the backslash escapes the comma."""
        dialog = make_gm_dialog()
        assert dialog._extract_cn_from_dn("CN=Sales\\, EU,OU=Groups,DC=local") == \
            "Sales, EU"


# ===========================================================================
# GroupManagementDialog - list rendering
# ===========================================================================

@pytest.mark.gui
class TestGroupListRendering:
    """``refresh_group_list`` and the item payload every operation relies on."""

    def test_every_item_stores_its_own_group_object(self, make_gm_dialog):
        """Each row carries the very object from ``self.groups``, not a copy."""
        groups = [group(name=f"G{i}", dn=f"CN=G{i},DC=local") for i in range(3)]
        dialog = make_gm_dialog(groups=groups)
        stored = [dialog.group_list.item(i).data(Qt.ItemDataRole.UserRole)
                  for i in range(dialog.group_list.count())]
        assert all(a is b for a, b in zip(stored, groups))

    def test_refresh_is_idempotent(self, make_gm_dialog):
        """Refreshing twice rebuilds the same rows, it does not append them."""
        groups = [group(name="A", dn="CN=A,DC=local"), group(name="B", dn="CN=B,DC=local")]
        dialog = make_gm_dialog(groups=groups)
        before = [dialog.group_list.item(i).text() for i in range(2)]
        dialog.refresh_group_list()
        dialog.refresh_group_list()
        assert dialog.group_list.count() == 2
        assert [dialog.group_list.item(i).text() for i in range(2)] == before

    def test_item_text_shows_status_icon_and_type(self, make_gm_dialog):
        """The row shows the status glyph, the name and the group type."""
        verified = group(name="Students", group_type="security")
        verified.mark_verified(exists=True)
        dialog = make_gm_dialog(groups=[verified])
        assert dialog.group_list.item(0).text() == "✓ Students (security)"

    def test_tooltip_carries_dn_and_verification_state(self, make_gm_dialog):
        """The tooltip is the only place the full DN is shown."""
        dialog = make_gm_dialog(groups=[group(name="Students")])
        tooltip = dialog.group_list.item(0).toolTip()
        assert "CN=Students,OU=Groups,DC=skola,DC=local" in tooltip
        assert "Never verified" in tooltip

    @pytest.mark.parametrize("status, colour", [
        (VerificationStatus.VERIFIED_EXISTS, "#4caf50"),
        (VerificationStatus.VERIFIED_NOT_FOUND, "#f44336"),
        (VerificationStatus.NOT_VERIFIED, "#9e9e9e"),
    ])
    def test_row_colour_reflects_verification_status(self, make_gm_dialog,
                                                     status, colour):
        """Verified / missing / unknown groups are colour coded."""
        dialog = make_gm_dialog(groups=[group(verification_status=status)])
        assert dialog.group_list.item(0).foreground().color().name() == colour

    def test_count_label_reports_total_and_verified(self, make_gm_dialog):
        """The counter shows how many of the groups were verified."""
        one, two = group(name="A", dn="CN=A,DC=local"), group(name="B", dn="CN=B,DC=local")
        one.mark_verified(exists=False)
        dialog = make_gm_dialog(groups=[one, two])
        assert dialog.group_count_label.text() == "Groups: 2 (1 verified)"

    @pytest.mark.bug
    def test_verified_label_tracks_the_verified_groups(self, make_gm_dialog):
        """The dedicated 'Verified:' counter must follow the real state."""
        verified = group(name="A", dn="CN=A,DC=local")
        verified.mark_verified(exists=True)
        dialog = make_gm_dialog(groups=[verified, group(name="B", dn="CN=B,DC=local")])
        assert dialog.group_verified_label.text() == "Verified: 1"

    def test_groups_from_items_skips_rows_without_a_group(self, make_gm_dialog):
        """Rows carrying foreign data are ignored instead of crashing."""
        first, second = group(name="A", dn="CN=A,DC=local"), group(name="B", dn="CN=B,DC=local")
        dialog = make_gm_dialog(groups=[first, second])
        stray = QListWidgetItem("stray")
        stray.setData(Qt.ItemDataRole.UserRole, "not a group")
        dialog.group_list.addItem(stray)
        empty = QListWidgetItem("empty")
        dialog.group_list.addItem(empty)
        items = [dialog.group_list.item(i) for i in range(dialog.group_list.count())]
        resolved = dialog._groups_from_items(items)
        assert resolved == [first, second]
        assert resolved[0] is first and resolved[1] is second

    def test_groups_from_items_accepts_an_empty_selection(self, make_gm_dialog):
        """No selection means no groups, not an exception."""
        dialog = make_gm_dialog(groups=[group()])
        assert dialog._groups_from_items([]) == []


# ===========================================================================
# GroupManagementDialog - removal
# ===========================================================================

@pytest.mark.gui
class TestRemoveAndClearGroups:
    """Removing selected groups and clearing the whole list."""

    def test_selection_is_removed_by_identity_even_when_dns_collide(
            self, make_gm_dialog, dialogs):
        """Two objects may share a DN - only the selected object may disappear."""
        first = group(name="First", dn="CN=Same,DC=local")
        second = group(name="Second", dn="cn=same,dc=local")
        third = group(name="Other", dn="CN=Other,DC=local")
        dialog = make_gm_dialog(groups=[first, second, third])
        select_rows(dialog.group_list, 0)
        dialog.remove_selected_groups()
        assert len(dialog.groups) == 2
        assert dialog.groups[0] is second
        assert dialog.groups[1] is third
        assert dialog.group_list.count() == 2

    def test_several_selected_groups_are_removed_at_once(self, make_gm_dialog, dialogs):
        """Every selected object goes, the rest keeps its order."""
        groups = [group(name=f"G{i}", dn=f"CN=G{i},DC=local") for i in range(4)]
        dialog = make_gm_dialog(groups=groups)
        select_rows(dialog.group_list, 0, 2)
        dialog.remove_selected_groups()
        assert [g.name for g in dialog.groups] == ["G1", "G3"]

    def test_declined_confirmation_keeps_every_group(self, make_gm_dialog, dialogs):
        """Answering 'No' to the confirmation changes nothing."""
        from PyQt6.QtWidgets import QMessageBox
        groups = [group(name="A", dn="CN=A,DC=local")]
        dialog = make_gm_dialog(groups=groups)
        dialogs.question_answer = QMessageBox.StandardButton.No
        select_rows(dialog.group_list, 0)
        dialog.remove_selected_groups()
        assert dialog.groups == groups

    def test_removing_without_a_selection_warns(self, make_gm_dialog, dialogs):
        """Nothing selected means a warning and no confirmation question."""
        dialog = make_gm_dialog(groups=[group()])
        dialog.group_list.clearSelection()
        dialog.remove_selected_groups()
        assert dialogs.kinds() == ["warning"]
        assert dialogs.titles() == ["No Selection"]
        assert len(dialog.groups) == 1

    def test_clear_all_empties_list_and_widget(self, make_gm_dialog, dialogs):
        """A confirmed 'Clear All' drops every group."""
        dialog = make_gm_dialog(groups=[group(name="A", dn="CN=A,DC=local"),
                                        group(name="B", dn="CN=B,DC=local")])
        dialog.clear_all_groups()
        assert dialog.groups == []
        assert dialog.group_list.count() == 0
        assert dialog.group_count_label.text() == "Groups: 0 (0 verified)"

    def test_clear_all_is_cancellable(self, make_gm_dialog, dialogs):
        """Answering 'No' keeps the list."""
        from PyQt6.QtWidgets import QMessageBox
        dialog = make_gm_dialog(groups=[group()])
        dialogs.question_answer = QMessageBox.StandardButton.No
        dialog.clear_all_groups()
        assert len(dialog.groups) == 1

    def test_clear_all_on_an_empty_list_asks_nothing(self, make_gm_dialog, dialogs):
        """There is nothing to confirm when the list is already empty."""
        dialog = make_gm_dialog()
        dialog.clear_all_groups()
        assert dialogs.calls == []


# ===========================================================================
# GroupManagementDialog - template refresh after verification
# ===========================================================================

@pytest.mark.gui
class TestUpdateTemplatesWithGroups:
    """``_update_templates_with_groups`` after a verification run."""

    def test_every_matching_group_of_a_template_is_refreshed(self, make_gm_dialog):
        """All matching members are replaced - not only the first one."""
        old_a = group(name="A", dn="CN=A,DC=local")
        old_b = group(name="B", dn="CN=B,DC=local")
        old_c = group(name="C", dn="CN=C,DC=local")
        tmpl = template(name="T", groups=[old_a, old_b, old_c])
        dialog = make_gm_dialog(templates=[tmpl])

        new_a = group(name="A", dn="CN=A,DC=local", members_count=7)
        new_b = group(name="B", dn="CN=B,DC=local", members_count=9)
        new_a.mark_verified(exists=True)
        new_b.mark_verified(exists=True)
        dialog._update_templates_with_groups([new_a, new_b])

        stored = dialog.template_manager.get_template("T")
        assert stored.groups[0] is new_a
        assert stored.groups[1] is new_b
        assert stored.groups[2] is old_c
        assert stored.get_verified_count() == 2
        assert stored.last_verified is not None

    def test_matching_is_case_insensitive_on_the_dn(self, make_gm_dialog):
        """AD DNs are case insensitive, so the refresh must be too."""
        old = group(name="A", dn="CN=A,OU=Groups,DC=local")
        tmpl = template(name="T", groups=[old])
        dialog = make_gm_dialog(templates=[tmpl])
        updated = group(name="A refreshed", dn="cn=a,ou=groups,dc=local")
        dialog._update_templates_with_groups([updated])
        assert dialog.template_manager.get_template("T").groups[0] is updated

    def test_template_without_a_matching_group_is_left_alone(self, make_gm_dialog):
        """A template nothing changed in keeps its (missing) verification stamp."""
        untouched = template(name="T", groups=[group(name="A", dn="CN=A,DC=local")])
        dialog = make_gm_dialog(templates=[untouched])
        dialog._update_templates_with_groups([group(name="Z", dn="CN=Z,DC=local")])
        assert dialog.template_manager.get_template("T").last_verified is None

    def test_update_with_no_groups_is_a_no_op(self, make_gm_dialog):
        """An empty verification result must not touch any template."""
        tmpl = template(name="T", groups=[group(name="A", dn="CN=A,DC=local")])
        dialog = make_gm_dialog(templates=[tmpl])
        dialog._update_templates_with_groups([])
        assert dialog.template_manager.get_template("T").last_verified is None
        assert dialog.template_list.count() == 1

    def test_verification_without_credentials_is_refused(self, make_gm_dialog, dialogs):
        """Offline mode cannot verify anything and says so."""
        dialog = make_gm_dialog(groups=[group()])
        select_rows(dialog.group_list, 0)
        dialog.verify_selected_groups()
        assert dialogs.titles() == ["No Credentials"]


# ===========================================================================
# GroupManagementDialog - template list
# ===========================================================================

@pytest.mark.gui
class TestTemplateListRendering:
    """``refresh_template_list`` and the payload rename/copy/delete depend on."""

    def test_every_row_stores_its_template_object(self, make_gm_dialog):
        """Rename/copy/delete read the template back from the row."""
        templates = [template(name="T1"), template(name="T2")]
        dialog = make_gm_dialog(templates=templates)
        stored = [dialog.template_list.item(i).data(Qt.ItemDataRole.UserRole)
                  for i in range(dialog.template_list.count())]
        assert [t.name for t in stored] == ["T1", "T2"]
        assert stored[0] is dialog.template_manager.get_template("T1")

    @pytest.mark.parametrize("count, fragment", [
        (0, "(0 groups)"),
        (1, "(1 group,"),
        (3, "(3 groups,"),
    ])
    def test_row_text_uses_the_right_plural(self, make_gm_dialog, count, fragment):
        """Singular is used for exactly one group."""
        groups = [group(name=f"G{i}", dn=f"CN=G{i},DC=local") for i in range(count)]
        dialog = make_gm_dialog(templates=[template(name="T", groups=groups)])
        assert fragment in dialog.template_list.item(0).text()

    def test_fully_verified_template_is_marked_as_such(self, make_gm_dialog):
        """A template whose groups all exist is reported as verified."""
        verified = group(name="A", dn="CN=A,DC=local")
        verified.mark_verified(exists=True)
        dialog = make_gm_dialog(templates=[template(name="T", groups=[verified])])
        assert "all verified" in dialog.template_list.item(0).text()

    def test_count_label_follows_the_manager(self, make_gm_dialog):
        """The counter is rebuilt from the manager on every refresh."""
        dialog = make_gm_dialog(templates=[template(name="T1")])
        assert dialog.template_count_label.text() == "Templates: 1"
        dialog.template_manager.add_template(template(name="T2"))
        dialog.refresh_template_list()
        assert dialog.template_count_label.text() == "Templates: 2"

    @pytest.mark.bug
    def test_row_text_contains_no_raw_html_entities(self, make_gm_dialog):
        """List rows are plain text, so the warning glyph must be a real character."""
        dialog = make_gm_dialog(
            templates=[template(name="T", groups=[group(name="A", dn="CN=A,DC=local")])])
        assert "&#" not in dialog.template_list.item(0).text()


@pytest.mark.gui
class TestDialogChrome:
    """Window title, offline banner and the labels of the action buttons."""

    def test_offline_mode_is_announced_in_the_title(self, make_gm_dialog):
        """Without credentials the dialog says so in its title."""
        assert make_gm_dialog().windowTitle() == "Group Management (Offline Mode)"

    def test_with_credentials_the_title_has_no_suffix(self, make_gm_dialog):
        """With credentials the dialog is the plain group manager."""
        dialog = make_gm_dialog(credentials=True)
        assert dialog.windowTitle() == "Group Management"
        assert dialog.has_credentials is True
        assert hasattr(dialog, "ou_dn_input")

    def test_discovery_is_only_offered_with_credentials(self, make_gm_dialog,
                                                        dialogs):
        """Offline mode has no OU input, and discovery quietly does nothing."""
        dialog = make_gm_dialog()
        assert hasattr(dialog, "ou_dn_input") is False
        dialog.discover_groups()
        assert dialogs.calls == []

    def test_discovery_without_an_ou_warns(self, make_gm_dialog, dialogs):
        """An empty OU DN is refused before any connection attempt."""
        dialog = make_gm_dialog(credentials=True)
        dialog.ou_dn_input.setText("   ")
        dialog.discover_groups()
        assert dialogs.titles() == ["Missing Input"]

    @pytest.mark.bug
    def test_buttons_are_labelled_with_real_characters(self, make_gm_dialog):
        """A QPushButton draws plain text, so '&#10133;' never becomes a glyph."""
        dialog = make_gm_dialog(credentials=True)
        offenders = [b.text() for b in dialog.findChildren(QPushButton)
                     if "&#" in b.text()]
        assert offenders == []


# ===========================================================================
# GroupManagementDialog - imported groups
# ===========================================================================

@pytest.mark.gui
class TestHandleImportedGroups:
    """The no-duplicate / replace / keep / cancel paths of a group import."""

    def test_new_groups_are_added_without_a_comparison_dialog(
            self, make_gm_dialog, dialogs, dialog_driver):
        """Without duplicates the user is only shown the summary."""
        dialog = make_gm_dialog(groups=[group(name="Old", dn="CN=Old,DC=local")])
        fresh = group(name="New", dn="CN=New,DC=local")
        dialog.handle_imported_groups([fresh])
        assert dialog_driver.opened(DuplicateGroupComparisonDialog) is False
        assert dialog.groups[-1] is fresh
        assert dialogs.titles() == ["Import Complete"]
        assert "Added 1 new group(s)." in dialogs.texts()[0]
        assert dialog.group_list.count() == 2

    def test_replacing_a_duplicate_swaps_in_the_imported_object(
            self, make_gm_dialog, dialogs, dialog_driver):
        """'Replace' drops the existing object and keeps the imported one."""
        existing = group(name="Old", dn="CN=Same,DC=local", description="old")
        dialog = make_gm_dialog(groups=[existing])
        imported = group(name="New", dn="cn=same,dc=local", description="new")
        other = group(name="Fresh", dn="CN=Fresh,DC=local")
        dialog_driver.on(DuplicateGroupComparisonDialog, bulk(DuplicateResolution.REPLACE))
        dialog.handle_imported_groups([imported, other])
        assert len(dialog.groups) == 2
        assert any(g is imported for g in dialog.groups)
        assert all(g is not existing for g in dialog.groups)
        assert "Replaced 1 duplicate(s)." in dialogs.texts()[-1]

    def test_keeping_a_duplicate_leaves_the_existing_object_in_place(
            self, make_gm_dialog, dialogs, dialog_driver):
        """The default resolution keeps what is already in the list."""
        existing = group(name="Old", dn="CN=Same,DC=local")
        dialog = make_gm_dialog(groups=[existing])
        imported = group(name="New", dn="CN=Same,DC=local")
        dialog_driver.on(DuplicateGroupComparisonDialog, accepted)
        dialog.handle_imported_groups([imported])
        assert dialog.groups == [existing]
        assert dialog.groups[0] is existing
        assert "Skipped 1 duplicate(s)." in dialogs.texts()[-1]

    def test_cancelling_the_comparison_adds_only_the_new_groups(
            self, make_gm_dialog, dialogs, dialog_driver):
        """Cancel keeps every existing group and adds the non-duplicates."""
        existing = group(name="Old", dn="CN=Same,DC=local")
        dialog = make_gm_dialog(groups=[existing])
        imported = group(name="New", dn="CN=Same,DC=local")
        fresh = group(name="Fresh", dn="CN=Fresh,DC=local")
        dialog_driver.on(DuplicateGroupComparisonDialog, rejected)
        dialog.handle_imported_groups([imported, fresh])
        assert [g.name for g in dialog.groups] == ["Old", "Fresh"]
        assert dialogs.calls == []

    def test_importing_nothing_shows_nothing(self, make_gm_dialog, dialogs,
                                             dialog_driver):
        """An empty import is a silent no-op."""
        existing = group()
        dialog = make_gm_dialog(groups=[existing])
        dialog.handle_imported_groups([])
        assert dialog.groups == [existing]
        assert dialogs.calls == []

    @pytest.mark.bug
    def test_import_never_stores_the_same_dn_twice(self, make_gm_dialog,
                                                   dialog_driver):
        """The list must keep the uniqueness that manual adding enforces."""
        dialog = make_gm_dialog()
        dialog.handle_imported_groups([group(name="A", dn="CN=A,DC=local"),
                                       group(name="A duplicate", dn="CN=A,DC=local")])
        dns = [g.dn.lower() for g in dialog.groups]
        assert len(dns) == len(set(dns))


# ===========================================================================
# GroupManagementDialog - imported templates
# ===========================================================================

@pytest.mark.gui
class TestHandleImportedTemplates:
    """The no-duplicate / replace / keep / cancel paths of a template import."""

    def test_new_templates_are_added_without_a_comparison_dialog(
            self, make_gm_dialog, dialogs, dialog_driver):
        """Without duplicates the user only sees the summary."""
        dialog = make_gm_dialog(templates=[template(name="Old")])
        dialog.handle_imported_templates([template(name="New")])
        assert dialog_driver.opened(DuplicateTemplateComparisonDialog) is False
        assert [t.name for t in dialog.template_manager.get_all_templates()] == \
            ["Old", "New"]
        assert "Added 1 new template(s)." in dialogs.texts()[-1]

    def test_replacing_a_duplicate_swaps_in_the_imported_template(
            self, make_gm_dialog, dialogs, dialog_driver):
        """'Replace' removes the old template and stores the imported one."""
        existing = template(name="T", groups=[group(name="A", dn="CN=A,DC=local")])
        dialog = make_gm_dialog(templates=[existing])
        imported = template(name="T", groups=[group(name="B", dn="CN=B,DC=local"),
                                              group(name="C", dn="CN=C,DC=local")])
        dialog_driver.on(DuplicateTemplateComparisonDialog,
                         bulk(DuplicateResolution.REPLACE))
        dialog.handle_imported_templates([imported])
        stored = dialog.template_manager.get_all_templates()
        assert len(stored) == 1
        assert stored[0] is imported
        assert stored[0].get_group_count() == 2
        assert "Replaced 1 duplicate(s)." in dialogs.texts()[-1]

    def test_keeping_a_duplicate_leaves_the_existing_template(
            self, make_gm_dialog, dialogs, dialog_driver):
        """The default resolution discards the imported version."""
        existing = template(name="T")
        dialog = make_gm_dialog(templates=[existing])
        dialog_driver.on(DuplicateTemplateComparisonDialog, accepted)
        dialog.handle_imported_templates([template(name="T", groups=[group()])])
        stored = dialog.template_manager.get_all_templates()
        assert stored[0] is existing
        assert stored[0].get_group_count() == 0
        assert "Skipped 1 duplicate(s)." in dialogs.texts()[-1]

    def test_cancelling_the_comparison_adds_only_the_new_templates(
            self, make_gm_dialog, dialogs, dialog_driver):
        """Cancel still imports the templates that are not duplicates."""
        dialog = make_gm_dialog(templates=[template(name="T")])
        dialog_driver.on(DuplicateTemplateComparisonDialog, rejected)
        dialog.handle_imported_templates([template(name="T"), template(name="Nová")])
        assert [t.name for t in dialog.template_manager.get_all_templates()] == \
            ["T", "Nová"]
        assert dialogs.calls == []

    def test_importing_nothing_shows_nothing(self, make_gm_dialog, dialogs,
                                             dialog_driver):
        """An empty import returns immediately."""
        dialog = make_gm_dialog(templates=[template(name="T")])
        dialog.handle_imported_templates([])
        assert dialogs.calls == []
        assert dialog.template_list.count() == 1

    @pytest.mark.bug
    def test_summary_counts_only_the_templates_really_added(self, make_gm_dialog,
                                                            dialogs, dialog_driver):
        """The summary must not promise more templates than the manager holds."""
        dialog = make_gm_dialog()
        dialog.handle_imported_templates([template(name="T"), template(name="T")])
        stored = dialog.template_manager.get_all_templates()
        assert f"Added {len(stored)} new template(s)." in dialogs.texts()[-1]


# ===========================================================================
# GroupManagementDialog - template creation
# ===========================================================================

@pytest.mark.gui
class TestCreateTemplateFromGroups:
    """The inline-validated *Create Template* dialog."""

    def test_without_groups_the_dialog_is_never_opened(self, make_gm_dialog,
                                                       dialogs, dialog_driver):
        """There is nothing to build a template from."""
        dialog = make_gm_dialog()
        dialog.create_template_from_groups()
        assert dialogs.titles() == ["No Groups"]
        assert dialog_driver.seen == []

    def test_valid_form_creates_a_template_from_the_checked_groups(
            self, make_gm_dialog, dialogs, dialog_driver):
        """Only the checked group objects end up in the new template."""
        first = group(name="A", dn="CN=A,DC=local")
        second = group(name="B", dn="CN=B,DC=local")
        dialog = make_gm_dialog(groups=[first, second])
        dialog_driver.on(QDialog, fill_template_form(name="  Učitelé  ", rows=[1],
                                                     description="  popis  "))
        dialog.create_template_from_groups()
        created = dialog.template_manager.get_template("Učitelé")
        assert created is not None
        assert created.groups == [second]
        assert created.groups[0] is second
        assert created.description == "popis"
        assert created.created_date is not None and created.modified_date is not None
        assert dialogs.titles() == ["Template Created"]
        assert dialog.template_list.count() == 1

    def test_ok_stays_disabled_until_the_form_is_complete(self, make_gm_dialog,
                                                          dialog_driver):
        """The inline validator drives the OK button."""
        states = []
        dialog = make_gm_dialog(groups=[group(name="A", dn="CN=A,DC=local")])
        dialog_driver.on(QDialog, fill_template_form(name="T", rows=[0], states=states))
        dialog.create_template_from_groups()
        (_, ok_before, report_before), (_, ok_after, report_after) = states
        assert ok_before is False
        assert "Template name is required." in report_before
        assert "At least one group must be selected." in report_before
        assert ok_after is True
        assert "Ready to create template" in report_after

    @pytest.mark.parametrize("name, rows, complaint", [
        ("", [0], "Template name is required."),
        ("   ", [0], "Template name is required."),
        ("T", [], "At least one group must be selected."),
    ])
    def test_incomplete_form_creates_nothing_and_explains_why(
            self, make_gm_dialog, dialogs, dialog_driver, name, rows, complaint):
        """Accepting an invalid form lists the missing pieces."""
        dialog = make_gm_dialog(groups=[group(name="A", dn="CN=A,DC=local")])
        dialog_driver.on(QDialog, fill_template_form(name=name, rows=rows))
        dialog.create_template_from_groups()
        assert dialog.template_manager.get_all_templates() == []
        assert dialogs.titles() == ["Required Fields Missing"]
        assert complaint in dialogs.texts()[0]

    def test_duplicate_name_is_refused(self, make_gm_dialog, dialogs, dialog_driver):
        """An existing template name blocks creation."""
        dialog = make_gm_dialog(groups=[group(name="A", dn="CN=A,DC=local")],
                                templates=[template(name="T")])
        dialog_driver.on(QDialog, fill_template_form(name="T", rows=[0]))
        dialog.create_template_from_groups()
        assert len(dialog.template_manager.get_all_templates()) == 1
        assert dialogs.titles() == ["Required Fields Missing"]
        assert 'already exists' in dialogs.texts()[0]

    def test_cancelling_creates_nothing(self, make_gm_dialog, dialogs, dialog_driver):
        """A cancelled dialog leaves the manager untouched."""
        dialog = make_gm_dialog(groups=[group(name="A", dn="CN=A,DC=local")])
        dialog_driver.on(QDialog, fill_template_form(name="T", rows=[0], submit=False))
        dialog.create_template_from_groups()
        assert dialog.template_manager.get_all_templates() == []
        assert dialogs.calls == []

    def test_select_all_button_checks_every_group(self, make_gm_dialog, dialog_driver):
        """'Select All' is the quick path to a template of everything."""
        groups = [group(name=f"G{i}", dn=f"CN=G{i},DC=local") for i in range(3)]
        dialog = make_gm_dialog(groups=groups)

        def _handler(inner):
            form = TemplateForm(inner)
            form.name.setText("Vše")
            select_all = next(b for b in inner.findChildren(QPushButton)
                              if b.text() == "Select All")
            select_all.click()
            form.submit()
            return inner.result()

        dialog_driver.on(QDialog, _handler)
        dialog.create_template_from_groups()
        created = dialog.template_manager.get_template("Vše")
        assert [g.name for g in created.groups] == ["G0", "G1", "G2"]


# ===========================================================================
# GroupManagementDialog - rename / copy / delete
# ===========================================================================

@pytest.mark.gui
class TestTemplateOperations:
    """Rename, copy and delete of the selected template."""

    @pytest.mark.parametrize("operation", ["rename_template", "copy_template",
                                           "delete_template"])
    def test_operations_need_a_selection(self, make_gm_dialog, dialogs, operation):
        """Every operation warns instead of touching a random template."""
        dialog = make_gm_dialog(templates=[template(name="T")])
        dialog.template_list.clearSelection()
        getattr(dialog, operation)()
        assert dialogs.titles() == ["No Selection"]
        assert len(dialog.template_manager.get_all_templates()) == 1

    @pytest.mark.parametrize("operation", ["rename_template", "copy_template",
                                           "delete_template"])
    def test_operations_refuse_a_row_without_template_data(self, make_gm_dialog,
                                                           dialogs, operation):
        """A row that lost its payload is reported, not dereferenced."""
        dialog = make_gm_dialog(templates=[template(name="T")])
        dialog.template_list.item(0).setData(Qt.ItemDataRole.UserRole, None)
        select_rows(dialog.template_list, 0)
        getattr(dialog, operation)()
        assert dialogs.titles() == ["Invalid Selection"]

    def test_rename_updates_manager_and_list(self, make_gm_dialog, dialogs):
        """A successful rename renames the object and rebuilds the row."""
        dialog = make_gm_dialog(templates=[template(name="T")])
        select_rows(dialog.template_list, 0)
        dialogs.text_answer = ("  Šablona žáků  ", True)
        dialog.rename_template()
        assert dialog.template_manager.template_exists("Šablona žáků")
        assert dialog.template_manager.get_template("T") is None
        assert dialog.template_list.item(0).text().startswith("Šablona žáků")
        assert dialogs.titles()[-1] == "Success"

    def test_rename_to_the_same_name_does_nothing(self, make_gm_dialog, dialogs):
        """Confirming the pre-filled name is not an error and shows no message."""
        dialog = make_gm_dialog(templates=[template(name="T")])
        select_rows(dialog.template_list, 0)
        dialogs.text_answer = ("T", True)
        dialogs.clear()
        dialog.rename_template()
        assert dialogs.kinds() == ["get_text"]
        assert dialog.template_manager.template_exists("T")

    def test_rename_to_whitespace_is_refused(self, make_gm_dialog, dialogs):
        """A name of only spaces is empty once stripped."""
        dialog = make_gm_dialog(templates=[template(name="T")])
        select_rows(dialog.template_list, 0)
        dialogs.text_answer = ("    ", True)
        dialog.rename_template()
        assert dialogs.titles()[-1] == "Invalid Name"
        assert dialog.template_manager.template_exists("T")

    def test_rename_to_an_existing_name_fails_loudly(self, make_gm_dialog, dialogs):
        """The manager refuses the collision and the user is told."""
        dialog = make_gm_dialog(templates=[template(name="T1"), template(name="T2")])
        select_rows(dialog.template_list, 0)
        dialogs.text_answer = ("T2", True)
        dialog.rename_template()
        assert dialogs.titles()[-1] == "Failed"
        assert dialog.template_manager.template_exists("T1")
        assert len(dialog.template_manager.get_all_templates()) == 2

    def test_rename_cancelled_in_the_prompt_changes_nothing(self, make_gm_dialog,
                                                            dialogs):
        """Cancelling the input dialog is silent."""
        dialog = make_gm_dialog(templates=[template(name="T")])
        select_rows(dialog.template_list, 0)
        dialogs.text_answer = ("Nové jméno", False)
        dialog.rename_template()
        assert dialog.template_manager.template_exists("T")
        assert dialogs.kinds() == ["get_text"]

    def test_copy_creates_a_second_template_with_the_same_groups(self, make_gm_dialog,
                                                                 dialogs):
        """The copy carries the description and the same group members."""
        member = group(name="A", dn="CN=A,DC=local")
        original = template(name="T", groups=[member], description="popis")
        dialog = make_gm_dialog(templates=[original])
        select_rows(dialog.template_list, 0)
        dialogs.text_answer = ("T (Kopie)", True)
        dialog.copy_template()
        copied = dialog.template_manager.get_template("T (Kopie)")
        assert copied is not None and copied is not original
        assert copied.description == "popis"
        assert [g.dn for g in copied.groups] == [member.dn]
        assert dialog.template_list.count() == 2

    def test_copy_to_an_existing_name_fails(self, make_gm_dialog, dialogs):
        """A colliding copy name is refused and nothing is created."""
        dialog = make_gm_dialog(templates=[template(name="T1"), template(name="T2")])
        select_rows(dialog.template_list, 0)
        dialogs.text_answer = ("T2", True)
        dialog.copy_template()
        assert dialogs.titles()[-1] == "Failed"
        assert len(dialog.template_manager.get_all_templates()) == 2

    def test_delete_removes_the_confirmed_template(self, make_gm_dialog, dialogs):
        """A confirmed delete drops exactly the selected template."""
        dialog = make_gm_dialog(templates=[template(name="T1"), template(name="T2")])
        select_rows(dialog.template_list, 1)
        dialog.delete_template()
        assert [t.name for t in dialog.template_manager.get_all_templates()] == ["T1"]
        assert dialog.template_list.count() == 1
        assert dialogs.titles()[-1] == "Success"

    def test_delete_is_cancellable(self, make_gm_dialog, dialogs):
        """Answering 'No' keeps the template."""
        from PyQt6.QtWidgets import QMessageBox
        dialog = make_gm_dialog(templates=[template(name="T")])
        select_rows(dialog.template_list, 0)
        dialogs.question_answer = QMessageBox.StandardButton.No
        dialog.delete_template()
        assert dialog.template_manager.template_exists("T")

    def test_public_accessors_return_detached_lists(self, make_gm_dialog):
        """``get_groups``/``get_templates`` hand out copies of the containers."""
        dialog = make_gm_dialog(groups=[group()], templates=[template(name="T")])
        groups, templates = dialog.get_groups(), dialog.get_templates()
        groups.clear()
        templates.clear()
        assert len(dialog.groups) == 1
        assert len(dialog.template_manager.get_all_templates()) == 1


# ===========================================================================
# DuplicateComparisonDialog
# ===========================================================================

@pytest.mark.gui
class TestDuplicateComparisonDialogs:
    """Per-duplicate resolutions and the bulk buttons."""

    def test_group_duplicates_default_to_keep(self, qapp, dialogs):
        """Nothing is replaced unless the user says so."""
        pairs = [(group(name="A", dn="CN=A,DC=local"), group(name="A2", dn="CN=A,DC=local")),
                 (group(name="B", dn="CN=B,DC=local"), group(name="B2", dn="CN=B,DC=local"))]
        dialog = DuplicateGroupComparisonDialog(pairs)
        assert dialog.get_resolutions() == {
            "CN=A,DC=local": DuplicateResolution.KEEP,
            "CN=B,DC=local": DuplicateResolution.KEEP,
        }
        dialog.deleteLater()

    def test_group_row_label_falls_back_to_the_dn(self, qapp, dialogs):
        """A nameless group is still identifiable in the list."""
        pairs = [(ADGroup(name="", dn="CN=Nameless,DC=local"),
                  group(name="X", dn="CN=Nameless,DC=local"))]
        dialog = DuplicateGroupComparisonDialog(pairs)
        assert "CN=Nameless,DC=local" in dialog._list_widget.item(0).text()
        dialog.deleteLater()

    def test_changing_one_combo_only_changes_that_entry(self, qapp, dialogs):
        """Each duplicate keeps its own decision."""
        pairs = [(group(name="A", dn="CN=A,DC=local"), group(name="A2", dn="CN=A,DC=local")),
                 (group(name="B", dn="CN=B,DC=local"), group(name="B2", dn="CN=B,DC=local"))]
        dialog = DuplicateGroupComparisonDialog(pairs)
        dialog._list_widget.setCurrentRow(1)
        combo = dialog.findChildren(QComboBox)[0]
        combo.setCurrentIndex(1)
        assert dialog.get_resolutions() == {
            "CN=A,DC=local": DuplicateResolution.KEEP,
            "CN=B,DC=local": DuplicateResolution.REPLACE,
        }
        assert dialog._list_widget.item(1).text().startswith("🔄")
        assert dialog._list_widget.item(0).text().startswith("⚠")
        dialog.deleteLater()

    def test_bulk_buttons_set_and_reset_every_entry(self, qapp, dialogs):
        """Bulk replace then bulk keep returns to the initial state."""
        pairs = [(group(name=f"G{i}", dn=f"CN=G{i},DC=local"),
                  group(name=f"I{i}", dn=f"CN=G{i},DC=local")) for i in range(3)]
        dialog = DuplicateGroupComparisonDialog(pairs)
        dialog._list_widget.setCurrentRow(0)
        replace_btn = next(b for b in dialog.findChildren(QPushButton)
                           if "Replace" in b.text())
        keep_btn = next(b for b in dialog.findChildren(QPushButton)
                        if "Keep" in b.text())
        replace_btn.click()
        assert set(dialog.get_resolutions().values()) == {DuplicateResolution.REPLACE}
        assert all(dialog._list_widget.item(i).text().startswith("🔄") for i in range(3))
        keep_btn.click()
        assert set(dialog.get_resolutions().values()) == {DuplicateResolution.KEEP}
        assert all(dialog._list_widget.item(i).text().startswith("⚠") for i in range(3))
        dialog.deleteLater()

    def test_detail_panel_shows_both_versions_of_a_group(self, qapp, dialogs):
        """Selecting a row builds the side-by-side comparison."""
        from PyQt6.QtWidgets import QGroupBox, QLabel
        existing = group(name="Old", dn="CN=A,DC=local", description="staré",
                         members_count=1)
        imported = group(name="New", dn="CN=A,DC=local", description="nové",
                         members_count=42)
        dialog = DuplicateGroupComparisonDialog([(existing, imported)])
        dialog._list_widget.setCurrentRow(0)
        titles = [b.title() for b in dialog.findChildren(QGroupBox)]
        labels = [lbl.text() for lbl in dialog.findChildren(QLabel)]
        assert "Existing Version" in titles and "Imported Version" in titles
        assert "staré" in labels and "nové" in labels
        assert "42" in labels
        dialog.deleteLater()

    def test_template_duplicates_are_identified_by_name(self, qapp, dialogs):
        """Templates have no DN, so the name is the identifier."""
        pairs = [(template(name="Šablona"), template(name="Šablona", groups=[group()]))]
        dialog = DuplicateTemplateComparisonDialog(pairs)
        dialog._list_widget.setCurrentRow(0)
        assert list(dialog.get_resolutions()) == ["Šablona"]
        dialog.deleteLater()

    def test_dialog_without_duplicates_resolves_nothing(self, qapp, dialogs):
        """An empty pair list builds a valid, empty dialog."""
        dialog = DuplicateTemplateComparisonDialog([])
        assert dialog.get_resolutions() == {}
        assert dialog._list_widget.count() == 0
        dialog.deleteLater()


# ===========================================================================
# group_tasks - round trips
# ===========================================================================

@pytest.mark.integration
class TestGroupTaskRoundTrip:
    """Real AES-GCM round trips through a USRX container in ``tmp_path``."""

    def test_groups_survive_an_encrypted_round_trip(self, qapp, tmp_path):
        """Every exported field comes back unchanged, diacritics included."""
        exported = [
            group(name="Žáci 9.A", dn="CN=Žáci 9.A,OU=Groups,DC=škola,DC=local",
                  description="Třídní skupina", group_type="security",
                  members_count=27, metadata={"ou": "Groups", "nested": [1, 2]}),
            group(name="Učitelé", dn="CN=Učitelé,OU=Groups,DC=škola,DC=local"),
        ]
        path = tmp_path / "groups.usrx"
        export = GroupExportTask(groups=exported, output_path=str(path),
                                 password=PASSWORD, encryption_method="aes-gcm")
        assert export.execute() == "Exported 2 groups successfully"
        assert path.exists()

        task = GroupImportTask(input_path=str(path), password=PASSWORD)
        assert task.execute() == "Imported 2 groups successfully"
        first, second = task.get_imported_groups()
        assert (first.name, first.dn) == (exported[0].name, exported[0].dn)
        assert first.description == "Třídní skupina"
        assert first.group_type == "security"
        assert first.members_count == 27
        assert first.metadata == {"ou": "Groups", "nested": [1, 2]}
        assert second.name == "Učitelé"
        assert second.members_count == 0

    def test_imported_groups_always_start_unverified(self, qapp, tmp_path):
        """Verification is machine-local state and is not carried by the file."""
        verified = group(name="A", dn="CN=A,DC=local")
        verified.mark_verified(exists=True)
        path = tmp_path / "groups.usrx"
        GroupExportTask([verified], str(path), PASSWORD, "aes-gcm").execute()
        task = GroupImportTask(str(path), PASSWORD)
        task.execute()
        imported = task.get_imported_groups()[0]
        assert imported.verification_status is VerificationStatus.NOT_VERIFIED
        assert imported.last_verified is None

    def test_empty_export_produces_an_importable_file(self, qapp, tmp_path):
        """Exporting nothing is allowed and imports back as nothing."""
        path = tmp_path / "empty.usrx"
        assert GroupExportTask([], str(path), PASSWORD, "aes-gcm").execute() == \
            "Exported 0 groups successfully"
        task = GroupImportTask(str(path), PASSWORD)
        assert task.execute() == "Imported 0 groups successfully"
        assert task.get_imported_groups() == []

    def test_explicitly_named_method_decrypts_the_same_file(self, qapp, tmp_path):
        """Choosing aes-gcm by hand works exactly like auto-detection."""
        path = tmp_path / "groups.usrx"
        GroupExportTask([group()], str(path), PASSWORD, "aes-gcm").execute()
        task = GroupImportTask(str(path), PASSWORD, encryption_method="aes-gcm")
        task.execute()
        assert len(task.get_imported_groups()) == 1

    def test_export_reports_progress_and_a_success_log(self, qapp, tmp_path):
        """The task drives the progress dialog from 10 % to 100 %."""
        from utils.progress_tasks import LogLevel
        task = GroupExportTask([group()], str(tmp_path / "g.usrx"), PASSWORD, "aes-gcm")
        progress, logs = attach(task)
        task.execute()
        values = [value for value, _ in progress]
        assert values[0] == 10 and values[-1] == 100
        assert values == sorted(values)
        assert any(level is LogLevel.SUCCESS for level, _ in logs)

    def test_templates_survive_an_encrypted_round_trip(self, qapp, tmp_path):
        """Template metadata, dates and member groups come back intact."""
        member = group(name="Žáci", dn="CN=Žáci,DC=local", members_count=3)
        exported = template(name="Třídní šablona", groups=[member],
                            description="Pro třídní učitele",
                            metadata={"author": "Dominika"})
        path = tmp_path / "templates.usrx"
        assert TemplateExportTask([exported], str(path), PASSWORD,
                                  "aes-gcm").execute() == \
            "Exported 1 templates successfully"

        task = TemplateImportTask(str(path), PASSWORD)
        assert task.execute() == "Imported 1 templates successfully"
        imported = task.get_imported_templates()[0]
        assert imported.name == "Třídní šablona"
        assert imported.description == "Pro třídní učitele"
        assert imported.metadata == {"author": "Dominika"}
        assert imported.created_date == exported.created_date
        assert imported.modified_date == exported.modified_date
        assert [(g.name, g.dn, g.members_count) for g in imported.groups] == \
            [("Žáci", "CN=Žáci,DC=local", 3)]

    def test_template_without_groups_round_trips(self, qapp, tmp_path):
        """An empty template is a valid template."""
        path = tmp_path / "templates.usrx"
        TemplateExportTask([template(name="Prázdná")], str(path), PASSWORD,
                           "aes-gcm").execute()
        task = TemplateImportTask(str(path), PASSWORD)
        task.execute()
        assert task.get_imported_templates()[0].get_group_count() == 0


# ===========================================================================
# group_tasks - error paths
# ===========================================================================

@pytest.mark.integration
class TestGroupTaskErrorPaths:
    """Every way an export/import can go wrong, and the exception it raises."""

    def test_missing_input_file_raises_file_not_found(self, qapp, tmp_path):
        """A path that does not exist fails before any decryption."""
        task = GroupImportTask(str(tmp_path / "gone.usrx"), PASSWORD)
        with pytest.raises(FileNotFoundError):
            task.execute()
        assert task.get_imported_groups() == []

    def test_wrong_password_raises_runtime_error(self, qapp, tmp_path):
        """The GCM tag check fails and the user gets a readable message."""
        path = tmp_path / "groups.usrx"
        GroupExportTask([group()], str(path), PASSWORD, "aes-gcm").execute()
        with pytest.raises(RuntimeError, match="incorrect password"):
            GroupImportTask(str(path), "úplně jiné heslo").execute()

    def test_malformed_json_payload_raises_value_error(self, qapp, tmp_path):
        """A container holding garbage instead of JSON is reported as such."""
        path = write_usrx(tmp_path / "broken.usrx", "{not json at all")
        with pytest.raises(json.JSONDecodeError):
            GroupImportTask(path, PASSWORD).execute()

    def test_templates_file_is_rejected_by_the_group_import(self, qapp, tmp_path):
        """The schema marker guards against importing the wrong file."""
        path = tmp_path / "templates.usrx"
        TemplateExportTask([template(name="T")], str(path), PASSWORD,
                           "aes-gcm").execute()
        with pytest.raises(ValueError, match="not a groups export file"):
            GroupImportTask(str(path), PASSWORD).execute()

    def test_groups_file_is_rejected_by_the_template_import(self, qapp, tmp_path):
        """And the other way round."""
        path = tmp_path / "groups.usrx"
        GroupExportTask([group()], str(path), PASSWORD, "aes-gcm").execute()
        with pytest.raises(ValueError, match="not a templates export file"):
            TemplateImportTask(str(path), PASSWORD).execute()

    @pytest.mark.parametrize("payload", [
        {"version": "1.0"},
        {"export_type": "", "groups": []},
        {"export_type": "ad_groups", "groups": []},
    ])
    def test_foreign_schema_is_rejected(self, qapp, tmp_path, payload):
        """Anything but ``export_type == 'groups'`` is refused."""
        path = write_usrx(tmp_path / "foreign.usrx", payload)
        with pytest.raises(ValueError, match="Invalid file format"):
            GroupImportTask(path, PASSWORD).execute()

    def test_groups_key_may_be_missing_from_a_groups_file(self, qapp, tmp_path):
        """A file with the right marker but no payload imports nothing."""
        path = write_usrx(tmp_path / "bare.usrx", {"export_type": "groups"})
        task = GroupImportTask(path, PASSWORD)
        assert task.execute() == "Imported 0 groups successfully"

    def test_unusable_group_entry_is_skipped_with_a_warning(self, qapp, tmp_path):
        """One broken record must not cost the user the whole import."""
        from utils.progress_tasks import LogLevel
        payload = {
            "export_type": "groups",
            "groups": [
                {"name": "Good", "dn": "CN=Good,DC=local"},
                {"name": "No DN"},
                {"name": "Also good", "dn": "CN=Also,DC=local", "members_count": 2},
            ],
        }
        path = write_usrx(tmp_path / "partial.usrx", payload)
        task = GroupImportTask(path, PASSWORD)
        progress, logs = attach(task)
        assert task.execute() == "Imported 2 groups successfully"
        assert [g.name for g in task.get_imported_groups()] == ["Good", "Also good"]
        assert any(level is LogLevel.WARNING and "No DN" in message
                   for level, message in logs)

    def test_unusable_template_entry_is_skipped_with_a_warning(self, qapp, tmp_path):
        """A template with an unparsable date does not abort the import."""
        from utils.progress_tasks import LogLevel
        payload = {
            "export_type": "group_templates",
            "templates": [
                {"name": "Broken", "created_date": "not-a-date"},
                {"name": "Fine", "groups": [{"name": "A", "dn": "CN=A,DC=local"}]},
            ],
        }
        path = write_usrx(tmp_path / "partial.usrx", payload)
        task = TemplateImportTask(path, PASSWORD)
        progress, logs = attach(task)
        assert task.execute() == "Imported 1 templates successfully"
        assert [t.name for t in task.get_imported_templates()] == ["Fine"]
        assert any(level is LogLevel.WARNING for level, _ in logs)

    def test_export_without_a_password_is_refused(self, qapp, tmp_path):
        """An empty password would produce a file protected by nothing."""
        with pytest.raises(ValueError, match="Password cannot be empty"):
            GroupExportTask([group()], str(tmp_path / "g.usrx"), "",
                            "aes-gcm").execute()

    def test_export_with_an_unknown_method_is_refused(self, qapp, tmp_path):
        """Only the two supported backends may be requested."""
        with pytest.raises(ValueError, match="Unsupported encryption method"):
            GroupExportTask([group()], str(tmp_path / "g.usrx"), PASSWORD,
                            "rot13").execute()
        assert not (tmp_path / "g.usrx").exists()

    def test_export_with_an_uninstalled_backend_reports_the_missing_component(
            self, qapp, tmp_path):
        """Asking for GPG without python-gnupg fails with an explanation."""
        if get_available_methods().get("gpg"):
            pytest.skip("GPG is installed on this machine")
        with pytest.raises(RuntimeError, match="gnupg"):
            GroupExportTask([group()], str(tmp_path / "g.usrx"), PASSWORD,
                            "gpg").execute()

    def test_cancelled_export_stops_before_writing_anything(self, qapp, tmp_path):
        """Cancellation is honoured while the groups are being serialised."""
        path = tmp_path / "cancelled.usrx"
        task = GroupExportTask([group()], str(path), PASSWORD, "aes-gcm")
        task.request_cancel()
        with pytest.raises(TaskCancelledException):
            task.execute()
        assert not path.exists()

    def test_cancelled_import_keeps_the_groups_it_had(self, qapp, tmp_path):
        """A cancelled import reports nothing as imported."""
        path = tmp_path / "groups.usrx"
        GroupExportTask([group()], str(path), PASSWORD, "aes-gcm").execute()
        task = GroupImportTask(str(path), PASSWORD)
        task.request_cancel()
        with pytest.raises(TaskCancelledException):
            task.execute()
        assert task.get_imported_groups() == []

    def test_export_writes_a_usrx_container_describing_its_payload(
            self, qapp, tmp_path):
        """The header the import dialog reads back names the content and count."""
        from utils.usrx_format import USRXFile, is_usrx_file
        path = tmp_path / "groups.usrx"
        GroupExportTask([group(name="A", dn="CN=A,DC=local"),
                         group(name="B", dn="CN=B,DC=local")],
                        str(path), PASSWORD, "aes-gcm").execute()
        assert is_usrx_file(str(path))
        header = USRXFile.read_header(str(path))
        assert header["encryption_type"] == "aes-gcm"
        assert header["metadata"]["content_type"] == "ad_groups"
        assert header["metadata"]["total_groups"] == 2

    def test_failed_export_is_logged_as_an_error(self, qapp, tmp_path):
        """The failure reaches the progress dialog's log as an ERROR entry."""
        from utils.progress_tasks import LogLevel
        task = GroupExportTask([group()], str(tmp_path / "g.usrx"), PASSWORD, "rot13")
        progress, logs = attach(task)
        with pytest.raises(ValueError):
            task.execute()
        assert any(level is LogLevel.ERROR and "Export failed" in message
                   for level, message in logs)


# ===========================================================================
# GroupManagementDialog - export/import entry points
# ===========================================================================

@pytest.mark.gui
class TestExportEntryPoints:
    """The guards in front of the export tasks."""

    def test_exporting_without_groups_warns(self, make_gm_dialog, dialogs,
                                            dialog_driver):
        """Nothing to export means no dialog at all."""
        dialog = make_gm_dialog()
        dialog.export_groups()
        assert dialogs.titles() == ["No Groups"]
        assert dialog_driver.seen == []

    def test_exporting_without_templates_warns(self, make_gm_dialog, dialogs,
                                               dialog_driver):
        """Same guard on the templates tab."""
        dialog = make_gm_dialog(groups=[group()])
        dialog.export_templates()
        assert dialogs.titles() == ["No Templates"]
        assert dialog_driver.seen == []

    def test_cancelled_export_dialog_starts_no_task(self, make_gm_dialog, dialogs,
                                                    dialog_driver):
        """Cancelling the export dialog leaves the file system untouched."""
        dialog = make_gm_dialog(groups=[group()])
        dialog_driver.on(ExportDialog, rejected)
        dialog.export_groups()
        assert dialog_driver.opened(ExportDialog)
        assert dialogs.calls == []

    def test_cancelled_import_dialog_starts_no_task(self, make_gm_dialog, dialogs,
                                                    dialog_driver):
        """Cancelling the import dialog is a silent no-op."""
        dialog = make_gm_dialog()
        dialog_driver.on(ImportDialog, rejected)
        dialog.import_groups()
        assert dialog_driver.opened(ImportDialog)
        assert dialog.groups == []
        assert dialogs.calls == []
