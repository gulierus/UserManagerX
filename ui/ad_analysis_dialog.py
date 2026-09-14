"""
Active Directory source analysis dialog
=======================================

Replaces the message box that used to report the analysis result.

A message box could only say *how many* records had problems; it could not show
**which** ones, could not be resized, and offered no way to act on what it
found. This dialog lists every affected person in a table that matches the one
on the Operations tab, highlights the cells that are actually wrong, and can
repair them - one person at a time or all at once.

"Repairing" means filling in what the validator reported as missing or
malformed: the user name, the password and the display name, using the formats
configured in the *Formats…* menu. Data only a human can supply (a missing
surname, for instance) is reported but never invented.
"""

import logging
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from models import Person
from services.ad_validator import ADValidator

logger = logging.getLogger(__name__)

#: Same look as the person table on the Operations tab.
TABLE_STYLE = """
    QTableWidget {
        gridline-color: #555;
    }
    QTableWidget::item {
        padding: 5px;
    }
    QTableWidget::item:selected {
        background-color: #4a6fa5;
    }
    QHeaderView::section {
        padding: 5px;
        border: 1px solid #555;
        font-weight: bold;
    }
"""

#: Cell backgrounds for the two severities.
ERROR_COLOR = QColor("#5a2a2a")
WARNING_COLOR = QColor("#5a4a2a")

#: Person fields shown as columns, in order: ``(caption, attribute)``.
FIELD_COLUMNS = [
    ("Name", None),
    ("Class", "class_name"),
    ("Username", "ad_username"),
    ("Password", "ad_password"),
    ("Email", "ad_email"),
    ("Display Name", "ad_display_name"),
]

#: Validator field names that the automatic repair can supply.
REPAIRABLE_FIELDS = {"ad_username", "ad_password", "ad_display_name"}


