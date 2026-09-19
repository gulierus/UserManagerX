"""
Home directory template manager
===============================

Lets the user create, edit, rename and delete home-directory path templates -
the same way group templates are managed in the *Manage Groups* window.

A template is a path with placeholders, for example::

    \\\\server\\students\\{class_name}\\{username}

Every person the template is applied to gets their own path. The templates the
user creates are stored in the application settings, so they survive a restart
and appear in every place that offers a template (the property editor and the
bulk-edit dialog).
"""

import logging
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSplitter, QVBoxLayout, QWidget,
)

from models import Person
from utils.home_directory_utils import (
    HomeDirectoryPathGenerator, HomeDirectoryTemplateManager, PlaceholderError,
)

logger = logging.getLogger(__name__)


class HomeDirectoryTemplateDialog(QDialog):
    """Create and maintain home directory path templates."""

    #: Person used for the live preview.
    SAMPLE = ("Jan", "Novák", "6.A", "novakjan")

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Home Directory Templates")
        self.setModal(True)
        self.resize(820, 520)

        self.manager = HomeDirectoryTemplateManager()
        self._current_name: Optional[str] = None

        self._build_ui()
        self._refresh_list()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            "A template is a path with placeholders. Every person it is "
            "applied to gets their own path, so one template covers a whole "
            "class or the whole school."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px;"
        )
        layout.addWidget(info)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # --- left: the list ------------------------------------------------
        list_panel = QWidget()
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.addWidget(QLabel("<b>Templates</b>"))

        self._list = QListWidget()
        self._list.currentItemChanged.connect(self._on_selection_changed)
        list_layout.addWidget(self._list, stretch=1)

        list_buttons = QHBoxLayout()
        add_btn = QPushButton("➕ New")
        add_btn.clicked.connect(self.add_template)
        list_buttons.addWidget(add_btn)

        rename_btn = QPushButton("✏️ Rename")
        rename_btn.clicked.connect(self.rename_template)
        list_buttons.addWidget(rename_btn)

        delete_btn = QPushButton("🗑️ Delete")
        delete_btn.clicked.connect(self.delete_template)
        list_buttons.addWidget(delete_btn)
        list_layout.addLayout(list_buttons)

        splitter.addWidget(list_panel)

        # --- right: the editor ----------------------------------------------
        editor_panel = QWidget()
        editor_layout = QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)

        path_group = QGroupBox("Path pattern")
        path_layout = QVBoxLayout(path_group)

        self._path_input = QLineEdit()
        self._path_input.setPlaceholderText(
            r"\\server\students\{class_name}\{username}"
        )
        self._path_input.setEnabled(False)
        self._path_input.textChanged.connect(self._on_path_changed)
        path_layout.addWidget(self._path_input)

        self._builtin_note = QLabel()
        self._builtin_note.setWordWrap(True)
        self._builtin_note.setStyleSheet("color: #FFA726; font-size: 10px;")
        self._builtin_note.setVisible(False)
        path_layout.addWidget(self._builtin_note)

        editor_layout.addWidget(path_group)

        help_label = QLabel(
            "<b>Placeholders</b><br>"
            "<code>{first_name}</code>, <code>{last_name}</code>, "
            "<code>{username}</code>, <code>{class_name}</code>, "
            "<code>{enrollment_year}</code><br>"
            "<code>{last_name:1}</code> — the first part of a compound surname "
            "&nbsp;·&nbsp; <code>{last_name:-1}</code> — the last part<br>"
            "<code>{first_name:1:3}</code> — the first 3 letters of the first "
            "part of the first name"
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #999; font-size: 10px;")
        editor_layout.addWidget(help_label)

        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)
        self._preview_label = QLabel()
        self._preview_label.setWordWrap(True)
        self._preview_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 11px;"
        )
        preview_layout.addWidget(self._preview_label)
        editor_layout.addWidget(preview_group)

        editor_layout.addStretch()
        splitter.addWidget(editor_panel)

        splitter.setSizes([260, 560])
        layout.addWidget(splitter, stretch=1)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Close
        )
        button_box.button(QDialogButtonBox.StandardButton.Save).setText("Save")
        button_box.accepted.connect(self._on_save)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    # -- list handling -------------------------------------------------------

    def _refresh_list(self, select: Optional[str] = None) -> None:
        """Rebuild the list from the manager."""
        self._list.blockSignals(True)
        self._list.clear()
        for name in sorted(self.manager.get_all_templates()):
            item = QListWidgetItem(
                f"{'🔒 ' if self.manager.is_builtin(name) else ''}{name}"
            )
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setToolTip(self.manager.get_template(name) or "")
            self._list.addItem(item)
        self._list.blockSignals(False)

        if select:
            for index in range(self._list.count()):
                if self._list.item(index).data(Qt.ItemDataRole.UserRole) == select:
                    self._list.setCurrentRow(index)
                    return
        if self._list.count():
            self._list.setCurrentRow(0)
        else:
            self._on_selection_changed(None, None)

    def _selected_name(self) -> Optional[str]:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_selection_changed(self, current, _previous) -> None:
        name = current.data(Qt.ItemDataRole.UserRole) if current else None
        self._current_name = name

        self._path_input.blockSignals(True)
        self._path_input.setText(self.manager.get_template(name) or "" if name else "")
        self._path_input.blockSignals(False)
        self._path_input.setEnabled(name is not None)

        is_builtin = bool(name) and self.manager.is_builtin(name)
        self._builtin_note.setVisible(is_builtin)
        if is_builtin:
            self._builtin_note.setText(
                "This template ships with the application. You can edit it - "
                "your version is stored separately - but it cannot be deleted."
            )
        self._update_preview()

    # -- editing ---------------------------------------------------------------

    def _on_path_changed(self, text: str) -> None:
        if self._current_name:
            self.manager.templates[self._current_name] = text
        self._update_preview()

    def _update_preview(self) -> None:
        """Render the current pattern for the sample person."""
        template = self._path_input.text().strip()
        if not template:
            self._preview_label.setText("")
            return

        first, last, class_name, username = self.SAMPLE
        person = Person(first_name=first, last_name=last, class_name=class_name,
                        ad_username=username)
        try:
            generated = HomeDirectoryPathGenerator.generate_path(template, person)
            self._preview_label.setText(
                f"{first} {last} ({class_name})\n→ {generated}"
            )
            self._preview_label.setStyleSheet(
                "font-family: Consolas, monospace; font-size: 11px; color: #4CAF50;"
            )
        except PlaceholderError as exc:
            self._preview_label.setText(f"⚠ {exc}")
            self._preview_label.setStyleSheet(
                "font-family: Consolas, monospace; font-size: 11px; color: #ff6b6b;"
            )

    def add_template(self) -> None:
        """Create a new, empty template."""
        name, ok = QInputDialog.getText(self, "New Template", "Template name:")
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Invalid Name", "The name cannot be empty.")
            return
        if self.manager.template_exists(name):
            QMessageBox.warning(self, "Duplicate Name",
                                f"A template called '{name}' already exists.")
            return

        self.manager.add_template(name, r"\\server\students\{class_name}\{username}")
        self._refresh_list(select=name)

    def rename_template(self) -> None:
        """Rename the selected template."""
        name = self._selected_name()
        if not name:
            QMessageBox.warning(self, "No Selection", "Select a template first.")
            return

        new_name, ok = QInputDialog.getText(
            self, "Rename Template", "New name:", QLineEdit.EchoMode.Normal, name
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name or new_name == name:
            return
        if self.manager.template_exists(new_name):
            QMessageBox.warning(self, "Duplicate Name",
                                f"A template called '{new_name}' already exists.")
            return

        if self.manager.rename_template(name, new_name):
            # A renamed built-in stops being built-in - the shipped one comes
            # back under its own name on the next start.
            self.manager.builtin_names.discard(name)
            self._refresh_list(select=new_name)

    def delete_template(self) -> None:
        """Delete the selected template."""
        name = self._selected_name()
        if not name:
            QMessageBox.warning(self, "No Selection", "Select a template first.")
            return
        if self.manager.is_builtin(name):
            QMessageBox.information(
                self, "Built-in Template",
                f"'{name}' ships with the application and cannot be deleted.\n\n"
                f"You can edit its path instead."
            )
            return

        if QMessageBox.question(
            self, "Delete Template", f"Delete the template '{name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        self.manager.remove_template(name)
        self._refresh_list()

    # -- saving -----------------------------------------------------------------

    def _on_save(self) -> None:
        """Validate every template, then persist them."""
        broken = []
        for name, template in self.manager.get_all_templates().items():
            if not (template or "").strip():
                broken.append(f"{name}: the path is empty")
                continue
            problems = HomeDirectoryPathGenerator.validate_template(template)
            if problems:
                broken.append(f"{name}: {problems[0]}")

        if broken:
            QMessageBox.warning(
                self, "Invalid Templates",
                "These templates cannot be used:\n\n"
                + "\n".join(f"• {problem}" for problem in broken)
            )
            return

        if self.manager.save():
            QMessageBox.information(
                self, "Saved",
                f"{len(self.manager.get_all_templates())} template(s) saved."
            )
            self.accept()
        else:
            QMessageBox.warning(
                self, "Not Saved",
                "The templates are active for this session but could not be "
                "written to the settings file."
            )
