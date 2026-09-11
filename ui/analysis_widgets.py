"""
Analysis presentation widgets
=============================

UI layer for :mod:`utils.source_analysis`.

``IssueWidget``
    Renders a single :class:`~utils.source_analysis.SourceIssue`: title,
    summary, a detailed explanation, the list of affected items and radio
    buttons for every offered solution.

``AnalysisPanel``
    Renders a whole :class:`~utils.source_analysis.AnalysisReport` - statistics
    plus one :class:`IssueWidget` per issue - and forwards the user's fix
    requests to its owner.

Both widgets are purely presentational: they never modify data themselves.
The owning dialog applies the fix to its working copy and then feeds a freshly
computed report back in, which guarantees that every following fix operates on
the already updated data.
"""

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QRadioButton, QScrollArea, QTextEdit, QVBoxLayout,
    QWidget,
)

from utils.source_analysis import (
    AnalysisReport, FixOption, IssueSeverity, ParameterKind, SourceIssue,
)
from utils.class_name_utils import validate_template

logger = logging.getLogger(__name__)


_SEVERITY_COLORS = {
    IssueSeverity.ERROR: "#ff6b6b",
    IssueSeverity.WARNING: "#FFA726",
    IssueSeverity.INFO: "#64B5F6",
}


class IssueWidget(QGroupBox):
    """
    Display one issue together with its possible solutions.

    Signals:
        fix_requested(object, str, str): ``(issue, fix_key, parameter)``
    """

    fix_requested = pyqtSignal(object, str, str)

    #: Number of affected items listed before the list is truncated.
    MAX_AFFECTED_SHOWN = 25

    def __init__(self, issue: SourceIssue, allow_fixes: bool = True,
                 parent: Optional[QWidget] = None):
        """
        Args:
            issue: The issue to render.
            allow_fixes: When False the solution controls are hidden (used by
                dialogs that only report, e.g. the merge preview).
            parent: Parent widget.
        """
        super().__init__(parent)
        self.issue = issue
        self._allow_fixes = allow_fixes
        self._button_group: Optional[QButtonGroup] = None
        self._parameter_inputs = {}
        self._build_ui()

    def _build_ui(self) -> None:
        color = _SEVERITY_COLORS[self.issue.severity]
        self.setTitle(f"{self.issue.severity.icon}  {self.issue.title}")
        self.setStyleSheet(
            f"QGroupBox {{ font-weight: bold; border: 1px solid {color};"
            f" border-radius: 4px; margin-top: 8px; padding-top: 6px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 8px;"
            f" padding: 0 4px; color: {color}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        summary = QLabel(self.issue.summary)
        summary.setWordWrap(True)
        summary.setStyleSheet("font-weight: normal;")
        layout.addWidget(summary)

        if self.issue.detail:
            detail = QLabel(self.issue.detail.replace("\n", "<br>"))
            detail.setWordWrap(True)
            detail.setStyleSheet(
                "font-weight: normal; color: #aaa; font-size: 10px;"
            )
            layout.addWidget(detail)

        if self.issue.impacts:
            impacts = QLabel("Affects: " + ", ".join(self.issue.impacts))
            impacts.setWordWrap(True)
            impacts.setStyleSheet(
                "font-weight: normal; color: #888; font-size: 10px; font-style: italic;"
            )
            layout.addWidget(impacts)

        if self.issue.affected:
            shown = self.issue.affected[:self.MAX_AFFECTED_SHOWN]
            text = "\n".join(f"• {item}" for item in shown)
            if len(self.issue.affected) > len(shown):
                text += f"\n… and {len(self.issue.affected) - len(shown)} more"
            box = QTextEdit()
            box.setReadOnly(True)
            box.setPlainText(text)
            box.setMaximumHeight(110)
            box.setStyleSheet("font-weight: normal; font-size: 10px;")
            layout.addWidget(box)

        if self._allow_fixes and self.issue.fixes:
            layout.addWidget(self._build_fix_section())

    def _build_fix_section(self) -> QWidget:
        container = QFrame()
        container.setFrameShape(QFrame.Shape.StyledPanel)
        box = QVBoxLayout(container)
        box.setSpacing(4)

        caption = QLabel("<b>Possible solutions</b>")
        caption.setStyleSheet("font-size: 11px;")
        box.addWidget(caption)

        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        for index, fix in enumerate(self.issue.fixes):
            radio = QRadioButton(fix.label)
            radio.setStyleSheet("font-weight: normal;")
            radio.setProperty("fix_key", fix.key)
            self._button_group.addButton(radio, index)
            box.addWidget(radio)

            if fix.description:
                description = QLabel(fix.description)
                description.setWordWrap(True)
                description.setStyleSheet(
                    "font-weight: normal; color: #888; font-size: 10px;"
                    " margin-left: 20px;"
                )
                box.addWidget(description)

            if fix.parameter_kind is not ParameterKind.NONE:
                row = QHBoxLayout()
                row.addSpacing(20)
                row.addWidget(QLabel(fix.parameter_label or "Value:"))
                edit = QLineEdit(fix.parameter_default)
                edit.setStyleSheet("font-weight: normal;")
                if fix.parameter_kind is ParameterKind.TEMPLATE:
                    edit.setPlaceholderText("e.g. {arabic}.{letter_upper}")
                edit.textChanged.connect(
                    lambda _t, r=radio: r.setChecked(True)
                )
                row.addWidget(edit, stretch=1)
                box.addLayout(row)
                self._parameter_inputs[fix.key] = edit

            if index == 0:
                radio.setChecked(True)

        apply_row = QHBoxLayout()
        apply_row.addStretch()
        self._apply_button = QPushButton("Apply selected solution")
        self._apply_button.setStyleSheet("font-weight: normal;")
        self._apply_button.clicked.connect(self._on_apply)
        apply_row.addWidget(self._apply_button)
        box.addLayout(apply_row)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(
            "font-weight: normal; color: #4CAF50; font-size: 10px;"
        )
        self._status_label.setVisible(False)
        box.addWidget(self._status_label)

        return container

    def selected_fix(self) -> Optional[FixOption]:
        """Return the currently selected solution."""
        if self._button_group is None:
            return None
        checked = self._button_group.checkedId()
        if checked < 0 or checked >= len(self.issue.fixes):
            return None
        return self.issue.fixes[checked]

    def selected_parameter(self) -> str:
        """Return the parameter text belonging to the selected solution."""
        fix = self.selected_fix()
        if fix is None:
            return ""
        edit = self._parameter_inputs.get(fix.key)
        return edit.text() if edit is not None else ""

    def show_status(self, message: str, success: bool = True) -> None:
        """Display feedback under the solution list."""
        if not hasattr(self, "_status_label"):
            return
        color = "#4CAF50" if success else "#ff6b6b"
        self._status_label.setStyleSheet(
            f"font-weight: normal; color: {color}; font-size: 10px;"
        )
        self._status_label.setText(message)
        self._status_label.setVisible(True)

    def _on_apply(self) -> None:
        fix = self.selected_fix()
        if fix is None:
            return

        parameter = self.selected_parameter()

        if fix.parameter_kind is ParameterKind.TEMPLATE:
            error = validate_template(parameter)
            if error:
                self.show_status(error, success=False)
                return
        if fix.is_noop:
            self.show_status("Nothing to change for this solution.", success=True)
            return

        self.fix_requested.emit(self.issue, fix.key, parameter)


class AnalysisPanel(QWidget):
    """
    Scrollable panel showing statistics and every issue of a report.

    Signals:
        fix_requested(object, str, str): forwarded from the issue widgets.
    """

    fix_requested = pyqtSignal(object, str, str)

    def __init__(self, allow_fixes: bool = True, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._allow_fixes = allow_fixes
        self._report: Optional[AnalysisReport] = None
        self._issue_widgets: List[IssueWidget] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        self._headline = QLabel()
        self._headline.setWordWrap(True)
        outer.addWidget(self._headline)

        self._stats_box = QGroupBox("Statistics")
        self._stats_layout = QFormLayout(self._stats_box)
        outer.addWidget(self._stats_box)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(8)
        self._container_layout.addStretch()
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, stretch=1)

    # -- public API ------------------------------------------------------

    def set_report(self, report: AnalysisReport) -> None:
        """Replace the displayed report (rebuilds every widget)."""
        self._report = report
        self._rebuild_stats(report)
        self._rebuild_issues(report)

    def report(self) -> Optional[AnalysisReport]:
        """Return the report currently displayed."""
        return self._report

    def issue_widget(self, issue_key: str) -> Optional[IssueWidget]:
        """Return the widget rendering *issue_key*, if present."""
        for widget in self._issue_widgets:
            if widget.issue.key == issue_key:
                return widget
        return None

    # -- rendering -------------------------------------------------------

    def _rebuild_stats(self, report: AnalysisReport) -> None:
        while self._stats_layout.count():
            item = self._stats_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for key, value in report.stats.items():
            value_label = QLabel(str(value))
            value_label.setStyleSheet("font-weight: bold;")
            self._stats_layout.addRow(f"{key}:", value_label)

        self._stats_box.setVisible(bool(report.stats))

        errors = len(report.errors)
        warnings = len(report.warnings)
        infos = len(report.infos)

        if report.is_clean:
            self._headline.setText(
                "<span style='color:#4CAF50; font-size:13px;'>✓ No problems "
                "were found. The operation can run safely.</span>"
            )
        else:
            parts = []
            if errors:
                parts.append(f"<span style='color:#ff6b6b;'>{errors} problem(s) "
                             f"that block a correct result</span>")
            if warnings:
                parts.append(f"<span style='color:#FFA726;'>{warnings} "
                             f"warning(s)</span>")
            if infos:
                parts.append(f"<span style='color:#64B5F6;'>{infos} note(s)</span>")
            self._headline.setText(
                f"<b>{report.title}</b><br>" + " &nbsp;•&nbsp; ".join(parts)
            )

    def _rebuild_issues(self, report: AnalysisReport) -> None:
        # Remove previous widgets (keep the trailing stretch)
        while self._container_layout.count() > 1:
            item = self._container_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        self._issue_widgets = []

        for index, issue in enumerate(report.issues):
            widget = IssueWidget(issue, allow_fixes=self._allow_fixes, parent=self)
            widget.fix_requested.connect(self.fix_requested)
            self._container_layout.insertWidget(index, widget)
            self._issue_widgets.append(widget)

        if report.is_clean:
            placeholder = QLabel(
                "Everything checked out. You can close this window and run the "
                "operation."
            )
            placeholder.setWordWrap(True)
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            placeholder.setStyleSheet("color: #888; padding: 20px;")
            self._container_layout.insertWidget(0, placeholder)
