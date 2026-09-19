"""
Second tab: Source Comparison and Synchronization

Layout
------
Three panels side by side - *Left Source*, *Right Source* and *Output Source*.
Every operation that belongs to one particular source is placed **inside that
source's panel**, directly underneath it, so a button is always visually
aligned with the data it works on.  The only operation that needs both inputs
(*Merge Both Sources*) sits in a separate, centred bar below the three panels.

Operations
----------
Left / Right panel
    * Duplicate to a new source
    * Shift class years          (:class:`~ui.class_operations_dialogs.ClassShiftDialog`)
    * Convert class numerals     (:class:`~ui.class_operations_dialogs.ClassNumeralConversionDialog`)
    * Analyze source             (:class:`~ui.class_operations_dialogs.SourceAnalysisDialog`)

Both sources
    * Merge                      (:class:`~ui.merge_dialog.MergeSourcesDialog`)

Output panel
    * Person operations: add / edit / delete
    * Class operations: add / rename / delete / convert numerals
"""

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QComboBox, QSplitter, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from models import Class, Person, Source
from ui.class_operations_dialogs import (
    ClassNumeralConversionDialog, ClassShiftDialog, SourceAnalysisDialog,
)
from ui.merge_dialog import MergeSourcesDialog
from ui.property_editor import PropertyEditorDialog
from utils.class_name_utils import convert_class_name
from utils.source_analysis import rename_class

from ui.source_combo import (
    ACCESS_SETTING_CATEGORY, ACCESS_SETTING_KEY, PLACEHOLDER_TEXT,
    combo_source_name, find_source_index, populate_source_combo,
)

logger = logging.getLogger(__name__)


