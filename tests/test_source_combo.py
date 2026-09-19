"""
Tests for the read-only / editable marker shown in every source combo box.

Version 25, point 05: a source selector used to show the bare name, so the
user only discovered that a source could not be edited after selecting it and
finding half the buttons disabled.  Every source combo now appends the access
mode, and Settings -> General can turn the suffix off again.
"""

import pytest
from PyQt6.QtWidgets import QComboBox

from ui.source_combo import (
    ACCESS_SETTING_CATEGORY,
    ACCESS_SETTING_KEY,
    EDITABLE_LABEL,
    PLACEHOLDER_TEXT,
    READONLY_LABEL,
    access_label,
    combo_source_name,
    find_source_index,
    is_readonly,
    populate_source_combo,
    source_display_label,
    source_labels,
    show_access_enabled,
    strip_access_suffix,
)


@pytest.fixture
def combo(qapp):
    """A throw-away combo box."""
    widget = QComboBox()
    yield widget
    widget.deleteLater()


class TestAccessLabel:
    """Which word describes a source."""

    def test_a_read_only_source_is_called_read_only(self, make_source):
        assert access_label(make_source(readonly=True)) == READONLY_LABEL

    def test_an_ordinary_source_is_called_editable(self, make_source):
        assert access_label(make_source()) == EDITABLE_LABEL

    def test_an_object_without_the_flag_counts_as_editable(self):
        # Defensive: models.Source defaults to editable, and a stand-in used
        # somewhere in the code base must not be reported as locked.
        assert is_readonly(object()) is False


class TestDisplayLabel:
    """The text one row of the combo shows."""

    def test_the_mode_is_appended_to_the_name(self, make_source):
        assert source_display_label(make_source(name="Roster"),
                                    show_access=True) == "Roster (editable)"

    def test_a_read_only_source_says_so(self, make_source):
        assert source_display_label(make_source(name="AD", readonly=True),
                                    show_access=True) == "AD (read-only)"

    def test_the_suffix_can_be_switched_off(self, make_source):
        assert source_display_label(make_source(name="Roster"),
                                    show_access=False) == "Roster"

    def test_the_setting_decides_when_nothing_is_forced(self, make_source,
                                                        isolated_settings):
        isolated_settings.set(ACCESS_SETTING_KEY, False,
                              category=ACCESS_SETTING_CATEGORY)
        assert source_display_label(make_source(name="Roster")) == "Roster"


class TestStripAccessSuffix:
    """Turning a label back into a name without the combo at hand."""

    @pytest.mark.parametrize("label,expected", [
        ("Roster (editable)", "Roster"),
        ("AD school.local (read-only)", "AD school.local"),
        ("Roster", "Roster"),
        ("", ""),
    ])
    def test_known_suffixes_are_removed_and_nothing_else(self, label, expected):
        assert strip_access_suffix(label) == expected

    def test_an_unrelated_parenthesis_is_kept(self):
        # 'AD school.local (2)' is a real name produced by the AD loader.
        assert strip_access_suffix("AD school.local (2)") == "AD school.local (2)"


