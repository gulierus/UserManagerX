"""
Merge dialog for the *Comparison and Sync* tab
==============================================

``MergeSourcesDialog`` replaces the old minimal "Merge Strategy" window.  It
combines three things in one place:

1. **Strategy selection** - Union or Intersection, with a live preview of how
   many students each strategy would produce.
2. **A very detailed behaviour description** - a complete, situation by
   situation explanation of what the merge does, opened with one click.
3. **A pre-flight analysis** - every problem that can make the merge fail or
   silently produce a wrong result, each with the same solutions the other
   dialogs offer.

Fixes are applied to working copies of both sources, so the sources selected
in the panels are never modified.
"""

import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QRadioButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from models import Source
from ui.analysis_widgets import AnalysisPanel
from utils.source_analysis import (
    AnalysisReport, SourceIssue, analyze_merge, apply_fix,
)

logger = logging.getLogger(__name__)


#: Long-form documentation shown by the "Detailed behaviour" tab.
MERGE_BEHAVIOUR_HTML = """
<h3>How merging works</h3>
<p>A merge never modifies the two selected sources. It always creates a
<b>new, editable source</b> that contains copies of the selected students.</p>

<h3>How two students are recognised as the same person</h3>
<p>A student is identified by the triple<br>
<b>(first name, last name, class name)</b>.</p>
<ul>
  <li>First and last name are compared <b>without diacritics</b> and
      <b>case insensitively</b>: <i>"Novák"</i> and <i>"novak"</i> are the
      same person.</li>
  <li>The class name is compared <b>exactly</b>, including case and spaces:
      <i>"6.A"</i>, <i>"6. A"</i> and <i>"VI.A"</i> are three different
      classes, so the same child counts as three different people.</li>
  <li>Consequence: unify the class names of both sources before merging if
      they use different notations. The analysis tab detects this and offers
      the conversion.</li>
</ul>

<h3>Strategy: Union</h3>
<ul>
  <li>The result contains <b>every student of both sources</b>.</li>
  <li>A student present in both sources is taken <b>from the left source</b>;
      the right-hand record (including its AD user name, password, e-mail,
      groups and home directory) is discarded.</li>
  <li>Students that exist only in the right source are added unchanged.</li>
  <li>Classes are created as needed. A class that exists in both sources ends
      up as one class containing the students of both.</li>
  <li>An empty source on one side means the result equals the other source.</li>
</ul>

<h3>Strategy: Intersection</h3>
<ul>
  <li>The result contains <b>only students that exist in both sources</b>.</li>
  <li>The data always comes from the <b>left</b> source.</li>
  <li>Students that exist in only one source are dropped.</li>
  <li>If the two sources have no student in common - which happens easily when
      the class names differ - the result is <b>empty</b>. The analysis warns
      about this before you run the merge.</li>
  <li>An empty source on either side always produces an empty result.</li>
</ul>

<h3>Situation by situation</h3>
<table cellpadding="4" cellspacing="0" border="1" width="100%">
<tr><th align="left">Situation</th><th align="left">Union</th><th align="left">Intersection</th></tr>
<tr><td>Student only in the left source</td><td>kept (left data)</td><td>dropped</td></tr>
<tr><td>Student only in the right source</td><td>kept (right data)</td><td>dropped</td></tr>
<tr><td>Student in both, identical data</td><td>kept once</td><td>kept once</td></tr>
<tr><td>Student in both, different AD data</td><td>kept once, <b>left</b> data wins</td><td>kept once, <b>left</b> data wins</td></tr>
<tr><td>Same student, different class</td><td>kept <b>twice</b> (one record per class)</td><td>dropped - the class is part of the identity</td></tr>
<tr><td>Same student listed twice inside one source</td><td>kept once</td><td>kept once</td></tr>
<tr><td>Student without a first or last name</td><td>all such records collapse into one per class</td><td>same</td></tr>
<tr><td>Class exists in both sources</td><td>one class with all students</td><td>one class with the common students</td></tr>
<tr><td>Class written differently ("6.A" vs "VI.A")</td><td>two separate classes in the result</td><td>no student matches - class disappears</td></tr>
<tr><td>Empty class</td><td>not carried over (classes are rebuilt from students)</td><td>not carried over</td></tr>
<tr><td>One source is empty</td><td>result equals the other source</td><td>result is empty</td></tr>
<tr><td>Both panels point to the same source</td><td>exact copy</td><td>exact copy</td></tr>
<tr><td>A source is read-only</td><td>no problem - the result is a new editable source</td><td>same</td></tr>
<tr><td>A student's <i>class_name</i> differs from the class object it is stored in</td>
    <td>the student is grouped by their own <i>class_name</i></td>
    <td>same</td></tr>
</table>

<h3>What the result looks like</h3>
<ul>
  <li>Name: the name you enter after confirming this dialog.</li>
  <li>Type: <i>manual</i> - the result is always editable, even when both
      inputs were read-only.</li>
  <li>Classes: rebuilt from the students' own class names. Left-hand classes
      keep their original order, right-hand-only classes are appended.</li>
  <li>Students are <b>copies</b>. Editing the result never changes the two
      input sources.</li>
</ul>
"""