class SourcePanel(QWidget):
    """
    Panel showing one source plus the operations that belong to it.

    Signals:
        duplicate_requested(): "Duplicate" was pressed.
        shift_requested(): "Shift Classes" was pressed.
        convert_requested(): "Convert Class Numerals" was pressed.
        analyze_requested(): "Analyze Source" was pressed.
        enrollment_requested(): "Enrollment Years" was pressed.
    """

    duplicate_requested = pyqtSignal()
    shift_requested = pyqtSignal()
    convert_requested = pyqtSignal()
    analyze_requested = pyqtSignal()
    enrollment_requested = pyqtSignal()

    def __init__(self, title: str, source_manager, allow_edit: bool = False,
                 accent: str = "#4CAF50"):
        """
        Args:
            title: Panel caption, e.g. ``"Left Source"``.
            source_manager: Application-wide :class:`~models.SourceManager`.
            allow_edit: True for the output panel (person/class editing).
            accent: Colour used for the operation group caption.
        """
        super().__init__()
        self.source_manager = source_manager
        self.current_source: Optional[Source] = None
        self.allow_edit = allow_edit
        self.title = title
        self.accent = accent
        self.init_ui(title)

        # Connect to source manager signals for auto-refresh
        self.source_manager.source_added.connect(self.on_source_changed_external)
        self.source_manager.source_removed.connect(self.on_source_changed_external)
        self.source_manager.source_modified.connect(self.on_source_modified_external)

        # Relabel the combo as soon as the "show access mode" switch changes.
        self._connect_access_setting()

    def _connect_access_setting(self) -> None:
        """
        Watch the "show read-only / editable" setting.

        A settings backend that cannot emit signals only costs the live
        update, so every failure here is logged and ignored.
        """
        try:
            from utils.settings_manager import get_settings
            get_settings().settings_changed.connect(self._on_setting_changed)
        except Exception:
            logger.debug("Source access labels will not update live",
                         exc_info=True)

    def _on_setting_changed(self, category: str, key: str, _value) -> None:
        """Rebuild the combo when the access-mode switch changes."""
        if category == ACCESS_SETTING_CATEGORY and key == ACCESS_SETTING_KEY:
            self.refresh_sources()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def init_ui(self, title: str) -> None:
        """Build the panel."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)

        # Title and source selector
        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{title}</b>"))

        self.source_combo = QComboBox()
        self.source_combo.addItem(PLACEHOLDER_TEXT, None)
        self.source_combo.currentTextChanged.connect(self.on_source_changed)
        header.addWidget(self.source_combo, stretch=1)

        layout.addLayout(header)

        # Search
        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by name...")
        self.search_input.textChanged.connect(self.on_search)
        search_layout.addWidget(self.search_input)
        layout.addLayout(search_layout)

        # Tree widget - only show name and class, NO AD username
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Class / Enrollment year"])
        self.tree.setColumnWidth(0, 200)
        layout.addWidget(self.tree, stretch=1)

        # Statistics label
        self.stats_label = QLabel()
        self.stats_label.setStyleSheet("color: #aaa; font-size: 10px;")
        layout.addWidget(self.stats_label)

        if self.allow_edit:
            self._add_edit_buttons(layout)
        else:
            self._add_source_operation_buttons(layout)

    def _add_source_operation_buttons(self, layout: QVBoxLayout) -> None:
        """
        Add the operations of an input panel.

        The buttons live inside the panel so they are always aligned with the
        source they act on, no matter how the user drags the splitter.
        """
        group = QGroupBox(f"{self.title} Operations")
        group.setStyleSheet(
            f"QGroupBox {{ font-weight: bold; margin-top: 6px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 8px;"
            f" padding: 0 4px; color: {self.accent}; }}"
        )
        grid = QGridLayout(group)
        grid.setSpacing(4)

        def make_button(text: str, tooltip: str, slot) -> QPushButton:
            button = QPushButton(text)
            button.setToolTip(tooltip)
            button.setMinimumHeight(30)
            button.setStyleSheet("font-weight: normal; text-align: left; padding-left: 6px;")
            button.clicked.connect(slot)
            return button

        self.duplicate_button = make_button(
            "📋 Duplicate",
            f"Create an editable copy of the source selected in {self.title}",
            self.duplicate_requested.emit,
        )
        self.shift_button = make_button(
            "⬆️ Shift Classes",
            "Move every class one school year up (6.A → 7.A, IX.B → X.B)",
            self.shift_requested.emit,
        )
        self.convert_button = make_button(
            "🔢 Convert Class Numerals",
            "Convert class names between Roman and Arabic numerals, or to a "
            "custom format",
            self.convert_requested.emit,
        )
        self.analyze_button = make_button(
            "🔍 Analyze Source",
            "Check the data for problems that can break shifting or merging, "
            "and fix them",
            self.analyze_requested.emit,
        )
        self.enrollment_button = make_button(
            "🎓 Enrollment Years",
            "Work out, for every class, the year it started the first grade "
            "(from the grade in the class name and the current school year)",
            self.enrollment_requested.emit,
        )

        grid.addWidget(self.duplicate_button, 0, 0)
        grid.addWidget(self.shift_button, 0, 1)
        grid.addWidget(self.convert_button, 1, 0)
        grid.addWidget(self.analyze_button, 1, 1)
        grid.addWidget(self.enrollment_button, 2, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        layout.addWidget(group)

    def _add_edit_buttons(self, layout: QVBoxLayout) -> None:
        """Add the person and class operations of the output panel."""
        person_group = QGroupBox("Person Operations")
        person_layout = QVBoxLayout(person_group)

        person_row = QHBoxLayout()
        add_person_btn = QPushButton("➕ Add Person")
        add_person_btn.clicked.connect(self.on_add_person)
        person_row.addWidget(add_person_btn)

        edit_person_btn = QPushButton("✏️ Edit Person")
        edit_person_btn.setToolTip(
            "Edit every property of the selected person in a single window"
        )
        edit_person_btn.clicked.connect(self.on_edit_person)
        person_row.addWidget(edit_person_btn)

        delete_person_btn = QPushButton("🗑️ Delete Person(s)")
        delete_person_btn.clicked.connect(self.on_delete_persons)
        person_row.addWidget(delete_person_btn)

        person_layout.addLayout(person_row)
        layout.addWidget(person_group)

        class_group = QGroupBox("Class Operations")
        class_layout = QVBoxLayout(class_group)

        class_row1 = QHBoxLayout()
        add_class_btn = QPushButton("➕ Add Class")
        add_class_btn.clicked.connect(self.on_add_class)
        class_row1.addWidget(add_class_btn)

        rename_class_btn = QPushButton("✏️ Rename Class")
        rename_class_btn.clicked.connect(self.on_rename_class)
        class_row1.addWidget(rename_class_btn)

        delete_class_btn = QPushButton("🗑️ Delete Class")
        delete_class_btn.clicked.connect(self.on_delete_class)
        class_row1.addWidget(delete_class_btn)

        class_layout.addLayout(class_row1)

        class_row2 = QHBoxLayout()
        convert_class_btn = QPushButton("🔢 Convert Numerals (Selected Class)")
        # It sits alone on its own row, so it inherited the layout's default
        # height instead of the one Qt gives the three buttons above it. Pin it
        # to the same sizeHint so the group looks like one block.
        convert_class_btn.setMinimumHeight(add_class_btn.sizeHint().height())
        convert_class_btn.setToolTip(
            "Convert the numeral of the selected class between Roman and "
            "Arabic notation, or rewrite it with a custom template"
        )
        convert_class_btn.clicked.connect(self.on_convert_selected_class)
        class_row2.addWidget(convert_class_btn)
        class_row2.addStretch()

        class_layout.addLayout(class_row2)
        layout.addWidget(class_group)

    # ------------------------------------------------------------------
    # Source plumbing
    # ------------------------------------------------------------------

    def on_source_changed_external(self, *args) -> None:
        """Handle external source changes (auto-refresh)."""
        self.refresh_sources()

    def on_source_modified_external(self, source_name: str) -> None:
        """Handle source modification."""
        if self.current_source and self.current_source.name == source_name:
            self.refresh_tree(self.search_input.text().strip())

    def refresh_sources(self) -> None:
        """Refresh the list of available sources, keeping the selection."""
        # Remember the source, not the label - the label changes with the
        # access mode and with the "show access mode" setting.
        current = combo_source_name(self.source_combo)
        self.source_combo.blockSignals(True)
        populate_source_combo(self.source_combo, self.source_manager.sources)

        index = find_source_index(self.source_combo, current)
        if index >= 0:
            self.source_combo.setCurrentIndex(index)
        else:
            # The previously selected source disappeared
            self.current_source = None
            self.tree.clear()
            self.stats_label.setText("")

        self.source_combo.blockSignals(False)

    def on_source_changed(self, source_name: str) -> None:
        """
        Handle source selection change.

        The combo shows "Roster (editable)"; this translates the label back
        into the source name.  A plain name that is not in the list is passed
        through unchanged, so direct calls keep working.
        """
        source_name = combo_source_name(self.source_combo, source_name)

        if source_name == PLACEHOLDER_TEXT:
            self.current_source = None
            self.tree.clear()
            self.stats_label.setText("")
            return

        self.current_source = self.source_manager.get_source_by_name(source_name)
        self.refresh_tree()

    def refresh_tree(self, filter_text: str = "") -> None:
        """Rebuild the tree from the current source."""
        self.tree.clear()

        if not self.current_source:
            self.stats_label.setText("")
            return

        total_persons = 0

        for cls in self.current_source.classes:
            # The enrollment year goes in the second column, which is empty for
            # a class row anyway. Appending it to the name would work too, but
            # the years then line up under each other and the class name stays
            # exactly what the source called it.
            year_text = (f"enrolled {cls.enrollment_year}"
                         if cls.enrollment_year else "")

            class_item = QTreeWidgetItem([cls.name or "(unnamed class)", year_text])
            class_item.setData(0, Qt.ItemDataRole.UserRole, cls)
            if cls.enrollment_year:
                class_item.setToolTip(
                    1, f"{cls.name} started the first grade in "
                       f"{cls.enrollment_year}"
                )

            for person in cls.persons:
                if filter_text and not person.matches_search(filter_text):
                    continue

                person_item = QTreeWidgetItem([
                    f"{person.first_name} {person.last_name}".strip(),
                    person.class_name,
                ])
                person_item.setData(0, Qt.ItemDataRole.UserRole, person)
                class_item.addChild(person_item)
                total_persons += 1

            if class_item.childCount() > 0 or not filter_text:
                self.tree.addTopLevelItem(class_item)
                class_item.setExpanded(True)

        if filter_text:
            self.stats_label.setText(f"Showing {total_persons} persons (filtered)")
        else:
            read_only = " • read-only" if self.current_source.readonly else ""
            self.stats_label.setText(
                f"Total: {len(self.current_source.classes)} classes, "
                f"{len(self.current_source.get_all_persons())} persons{read_only}"
            )

    def on_search(self, text: str) -> None:
        """Handle search text change."""
        self.refresh_tree(text.strip())

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    def _require_editable_source(self) -> bool:
        """Show a message and return False when the source cannot be edited."""
        if not self.current_source:
            QMessageBox.warning(self, "No Source", "Please select a source first")
            return False
        if self.current_source.readonly:
            QMessageBox.warning(self, "Read-Only", "This source is read-only")
            return False
        return True

    def _selected_person(self) -> Optional[Person]:
        """Return the selected person, or None (a message is shown)."""
        current = self.tree.currentItem()
        if not current:
            QMessageBox.warning(self, "No Selection", "Please select a person")
            return None
        person = current.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(person, Person):
            QMessageBox.warning(self, "Invalid Selection",
                                "Please select a person, not a class")
            return None
        return person

    def _selected_class(self) -> Optional[Class]:
        """Return the selected class, or None (a message is shown)."""
        current = self.tree.currentItem()
        if not current:
            QMessageBox.warning(self, "No Selection", "Please select a class")
            return None
        cls = current.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(cls, Class):
            QMessageBox.warning(self, "Invalid Selection",
                                "Please select a class (not a person)")
            return None
        return cls

    # ------------------------------------------------------------------
    # Person operations
    # ------------------------------------------------------------------

    def on_add_person(self) -> None:
        """Add a new person to a class."""
        if not self._require_editable_source():
            return

        class_name, ok = QInputDialog.getText(self, "Add Person", "Class name:")
        if not ok:
            return
        class_name = class_name.strip()
        if not class_name:
            QMessageBox.warning(self, "Invalid Input", "Class name cannot be empty")
            return

        first_name, ok = QInputDialog.getText(self, "Add Person", "First name:")
        if not ok:
            return
        first_name = first_name.strip()
        if not first_name:
            QMessageBox.warning(self, "Invalid Input", "First name cannot be empty")
            return

        last_name, ok = QInputDialog.getText(self, "Add Person", "Last name:")
        if not ok:
            return
        last_name = last_name.strip()
        if not last_name:
            QMessageBox.warning(self, "Invalid Input", "Last name cannot be empty")
            return

        cls = self.current_source.find_class_by_name(class_name)
        if cls:
            if cls.find_person_by_name(first_name, last_name):
                QMessageBox.warning(
                    self, "Duplicate Person",
                    f"{first_name} {last_name} already exists in class {class_name}"
                )
                return
        else:
            cls = Class(name=class_name)
            self.current_source.add_class(cls)

        person = Person(first_name=first_name, last_name=last_name,
                        class_name=class_name)
        cls.add_person(person)

        self.refresh_tree()
        self.source_manager.notify_source_modified(self.current_source.name)

        QMessageBox.information(
            self, "Success", f"Added {first_name} {last_name} to class {class_name}"
        )
        logger.info(f"Added person: {person}")

    def on_edit_person(self) -> None:
        """
        Edit the selected person in a single window.

        The very same dialog is used on the *Operations* tab
        (Active Directory Management → Edit), so both places behave and look
        identical.
        """
        person = self._selected_person()
        if person is None:
            return
        if not self._require_editable_source():
            return

        existing_usernames = {
            p.ad_username for p in self.current_source.get_all_persons()
            if p.ad_username and p is not person
        }

        previous_class = person.class_name
        # The identity that decides whether a record is a duplicate is the
        # normalized name *together with the class* - moving a student onto a
        # namesake creates just as much of a duplicate as renaming them does,
        # so the class has to be part of the "did anything change?" key.
        previous_identity = person.get_normalized_name()

        dialog = PropertyEditorDialog(
            person,
            existing_usernames,
            available_groups=[],
            available_templates=[],
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        if person.class_name != previous_class:
            self._move_person_to_class(person, previous_class, person.class_name)

        if person.get_normalized_name() != previous_identity:
            self._warn_about_duplicate(person)

        self.refresh_tree(self.search_input.text().strip())
        self.source_manager.notify_source_modified(self.current_source.name)
        logger.info(f"Edited person: {person}")

    def _move_person_to_class(self, person: Person, old_class_name: str,
                              new_class_name: str) -> None:
        """Move *person* into the class matching their new ``class_name``."""
        source = self.current_source
        if source is None:
            return

        # ``Person`` compares by value, so ``in`` / ``list.remove()`` would
        # pull the first *equal* record out of the first class that happens to
        # hold a namesake.  Match the object itself instead.
        for cls in list(source.classes):
            if any(existing is person for existing in cls.persons):
                cls.remove_person(person)
                break

        target = source.find_class_by_name(new_class_name)
        if target is None:
            target = Class(name=new_class_name)
            source.add_class(target)
        target.persons.append(person)

        logger.info(
            "Moved %s %s from class %r to %r",
            person.first_name, person.last_name, old_class_name, new_class_name,
        )

    def _warn_about_duplicate(self, person: Person) -> None:
        """Warn when the edited person now collides with another record."""
        cls = self.current_source.find_class_by_name(person.class_name)
        if not cls:
            return
        key = person.get_normalized_name()
        clashes = [p for p in cls.persons if p is not person
                   and p.get_normalized_name() == key]
        if clashes:
            QMessageBox.warning(
                self, "Duplicate Person",
                f"{person.first_name} {person.last_name} now exists more than "
                f"once in class {person.class_name}.\n\n"
                "Duplicate persons are collapsed into one record when sources "
                "are merged."
            )

    def on_delete_persons(self) -> None:
        """Delete the selected person(s)."""
        if not self._require_editable_source():
            return

        selected_items = self.tree.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Selection", "Please select person(s) to delete")
            return

        persons_to_delete = [
            item.data(0, Qt.ItemDataRole.UserRole) for item in selected_items
            if isinstance(item.data(0, Qt.ItemDataRole.UserRole), Person)
        ]

        if not persons_to_delete:
            QMessageBox.warning(self, "No Persons", "No persons selected")
            return

        reply = QMessageBox.question(
            self, "Confirm Deletion",
            f"Delete {len(persons_to_delete)} person(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        removed = 0
        for person in persons_to_delete:
            for cls in self.current_source.classes:
                # Two students carrying identical data compare equal, so a
                # value based lookup deleted the first of them instead of the
                # record the user selected.  ``Class.remove_person`` matches by
                # identity, which keeps duplicates individually deletable.
                if any(existing is person for existing in cls.persons):
                    cls.remove_person(person)
                    removed += 1
                    break

        self.refresh_tree(self.search_input.text().strip())
        self.source_manager.notify_source_modified(self.current_source.name)

        QMessageBox.information(self, "Success", f"Deleted {removed} person(s)")
        logger.info(f"Deleted {removed} persons")

    # ------------------------------------------------------------------
    # Class operations
    # ------------------------------------------------------------------

    def on_add_class(self) -> None:
        """Add a new class."""
        if not self._require_editable_source():
            return

        class_name, ok = QInputDialog.getText(
            self, "Add Class", "Class name (e.g., 6.A, IX.B):"
        )
        if not ok:
            return

        class_name = class_name.strip()
        if not class_name:
            QMessageBox.warning(self, "Invalid Name", "Class name cannot be empty")
            return

        if self.current_source.find_class_by_name(class_name):
            QMessageBox.warning(self, "Duplicate Class",
                                f"Class {class_name} already exists")
            return

        self.current_source.add_class(Class(name=class_name))

        self.refresh_tree()
        self.source_manager.notify_source_modified(self.current_source.name)

        QMessageBox.information(self, "Success", f"Added class {class_name}")
        logger.info(f"Added class: {class_name}")

    def on_rename_class(self) -> None:
        """Rename the selected class."""
        cls = self._selected_class()
        if cls is None:
            return
        if not self._require_editable_source():
            return

        new_name, ok = QInputDialog.getText(
            self, "Rename Class", f"New name for class {cls.name}:",
            QLineEdit.EchoMode.Normal, cls.name
        )
        if not ok:
            return

        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(self, "Invalid Name", "Class name cannot be empty")
            return
        if new_name == cls.name:
            return

        existing = self.current_source.find_class_by_name(new_name)
        if existing is not None:
            reply = QMessageBox.question(
                self, "Class Exists",
                f"Class {new_name} already exists.\n\n"
                f"Move the {len(cls.persons)} student(s) of {cls.name} into it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        old_name = cls.name
        rename_class(self.current_source, cls, new_name)

        self.refresh_tree()
        self.source_manager.notify_source_modified(self.current_source.name)

        QMessageBox.information(self, "Success",
                                f"Renamed class from {old_name} to {new_name}")
        logger.info(f"Renamed class: {old_name} -> {new_name}")

    def on_convert_selected_class(self) -> None:
        """Convert the numeral of the selected class (Roman <-> Arabic)."""
        cls = self._selected_class()
        if cls is None:
            return
        if not self._require_editable_source():
            return

        dialog = ClassNumeralConversionDialog(
            [cls.name],
            title=f"Convert Class Numerals - {cls.name}",
            description=(
                f"Rewrite the name of class <b>{cls.name}</b>.<br>"
                "Choose one of the unified formats or build your own with "
                "placeholders. If the name contains no recognisable number it "
                "is left unchanged."
            ),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        result = convert_class_name(cls.name, dialog.get_template())

        if not result.recognised:
            QMessageBox.warning(
                self, "No Number Found",
                f"No number could be found in '{cls.name}', so the name was "
                f"left unchanged."
            )
            return

        if not result.changed:
            QMessageBox.information(self, "Nothing To Do",
                                    "The class name already has the target format.")
            return

        existing = self.current_source.find_class_by_name(result.result)
        if existing is not None:
            reply = QMessageBox.question(
                self, "Class Exists",
                f"Class {result.result} already exists.\n\n"
                f"Move the {len(cls.persons)} student(s) of {cls.name} into it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        old_name = cls.name
        rename_class(self.current_source, cls, result.result)

        self.refresh_tree()
        self.source_manager.notify_source_modified(self.current_source.name)

        QMessageBox.information(self, "Success",
                                f"Converted class {old_name} → {result.result}")
        logger.info("Converted class %r -> %r", old_name, result.result)

    def on_delete_class(self) -> None:
        """Delete the selected class."""
        cls = self._selected_class()
        if cls is None:
            return
        if not self._require_editable_source():
            return

        reply = QMessageBox.question(
            self, "Confirm Deletion",
            f"Delete class {cls.name} with {len(cls.persons)} person(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        self.current_source.remove_class(cls)

        self.refresh_tree()
        self.source_manager.notify_source_modified(self.current_source.name)

        QMessageBox.information(self, "Success", f"Deleted class {cls.name}")
        logger.info(f"Deleted class: {cls.name}")


class ComparisonTab(QWidget):
    """Second tab for comparing, repairing and merging sources."""

    def __init__(self, source_manager):
        super().__init__()
        self.source_manager = source_manager
        self.init_ui()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def init_ui(self) -> None:
        """Initialize the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        title = QLabel("Source Comparison and Synchronization")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel(
            "Compare, repair and merge data sources. Each source has its own "
            "operations directly underneath it. Person and class editing is "
            "available on the Output panel."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #aaa; margin-bottom: 10px;")
        layout.addWidget(desc)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.left_panel = SourcePanel(
            "Left Source", self.source_manager, accent="#4CAF50"
        )
        self.right_panel = SourcePanel(
            "Right Source", self.source_manager, accent="#FF9800"
        )
        self.output_panel = SourcePanel(
            "Output Source", self.source_manager, allow_edit=True, accent="#2196F3"
        )

        self.left_panel.duplicate_requested.connect(lambda: self.copy_to_output('left'))
        self.left_panel.shift_requested.connect(lambda: self.shift_classes('left'))
        self.left_panel.convert_requested.connect(lambda: self.convert_class_numerals('left'))
        self.left_panel.analyze_requested.connect(lambda: self.analyze_source('left'))
        self.left_panel.enrollment_requested.connect(lambda: self.update_enrollment_years('left'))

        self.right_panel.duplicate_requested.connect(lambda: self.copy_to_output('right'))
        self.right_panel.shift_requested.connect(lambda: self.shift_classes('right'))
        self.right_panel.convert_requested.connect(lambda: self.convert_class_numerals('right'))
        self.right_panel.analyze_requested.connect(lambda: self.analyze_source('right'))
        self.right_panel.enrollment_requested.connect(lambda: self.update_enrollment_years('right'))

        splitter.addWidget(self.left_panel)
        splitter.addWidget(self.right_panel)
        splitter.addWidget(self.output_panel)
        splitter.setSizes([1, 1, 1])

        layout.addWidget(splitter, stretch=1)

        layout.addLayout(self._build_shared_operations())

        self.refresh_all_panels()

    def _build_shared_operations(self) -> QVBoxLayout:
        """Build the centred bar with the operations that need both sources."""
        container = QVBoxLayout()
        container.setSpacing(4)

        caption = QLabel("Operations on both sources")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setStyleSheet("font-weight: bold; color: #2196F3; font-size: 10px;")
        container.addWidget(caption)

        row = QHBoxLayout()
        row.addStretch()

        merge_btn = QPushButton("🔀 Merge Both Sources")
        merge_btn.setToolTip(
            "Combine the left and the right source into a new one "
            "(union or intersection)"
        )
        merge_btn.setMinimumHeight(32)
        merge_btn.setMinimumWidth(260)
        merge_btn.clicked.connect(self.merge_sources)
        row.addWidget(merge_btn)

        row.addStretch()
        container.addLayout(row)
        return container

    def refresh_all_panels(self) -> None:
        """Refresh all three panels."""
        self.left_panel.refresh_sources()
        self.right_panel.refresh_sources()
        self.output_panel.refresh_sources()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _panel_source(self, side: str) -> Optional[Source]:
        """Return the source selected in *side* ('left' or 'right')."""
        panel = self.left_panel if side == 'left' else self.right_panel
        source = panel.current_source
        if not source:
            QMessageBox.warning(
                self, "No Source",
                f"No source is selected in the {panel.title} panel"
            )
            return None
        return source

    def _ask_for_source_name(self, title: str, default_name: str) -> Optional[str]:
        """
        Ask for a source name, validating it and handling duplicates.

        Returns:
            The confirmed name, or ``None`` when the user cancelled.
        """
        suggestion = default_name
        while True:
            name, ok = QInputDialog.getText(
                self, title, "Enter name for the new source:",
                QLineEdit.EchoMode.Normal, suggestion
            )
            if not ok:
                return None

            name = name.strip()
            if not name:
                QMessageBox.warning(self, "Invalid Name", "Name cannot be empty")
                continue

            if self.source_manager.source_name_exists(name):
                reply = QMessageBox.warning(
                    self, "Duplicate Name",
                    f"Source '{name}' already exists. Use it anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    suggestion = name
                    continue

            return name

    def _store_result(self, repaired: Source, original: Source,
                      default_name: str, operation: str) -> None:
        """
        Store a modified working copy either in place or as a new source.

        Args:
            repaired: The modified working copy.
            original: The source the copy was made from.
            default_name: Suggested name for a new source.
            operation: Human readable operation name for the messages.
        """
        box = QMessageBox(self)
        box.setWindowTitle("Save Result")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"How should the result of '{operation}' be stored?")

        update_button = None
        if not original.readonly:
            update_button = box.addButton(
                f"Update '{original.name}'", QMessageBox.ButtonRole.AcceptRole
            )
        new_button = box.addButton("Create new source…",
                                   QMessageBox.ButtonRole.AcceptRole)
        cancel_button = box.addButton("Discard", QMessageBox.ButtonRole.RejectRole)

        if original.readonly:
            box.setInformativeText(
                f"'{original.name}' is read-only, so it cannot be updated in "
                f"place. The result can only be stored as a new source."
            )

        box.exec()
        clicked = box.clickedButton()

        if clicked is cancel_button:
            logger.info("User discarded the result of %s", operation)
            return

        if update_button is not None and clicked is update_button:
            original.classes = repaired.classes
            self.source_manager.notify_source_modified(original.name)
            self.refresh_all_panels()
            QMessageBox.information(
                self, "Updated",
                f"'{original.name}' now contains "
                f"{len(original.classes)} class(es) and "
                f"{len(original.get_all_persons())} student(s)."
            )
            logger.info("Updated source %r after %s", original.name, operation)
            return

        if clicked is new_button:
            name = self._ask_for_source_name("Name New Source", default_name)
            if not name:
                return
            repaired.name = name
            repaired.readonly = False
            repaired.source_type = 'manual'
            self.source_manager.add_source(repaired)
            QMessageBox.information(
                self, "Success",
                f"Created source '{name}' with {len(repaired.classes)} class(es) "
                f"and {len(repaired.get_all_persons())} student(s)."
            )
            logger.info("Created source %r from %s", name, operation)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def copy_to_output(self, side: str) -> None:
        """Create an editable copy of the selected source."""
        source = self._panel_source(side)
        if source is None:
            return

        name = self._ask_for_source_name("Name Output Source", f"{source.name}-copy")
        if not name:
            return

        try:
            new_source = source.deep_copy()
        except Exception as exc:
            logger.exception("Could not copy source %r", source.name)
            QMessageBox.critical(self, "Error", f"Failed to copy source: {exc}")
            return

        new_source.name = name
        new_source.readonly = False
        new_source.source_type = 'manual'

        self.source_manager.add_source(new_source)

        QMessageBox.information(self, "Success", f"Created copy: {name}")
        logger.info(f"Created source copy: {name}")

    def update_enrollment_years(self, side: str) -> None:
        """
        Recalculate the enrollment year of every class of one input source.

        The year lookup asks the internet, which can take seconds on a machine
        without a connection. It therefore runs in a background task behind a
        progress dialog - doing it inline froze the whole window - and only the
        confirmation dialog happens on the GUI thread.
        """
        source = self._panel_source(side)
        if source is None:
            return

        if not source.classes:
            QMessageBox.information(self, "No Classes",
                                    f"'{source.name}' contains no classes.")
            return

        from utils.enrollment_task import EnrollmentYearTask
        from utils.progress_dialog import ProgressDialog

        task = EnrollmentYearTask(source)
        progress = ProgressDialog(task, self)
        progress.start_task()
        progress.exec()

        if progress.was_cancelled():
            logger.info("Enrollment year calculation cancelled for %r", source.name)
            return

        resolution = task.resolution
        if resolution is None:
            QMessageBox.critical(
                self, "Error",
                "The current year could not be determined, so no enrollment "
                "year was calculated."
            )
            return

        if not task.changes:
            QMessageBox.information(
                self, "Nothing Changed",
                f"No enrollment year needed to be changed.\n\n"
                f"Current year: {resolution.describe()}"
            )
            return

        from ui.enrollment_year_dialog import EnrollmentYearDialog
        from utils.school_year import apply_enrollment_changes

        dialog = EnrollmentYearDialog(task.changes, resolution, source.name, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        updated = apply_enrollment_changes(source, dialog.selected_changes())
        if not updated:
            return

        self.source_manager.notify_source_modified(source.name)
        self.refresh_all_panels()
        QMessageBox.information(
            self, "Enrollment Years Updated",
            f"Set the enrollment year of {updated} class(es) in '{source.name}'.\n\n"
            f"Current year: {resolution.describe()}"
        )

    def analyze_source(self, side: str) -> None:
        """Run the deep analysis on one input source."""
        source = self._panel_source(side)
        if source is None:
            return

        panel = self.left_panel if side == 'left' else self.right_panel

        try:
            dialog = SourceAnalysisDialog(source, side=panel.title, parent=self)
        except Exception as exc:
            logger.exception("Could not open the analysis dialog")
            QMessageBox.critical(self, "Error", f"Analysis failed: {exc}")
            return

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        if not dialog.has_changes():
            QMessageBox.information(self, "No Changes",
                                    "No solution was applied, nothing to save.")
            return

        self._store_result(
            dialog.get_repaired_source(), source,
            f"{source.name}-fixed", "Analyze Source",
        )

    def convert_class_numerals(self, side: str) -> None:
        """Convert every class name of one input source."""
        source = self._panel_source(side)
        if source is None:
            return

        if not source.classes:
            QMessageBox.information(self, "No Classes",
                                    f"'{source.name}' contains no classes.")
            return

        dialog = ClassNumeralConversionDialog(
            [cls.name for cls in source.classes],
            title=f"Convert Class Numerals - {source.name}",
            description=(
                f"Rewrite every class name of <b>{source.name}</b>.<br>"
                "Use one of the unified formats or build your own with "
                "placeholders. Class names without a recognisable number are "
                "left unchanged - they are never removed."
            ),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        template = dialog.get_template()

        try:
            working = source.deep_copy()
            changed = 0
            for cls in list(working.classes):
                result = convert_class_name(cls.name, template)
                if result.changed and rename_class(working, cls, result.result):
                    changed += 1
        except Exception as exc:
            logger.exception("Class numeral conversion failed")
            QMessageBox.critical(self, "Error", f"Conversion failed: {exc}")
            return

        if not changed:
            QMessageBox.information(self, "Nothing Changed",
                                    "No class name needed to be changed.")
            return

        self._store_result(
            working, source, f"{source.name}-converted", "Convert Class Numerals"
        )

    def shift_classes(self, side: str) -> None:
        """Shift the class years of one input source into a new source."""
        source = self._panel_source(side)
        if source is None:
            return

        try:
            dialog = ClassShiftDialog(source, self)
        except Exception as exc:
            logger.exception("Could not open the shift dialog")
            QMessageBox.critical(self, "Error", f"Failed to prepare the shift: {exc}")
            return

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        name = self._ask_for_source_name("Name Shifted Source", f"{source.name}-shifted")
        if not name:
            return

        try:
            new_source = dialog.build_result_source(name)
        except Exception as exc:
            logger.exception("Error shifting classes")
            QMessageBox.critical(self, "Error", f"Failed to shift classes: {exc}")
            return

        self.source_manager.add_source(new_source)

        QMessageBox.information(
            self, "Success",
            f"Created shifted source: {name}\n"
            f"Classes: {len(new_source.classes)}\n"
            f"Students: {len(new_source.get_all_persons())}"
        )
        logger.info(f"Created shifted source: {name}")

    def merge_sources(self) -> None:
        """Merge the left and the right source into a new one."""
        left = self.left_panel.current_source
        right = self.right_panel.current_source

        if not left or not right:
            QMessageBox.warning(self, "Missing Source",
                                "Please select both left and right sources")
            return

        try:
            dialog = MergeSourcesDialog(left, right, self)
        except Exception as exc:
            logger.exception("Could not open the merge dialog")
            QMessageBox.critical(self, "Error", f"Failed to prepare the merge: {exc}")
            return

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        strategy = dialog.get_strategy()

        name = self._ask_for_source_name("Name Merged Source",
                                         f"{left.name}+{right.name}")
        if not name:
            return

        try:
            merged, stats = self.source_manager.merge_sources_detailed(
                dialog.get_left_source(), dialog.get_right_source(), strategy, name
            )
        except Exception as exc:
            logger.exception("Error merging sources")
            QMessageBox.critical(self, "Error", f"Failed to merge: {exc}")
            return

        self.source_manager.add_source(merged)

        details = [
            f"Result: {stats['result_persons']} persons in "
            f"{stats['result_classes']} classes",
            f"Only in left: {stats['only_left']} • only in right: "
            f"{stats['only_right']} • in both: {stats['in_both']}",
        ]
        collapsed = stats['left_duplicates_collapsed'] + stats['right_duplicates_collapsed']
        if collapsed:
            details.append(f"Duplicate records collapsed: {collapsed}")
        if dialog.has_changes():
            details.append(
                "Solutions applied during the analysis were used for this merge "
                "(the original sources were not modified)."
            )

        QMessageBox.information(
            self, "Success",
            f"Merged sources using the '{strategy}' strategy.\n\n" + "\n".join(details)
        )
        logger.info(f"Merged sources: {left.name} + {right.name} = {name}")
