"""
Turning Microsoft 365 groups into school classes
================================================

Version 26, point 21 a describes a two-step window, and both steps exist for a
reason:

**Step 1 - choose.**  A tenant holds every group in the school: classes, staff,
projects, distribution lists.  The user picks the ones that are classes.  The
left half lists them with a search box and select-all / clear-all; the right
half shows the owners, the members and the facts that tell one group from
another.

**Step 2 - name.**  A Microsoft 365 group is not a class: it has no class name
and nothing in it says "6.A".  Without this step a source would arrive with
classes called *Trida-6.A-2020* or worse, and it would be useless on the
"2. Comparison and Sync" tab, which matches classes by name.  The user names
each class - by hand, or with a template that pulls the name out of the group's
own fields.

The wizard produces :class:`~models_m365.M365GroupSelection` objects and
nothing else; building the :class:`~models.Source` from them is the caller's
job, so this dialog can be reused by the "from file" import unchanged.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

from models_m365 import M365Group, M365GroupSelection
from utils.name_templates import (
    GROUP_FIELDS, TemplateError, describe_operations, group_values, preview,
    render,
)

logger = logging.getLogger(__name__)

#: Item data role carrying the group behind a row.
GROUP_ROLE = int(Qt.ItemDataRole.UserRole) + 1

#: The template the class-name field starts with.  It covers the common school
#: convention - a group called "Trida-6.A" becomes the class "6.A" - and is
#: harmless when it does not apply, because the preview shows the result before
#: anything is created.
DEFAULT_CLASS_TEMPLATE = "{display_name|after:Trida-}"


class GroupSelectionStep(QWidget):
    """Step 1: which groups and teams become classes."""

    def __init__(self, groups: List[M365Group], parent=None):
        """
        Args:
            groups: Everything read from the tenant.
            parent: Qt parent.
        """
        super().__init__(parent)
        self.groups = list(groups)
        self._init_ui()
        self._populate()

    # -- construction -----------------------------------------------------

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        caption = QLabel(
            "<b>Step 1 of 2 — choose the groups and teams</b><br>"
            "<span style='color:#888;'>Tick the groups that are school "
            "classes. Click a group to see who is in it.</span>"
        )
        caption.setWordWrap(True)
        layout.addWidget(caption)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- left: the list ------------------------------------------------
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 6, 0)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by name, alias or description...")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._apply_filter)
        left_layout.addWidget(self.search_input)

        self.group_list = QListWidget()
        self.group_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.group_list.currentItemChanged.connect(self._show_details)
        self.group_list.itemChanged.connect(self._on_item_checked)
        left_layout.addWidget(self.group_list, stretch=1)

        buttons = QHBoxLayout()
        select_all = QPushButton("Select all shown")
        select_all.setToolTip(
            "Tick every group the search currently shows.\n"
            "Groups hidden by the search are left alone."
        )
        select_all.clicked.connect(lambda: self._set_all_shown(True))
        buttons.addWidget(select_all)

        clear_all = QPushButton("Clear selection")
        clear_all.setToolTip("Untick every group, including hidden ones.")
        clear_all.clicked.connect(self._clear_selection)
        buttons.addWidget(clear_all)
        buttons.addStretch()
        left_layout.addLayout(buttons)

        self.count_label = QLabel()
        self.count_label.setStyleSheet("color: #888; font-size: 11px;")
        left_layout.addWidget(self.count_label)

        splitter.addWidget(left)

        # --- right: the details --------------------------------------------
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)

        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        self.details_text.setMaximumHeight(150)
        right_layout.addWidget(self.details_text)

        people_group = QGroupBox("Owners and members")
        people_layout = QVBoxLayout(people_group)
        self.people_list = QListWidget()
        people_layout.addWidget(self.people_list)
        right_layout.addWidget(people_group, stretch=1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)

    # -- data -------------------------------------------------------------

    def _populate(self) -> None:
        """Fill the list from the groups, remembering nothing is ticked yet."""
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for group in self.groups:
            item = QListWidgetItem(self._row_text(group))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(GROUP_ROLE, group)
            item.setToolTip(group.description or group.display_name)
            self.group_list.addItem(item)
        self.group_list.blockSignals(False)
        self._update_count()

    @staticmethod
    def _row_text(group: M365Group) -> str:
        """One line describing a group in the list."""
        if group.members_loaded:
            people = f"{group.member_count} member(s)"
        else:
            # An empty list means "not read yet" here, and saying "0 members"
            # would be a lie the user would act on.
            people = "members not read"
        return f"{group.display_name}  ·  {group.kind}  ·  {people}"

    def _apply_filter(self, needle: str) -> None:
        """Hide the rows that do not match the search box."""
        for row in range(self.group_list.count()):
            item = self.group_list.item(row)
            group = item.data(GROUP_ROLE)
            item.setHidden(not group.matches(needle))
        self._update_count()

    def _set_all_shown(self, checked: bool) -> None:
        """Tick or untick every row the search currently shows."""
        self.group_list.blockSignals(True)
        for row in range(self.group_list.count()):
            item = self.group_list.item(row)
            if not item.isHidden():
                item.setCheckState(Qt.CheckState.Checked if checked
                                   else Qt.CheckState.Unchecked)
        self.group_list.blockSignals(False)
        self._update_count()

    def _clear_selection(self) -> None:
        """
        Untick everything, including rows the search is hiding.

        Deliberately not symmetrical with "select all shown": a user who clears
        the selection means *all of it*, and leaving hidden ticks behind would
        carry invisible choices into step 2.
        """
        self.group_list.blockSignals(True)
        for row in range(self.group_list.count()):
            self.group_list.item(row).setCheckState(Qt.CheckState.Unchecked)
        self.group_list.blockSignals(False)
        self._update_count()

    def _on_item_checked(self, _item) -> None:
        self._update_count()

    def _update_count(self) -> None:
        """Say how many are shown and how many are ticked."""
        shown = sum(1 for row in range(self.group_list.count())
                    if not self.group_list.item(row).isHidden())
        self.count_label.setText(
            f"{shown} of {self.group_list.count()} shown · "
            f"{len(self.selected_groups())} selected"
        )

    def _show_details(self, item, _previous=None) -> None:
        """Describe the clicked group on the right."""
        if item is None:
            self.details_text.clear()
            self.people_list.clear()
            return

        group: M365Group = item.data(GROUP_ROLE)
        self.details_text.setHtml(self._details_html(group))

        self.people_list.clear()
        if not group.members_loaded:
            self.people_list.addItem(
                "The members of this group have not been read.")
            return

        for owner in group.owners:
            row = QListWidgetItem(f"👑  {owner.best_name}   ({owner.user_principal_name})")
            row.setToolTip("Owner")
            self.people_list.addItem(row)
        for member in group.members:
            row = QListWidgetItem(f"👤  {member.best_name}   ({member.user_principal_name})")
            if not member.account_enabled:
                row.setToolTip("This account is disabled")
            self.people_list.addItem(row)

        if not group.owners and not group.members:
            self.people_list.addItem("This group has no owners and no members.")

    @staticmethod
    def _details_html(group: M365Group) -> str:
        """The facts that tell one group from another."""
        def row(label: str, value: str) -> str:
            if not value:
                return ""
            return (f"<tr><td style='color:#888;padding-right:10px;'>{label}</td>"
                    f"<td>{value}</td></tr>")

        return (
            f"<div style='font-size:13px;'><b>{group.display_name}</b></div>"
            f"<table style='font-size:12px;'>"
            + row("Type", group.kind)
            + row("Alias", group.mail_nickname)
            + row("Email", group.mail)
            + row("Visibility", group.visibility)
            + row("Description", group.description)
            + row("Owners", ", ".join(group.owner_names))
            + row("Members", str(group.member_count) if group.members_loaded
                  else "not read")
            + "</table>"
        )

    # -- result -----------------------------------------------------------

    def selected_groups(self) -> List[M365Group]:
        """The ticked groups, in the order they are listed."""
        chosen = []
        for row in range(self.group_list.count()):
            item = self.group_list.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                chosen.append(item.data(GROUP_ROLE))
        return chosen


class ClassNamingStep(QWidget):
    """Step 2: one class per selected group, and what it is called."""

    #: Table columns.
    COLUMNS = ["Group or team", "People", "Class name"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selections: List[M365GroupSelection] = []
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        caption = QLabel(
            "<b>Step 2 of 2 — name the classes</b><br>"
            "<span style='color:#888;'>Each selected group becomes one class. "
            "A Microsoft 365 group carries no class name, so the name has to "
            "come from you — type it, or build it from the group's own fields "
            "with a template.</span>"
        )
        caption.setWordWrap(True)
        layout.addWidget(caption)

        # --- the template row ------------------------------------------------
        template_group = QGroupBox("Build every name from a template")
        template_layout = QVBoxLayout(template_group)

        row = QHBoxLayout()
        row.addWidget(QLabel("Template:"))
        self.template_input = QLineEdit(DEFAULT_CLASS_TEMPLATE)
        self.template_input.setPlaceholderText("{display_name}")
        self.template_input.textChanged.connect(self._update_preview)
        row.addWidget(self.template_input, stretch=1)

        apply_button = QPushButton("Apply to all")
        apply_button.setToolTip(
            "Fill in every class name from the template.\n"
            "Names you have already typed by hand are overwritten."
        )
        apply_button.clicked.connect(lambda: self.apply_template(only_empty=False))
        row.addWidget(apply_button)
        template_layout.addLayout(row)

        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet("color: #888; font-size: 11px;")
        template_layout.addWidget(self.preview_label)

        help_label = QLabel(
            "Fields: " + " · ".join(f"<code>{{{name}}}</code>"
                                    for name in GROUP_FIELDS)
            + "<br>Parts: <code>{display_name[6:9]}</code> · "
            + describe_operations().replace("|", "<code>|") .replace(" ", " ")
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #777; font-size: 10px;")
        template_layout.addWidget(help_label)

        layout.addWidget(template_group)

        # --- the table --------------------------------------------------------
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._on_name_edited)
        layout.addWidget(self.table, stretch=1)

        # --- options ----------------------------------------------------------
        self.include_owners_check = QCheckBox(
            "Include the owners of each group as pupils")
        self.include_owners_check.setToolTip(
            "Off by default: in a school group the owner is usually the "
            "teacher, not a pupil."
        )
        self.include_owners_check.toggled.connect(self._on_include_owners)
        layout.addWidget(self.include_owners_check)

        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: #ff6b6b; font-size: 11px;")
        layout.addWidget(self.problem_label)

    # -- filling ----------------------------------------------------------

    def set_groups(self, groups: List[M365Group]) -> None:
        """
        Start step 2 with the groups chosen in step 1.

        A name the user already typed for a group is kept when they go back to
        step 1 and return - losing it would punish them for checking something.
        """
        previous = {selection.group.object_id or selection.group.display_name:
                    selection.class_name for selection in self.selections}

        self.selections = []
        for group in groups:
            key = group.object_id or group.display_name
            self.selections.append(M365GroupSelection(
                group=group,
                class_name=previous.get(key, ""),
                include_owners=self.include_owners_check.isChecked(),
            ))

        # Fill in the names that are still empty - the ones the user has
        # already typed (or that came back from a trip to step 1) are left
        # alone.  Applying the template only when *nothing* was remembered
        # left a group added after the first visit with no name at all.
        self.apply_template(only_empty=True)
        self._update_preview()

    def _rebuild_table(self) -> None:
        """Draw one row per selected group."""
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.selections))

        for row, selection in enumerate(self.selections):
            group = selection.group

            name_item = QTableWidgetItem(group.display_name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setToolTip(f"{group.kind} · {group.mail_nickname}")
            self.table.setItem(row, 0, name_item)

            count = len(selection.people())
            people_item = QTableWidgetItem(str(count))
            people_item.setFlags(people_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if not group.members_loaded:
                people_item.setText("?")
                people_item.setToolTip(
                    "The members of this group were not read, so the class "
                    "would be created empty.")
            self.table.setItem(row, 1, people_item)

            self.table.setItem(row, 2, QTableWidgetItem(selection.class_name))

        self.table.blockSignals(False)
        self._validate()

    # -- the template -----------------------------------------------------

    def apply_template(self, only_empty: bool = False) -> None:
        """
        Fill class names from the template.

        Args:
            only_empty: Fill in only the names that are still blank, leaving
                anything the user typed alone.  The "Apply to all" button
                passes False, because that is what the button says it does.
        """
        template = self.template_input.text()
        for selection in self.selections:
            if only_empty and (selection.class_name or "").strip():
                continue
            try:
                selection.class_name = render(template,
                                              group_values(selection.group))
            except TemplateError:
                # The preview already says what is wrong; leaving the name
                # empty is better than writing an error message into it.
                selection.class_name = ""
        self._rebuild_table()

    def _update_preview(self) -> None:
        """Show what the template makes of the first selected group."""
        if not self.selections:
            self.preview_label.setText("")
            return
        first = self.selections[0].group
        rendered = preview(self.template_input.text(), group_values(first))
        self.preview_label.setText(
            f"{first.display_name}  →  <b>{rendered}</b>")

    def _on_include_owners(self, checked: bool) -> None:
        """The owners count as pupils, or they do not."""
        for selection in self.selections:
            selection.include_owners = checked
        self._rebuild_table()

    def _on_name_edited(self, item) -> None:
        """Take a hand-typed class name."""
        if item.column() != 2:
            return
        row = item.row()
        if 0 <= row < len(self.selections):
            self.selections[row].class_name = item.text().strip()
        self._validate()

    # -- validation -------------------------------------------------------

    def problems(self) -> List[str]:
        """
        Everything that would make the resulting source unusable.

        Returns:
            A list of problems, empty when the step is complete.
        """
        problems: List[str] = []

        if not self.selections:
            return ["No group was selected in step 1."]

        nameless = [selection.group.display_name for selection in self.selections
                    if not (selection.class_name or "").strip()]
        if nameless:
            shown = ", ".join(nameless[:3])
            more = f" and {len(nameless) - 3} more" if len(nameless) > 3 else ""
            problems.append(f"These groups have no class name: {shown}{more}.")

        seen: Dict[str, str] = {}
        duplicates = []
        for selection in self.selections:
            name = (selection.class_name or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                duplicates.append(name)
            seen[key] = name
        if duplicates:
            # Two classes with one name are indistinguishable everywhere the
            # application looks a class up by name.
            problems.append(
                "Two or more classes would have the same name: "
                + ", ".join(sorted(set(duplicates)))
            )

        return problems

    def _validate(self) -> None:
        """Show the problems under the table."""
        problems = self.problems()
        self.problem_label.setText("  ".join(problems))

    def result(self) -> List[M365GroupSelection]:
        """The finished selections, ready to be turned into classes."""
        return list(self.selections)


class M365ImportWizard(QDialog):
    """
    The two-step window that turns Microsoft 365 groups into school classes.

    Used by both Microsoft 365 sources - the one that reads the tenant and the
    one that reads a file - because the problem is the same once the groups are
    in memory, and so the window should be.

    Example:
        wizard = M365ImportWizard(groups, parent=self)
        if wizard.exec() == QDialog.DialogCode.Accepted:
            for selection in wizard.result():
                ...
    """

    def __init__(self, groups: List[M365Group], parent=None,
                 title: str = "Import from Microsoft 365"):
        """
        Args:
            groups: Everything that could become a class.
            parent: Qt parent.
            title: Window title, so the file import can say where the groups
                came from.
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(900, 620)

        self._groups = list(groups)
        self._init_ui()
        self._show_step(0)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.steps = QStackedWidget()
        self.selection_step = GroupSelectionStep(self._groups)
        self.naming_step = ClassNamingStep()
        self.steps.addWidget(self.selection_step)
        self.steps.addWidget(self.naming_step)
        layout.addWidget(self.steps, stretch=1)

        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.setStyleSheet("color: #ff6b6b;")
        layout.addWidget(self.message_label)

        buttons = QHBoxLayout()
        self.back_button = QPushButton("< Back")
        self.back_button.clicked.connect(self.go_back)
        buttons.addWidget(self.back_button)
        buttons.addStretch()

        self.next_button = QPushButton("Next >")
        self.next_button.setDefault(True)
        self.next_button.clicked.connect(self.go_next)
        buttons.addWidget(self.next_button)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    # -- navigation -------------------------------------------------------

    def _show_step(self, index: int) -> None:
        """Move to a step and set the buttons to match it."""
        self.steps.setCurrentIndex(index)
        self.back_button.setEnabled(index > 0)
        self.next_button.setText("Finish" if index == 1 else "Next >")
        self.message_label.setText("")

    def go_back(self) -> None:
        """
        Return to step 1.

        The names typed in step 2 are kept; :meth:`ClassNamingStep.set_groups`
        restores them when the user comes forward again.
        """
        self._show_step(0)

    def go_next(self) -> None:
        """Move forward, or finish - whichever the current step means."""
        if self.steps.currentIndex() == 0:
            chosen = self.selection_step.selected_groups()
            if not chosen:
                self.message_label.setText(
                    "Tick at least one group or team to continue.")
                return
            self.naming_step.set_groups(chosen)
            self._show_step(1)
            return

        problems = self.naming_step.problems()
        if problems:
            self.message_label.setText("  ".join(problems))
            return
        self.accept()

    # -- result -----------------------------------------------------------

    def result_selections(self) -> List[M365GroupSelection]:
        """The groups, their class names and whether owners count as pupils."""
        return self.naming_step.result()
