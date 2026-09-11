"""
DuplicateComparisonDialog
Shared module for resolving duplicate groups and templates during import.
Extracted and redesigned (Issue 3b/3c).

Design
------
The dialog shows ALL duplicates in a list on the left.  When the user
clicks a duplicate, a detailed side-by-side comparison is shown on the
right.  Each duplicate has its own action combo (Keep Existing / Replace
with Imported).

Bulk controls are available at the bottom:
  • "Set all to: Keep"   — sets every unresolved item to Keep
  • "Set all to: Replace" — sets every unresolved item to Replace
  • Apply / Cancel

The dialog returns a dict  {identifier → action}  where action is one of
  DuplicateResolution.KEEP  or  DuplicateResolution.REPLACE.
"""

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Dict, Generic, List, Optional, Tuple, TypeVar

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
    QScrollArea, QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

from models import ADGroup, GroupTemplate

logger = logging.getLogger(__name__)


class DuplicateResolution(Enum):
    """Action to take for a duplicate item."""
    KEEP = auto()      # Keep the existing version, discard import
    REPLACE = auto()   # Replace existing with imported version


# ---------------------------------------------------------------------------
# Internal dataclass
# ---------------------------------------------------------------------------

@dataclass
class _DuplicateEntry:
    """Holds one pair of (existing, imported) items with its chosen action."""
    identifier: str           # DN for groups, name for templates
    label: str                # Display label in the list
    existing: object
    imported: object
    action: DuplicateResolution = DuplicateResolution.KEEP


# ---------------------------------------------------------------------------
# Base dialog — generic over the item type
# ---------------------------------------------------------------------------

