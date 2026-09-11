"""Validates the shared test scaffold itself (fixtures, isolation, stubs)."""

import pytest
from PyQt6.QtWidgets import QMessageBox, QInputDialog


def test_qapplication_available(qapp):
    assert qapp is not None


def test_dialog_recorder_captures_messages(dialogs):
    QMessageBox.warning(None, "Title", "Body text")
    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("body text")


def test_dialog_recorder_answers_questions(dialogs):
    dialogs.question_answer = QMessageBox.StandardButton.No
    answer = QMessageBox.question(None, "Q", "Really?")
    assert answer == QMessageBox.StandardButton.No


def test_input_dialog_is_stubbed(dialogs):
    dialogs.text_answer = ("chosen", True)
    value, ok = QInputDialog.getText(None, "T", "Label")
    assert (value, ok) == ("chosen", True)
    assert dialogs.kinds() == ["get_text"]


def test_settings_are_isolated(isolated_settings, tmp_path):
    assert str(tmp_path) in str(isolated_settings.settings_file)
    isolated_settings.set("theme", "Blue", category="general")
    assert isolated_settings.get_str("theme", category="general") == "Blue"


def test_password_policy_resets_between_tests():
    from utils.password_policy import get_password_policy, PasswordPolicy, set_password_policy
    assert get_password_policy().length == PasswordPolicy().length
    set_password_policy(PasswordPolicy(length=32, max_length=64), persist=False)
    assert get_password_policy().length == 32


def test_password_policy_really_reset():
    from utils.password_policy import get_password_policy, PasswordPolicy
    assert get_password_policy().length == PasswordPolicy().length


def test_model_factories(make_source):
    source = make_source("S", [("6.A", 2), ("IX.", 3)])
    assert len(source.classes) == 2
    assert len(source.get_all_persons()) == 5
