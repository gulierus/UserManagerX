"""
Password format dialog
======================

Lets the user define what a generated password has to look like.

The dialog edits a :class:`~utils.password_policy.PasswordPolicy` and stores it
in the application settings.  Because both ``generate_password()`` and
``validate_password()`` read that very policy, a change here immediately
affects generation *and* validation - a generated password can no longer be
reported as invalid.
"""

import logging
from typing import Optional

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from utils.password_policy import (
    ABSOLUTE_MAX_LENGTH, ABSOLUTE_MIN_LENGTH, AMBIGUOUS_CHARACTERS,
    DEFAULT_SPECIAL_CHARACTERS, PasswordPolicy, get_password_policy,
    set_password_policy,
)

logger = logging.getLogger(__name__)


class PasswordPolicyDialog(QDialog):
    """Editor for the application-wide password format."""

    #: How many sample passwords are shown in the preview.
    SAMPLE_COUNT = 3

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Password Format")
        self.setModal(True)
        self.setMinimumWidth(560)

        self._policy = get_password_policy()
        self._build_ui()
        self._load(self._policy)
        self._refresh_preview()

    # -- construction ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(
            "Define how generated passwords look. The same rules are used to "
            "<b>generate</b> and to <b>validate</b> passwords, so a generated "
            "password always passes validation."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            "background-color: #2a4a5a; padding: 10px; border-radius: 5px;"
        )
        layout.addWidget(info)

        # --- length -------------------------------------------------------
        length_group = QGroupBox("Length")
        length_form = QFormLayout(length_group)

        self._length_spin = QSpinBox()
        self._length_spin.setRange(ABSOLUTE_MIN_LENGTH, ABSOLUTE_MAX_LENGTH)
        self._length_spin.setToolTip("Number of characters a new password gets")
        self._length_spin.valueChanged.connect(self._refresh_preview)
        length_form.addRow("Generated length:", self._length_spin)

        self._min_spin = QSpinBox()
        self._min_spin.setRange(ABSOLUTE_MIN_LENGTH, ABSOLUTE_MAX_LENGTH)
        self._min_spin.setToolTip(
            "Passwords shorter than this are reported as an error during validation"
        )
        self._min_spin.valueChanged.connect(self._refresh_preview)
        length_form.addRow("Minimum accepted length:", self._min_spin)

        self._max_spin = QSpinBox()
        self._max_spin.setRange(ABSOLUTE_MIN_LENGTH, ABSOLUTE_MAX_LENGTH)
        self._max_spin.setToolTip(
            "Passwords longer than this are reported as a warning during validation"
        )
        self._max_spin.valueChanged.connect(self._refresh_preview)
        length_form.addRow("Maximum accepted length:", self._max_spin)

        layout.addWidget(length_group)

        # --- character classes --------------------------------------------
        classes_group = QGroupBox("Required character types")
        classes_layout = QVBoxLayout(classes_group)

        self._lower_check = QCheckBox("Lower-case letters (a-z)")
        self._upper_check = QCheckBox("Upper-case letters (A-Z)")
        self._digit_check = QCheckBox("Digits (0-9)")
        self._special_check = QCheckBox("Special characters")
        for box in (self._lower_check, self._upper_check,
                    self._digit_check, self._special_check):
            box.toggled.connect(self._refresh_preview)
            classes_layout.addWidget(box)

        special_row = QHBoxLayout()
        special_row.addSpacing(20)
        special_row.addWidget(QLabel("Allowed special characters:"))
        self._special_input = QLineEdit()
        self._special_input.setPlaceholderText(DEFAULT_SPECIAL_CHARACTERS)
        self._special_input.setToolTip(
            "Only these characters are used as special characters. Keep the "
            "set small so passwords stay easy to type and safe to export."
        )
        self._special_input.textChanged.connect(self._refresh_preview)
        special_row.addWidget(self._special_input, stretch=1)
        classes_layout.addLayout(special_row)

        self._ambiguous_check = QCheckBox(
            f"Leave out easily confused characters ({AMBIGUOUS_CHARACTERS})"
        )
        self._ambiguous_check.setToolTip(
            "Useful when passwords are printed and typed by hand"
        )
        self._ambiguous_check.toggled.connect(self._refresh_preview)
        classes_layout.addWidget(self._ambiguous_check)

        layout.addWidget(classes_group)

        # --- preview -------------------------------------------------------
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)

        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        self._summary_label.setStyleSheet("color: #aaa; font-size: 10px;")
        preview_layout.addWidget(self._summary_label)

        self._sample_label = QLabel()
        self._sample_label.setWordWrap(True)
        self._sample_label.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 12px;"
        )
        preview_layout.addWidget(self._sample_label)

        refresh_row = QHBoxLayout()
        refresh_row.addStretch()
        refresh_button = QPushButton("↻ New samples")
        refresh_button.clicked.connect(self._refresh_preview)
        refresh_row.addWidget(refresh_button)
        preview_layout.addLayout(refresh_row)

        layout.addWidget(preview_group)

        # --- buttons -------------------------------------------------------
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.RestoreDefaults
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        button_box.button(
            QDialogButtonBox.StandardButton.RestoreDefaults
        ).clicked.connect(self._on_restore_defaults)
        layout.addWidget(button_box)

    # -- data --------------------------------------------------------------

    def _load(self, policy: PasswordPolicy) -> None:
        """Copy *policy* into the widgets."""
        for widget in (self._length_spin, self._min_spin, self._max_spin,
                       self._lower_check, self._upper_check, self._digit_check,
                       self._special_check, self._special_input,
                       self._ambiguous_check):
            widget.blockSignals(True)

        self._length_spin.setValue(policy.length)
        self._min_spin.setValue(policy.min_length)
        self._max_spin.setValue(policy.max_length)
        self._lower_check.setChecked(policy.require_lowercase)
        self._upper_check.setChecked(policy.require_uppercase)
        self._digit_check.setChecked(policy.require_digits)
        self._special_check.setChecked(policy.require_special)
        self._special_input.setText(policy.special_characters)
        self._ambiguous_check.setChecked(policy.exclude_ambiguous)

        for widget in (self._length_spin, self._min_spin, self._max_spin,
                       self._lower_check, self._upper_check, self._digit_check,
                       self._special_check, self._special_input,
                       self._ambiguous_check):
            widget.blockSignals(False)

    def build_policy(self) -> PasswordPolicy:
        """Build a policy from the current widget values."""
        return PasswordPolicy(
            length=self._length_spin.value(),
            min_length=self._min_spin.value(),
            max_length=self._max_spin.value(),
            require_lowercase=self._lower_check.isChecked(),
            require_uppercase=self._upper_check.isChecked(),
            require_digits=self._digit_check.isChecked(),
            require_special=self._special_check.isChecked(),
            special_characters=(self._special_input.text()
                                or DEFAULT_SPECIAL_CHARACTERS),
            exclude_ambiguous=self._ambiguous_check.isChecked(),
        )

    # -- preview -----------------------------------------------------------

    def _refresh_preview(self, *_args) -> None:
        """Recompute the summary and the sample passwords."""
        policy = self.build_policy()
        self._special_input.setEnabled(self._special_check.isChecked())

        problems = policy.problems()
        if problems:
            self._summary_label.setText(
                "<span style='color:#ff6b6b;'>" +
                "<br>".join(f"✖ {problem}" for problem in problems) +
                "</span>"
            )
            self._sample_label.setText("")
            return

        self._summary_label.setText(policy.describe())

        # Import here so a broken policy can never break the dialog itself
        from utils.ad_utils import generate_password

        samples = []
        for _ in range(self.SAMPLE_COUNT):
            try:
                samples.append(generate_password(policy=policy))
            except ValueError as exc:                # pragma: no cover - guarded above
                self._sample_label.setText(f"<span style='color:#ff6b6b;'>{exc}</span>")
                return

        self._sample_label.setText("&nbsp;&nbsp;".join(samples))

    # -- actions -----------------------------------------------------------

    def _on_restore_defaults(self) -> None:
        self._load(PasswordPolicy())
        self._refresh_preview()

    def _on_accept(self) -> None:
        policy = self.build_policy()
        problems = policy.problems()
        if problems:
            QMessageBox.warning(
                self, "Invalid Password Format",
                "The password format cannot be used:\n\n" +
                "\n".join(f"• {problem}" for problem in problems)
            )
            return

        try:
            saved = set_password_policy(policy)
        except ValueError as exc:                    # pragma: no cover - guarded above
            QMessageBox.warning(self, "Invalid Password Format", str(exc))
            return

        if not saved:
            QMessageBox.warning(
                self, "Not Saved",
                "The password format is active for this session but could not "
                "be written to the settings file."
            )

        self._policy = policy
        logger.info("Password policy updated: %s", policy.describe())
        self.accept()

    def get_policy(self) -> PasswordPolicy:
        """Return the policy that was confirmed."""
        return self._policy
