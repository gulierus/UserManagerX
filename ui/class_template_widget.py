"""
Class-name template widget
==========================

Reusable widget that lets the user choose *how* class names should be
rewritten:

* one of the predefined templates (unified Roman / unified Arabic), or
* a completely custom template built from placeholders.

The widget renders a live preview against the real class names it was given,
so the user always sees the exact result before anything is changed - including
the names the conversion cannot handle.

It is used by:

* ``ClassNumeralConversionDialog``  (Comparison tab - convert class numerals)
* ``ClassShiftDialog``              (Comparison tab - unify before shifting)
* ``SourceAnalysisDialog``          (Comparison tab - "unify class names" fix)
"""

import logging
from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QButtonGroup, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QRadioButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
)

from utils.class_name_utils import (
    PREDEFINED_TEMPLATES,
    TEMPLATE_ARABIC,
    TEMPLATE_PLACEHOLDERS,
    TEMPLATE_ROMAN,
    ClassNameResult,
    convert_class_name,
    validate_template,
)

logger = logging.getLogger(__name__)


class ClassTemplateWidget(QWidget):
    """
    Template chooser with live preview.

    Signals:
        template_changed(str): Emitted with the currently effective template
            whenever the selection or the custom text changes.  Emits an empty
            string while the custom template is invalid.
    """

    template_changed = pyqtSignal(str)

    def __init__(self, class_names: Optional[List[str]] = None,
                 show_preview: bool = True, parent: Optional[QWidget] = None):
        """
        Args:
            class_names: Names used to build the preview table.
            show_preview: Set to False to hide the preview table.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._class_names: List[str] = list(class_names or [])
        self._show_preview = show_preview
        self._results: List[ClassNameResult] = []
        self._build_ui()
        self._refresh()

    # -- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # --- target format -----------------------------------------------
        format_group = QGroupBox("Target format")
        format_layout = QVBoxLayout(format_group)

        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)

        self._roman_radio = QRadioButton(PREDEFINED_TEMPLATES["roman"][0])
        self._roman_radio.setToolTip(PREDEFINED_TEMPLATES["roman"][2])
        self._button_group.addButton(self._roman_radio, 1)
        format_layout.addWidget(self._roman_radio)
        format_layout.addWidget(self._hint(PREDEFINED_TEMPLATES["roman"][2]))

        self._arabic_radio = QRadioButton(PREDEFINED_TEMPLATES["arabic"][0])
        self._arabic_radio.setToolTip(PREDEFINED_TEMPLATES["arabic"][2])
        self._button_group.addButton(self._arabic_radio, 2)
        format_layout.addWidget(self._arabic_radio)
        format_layout.addWidget(self._hint(PREDEFINED_TEMPLATES["arabic"][2]))

        self._custom_radio = QRadioButton("Custom template")
        self._button_group.addButton(self._custom_radio, 3)
        format_layout.addWidget(self._custom_radio)

        custom_row = QHBoxLayout()
        custom_row.addSpacing(20)
        self._custom_input = QLineEdit(TEMPLATE_ARABIC)
        self._custom_input.setPlaceholderText("e.g. {arabic}.{letter_upper}")
        self._custom_input.textChanged.connect(self._on_custom_changed)
        custom_row.addWidget(self._custom_input)
        format_layout.addLayout(custom_row)

        self._error_label = QLabel()
        self._error_label.setStyleSheet("color: #ff6b6b; font-size: 10px;")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        format_layout.addWidget(self._error_label)

        # Placeholder help
        help_lines = "<br>".join(
            f"<code>{{{name}}}</code> &nbsp;-&nbsp; {description}"
            for name, description in TEMPLATE_PLACEHOLDERS.items()
        )
        help_label = QLabel(
            "<b>Available placeholders</b><br>" + help_lines +
            "<br><br>Anything that is not a placeholder is copied to the result "
            "as it is, so <code>Class {arabic}-{letter_upper}</code> produces "
            "<code>Class 6-A</code>."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #999; font-size: 10px;")
        format_layout.addWidget(help_label)

        self._roman_radio.setChecked(True)
        self._button_group.idToggled.connect(self._on_mode_changed)

        layout.addWidget(format_group)

        # --- preview -----------------------------------------------------
        if self._show_preview:
            preview_group = QGroupBox("Preview")
            preview_layout = QVBoxLayout(preview_group)

            self._preview_table = QTableWidget(0, 3)
            self._preview_table.setHorizontalHeaderLabels(
                ["Current name", "New name", "Note"]
            )
            self._preview_table.verticalHeader().setVisible(False)
            self._preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            self._preview_table.setSelectionMode(
                QTableWidget.SelectionMode.NoSelection
            )
            header = self._preview_table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self._preview_table.setMinimumHeight(160)
            preview_layout.addWidget(self._preview_table)

            self._summary_label = QLabel()
            self._summary_label.setStyleSheet("color: #aaa; font-size: 10px;")
            self._summary_label.setWordWrap(True)
            preview_layout.addWidget(self._summary_label)

            layout.addWidget(preview_group, stretch=1)
        else:
            self._preview_table = None
            self._summary_label = None

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet("color: #888; font-size: 10px; margin-left: 20px;")
        return label

    # -- slots -----------------------------------------------------------

    def _on_mode_changed(self, _id: int, checked: bool) -> None:
        if checked:
            self._custom_input.setEnabled(self._custom_radio.isChecked())
            self._refresh()

    def _on_custom_changed(self, _text: str) -> None:
        if not self._custom_radio.isChecked():
            self._custom_radio.setChecked(True)
        self._refresh()

    # -- public API ------------------------------------------------------

    def set_class_names(self, class_names: List[str]) -> None:
        """Replace the names used for the preview."""
        self._class_names = list(class_names or [])
        self._refresh()

    def get_template(self) -> str:
        """
        Return the currently selected template.

        Returns:
            The template string, or an empty string when the custom template
            is invalid.
        """
        if self._roman_radio.isChecked():
            return TEMPLATE_ROMAN
        if self._arabic_radio.isChecked():
            return TEMPLATE_ARABIC
        template = self._custom_input.text()
        return "" if validate_template(template) else template

    def get_error(self) -> Optional[str]:
        """Return the validation error of the custom template, if any."""
        if not self._custom_radio.isChecked():
            return None
        return validate_template(self._custom_input.text())

    def get_results(self) -> List[ClassNameResult]:
        """Return the preview results for the current template."""
        return list(self._results)

    def is_valid(self) -> bool:
        """True when the current selection can be applied."""
        return bool(self.get_template())

    def select_template(self, template: str) -> None:
        """Pre-select a template (falls back to the custom field)."""
        if template == TEMPLATE_ROMAN:
            self._roman_radio.setChecked(True)
        elif template == TEMPLATE_ARABIC:
            self._arabic_radio.setChecked(True)
        else:
            self._custom_input.setText(template)
            self._custom_radio.setChecked(True)
        self._refresh()

    # -- rendering -------------------------------------------------------

    def _refresh(self) -> None:
        """Recompute preview and emit :attr:`template_changed`."""
        error = self.get_error()
        if self._error_label is not None:
            self._error_label.setText(error or "")
            self._error_label.setVisible(bool(error))

        template = self.get_template()
        self._results = []

        if template:
            for name in self._class_names:
                self._results.append(convert_class_name(name, template))

        self._render_preview(bool(template))
        self.template_changed.emit(template)

    def _render_preview(self, valid: bool) -> None:
        if self._preview_table is None:
            return

        self._preview_table.setRowCount(len(self._results))

        changed = unchanged = skipped = 0
        produced = {}

        for row, result in enumerate(self._results):
            if not result.recognised:
                note, color = "no number found - left unchanged", "#FFA726"
                skipped += 1
            elif not result.changed:
                note, color = "already in the target format", "#888888"
                unchanged += 1
            else:
                note, color = result.message, "#4CAF50"
                changed += 1

            produced.setdefault(result.result, []).append(result.original)

            original_item = QTableWidgetItem(result.original or "(empty)")
            new_item = QTableWidgetItem(result.result or "(empty)")
            note_item = QTableWidgetItem(note)
            for item in (original_item, new_item, note_item):
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            # The per-row colour was computed and then thrown away, so every
            # note rendered in the same grey and the status was invisible.
            note_item.setForeground(QColor(color))
            self._preview_table.setItem(row, 0, original_item)
            self._preview_table.setItem(row, 1, new_item)
            self._preview_table.setItem(row, 2, note_item)

        collisions = {n: o for n, o in produced.items() if len(o) > 1}

        if self._summary_label is not None:
            if not valid:
                self._summary_label.setText(
                    "<span style='color:#ff6b6b;'>Fix the template to see a preview.</span>"
                )
            else:
                text = (
                    f"{changed} name(s) will change, {unchanged} already match "
                    f"the format, {skipped} contain no number and stay as they are."
                )
                if collisions:
                    joined = "; ".join(
                        f"{' + '.join(origins)} → {name}"
                        for name, origins in list(collisions.items())[:5]
                    )
                    text += (
                        f"<br><span style='color:#FFA726;'>⚠️ {len(collisions)} "
                        f"name collision(s): {joined}. Those classes will be "
                        f"merged into one.</span>"
                    )
                self._summary_label.setText(text)
