"""
Editing one of the Microsoft 365 name templates

Version 26, point 21 e asks for a combo box that opens the settings windows for
the generated **email (sign-in) name**, the **display name** and the **group
name**, all of them supporting placeholders.  One dialog serves all three: they
differ only in which fields are available and what the result is for.

The dialog shows a **live preview against a real example**, because a template
language is only usable if you can see what it does before you commit to it.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from utils.name_templates import (
    CLASS_FIELDS, PERSON_FIELDS, describe_operations, preview, validate,
)

logger = logging.getLogger(__name__)

#: Example values used for the preview, so the user sees a real result even
#: before a source is loaded.
EXAMPLE_PERSON = {
    'first_name': "Žofie",
    'last_name': "Křížová",
    'class_name': "6.A",
    'grade': "6",
    'roman': "VI",
    'letter': "A",
    'enrollment_year': "2020",
    'username': "krizovazofie",
    'domain': "skola.onmicrosoft.com",
}

EXAMPLE_CLASS = {
    'class_name': "6.A",
    'grade': "6",
    'roman': "VI",
    'letter': "A",
    'enrollment_year': "2020",
    'school_year': "2025/2026",
}


class M365NameFormatDialog(QDialog):
    """Edits one name template, with help and a live preview."""

    def __init__(self, title: str, template: str, kind: str,
                 explanation: str, domain: str = "", parent=None):
        """
        Args:
            title: Window title.
            template: The template to start from.
            kind: ``"person"`` or ``"class"`` - which fields are available.
            explanation: One sentence saying what the result is used for.
            domain: The real tenant domain, used in the preview when known.
            parent: Qt parent.
        """
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(620)

        self._kind = kind
        self._fields = PERSON_FIELDS if kind == "person" else CLASS_FIELDS
        self._example = dict(EXAMPLE_PERSON if kind == "person"
                             else EXAMPLE_CLASS)
        if domain and kind == "person":
            self._example['domain'] = domain

        self._init_ui(template, explanation)
        self._update_preview()

    def _init_ui(self, template: str, explanation: str) -> None:
        layout = QVBoxLayout(self)

        caption = QLabel(explanation)
        caption.setWordWrap(True)
        caption.setStyleSheet("color: #aaa;")
        layout.addWidget(caption)

        row = QHBoxLayout()
        row.addWidget(QLabel("Template:"))
        self.template_input = QLineEdit(template)
        self.template_input.textChanged.connect(self._update_preview)
        row.addWidget(self.template_input, stretch=1)
        layout.addLayout(row)

        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet("font-size: 13px;")
        layout.addWidget(self.preview_label)

        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: #ff6b6b; font-size: 11px;")
        layout.addWidget(self.problem_label)

        help_group = QGroupBox("Placeholders")
        help_layout = QVBoxLayout(help_group)

        fields_label = QLabel("<br>".join(
            f"<code>{{{name}}}</code> — {description}"
            for name, description in self._fields.items()))
        fields_label.setWordWrap(True)
        fields_label.setStyleSheet("font-size: 11px;")
        help_layout.addWidget(fields_label)

        parts_label = QLabel(
            "<b>Parts of a field:</b> <code>{class_name[0:1]}</code> takes a "
            "slice · " + describe_operations()
        )
        parts_label.setWordWrap(True)
        parts_label.setStyleSheet("color: #888; font-size: 11px;")
        help_layout.addWidget(parts_label)

        layout.addWidget(help_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Reset
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(
            lambda: self.template_input.setText(self._default_template()))
        layout.addWidget(buttons)

        self._buttons = buttons

    def _default_template(self) -> str:
        """The template this format starts life with."""
        from services.m365_services import (
            DEFAULT_DISPLAY_NAME_TEMPLATE, DEFAULT_GROUP_TEMPLATE,
            DEFAULT_UPN_TEMPLATE,
        )
        if self._kind == "class":
            return DEFAULT_GROUP_TEMPLATE
        if "@" in self.template_input.text():
            return DEFAULT_UPN_TEMPLATE
        return DEFAULT_DISPLAY_NAME_TEMPLATE

    def _update_preview(self) -> None:
        """Show the result and any problems, as the user types."""
        rendered = preview(self.template_input.text(), self._example)
        self.preview_label.setText(f"Example: <b>{rendered}</b>")

        problems = self.problems()
        self.problem_label.setText("  ".join(problems))
        ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(not problems)

    def problems(self) -> list:
        """Everything wrong with the template as it stands."""
        return validate(self.template_input.text(), self._fields)

    def _on_accept(self) -> None:
        """Refuse to close on a template that cannot work."""
        if self.problems():
            return
        self.accept()

    def template(self) -> str:
        """The template the user settled on."""
        return self.template_input.text().strip()
