"""
Enrollment year dialog
======================

Shows what the enrollment-year calculation would do to a source, and lets the
user decide - per class - whether to accept it.

The calculation runs automatically after a source is loaded, and on demand from
the *Comparison and Sync* tab. Classes that have no value yet are ticked by
default; classes whose stored value disagrees with the calculation are listed
separately and left for the user, because overwriting a year somebody set
deliberately would be the wrong default.
"""

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from utils.school_year import EnrollmentChange, YearResolution

logger = logging.getLogger(__name__)


class EnrollmentYearDialog(QDialog):
    """Review and confirm the calculated enrollment years."""

    COLUMNS = ["Apply", "Class", "Stored", "Calculated"]

    def __init__(self, changes: List[EnrollmentChange],
                 resolution: YearResolution, source_name: str = "",
                 parent: Optional[QWidget] = None):
        """
        Args:
            changes: What the calculation produced.
            resolution: Where the current year came from.
            source_name: Shown in the title.
            parent: Parent widget.
        """
        super().__init__(parent)
        self.changes = list(changes)
        self.resolution = resolution

        self.setWindowTitle(
            f"Enrollment Year - {source_name}" if source_name else "Enrollment Year"
        )
        self.setModal(True)
        self.resize(640, 520)
        self._build_ui()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        conflicts = [c for c in self.changes if c.conflicts]

        info = QLabel(
            "The enrollment year is the calendar year in which a class started "
            "the <b>first grade</b>. It is calculated from the grade in the "
            "class name and the current school year."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px;"
        )
        layout.addWidget(info)

        # Where "now" came from - and a warning when the two sources disagree.
        year_label = QLabel(f"Current year: <b>{self.resolution.year}</b> — "
                            f"{self.resolution.describe()}")
        year_label.setWordWrap(True)
        if self.resolution.sources_disagree:
            year_label.setStyleSheet(
                "color: #FFA726; font-weight: bold; padding: 6px;"
                " border: 1px solid #FFA726; border-radius: 4px;"
            )
        else:
            year_label.setStyleSheet("color: #aaa; font-size: 11px; padding: 4px;")
        layout.addWidget(year_label)

        if self.resolution.sources_disagree:
            warning = QLabel(
                "⚠️ The internet and this computer's clock report different "
                "years. The internet value is used. If that is wrong, cancel "
                "and correct the computer's date first."
            )
            warning.setWordWrap(True)
            warning.setStyleSheet("color: #FFA726; font-size: 10px;")
            layout.addWidget(warning)

        if conflicts:
            conflict_note = QLabel(
                f"⚠️ {len(conflicts)} class(es) already carry a different "
                f"enrollment year. Those rows are <b>not</b> ticked - review "
                f"them before applying."
            )
            conflict_note.setWordWrap(True)
            conflict_note.setStyleSheet("color: #FFA726; font-size: 11px;")
            layout.addWidget(conflict_note)

        # --- the table -----------------------------------------------------
        self._table = QTableWidget(len(self.changes), len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)

        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        self._checkboxes = []
        for row, change in enumerate(self.changes):
            checkbox = QCheckBox()
            # A class without a value is safe to fill in; one that disagrees
            # with a stored value is the user's call.
            checkbox.setChecked(change.is_new)
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            holder_layout.addWidget(checkbox)
            self._table.setCellWidget(row, 0, holder)
            self._checkboxes.append(checkbox)

            self._table.setItem(row, 1, QTableWidgetItem(change.class_name))
            stored = "—" if change.old_year is None else str(change.old_year)
            stored_item = QTableWidgetItem(stored)
            if change.conflicts:
                stored_item.setForeground(Qt.GlobalColor.yellow)
            self._table.setItem(row, 2, stored_item)
            self._table.setItem(row, 3, QTableWidgetItem(str(change.new_year)))

        layout.addWidget(self._table, stretch=1)

        # --- bulk selection --------------------------------------------------
        button_row = QHBoxLayout()
        all_button = QPushButton("Select All")
        all_button.clicked.connect(lambda: self._set_all(True))
        button_row.addWidget(all_button)

        none_button = QPushButton("Deselect All")
        none_button.clicked.connect(lambda: self._set_all(False))
        button_row.addWidget(none_button)

        if conflicts:
            new_only_button = QPushButton("Only classes without a value")
            new_only_button.setToolTip(
                "Fill in the classes that have no enrollment year yet and leave "
                "the conflicting ones alone."
            )
            new_only_button.clicked.connect(self._select_new_only)
            button_row.addWidget(new_only_button)

        button_row.addStretch()
        layout.addLayout(button_row)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.button(QDialogButtonBox.StandardButton.Ok).setText("Apply Selected")
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    # -- selection ---------------------------------------------------------

    def _set_all(self, checked: bool) -> None:
        for checkbox in self._checkboxes:
            checkbox.setChecked(checked)

    def _select_new_only(self) -> None:
        for checkbox, change in zip(self._checkboxes, self.changes):
            checkbox.setChecked(change.is_new)

    # -- result -------------------------------------------------------------

    def selected_changes(self) -> List[EnrollmentChange]:
        """Return the changes the user ticked."""
        return [change for checkbox, change in zip(self._checkboxes, self.changes)
                if checkbox.isChecked()]
