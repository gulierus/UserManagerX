"""
Dialogs for the class-level operations of the *Comparison and Sync* tab
======================================================================

``SourceAnalysisDialog``
    Deep analysis of a single source with selectable fixes ("Analyze Source").

``ClassShiftDialog``
    Analysis + options + preview for the class-year shift
    ("Shift Classes (Left/Right Source)").

``ClassNumeralConversionDialog``
    Roman <-> Arabic (or fully custom) conversion of class names
    ("Convert Class Numerals").

All three dialogs share the same principle:

* the data is never modified directly - every dialog works on a **deep copy**
  of the source it was given;
* after each applied fix the analysis is recomputed, so a following fix always
  sees the data produced by the previous one;
* the caller decides what happens with the result (create a new source or
  update the existing one).
"""

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from models import Class, Source
from ui.analysis_widgets import AnalysisPanel
from ui.class_template_widget import ClassTemplateWidget
from utils.class_name_utils import (
    ClassNameResult, convert_class_name, shift_class_name,
)
from utils.source_analysis import (
    AnalysisReport, SourceIssue, analyze_shift, analyze_source, apply_fix,
    rename_class,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_deep_copy(source: Source) -> Source:
    """
    Create an independent copy of *source*.

    Args:
        source: Source to copy.

    Returns:
        A deep copy, or - if copying fails for an exotic payload - a shallow
        rebuild that at least keeps the dialogs usable.
    """
    try:
        return source.deep_copy()
    except Exception:                                # pragma: no cover - defensive
        logger.exception("deep_copy() failed for source %r - falling back", source.name)
        clone = Source(name=source.name, source_type=source.source_type,
                       readonly=False)
        for cls in source.classes:
            new_class = Class(name=cls.name)
            new_class.persons = list(cls.persons)
            clone.add_class(new_class)
        return clone


class _WorkingCopyDialog(QDialog):
    """
    Base class for dialogs that analyse and repair a working copy of a source.

    Subclasses must implement :meth:`build_report` and are expected to call
    :meth:`refresh_analysis` once their UI is ready.
    """

    def __init__(self, source: Source, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.original_source = source
        self.working_source = _safe_deep_copy(source)
        self.fixes_applied: List[str] = []
        self.analysis_panel = AnalysisPanel(allow_fixes=True, parent=self)
        self.analysis_panel.fix_requested.connect(self.on_fix_requested)

    # -- to implement ----------------------------------------------------

    def build_report(self) -> AnalysisReport:
        """Return a freshly computed report for the working copy."""
        raise NotImplementedError

    def on_data_changed(self) -> None:
        """Hook called after a fix modified the working copy."""

    # -- shared behaviour ------------------------------------------------

    def refresh_analysis(self) -> None:
        """Recompute the report and repaint the analysis panel."""
        try:
            report = self.build_report()
        except Exception as exc:
            logger.exception("Analysis failed")
            QMessageBox.critical(
                self, "Analysis Error",
                f"The analysis could not be completed:\n\n{exc}"
            )
            return
        self.analysis_panel.set_report(report)

    def on_fix_requested(self, issue: SourceIssue, fix_key: str,
                         parameter: str) -> None:
        """Apply a fix chosen by the user and refresh everything."""
        try:
            result = apply_fix(self.working_source, issue, fix_key, parameter)
        except Exception as exc:                     # pragma: no cover - defensive
            logger.exception("Unexpected error while applying fix %s", fix_key)
            QMessageBox.critical(self, "Fix Failed",
                                 f"The solution could not be applied:\n\n{exc}")
            return

        if not result.applied:
            QMessageBox.warning(self, "Fix Not Applied", result.message)
            widget = self.analysis_panel.issue_widget(issue.key)
            if widget:
                widget.show_status(result.message, success=False)
            return

        if result.changed:
            self.fixes_applied.append(f"{issue.title}: {result.message}")
            logger.info("Applied fix %s for %s: %s", fix_key, issue.key, result.message)

        self.on_data_changed()
        self.refresh_analysis()

        widget = self.analysis_panel.issue_widget(issue.key)
        if widget:
            widget.show_status(result.message, success=True)
        else:
            QMessageBox.information(self, "Solution Applied", result.message)

    def has_changes(self) -> bool:
        """True when at least one fix modified the working copy."""
        return bool(self.fixes_applied)


# ---------------------------------------------------------------------------
# Source analysis
# ---------------------------------------------------------------------------

class SourceAnalysisDialog(_WorkingCopyDialog):
    """
    Deep analysis of one source with optional, chainable fixes.

    The dialog checks everything that can make ``Merge Both Sources`` or
    ``Shift Classes`` fail and lets the user repair the data step by step.
    Because all work happens on a copy, the user can always cancel.
    """

    def __init__(self, source: Source, side: str = "", parent: Optional[QWidget] = None):
        """
        Args:
            source: Source to analyse.
            side: Optional label such as ``"Left Source"`` used in the title.
            parent: Parent widget.
        """
        super().__init__(source, parent)
        self.side = side
        self.setWindowTitle(
            f"Analyze {side or 'Source'} - {source.name}" if side
            else f"Analyze Source - {source.name}"
        )
        self.setModal(True)
        self.resize(900, 720)
        self._build_ui()
        self.refresh_analysis()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            f"<b>Analysis of '{self.original_source.name}'</b><br>"
            "Every problem that can break <i>Merge Both Sources</i>, "
            "<i>Shift Classes</i> or <i>Convert Class Numerals</i> is listed "
            "below together with the solutions you can apply.<br>"
            "You may apply none, some or all of them - each solution works on "
            "the data produced by the previous one."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px;"
        )
        layout.addWidget(info)

        layout.addWidget(self.analysis_panel, stretch=1)

        self._changes_label = QLabel()
        self._changes_label.setWordWrap(True)
        self._changes_label.setStyleSheet("color: #4CAF50; font-size: 10px;")
        self._changes_label.setVisible(False)
        layout.addWidget(self._changes_label)

        button_box = QDialogButtonBox()
        self._apply_button = button_box.addButton(
            "Save Changes…", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._apply_button.setEnabled(False)
        self._apply_button.setToolTip(
            "Store the repaired data - either back into this source or as a new one."
        )
        close_button = button_box.addButton(
            "Close", QDialogButtonBox.ButtonRole.RejectRole
        )
        close_button.setToolTip("Discard every applied solution and close.")
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def build_report(self) -> AnalysisReport:
        """Analyse the working copy with every general data check."""
        return analyze_source(
            self.working_source,
            title=f"Analysis of '{self.original_source.name}'",
        )

    def on_data_changed(self) -> None:
        self._apply_button.setEnabled(True)
        self._changes_label.setVisible(True)
        self._changes_label.setText(
            "Applied solutions:<br>" +
            "<br>".join(f"• {entry}" for entry in self.fixes_applied)
        )

    def get_repaired_source(self) -> Source:
        """Return the working copy holding every applied fix."""
        return self.working_source

    def reject(self) -> None:
        """Confirm before throwing away applied fixes."""
        if self.has_changes():
            reply = QMessageBox.question(
                self, "Discard Changes",
                f"{len(self.fixes_applied)} solution(s) were applied to a working "
                "copy.\nClose without saving them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        super().reject()


# ---------------------------------------------------------------------------
# Class shift
# ---------------------------------------------------------------------------

class ClassShiftDialog(_WorkingCopyDialog):
    """
    Shift the school year of every class of a source.

    The dialog offers three things the previous version was missing:

    * a real parser - ``"IX."``, ``"6.A"``, ``"9 B"`` and ``"Blue class 6.A"``
      are all recognised, so classes are no longer skipped as
      "UNKNOWN FORMAT";
    * a full pre-flight analysis with fixes (inconsistent names, collisions,
      duplicates, …);
    * options for the shift itself (direction, graduation handling).
    """

    def __init__(self, source: Source, parent: Optional[QWidget] = None):
        super().__init__(source, parent)
        self.setWindowTitle(f"Shift Class Years - {source.name}")
        self.setModal(True)
        self.resize(900, 760)
        self._build_ui()
        self.refresh_analysis()
        self._refresh_preview()

    # -- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            "This function moves every class to the next school year "
            "(e.g. 6.A → 7.A, IX.B → X.B) and creates a <b>new source</b> with "
            "the result. The original source is never modified.<br>"
            "Text around the number is preserved, so "
            "<i>\"Blue class 6.A\"</i> becomes <i>\"Blue class 7.A\"</i>."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px;"
        )
        layout.addWidget(info)

        tabs = QTabWidget()

        # --- Tab 1: options + preview ------------------------------------
        options_tab = QWidget()
        options_layout = QVBoxLayout(options_tab)

        options_group = QGroupBox("Shift options")
        options_form = QVBoxLayout(options_group)

        delta_row = QHBoxLayout()
        delta_row.addWidget(QLabel("Shift classes by:"))
        self._delta_spin = QSpinBox()
        self._delta_spin.setRange(-10, 10)
        self._delta_spin.setValue(1)
        self._delta_spin.setSuffix(" year(s)")
        self._delta_spin.setToolTip(
            "Positive values move classes up (6 → 7), negative values move "
            "them back (7 → 6)."
        )
        self._delta_spin.valueChanged.connect(self._on_options_changed)
        delta_row.addWidget(self._delta_spin)
        delta_row.addStretch()
        options_form.addLayout(delta_row)

        graduation_row = QHBoxLayout()
        self._graduation_check = QCheckBox("Remove the graduating year:")
        self._graduation_check.setChecked(True)
        self._graduation_check.setToolTip(
            "Classes that reach this year leave the school and are not copied "
            "into the new source."
        )
        self._graduation_check.toggled.connect(self._on_options_changed)
        graduation_row.addWidget(self._graduation_check)

        self._graduation_spin = QSpinBox()
        self._graduation_spin.setRange(1, 20)
        self._graduation_spin.setValue(9)
        self._graduation_spin.valueChanged.connect(self._on_options_changed)
        graduation_row.addWidget(self._graduation_spin)
        graduation_row.addStretch()
        options_form.addLayout(graduation_row)

        options_layout.addWidget(options_group)

        preview_group = QGroupBox("Preview of the result")
        preview_layout = QVBoxLayout(preview_group)
        self._preview_text = QTextEdit()
        self._preview_text.setReadOnly(True)
        preview_layout.addWidget(self._preview_text)
        self._preview_summary = QLabel()
        self._preview_summary.setWordWrap(True)
        self._preview_summary.setStyleSheet("color: #aaa; font-size: 10px;")
        preview_layout.addWidget(self._preview_summary)
        options_layout.addWidget(preview_group, stretch=1)

        tabs.addTab(options_tab, "1. Options and preview")

        # --- Tab 2: analysis ---------------------------------------------
        analysis_tab = QWidget()
        analysis_layout = QVBoxLayout(analysis_tab)
        analysis_hint = QLabel(
            "Problems that can make the shift fail or produce a surprising "
            "result. Applying a solution changes the working copy only - the "
            "original source stays untouched."
        )
        analysis_hint.setWordWrap(True)
        analysis_hint.setStyleSheet("color: #aaa; font-size: 10px;")
        analysis_layout.addWidget(analysis_hint)
        analysis_layout.addWidget(self.analysis_panel, stretch=1)
        tabs.addTab(analysis_tab, "2. Analysis and fixes")

        self._tabs = tabs
        layout.addWidget(tabs, stretch=1)

        self._warning_label = QLabel()
        self._warning_label.setWordWrap(True)
        self._warning_label.setStyleSheet("color: #ff6b6b; font-weight: bold;")
        self._warning_label.setVisible(False)
        layout.addWidget(self._warning_label)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setText("Shift Classes")
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    # -- options -----------------------------------------------------------

    def _shift_kwargs(self) -> dict:
        return {
            "graduation_year": (self._graduation_spin.value()
                                if self._graduation_check.isChecked() else None),
            "remove_graduating": self._graduation_check.isChecked(),
        }

    def _on_options_changed(self, *_args) -> None:
        self._graduation_spin.setEnabled(self._graduation_check.isChecked())
        self._refresh_preview()
        self.refresh_analysis()

    # -- analysis ----------------------------------------------------------

    def build_report(self) -> AnalysisReport:
        return analyze_shift(
            self.working_source, self._delta_spin.value(), **self._shift_kwargs()
        )

    def on_data_changed(self) -> None:
        self._refresh_preview()

    # -- preview -----------------------------------------------------------

    def _compute_results(self) -> List[ClassNameResult]:
        delta = self._delta_spin.value()
        kwargs = self._shift_kwargs()
        return [
            shift_class_name(cls.name, delta, **kwargs)
            for cls in self.working_source.classes
        ]

    def _refresh_preview(self) -> None:
        results = self._compute_results()

        if not results:
            self._preview_text.setPlainText("This source contains no classes.")
            self._preview_summary.setText("")
            self._set_warning("The source has no classes - the result would be empty.")
            return

        lines = []
        for cls, result in zip(self.working_source.classes, results):
            suffix = f" ({len(cls.persons)} student(s))"
            lines.append(result.preview_line() + suffix)
        self._preview_text.setPlainText("\n".join(lines))

        shifted = sum(1 for r in results if r.changed)
        removed = sum(1 for r in results if r.removed)
        skipped = sum(1 for r in results if not r.recognised)
        unchanged = len(results) - shifted - removed - skipped

        removed_students = sum(
            len(cls.persons)
            for cls, result in zip(self.working_source.classes, results)
            if result.removed
        )

        self._preview_summary.setText(
            f"{shifted} class(es) will be shifted, {removed} removed "
            f"({removed_students} student(s) leave), {skipped} have no number "
            f"and are copied unchanged, {unchanged} stay as they are."
        )

        if shifted == 0 and removed == 0:
            self._set_warning(
                "Nothing would change: none of the class names contains a number "
                "that can be shifted. Open the 'Analysis and fixes' tab and "
                "unify the class names first."
            )
        else:
            self._set_warning("")

    def _set_warning(self, message: str) -> None:
        self._warning_label.setText(message)
        self._warning_label.setVisible(bool(message))

    # -- result ------------------------------------------------------------

    def _on_accept(self) -> None:
        results = self._compute_results()
        if not any(r.changed or r.removed for r in results):
            reply = QMessageBox.question(
                self, "Nothing Would Change",
                "The shift would not change a single class name.\n\n"
                "Create the new source anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._tabs.setCurrentIndex(1)
                return
        self.accept()

    def build_result_source(self, new_name: str) -> Source:
        """
        Produce the shifted source.

        Args:
            new_name: Name for the new source.

        Returns:
            A new :class:`~models.Source` with all shifted classes.
        """
        result_source = _safe_deep_copy(self.working_source)
        result_source.name = new_name
        result_source.readonly = False
        result_source.source_type = "manual"

        delta = self._delta_spin.value()
        kwargs = self._shift_kwargs()

        kept: List[Class] = []
        removed_students = 0

        for cls in list(result_source.classes):
            result = shift_class_name(cls.name, delta, **kwargs)

            if result.removed:
                removed_students += len(cls.persons)
                logger.info("Shift: removing graduating class %r", cls.name)
                continue

            kept.append(cls)

            if result.changed:
                for person in cls.persons:
                    person.class_name = result.result
                cls.name = result.result
            elif not result.recognised:
                logger.info("Shift: class %r has no number - copied unchanged", cls.name)

        result_source.classes = kept

        # Merge classes that ended up with the same name (e.g. "6.A" + "VI.A")
        merged: List[Class] = []
        by_name = {}
        for cls in result_source.classes:
            existing = by_name.get(cls.name)
            if existing is None:
                by_name[cls.name] = cls
                merged.append(cls)
            else:
                existing.persons.extend(cls.persons)
                logger.info("Shift: merged colliding class %r", cls.name)
        result_source.classes = merged

        logger.info(
            "Shift complete: %d class(es) kept, %d student(s) graduated out",
            len(merged), removed_students,
        )
        return result_source


# ---------------------------------------------------------------------------
# Numeral conversion
# ---------------------------------------------------------------------------

class ClassNumeralConversionDialog(QDialog):
    """
    Convert class names between Roman and Arabic numerals (or a custom format).

    The dialog is used in two ways:

    * for a whole source - every class name is converted;
    * for a single class of the output source.
    """

    def __init__(self, class_names: List[str], title: str = "Convert Class Numerals",
                 description: str = "", parent: Optional[QWidget] = None):
        """
        Args:
            class_names: The names that will be converted (used for the preview).
            title: Window title.
            description: Extra explanation shown above the options.
            parent: Parent widget.
        """
        super().__init__(parent)
        self.class_names = list(class_names)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(720, 640)
        self._build_ui(description)

    def _build_ui(self, description: str) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            description or
            "Choose the format the class names should be rewritten to. "
            "Class names in which no number can be found are always left "
            "unchanged - they are never deleted."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px;"
        )
        layout.addWidget(info)

        self.template_widget = ClassTemplateWidget(self.class_names, parent=self)
        self.template_widget.template_changed.connect(self._on_template_changed)
        layout.addWidget(self.template_widget, stretch=1)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = button_box.button(QDialogButtonBox.StandardButton.Ok)
        self._ok_button.setText("Convert")
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        self._on_template_changed(self.template_widget.get_template())

    def _on_template_changed(self, template: str) -> None:
        self._ok_button.setEnabled(bool(template))

    def _on_accept(self) -> None:
        if not self.template_widget.is_valid():
            QMessageBox.warning(
                self, "Invalid Template",
                self.template_widget.get_error() or "The template cannot be used."
            )
            return

        results = self.template_widget.get_results()
        if results and not any(r.changed for r in results):
            reply = QMessageBox.question(
                self, "Nothing Would Change",
                "None of the class names would change.\n\nContinue anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    # -- results -----------------------------------------------------------

    def get_template(self) -> str:
        """Return the template the user confirmed."""
        return self.template_widget.get_template()

    def get_results(self) -> List[ClassNameResult]:
        """Return the per-name conversion results."""
        return self.template_widget.get_results()

    def convert_name(self, name: str) -> ClassNameResult:
        """Convert a single name with the confirmed template."""
        return convert_class_name(name, self.get_template())