class ADAnalysisDialog(QDialog):
    """Detailed, actionable report of an Active Directory source analysis."""

    def __init__(self, source, parent: Optional[QWidget] = None):
        """
        Args:
            source: The source to analyse.
            parent: Parent widget.
        """
        super().__init__(parent)
        self.source = source
        self.changed = False

        self.setWindowTitle(f"Source Analysis - {getattr(source, 'name', '')}")
        self.setModal(True)
        # Resizable, with a sensible starting size: the table needs room.
        self.setSizeGripEnabled(True)
        self.resize(1050, 680)
        self.setMinimumSize(640, 400)

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Everything above the buttons scrolls, so the window works even when
        # it is made small.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(4, 4, 4, 4)

        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        self._summary_label.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px;"
        )
        content_layout.addWidget(self._summary_label)

        legend = QLabel(
            "Cells with a problem are highlighted: "
            "<span style='background-color:#5a2a2a;'>&nbsp;error&nbsp;</span> "
            "&nbsp; <span style='background-color:#5a4a2a;'>&nbsp;warning&nbsp;</span>"
            " &nbsp;·&nbsp; <b>Fix</b> fills in the user name, password and "
            "display name that are missing or malformed."
        )
        legend.setWordWrap(True)
        legend.setStyleSheet("color: #aaa; font-size: 10px;")
        content_layout.addWidget(legend)

        table_group = QGroupBox("Persons with problems")
        table_layout = QVBoxLayout(table_group)

        self._table = QTableWidget(0, len(FIELD_COLUMNS) + 2)
        self._table.setHorizontalHeaderLabels(
            [caption for caption, _ in FIELD_COLUMNS] + ["Problems", "Fix"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setStyleSheet(TABLE_STYLE)

        header = self._table.horizontalHeader()
        for index in range(len(FIELD_COLUMNS)):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(len(FIELD_COLUMNS), QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(len(FIELD_COLUMNS) + 1,
                                    QHeaderView.ResizeMode.ResizeToContents)

        self._table.setMinimumHeight(280)
        table_layout.addWidget(self._table)
        content_layout.addWidget(table_group, stretch=1)

        scroll.setWidget(content)
        layout.addWidget(scroll, stretch=1)

        # --- actions --------------------------------------------------------
        action_row = QHBoxLayout()
        self._fix_all_button = QPushButton("🔧 Fix All Listed Persons")
        self._fix_all_button.setToolTip(
            "Fill in the user name, password and display name for every person "
            "in the table that is missing one."
        )
        self._fix_all_button.clicked.connect(self.fix_all)
        action_row.addWidget(self._fix_all_button)

        self._status_label = QLabel()
        self._status_label.setStyleSheet("color: #4CAF50; font-size: 11px;")
        action_row.addWidget(self._status_label)
        action_row.addStretch()
        layout.addLayout(action_row)

        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        button_box.rejected.connect(self.accept)
        layout.addWidget(button_box)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _collect_problems(self) -> Dict[int, Dict]:
        """
        Validate the source and group the issues by person.

        Returns:
            ``{id(person): {"person": p, "fields": {field: severity},
            "messages": [...]}}`` for every person that has at least one issue.
        """
        result = ADValidator.validate_source(self.source)

        grouped: Dict[int, Dict] = {}
        for issue in result.issues:
            person = issue.person
            if person is None:
                continue
            entry = grouped.setdefault(id(person), {
                "person": person, "fields": {}, "messages": [],
            })
            # An error on a field outranks a warning on the same field.
            existing = entry["fields"].get(issue.field)
            if existing != "error":
                entry["fields"][issue.field] = issue.severity
            entry["messages"].append(f"{issue.field}: {issue.message}")
        return grouped

    def refresh(self) -> None:
        """Re-validate and rebuild the table."""
        grouped = self._collect_problems()
        persons = self.source.get_all_persons()

        errors = sum(1 for entry in grouped.values()
                     if "error" in entry["fields"].values())
        self._summary_label.setText(
            f"<b>{len(persons)}</b> person(s) in '{self.source.name}' · "
            f"<b>{len(grouped)}</b> with problems · "
            f"<b>{errors}</b> with errors that block synchronisation"
            + ("<br><span style='color:#4CAF50;'>✓ Everything is ready for "
               "synchronisation.</span>" if not grouped else "")
        )

        self._table.setRowCount(len(grouped))
        self._fix_all_button.setEnabled(bool(grouped))

        for row, entry in enumerate(grouped.values()):
            person = entry["person"]
            fields = entry["fields"]

            for column, (caption, attribute) in enumerate(FIELD_COLUMNS):
                if attribute is None:
                    text = f"{person.first_name} {person.last_name}".strip() or "(no name)"
                    severity = (fields.get("first_name")
                                or fields.get("last_name"))
                else:
                    value = getattr(person, attribute, None)
                    text = value if value else "(not set)"
                    severity = fields.get(attribute)

                item = QTableWidgetItem(str(text))
                item.setData(Qt.ItemDataRole.UserRole, person)
                if severity == "error":
                    item.setBackground(ERROR_COLOR)
                elif severity == "warning":
                    item.setBackground(WARNING_COLOR)
                self._table.setItem(row, column, item)

            problems_item = QTableWidgetItem("; ".join(entry["messages"]))
            problems_item.setToolTip("\n".join(entry["messages"]))
            self._table.setItem(row, len(FIELD_COLUMNS), problems_item)

            fix_button = QPushButton("🔧 Fix")
            fix_button.setEnabled(bool(REPAIRABLE_FIELDS & set(fields)))
            fix_button.setToolTip(
                "Fill in the missing user name, password and display name"
                if fix_button.isEnabled() else
                "Nothing here can be filled in automatically"
            )
            fix_button.clicked.connect(
                lambda _checked, p=person: self.fix_person(p)
            )
            self._table.setCellWidget(row, len(FIELD_COLUMNS) + 1, fix_button)

    # ------------------------------------------------------------------
    # Repair
    # ------------------------------------------------------------------

    def _repair(self, person: Person) -> Optional[str]:
        """
        Fill in whatever can be generated for one person.

        Args:
            person: The person to repair.

        Returns:
            ``None`` on success, otherwise why it could not be done.
        """
        from utils.ad_utils import (
            generate_display_name, generate_password, generate_username,
        )

        first = (person.first_name or "").strip()
        last = (person.last_name or "").strip()
        if not first or not last:
            missing = " and ".join(
                name for name, value in (("first name", first), ("last name", last))
                if not value
            )
            return f"missing {missing} - this cannot be generated"

        taken = {
            other.ad_username for other in self.source.get_all_persons()
            if other.ad_username and other is not person
        }

        if not person.ad_username:
            try:
                person.ad_username = generate_username(
                    first, last, taken, class_name=(person.class_name or "").strip()
                )
            except (ValueError, IndexError, RuntimeError) as exc:
                return f"user name could not be generated ({exc})"

        if not person.ad_password:
            try:
                person.ad_password = generate_password()
            except ValueError as exc:
                return f"password could not be generated ({exc})"

        if not person.ad_display_name:
            class_name = (person.class_name or "").strip()
            person.ad_display_name = (
                generate_display_name(first, last, class_name) if class_name
                else f"{first} {last}"
            )

        return None

    def fix_person(self, person: Person) -> None:
        """Repair one person and refresh the table."""
        problem = self._repair(person)
        if problem:
            self._set_status(f"⚠ {person.first_name} {person.last_name}: {problem}",
                             success=False)
        else:
            self.changed = True
            self._set_status(f"✓ Fixed {person.first_name} {person.last_name}")
        self.refresh()

    def fix_all(self) -> None:
        """Repair every person currently listed."""
        grouped = self._collect_problems()
        fixed = 0
        skipped: List[str] = []

        for entry in grouped.values():
            person = entry["person"]
            if not (REPAIRABLE_FIELDS & set(entry["fields"])):
                continue
            problem = self._repair(person)
            if problem:
                skipped.append(f"{person.first_name} {person.last_name}: {problem}")
            else:
                fixed += 1

        if fixed:
            self.changed = True

        message = f"✓ Fixed {fixed} person(s)"
        if skipped:
            message += f" · {len(skipped)} could not be fixed automatically"
            logger.info("Analysis repair skipped: %s", "; ".join(skipped))
        self._set_status(message, success=not skipped)
        self.refresh()

    def _set_status(self, text: str, success: bool = True) -> None:
        self._status_label.setStyleSheet(
            f"color: {'#4CAF50' if success else '#FFA726'}; font-size: 11px;"
        )
        self._status_label.setText(text)