class TestPopulateSourceCombo:
    """Filling a combo box."""

    def test_every_source_is_listed_after_the_placeholder(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A"), make_source(name="B")],
                              show_access=True)
        assert source_labels(combo) == [PLACEHOLDER_TEXT, "A (editable)",
                                        "B (editable)"]

    def test_the_plain_name_is_stored_as_item_data(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A")], show_access=True)
        assert combo.itemData(1) == "A"

    def test_the_placeholder_carries_no_source_name(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A")])
        assert combo.itemData(0) is None

    def test_the_placeholder_can_be_left_out(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A")], placeholder=None,
                              show_access=False)
        assert source_labels(combo) == ["A"]

    def test_a_previous_content_is_replaced_not_appended(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A")], show_access=False)
        populate_source_combo(combo, [make_source(name="B")], show_access=False)
        assert source_labels(combo) == [PLACEHOLDER_TEXT, "B"]

    def test_read_only_and_editable_sources_are_told_apart(self, combo, make_source):
        populate_source_combo(combo,
                              [make_source(name="File", readonly=True),
                               make_source(name="Work")], show_access=True)
        assert source_labels(combo)[1:] == ["File (read-only)", "Work (editable)"]


class TestComboSourceName:
    """Reading the selection back."""

    def test_the_current_selection_resolves_to_the_source_name(self, combo,
                                                               make_source):
        populate_source_combo(combo, [make_source(name="A")], show_access=True)
        combo.setCurrentIndex(1)
        assert combo_source_name(combo) == "A"

    def test_the_placeholder_resolves_to_its_own_text(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A")])
        combo.setCurrentIndex(0)
        assert combo_source_name(combo) == PLACEHOLDER_TEXT

    def test_a_label_is_translated_into_the_name(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A")], show_access=True)
        assert combo_source_name(combo, "A (editable)") == "A"

    def test_a_plain_name_that_is_not_listed_is_passed_through(self, combo,
                                                               make_source):
        populate_source_combo(combo, [make_source(name="A")], show_access=True)
        assert combo_source_name(combo, "Gone") == "Gone"

    def test_a_source_really_named_like_a_label_is_not_mangled(self, combo,
                                                               make_source):
        # The item data wins over any parsing of the visible text.
        populate_source_combo(combo, [make_source(name="Backup (editable)")],
                              show_access=True)
        combo.setCurrentIndex(1)
        assert combo_source_name(combo) == "Backup (editable)"


class TestFindSourceIndex:
    """Locating a source row whatever the label looks like."""

    def test_the_row_is_found_by_name(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A"), make_source(name="B")],
                              show_access=True)
        assert find_source_index(combo, "B") == 2

    def test_a_missing_source_reports_minus_one(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A")])
        assert find_source_index(combo, "B") == -1

    def test_an_empty_name_reports_minus_one(self, combo, make_source):
        populate_source_combo(combo, [make_source(name="A")])
        assert find_source_index(combo, "") == -1

    def test_a_combo_filled_without_data_still_works(self, combo):
        combo.addItems(["EduPage", "Encrypted JSON File"])
        assert find_source_index(combo, "EduPage") == 0


class TestShowAccessEnabled:
    """The Settings switch behind the suffix."""

    def test_enabled_by_default(self, isolated_settings):
        assert show_access_enabled(isolated_settings) is True

    def test_follows_the_stored_value(self, isolated_settings):
        isolated_settings.set(ACCESS_SETTING_KEY, False,
                              category=ACCESS_SETTING_CATEGORY)
        assert show_access_enabled(isolated_settings) is False

    def test_a_broken_settings_backend_does_not_hide_the_suffix(self):
        class Exploding:
            def get_bool(self, *a, **k):
                raise RuntimeError("settings file is unreadable")

        assert show_access_enabled(Exploding()) is True


@pytest.mark.gui
class TestOperationsTabShowsTheAccessMode:
    """The Operations tab source selector."""

    @pytest.fixture
    def tab(self, qapp, source_manager, isolated_settings):
        # isolated_settings replaces the settings singleton, so it has to be
        # in place *before* the tab subscribes to its signals.
        from ui.operations_tab import OperationsTab
        widget = OperationsTab(source_manager)
        yield widget
        widget.deleteLater()

    def test_the_mode_is_visible_in_the_list(self, tab, source_manager, make_source):
        source_manager.add_source(make_source(name="Roster"))
        source_manager.add_source(make_source(name="Archive", readonly=True))
        assert source_labels(tab.source_combo)[1:] == ["Roster (editable)",
                                                       "Archive (read-only)"]

    def test_selecting_a_decorated_row_selects_the_right_source(
            self, tab, source_manager, make_source):
        roster = make_source(name="Roster")
        source_manager.add_source(roster)
        tab.source_combo.setCurrentIndex(find_source_index(tab.source_combo, "Roster"))
        assert tab.current_source is roster

    def test_the_selection_survives_a_refresh(self, tab, source_manager, make_source):
        roster = make_source(name="Roster")
        source_manager.add_source(roster)
        tab.source_combo.setCurrentIndex(find_source_index(tab.source_combo, "Roster"))
        source_manager.add_source(make_source(name="Other"))
        assert tab.current_source is roster

    def test_turning_the_setting_off_relabels_the_combo_immediately(
            self, tab, source_manager, make_source, isolated_settings):
        source_manager.add_source(make_source(name="Roster"))
        assert source_labels(tab.source_combo)[1] == "Roster (editable)"

        isolated_settings.set(ACCESS_SETTING_KEY, False,
                              category=ACCESS_SETTING_CATEGORY)

        assert source_labels(tab.source_combo)[1] == "Roster"

    def test_relabelling_keeps_the_selected_source(
            self, tab, source_manager, make_source, isolated_settings):
        roster = make_source(name="Roster")
        source_manager.add_source(roster)
        tab.source_combo.setCurrentIndex(find_source_index(tab.source_combo, "Roster"))

        isolated_settings.set(ACCESS_SETTING_KEY, False,
                              category=ACCESS_SETTING_CATEGORY)

        assert tab.current_source is roster


@pytest.mark.gui
class TestComparisonPanelShowsTheAccessMode:
    """The three source selectors of the Comparison tab."""

    @pytest.fixture
    def panel(self, qapp, source_manager, isolated_settings):
        # See the note on the Operations tab fixture above.
        from ui.comparison_tab import SourcePanel
        widget = SourcePanel("Left Source", source_manager)
        yield widget
        widget.deleteLater()

    def test_the_mode_is_visible_in_the_list(self, panel, source_manager,
                                             make_source):
        source_manager.add_source(make_source(name="AD", readonly=True))
        assert source_labels(panel.source_combo)[1] == "AD (read-only)"

    def test_selecting_a_decorated_row_selects_the_right_source(
            self, panel, source_manager, make_source):
        roster = make_source(name="Roster", classes=[("6.A", 2)])
        source_manager.add_source(roster)
        panel.source_combo.setCurrentIndex(
            find_source_index(panel.source_combo, "Roster"))
        assert panel.current_source is roster

    def test_turning_the_setting_off_relabels_the_combo_immediately(
            self, panel, source_manager, make_source, isolated_settings):
        source_manager.add_source(make_source(name="Roster"))
        isolated_settings.set(ACCESS_SETTING_KEY, False,
                              category=ACCESS_SETTING_CATEGORY)
        assert source_labels(panel.source_combo)[1] == "Roster"


@pytest.mark.gui
class TestSettingsToggle:
    """The Settings -> General checkbox."""

    @pytest.fixture
    def settings_tab(self, qapp, isolated_settings):
        from ui.settings_tab import SettingsTab
        widget = SettingsTab()
        yield widget
        widget.deleteLater()

    def test_the_checkbox_starts_checked(self, settings_tab):
        assert settings_tab.show_source_access.isChecked() is True

    def test_unchecking_and_saving_stores_the_value(self, settings_tab,
                                                    isolated_settings, dialogs):
        settings_tab.show_source_access.setChecked(False)
        settings_tab.save_all_settings()
        assert isolated_settings.get_bool(ACCESS_SETTING_KEY, True,
                                          category=ACCESS_SETTING_CATEGORY) is False

    def test_saving_does_not_disturb_the_other_general_settings(
            self, settings_tab, isolated_settings, dialogs):
        settings_tab.warn_on_no_creds.setChecked(False)
        settings_tab.show_source_access.setChecked(False)
        settings_tab.save_all_settings()
        assert isolated_settings.get_bool("show_group_management_warning", True,
                                          category="general") is False

    def test_the_stored_value_is_reloaded_into_the_checkbox(self, settings_tab,
                                                            isolated_settings):
        isolated_settings.set(ACCESS_SETTING_KEY, False,
                              category=ACCESS_SETTING_CATEGORY)
        settings_tab._reload_general_settings()
        assert settings_tab.show_source_access.isChecked() is False

    def test_resetting_to_defaults_switches_the_suffix_back_on(
            self, settings_tab, isolated_settings, dialogs):
        isolated_settings.set(ACCESS_SETTING_KEY, False,
                              category=ACCESS_SETTING_CATEGORY)
        settings_tab._reload_general_settings()

        settings_tab.reset_to_defaults()

        assert settings_tab.show_source_access.isChecked() is True
