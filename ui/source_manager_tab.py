"""
Source management tab
=====================

One place to see every source the application currently holds, remove the ones
that are no longer needed, and inspect a single source in detail.

The other tabs each show sources through the lens of what they do with them
(load, compare, operate on). This tab is about the sources themselves: what is
in them, where they came from, and getting rid of the ones that have piled up
during a session.
"""

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QGroupBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter, QTextBrowser,
    QVBoxLayout, QWidget,
)

from models import Source
from utils.class_name_utils import analyze_class_names

logger = logging.getLogger(__name__)


class SourceManagerTab(QWidget):
    """Tab listing every source, with deletion and a detail panel."""

    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager

        self.source_manager.source_added.connect(self.refresh_sources)
        self.source_manager.source_removed.connect(self.refresh_sources)
        self.source_manager.source_modified.connect(self._on_source_modified)

        self.init_ui()
        self.refresh_sources()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def init_ui(self) -> None:
        """Build the tab."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        title = QLabel("Source Management")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        description = QLabel(
            "Every source currently loaded in the application. Select one to "
            "see what it contains, or select several to remove them together."
        )
        description.setWordWrap(True)
        description.setStyleSheet("color: #aaa; margin-bottom: 10px;")
        layout.addWidget(description)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- left: the list ------------------------------------------------
        list_panel = QWidget()
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(0, 0, 0, 0)

        self.count_label = QLabel()
        self.count_label.setStyleSheet("font-weight: bold;")
        list_layout.addWidget(self.count_label)

        self.source_list = QListWidget()
        self.source_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.source_list.itemSelectionChanged.connect(self._on_selection_changed)
        list_layout.addWidget(self.source_list, stretch=1)

        button_row = QHBoxLayout()
        self.delete_button = QPushButton("🗑️ Delete Selected")
        self.delete_button.setToolTip(
            "Remove the selected source(s) from the application.\n"
            "Nothing is deleted from EduPage, a file or Active Directory."
        )
        self.delete_button.clicked.connect(self.delete_selected_sources)
        button_row.addWidget(self.delete_button)

        select_all_button = QPushButton("Select All")
        select_all_button.clicked.connect(self.source_list.selectAll)
        button_row.addWidget(select_all_button)

        clear_button = QPushButton("Clear Selection")
        clear_button.clicked.connect(self.source_list.clearSelection)
        button_row.addWidget(clear_button)
        list_layout.addLayout(button_row)

        splitter.addWidget(list_panel)

        # --- right: the detail panel -----------------------------------------
        detail_group = QGroupBox("Source Details")
        detail_layout = QVBoxLayout(detail_group)

        self.detail_view = QTextBrowser()
        self.detail_view.setOpenExternalLinks(False)
        detail_layout.addWidget(self.detail_view)

        splitter.addWidget(detail_group)
        splitter.setSizes([300, 620])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setChildrenCollapsible(False)

        layout.addWidget(splitter, stretch=1)

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def refresh_sources(self, *_args) -> None:
        """Rebuild the list, keeping the selection where possible."""
        selected = {source.name for source in self.selected_sources()}

        self.source_list.blockSignals(True)
        self.source_list.clear()

        for source in self.source_manager.sources:
            persons = len(source.get_all_persons())
            label = (f"{source.name}\n    {source.source_type} · "
                     f"{len(source.classes)} classes · {persons} students"
                     f"{' · read-only' if source.readonly else ''}")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, source)
            self.source_list.addItem(item)
            if source.name in selected:
                item.setSelected(True)

        self.source_list.blockSignals(False)

        total = len(self.source_manager.sources)
        self.count_label.setText(
            f"{total} source{'' if total == 1 else 's'} loaded"
        )
        self._on_selection_changed()

    def _on_source_modified(self, _name: str) -> None:
        """A source changed elsewhere - its counts and details are stale."""
        self.refresh_sources()

    def selected_sources(self) -> List[Source]:
        """Return the sources behind the selected rows."""
        sources = []
        for item in self.source_list.selectedItems():
            source = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(source, Source):
                sources.append(source)
        return sources

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def _on_selection_changed(self) -> None:
        """Show the detail of one source, or explain what to select."""
        selected = self.selected_sources()
        self.delete_button.setEnabled(bool(selected))

        if not selected:
            self._show_message(
                "Select at least one source",
                "Choose a source on the left to see what it contains."
                if self.source_manager.sources else
                "There are no sources yet. Load one on the "
                "<b>1. Data Sources</b> tab."
            )
            return

        if len(selected) > 1:
            names = "".join(f"<li>{s.name}</li>" for s in selected)
            self._show_message(
                "Select only one source",
                f"{len(selected)} sources are selected. Details can only be "
                f"shown for one at a time.<ul>{names}</ul>"
                f"They can still be deleted together."
            )
            return

        self._show_details(selected[0])

    def _show_message(self, heading: str, detail: str) -> None:
        """Render an instruction instead of a source detail."""
        self.detail_view.setHtml(
            f"<div style='padding:24px; text-align:center; color:#aaa;'>"
            f"<h3 style='color:#64B5F6;'>{heading}</h3>"
            f"<p>{detail}</p></div>"
        )

    def _show_details(self, source: Source) -> None:
        """Render everything worth knowing about one source."""
        persons = source.get_all_persons()

        with_username = sum(1 for p in persons if p.ad_username)
        with_password = sum(1 for p in persons if p.ad_password)
        with_email = sum(1 for p in persons if p.ad_email)
        with_groups = sum(1 for p in persons if p.group_memberships)
        dirty = sum(1 for p in persons if p.is_dirty())

        analysis = analyze_class_names([c.name for c in source.classes])
        formats = [key for key in analysis.signatures if key != "free-text"]

        rows = [
            ("Name", source.name),
            ("Type", source.source_type),
            ("Read-only", "yes" if source.readonly else "no"),
            ("Classes", str(len(source.classes))),
            ("Students", str(len(persons))),
            ("Class-name formats", str(len(formats))),
            ("Dominant numeral style", analysis.dominant_style.label),
        ]
        for key, value in (source.source_info or {}).items():
            rows.append((f"Info: {key}", str(value)))

        info_rows = "".join(
            f"<tr><td style='padding:3px 12px 3px 0; color:#aaa;'>{key}</td>"
            f"<td style='padding:3px 0;'><b>{value}</b></td></tr>"
            for key, value in rows
        )

        credential_rows = "".join(
            f"<tr><td style='padding:3px 12px 3px 0; color:#aaa;'>{label}</td>"
            f"<td style='padding:3px 0;'><b>{value}</b> of {len(persons)}</td></tr>"
            for label, value in [
                ("With user name", with_username),
                ("With password", with_password),
                ("With e-mail", with_email),
                ("In at least one group", with_groups),
                ("With unsaved changes", dirty),
            ]
        ) if persons else "<tr><td colspan='2' style='color:#aaa;'>No students.</td></tr>"

        class_rows = "".join(
            f"<tr><td style='padding:2px 12px 2px 0;'>{cls.name or '(unnamed)'}</td>"
            f"<td style='padding:2px 0; color:#aaa;'>{len(cls.persons)} student(s)</td></tr>"
            for cls in source.classes
        ) or "<tr><td colspan='2' style='color:#aaa;'>No classes.</td></tr>"

        self.detail_view.setHtml(f"""
            <h2 style='margin-bottom:4px;'>{source.name}</h2>
            <h4 style='color:#64B5F6;'>Overview</h4>
            <table>{info_rows}</table>
            <h4 style='color:#64B5F6;'>Active Directory data</h4>
            <table>{credential_rows}</table>
            <h4 style='color:#64B5F6;'>Classes</h4>
            <table>{class_rows}</table>
        """)

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------

    def delete_selected_sources(self) -> None:
        """Remove the selected sources after confirmation."""
        selected = self.selected_sources()
        if not selected:
            QMessageBox.warning(self, "No Selection",
                                "Select at least one source to delete.")
            return

        names = "\n".join(f"• {s.name} ({len(s.get_all_persons())} students)"
                          for s in selected)
        reply = QMessageBox.question(
            self, "Delete Sources",
            f"Remove {len(selected)} source(s) from the application?\n\n{names}\n\n"
            f"This only removes them from this application - nothing is deleted "
            f"from EduPage, from a file or from Active Directory.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for source in selected:
            self.source_manager.remove_source(source)
            logger.info("Removed source via the source manager: %s", source.name)

        self.refresh_sources()
        QMessageBox.information(self, "Deleted",
                                f"Removed {len(selected)} source(s).")