class _BaseDuplicateDialog(QDialog):
    """
    Generic base for group and template duplicate-resolution dialogs.

    Subclasses must implement:
      _item_identifier(item) → str
      _item_label(item)      → str
      _build_detail_widget(existing, imported) → QWidget
    """

    def __init__(self, duplicates: List[Tuple], title: str, parent=None):
        """
        Args:
            duplicates: List of (existing, imported) tuples.
            title:      Dialog window title.
            parent:     Parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumSize(900, 550)

        self._entries: List[_DuplicateEntry] = []
        for existing, imported in duplicates:
            ident = self._item_identifier(existing)
            self._entries.append(
                _DuplicateEntry(
                    identifier=ident,
                    label=self._item_label(existing),
                    existing=existing,
                    imported=imported,
                )
            )

        self._current_index: Optional[int] = None
        self._build_ui()

    # ------------------------------------------------------------------
    # Subclass interface
    # ------------------------------------------------------------------

    def _item_identifier(self, item) -> str:  # pragma: no cover
        raise NotImplementedError

    def _item_label(self, item) -> str:  # pragma: no cover
        raise NotImplementedError

    def _build_detail_widget(self, existing, imported) -> QWidget:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # Header
        n = len(self._entries)
        layout.addWidget(QLabel(
            f"<b>{n} duplicate{'s' if n != 1 else ''} found.</b>  "
            "Select an item on the left to see the detailed comparison on the right."
            "  Choose an action for each, then click <b>Apply</b>."
        ))

        # Splitter: list | detail area
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ---- LEFT: duplicate list ----
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        left_layout.addWidget(QLabel("<b>Duplicates:</b>"))
        self._list_widget = QListWidget()
        self._list_widget.currentRowChanged.connect(self._on_row_changed)

        for entry in self._entries:
            item = QListWidgetItem(f"⚠ {entry.label}")
            item.setForeground(QColor("#FFA726"))
            self._list_widget.addItem(item)

        left_layout.addWidget(self._list_widget)
        splitter.addWidget(left_widget)

        # ---- RIGHT: detail + action area ----
        self._right_widget = QWidget()
        right_layout = QVBoxLayout(self._right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # Placeholder shown before any item is selected
        self._placeholder_label = QLabel(
            "<i>Select a duplicate from the list on the left to view details.</i>"
        )
        self._placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self._placeholder_label)

        # Detail container (hidden until a row is selected)
        self._detail_container = QWidget()
        self._detail_layout = QVBoxLayout(self._detail_container)
        self._detail_layout.setContentsMargins(0, 0, 0, 0)
        self._detail_container.hide()
        right_layout.addWidget(self._detail_container)

        splitter.addWidget(self._right_widget)
        splitter.setSizes([260, 620])
        layout.addWidget(splitter, stretch=1)

        # ---- BOTTOM: bulk controls + dialog buttons ----
        bottom_row = QHBoxLayout()

        bottom_row.addWidget(QLabel("Bulk action:"))

        keep_all_btn = QPushButton("Set All → Keep Existing")
        keep_all_btn.clicked.connect(lambda: self._set_all(DuplicateResolution.KEEP))
        bottom_row.addWidget(keep_all_btn)

        replace_all_btn = QPushButton("Set All → Replace with Imported")
        replace_all_btn.setStyleSheet("background-color: #4CAF50; font-weight: bold;")
        replace_all_btn.clicked.connect(lambda: self._set_all(DuplicateResolution.REPLACE))
        bottom_row.addWidget(replace_all_btn)

        bottom_row.addStretch()

        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        bottom_row.addWidget(btn_box)

        layout.addLayout(bottom_row)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_row_changed(self, row: int):
        if row < 0 or row >= len(self._entries):
            return
        self._current_index = row
        entry = self._entries[row]

        # Rebuild detail area
        self._placeholder_label.hide()
        self._detail_container.show()

        # Clear previous detail widget
        while self._detail_layout.count():
            child = self._detail_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        # Build new detail widget from subclass
        detail_widget = self._build_detail_widget(entry.existing, entry.imported)
        self._detail_layout.addWidget(detail_widget)

        # Action selector for this entry
        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("<b>Action for this duplicate:</b>"))
        action_combo = QComboBox()
        action_combo.addItem("Keep Existing", DuplicateResolution.KEEP)
        action_combo.addItem("Replace with Imported", DuplicateResolution.REPLACE)
        # Set combo to current action
        idx = 0 if entry.action == DuplicateResolution.KEEP else 1
        action_combo.setCurrentIndex(idx)
        action_combo.currentIndexChanged.connect(
            lambda _, row=row, combo=action_combo: self._on_action_changed(row, combo)
        )
        action_row.addWidget(action_combo)
        action_row.addStretch()

        action_widget = QWidget()
        action_widget.setLayout(action_row)
        self._detail_layout.addWidget(action_widget)

    def _on_action_changed(self, row: int, combo: QComboBox):
        action = combo.currentData()
        self._entries[row].action = action
        # Update list icon
        item = self._list_widget.item(row)
        if item:
            icon = "🔄" if action == DuplicateResolution.REPLACE else "⚠"
            entry = self._entries[row]
            item.setText(f"{icon} {entry.label}")
            color = QColor("#4CAF50") if action == DuplicateResolution.REPLACE else QColor("#FFA726")
            item.setForeground(color)

    def _set_all(self, action: DuplicateResolution):
        """Apply *action* to every entry and update the list."""
        for i, entry in enumerate(self._entries):
            entry.action = action
            item = self._list_widget.item(i)
            if item:
                icon = "🔄" if action == DuplicateResolution.REPLACE else "⚠"
                item.setText(f"{icon} {entry.label}")
                color = QColor("#4CAF50") if action == DuplicateResolution.REPLACE else QColor("#FFA726")
                item.setForeground(color)
        # Refresh detail if something is selected
        if self._current_index is not None:
            self._on_row_changed(self._current_index)

    # ------------------------------------------------------------------
    # Public result accessor
    # ------------------------------------------------------------------

    def get_resolutions(self) -> Dict[str, DuplicateResolution]:
        """
        Return a dict mapping each item's identifier to its chosen action.
        Call this after the dialog is accepted.
        """
        return {entry.identifier: entry.action for entry in self._entries}


# ===========================================================================
# Concrete dialog — Groups
# ===========================================================================

def _fmt_value(v) -> str:
    return str(v) if v is not None else "—"


class DuplicateGroupComparisonDialog(_BaseDuplicateDialog):
    """
    Duplicate-resolution dialog for :class:`models.ADGroup` objects.

    Usage::

        dlg = DuplicateGroupComparisonDialog(duplicate_pairs, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            resolutions = dlg.get_resolutions()
            # resolutions[group.dn] → DuplicateResolution.KEEP / REPLACE
    """

    def __init__(self, duplicates: List[Tuple[ADGroup, ADGroup]], parent=None):
        super().__init__(
            duplicates, title="Duplicate Groups — Compare and Resolve", parent=parent
        )

    def _item_identifier(self, item: ADGroup) -> str:
        return item.dn

    def _item_label(self, item: ADGroup) -> str:
        return item.name or item.dn

    def _build_detail_widget(self, existing: ADGroup, imported: ADGroup) -> QWidget:
        """Build a side-by-side comparison widget for two ADGroup objects."""
        widget = QWidget()
        layout = QHBoxLayout(widget)

        def _panel(title: str, group: ADGroup) -> QGroupBox:
            grp = QGroupBox(title)
            f = QFormLayout(grp)
            f.addRow("Name:", QLabel(_fmt_value(group.name)))
            f.addRow("DN:", QLabel(f"<code>{_fmt_value(group.dn)}</code>"))
            f.addRow("Description:", QLabel(_fmt_value(group.description)))
            f.addRow("Type:", QLabel(_fmt_value(group.group_type)))
            f.addRow("Members:", QLabel(_fmt_value(group.members_count)))
            f.addRow("Status:", QLabel(group.get_status_text()))
            return grp

        layout.addWidget(_panel("Existing Version", existing))
        layout.addWidget(_panel("Imported Version", imported))
        return widget


# ===========================================================================
# Concrete dialog — Templates
# ===========================================================================

class DuplicateTemplateComparisonDialog(_BaseDuplicateDialog):
    """
    Duplicate-resolution dialog for :class:`models.GroupTemplate` objects.

    Usage::

        dlg = DuplicateTemplateComparisonDialog(duplicate_pairs, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            resolutions = dlg.get_resolutions()
            # resolutions[template.name] → DuplicateResolution.KEEP / REPLACE
    """

    def __init__(self, duplicates: List[Tuple[GroupTemplate, GroupTemplate]], parent=None):
        super().__init__(
            duplicates, title="Duplicate Templates — Compare and Resolve", parent=parent
        )

    def _item_identifier(self, item: GroupTemplate) -> str:
        return item.name

    def _item_label(self, item: GroupTemplate) -> str:
        return item.name

    def _build_detail_widget(
        self, existing: GroupTemplate, imported: GroupTemplate
    ) -> QWidget:
        """Build a side-by-side comparison widget for two GroupTemplate objects."""

        def _fmt_date(dt) -> str:
            return dt.strftime("%Y-%m-%d %H:%M") if dt else "—"

        def _groups_text(groups) -> str:
            return "\n".join(f"• {g.name}" for g in groups) if groups else "(none)"

        widget = QWidget()
        layout = QHBoxLayout(widget)

        def _panel(title: str, tmpl: GroupTemplate) -> QGroupBox:
            grp = QGroupBox(title)
            v = QVBoxLayout(grp)
            f = QFormLayout()
            f.addRow("Name:", QLabel(_fmt_value(tmpl.name)))
            f.addRow("Description:", QLabel(_fmt_value(tmpl.description)))
            f.addRow("Created:", QLabel(_fmt_date(tmpl.created_date)))
            f.addRow("Modified:", QLabel(_fmt_date(tmpl.modified_date)))
            f.addRow("Groups:", QLabel(str(len(tmpl.groups))))
            v.addLayout(f)
            te = QTextEdit()
            te.setReadOnly(True)
            te.setPlainText(_groups_text(tmpl.groups))
            te.setMaximumHeight(120)
            v.addWidget(te)
            return grp

        layout.addWidget(_panel("Existing Version", existing))
        layout.addWidget(_panel("Imported Version", imported))
        return widget