class MergeSourcesDialog(QDialog):
    """
    Strategy selection, behaviour documentation and pre-flight analysis.

    Use :meth:`get_strategy`, :meth:`get_left_source` and
    :meth:`get_right_source` after the dialog was accepted - the returned
    sources are the (possibly repaired) working copies that must be merged.
    """

    def __init__(self, left: Source, right: Source, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.original_left = left
        self.original_right = right
        self.left = self._copy(left)
        self.right = self._copy(right)
        self.fixes_applied = []

        self.setWindowTitle("Merge Both Sources")
        self.setModal(True)
        self.resize(900, 760)

        self._build_ui()
        self._refresh_analysis()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _copy(source: Source) -> Source:
        try:
            clone = source.deep_copy()
            clone.readonly = False
            return clone
        except Exception:                            # pragma: no cover - defensive
            logger.exception("Could not copy source %r for merge analysis", source.name)
            return source

    # -- UI ----------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        header = QLabel(
            f"<b>Left source:</b> {self.original_left.name} &nbsp;&nbsp;"
            f"<b>Right source:</b> {self.original_right.name}"
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        tabs = QTabWidget()

        # --- strategy -----------------------------------------------------
        strategy_tab = QWidget()
        strategy_layout = QVBoxLayout(strategy_tab)

        strategy_group = QGroupBox("Select merge strategy")
        group_layout = QVBoxLayout(strategy_group)

        self.button_group = QButtonGroup(self)

        self._union_radio = QRadioButton("Union (combine all persons)")
        self._union_radio.setToolTip(
            "Output contains all persons from both sources.\n"
            "If a person exists in both, data from the left source is used."
        )
        self.button_group.addButton(self._union_radio, 1)
        group_layout.addWidget(self._union_radio)

        union_description = QLabel(
            "• Contains all persons from both sources\n"
            "• Duplicates use left source data\n"
            "• New persons from the right source are added"
        )
        union_description.setStyleSheet(
            "color: #aaa; margin-left: 20px; font-size: 11px;"
        )
        group_layout.addWidget(union_description)

        group_layout.addSpacing(10)

        self._intersection_radio = QRadioButton("Intersection (only common persons)")
        self._intersection_radio.setToolTip(
            "Output contains only persons that exist in BOTH sources.\n"
            "Data is taken from the left source."
        )
        self.button_group.addButton(self._intersection_radio, 2)
        group_layout.addWidget(self._intersection_radio)

        intersection_description = QLabel(
            "• Contains only persons in BOTH sources\n"
            "• Uses data from the left source\n"
            "• Persons unique to one source are excluded"
        )
        intersection_description.setStyleSheet(
            "color: #aaa; margin-left: 20px; font-size: 11px;"
        )
        group_layout.addWidget(intersection_description)

        self._union_radio.setChecked(True)
        self.button_group.idToggled.connect(self._on_strategy_changed)

        strategy_layout.addWidget(strategy_group)

        self._expected_label = QLabel()
        self._expected_label.setWordWrap(True)
        self._expected_label.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px;"
        )
        strategy_layout.addWidget(self._expected_label)
        strategy_layout.addStretch()

        tabs.addTab(strategy_tab, "1. Strategy")

        # --- detailed behaviour -------------------------------------------
        behaviour_tab = QWidget()
        behaviour_layout = QVBoxLayout(behaviour_tab)
        behaviour_hint = QLabel(
            "Complete description of what the merge does in every situation."
        )
        behaviour_hint.setStyleSheet("color: #aaa; font-size: 10px;")
        behaviour_layout.addWidget(behaviour_hint)

        behaviour_text = QTextEdit()
        behaviour_text.setReadOnly(True)
        behaviour_text.setHtml(MERGE_BEHAVIOUR_HTML)
        behaviour_layout.addWidget(behaviour_text, stretch=1)
        tabs.addTab(behaviour_tab, "2. Detailed behaviour")

        # --- analysis ------------------------------------------------------
        analysis_tab = QWidget()
        analysis_layout = QVBoxLayout(analysis_tab)
        analysis_hint = QLabel(
            "Problems that can make the merge fail or produce a surprising "
            "result. Solutions are applied to working copies - the two "
            "selected sources are never modified."
        )
        analysis_hint.setWordWrap(True)
        analysis_hint.setStyleSheet("color: #aaa; font-size: 10px;")
        analysis_layout.addWidget(analysis_hint)

        self.analysis_panel = AnalysisPanel(allow_fixes=True, parent=self)
        self.analysis_panel.fix_requested.connect(self._on_fix_requested)
        analysis_layout.addWidget(self.analysis_panel, stretch=1)
        tabs.addTab(analysis_tab, "3. Analysis and fixes")

        self._tabs = tabs
        layout.addWidget(tabs, stretch=1)

        self._changes_label = QLabel()
        self._changes_label.setWordWrap(True)
        self._changes_label.setStyleSheet("color: #4CAF50; font-size: 10px;")
        self._changes_label.setVisible(False)
        layout.addWidget(self._changes_label)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setText("Merge")
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    # -- behaviour ---------------------------------------------------------

    def _on_strategy_changed(self, _id: int, checked: bool) -> None:
        if checked:
            self._refresh_analysis()

    def _refresh_analysis(self) -> None:
        try:
            report: AnalysisReport = analyze_merge(
                self.left, self.right, self.get_strategy()
            )
        except Exception as exc:
            logger.exception("Merge analysis failed")
            QMessageBox.critical(
                self, "Analysis Error",
                f"The merge analysis could not be completed:\n\n{exc}"
            )
            return

        self.analysis_panel.set_report(report)

        stats = report.stats
        strategy = self.get_strategy()
        expected = stats.get(
            "Result with 'union'" if strategy == "union" else "Result with 'intersection'",
            0,
        )
        self._expected_label.setText(
            f"<b>Expected result with '{strategy}': {expected} student(s)</b><br>"
            f"Left: {stats.get('Left students', 0)} &nbsp;•&nbsp; "
            f"Right: {stats.get('Right students', 0)} &nbsp;•&nbsp; "
            f"only left: {stats.get('Students only in left', 0)} &nbsp;•&nbsp; "
            f"only right: {stats.get('Students only in right', 0)} &nbsp;•&nbsp; "
            f"in both: {stats.get('Students in both', 0)}"
            + ("<br><span style='color:#ff6b6b;'>The result would be empty - "
               "open the analysis tab.</span>" if expected == 0 else "")
        )

    def _on_fix_requested(self, issue: SourceIssue, fix_key: str,
                          parameter: str) -> None:
        targets = []
        if issue.target == "left":
            targets = [("Left", self.left)]
        elif issue.target == "right":
            targets = [("Right", self.right)]
        else:
            targets = [("Left", self.left), ("Right", self.right)]

        messages = []
        changed_total = 0
        for label, source in targets:
            try:
                result = apply_fix(source, issue, fix_key, parameter)
            except Exception as exc:                 # pragma: no cover - defensive
                logger.exception("Merge fix %s failed for %s", fix_key, label)
                QMessageBox.critical(self, "Fix Failed",
                                     f"The solution could not be applied:\n\n{exc}")
                return
            if not result.applied:
                QMessageBox.warning(self, "Fix Not Applied", result.message)
                return
            changed_total += result.changed
            messages.append(f"{label}: {result.message}")

        if changed_total:
            self.fixes_applied.append(f"{issue.title} - " + " ".join(messages))
            self._changes_label.setVisible(True)
            self._changes_label.setText(
                "Applied solutions (working copies only):<br>" +
                "<br>".join(f"• {entry}" for entry in self.fixes_applied)
            )

        self._refresh_analysis()
        widget = self.analysis_panel.issue_widget(issue.key)
        if widget:
            widget.show_status(" ".join(messages), success=True)

    def _on_accept(self) -> None:
        report = self.analysis_panel.report()
        if report and report.has_blocking_issues:
            titles = "\n".join(f"• {issue.title}" for issue in report.errors)
            reply = QMessageBox.question(
                self, "Problems Detected",
                f"The analysis found problems that will affect the result:\n\n"
                f"{titles}\n\nMerge anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._tabs.setCurrentIndex(2)
                return
        self.accept()

    # -- results -----------------------------------------------------------

    def get_strategy(self) -> str:
        """Return ``'union'`` or ``'intersection'``."""
        return "union" if self.button_group.checkedId() == 1 else "intersection"

    def get_left_source(self) -> Source:
        """Return the (possibly repaired) left working copy."""
        return self.left

    def get_right_source(self) -> Source:
        """Return the (possibly repaired) right working copy."""
        return self.right

    def has_changes(self) -> bool:
        """True when a fix was applied to one of the working copies."""
        return bool(self.fixes_applied)
