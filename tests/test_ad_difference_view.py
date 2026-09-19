"""
Tests for showing what Active Directory holds (Version 26, point 19 b).

Two windows use the same switch, the same colour and the same popup:
the editing window and the person table on the "3. Operations" tab.
"""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLineEdit, QTableWidgetItem

from models import ADGroup, ADStatus, Source
from services.ad_comparison import (
    AD_VALUES_METADATA_KEY, FieldDifference, compare_person_with_ad,
    store_comparison,
)
from ui.ad_difference_view import (
    DIFFERENCE_ROLE,
    OUTLINE_COLOR,
    SHOW_DIFFERENCES_CATEGORY,
    SHOW_DIFFERENCES_KEY,
    ADDifferenceDelegate,
    apply_tooltip_style,
    combined_difference,
    difference_tooltip,
    differences_shown,
    mark_item,
    mark_widget,
    person_differences,
    set_differences_shown,
    toggle_button_text,
)

pytestmark = pytest.mark.gui


@pytest.fixture
def discovered(make_person):
    """
    Factory: a person carrying an Active Directory snapshot.

    ``discovered(ad_email="stary@skola.cz")`` makes the directory disagree
    about the email.
    """
    def _build(**ad_overrides):
        person = make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                             ad_email="jan@skola.cz",
                             ad_display_name="Jan Novák")
        snapshot = {
            'givenName': "Jan",
            'sn': "Novák",
            'sAMAccountName': "novakjan",
            'mail': "jan@skola.cz",
            'displayName': "Jan Novák",
        }
        snapshot.update(ad_overrides)
        person.metadata[AD_VALUES_METADATA_KEY] = snapshot
        store_comparison(person, compare_person_with_ad(person, snapshot))
        return person
    return _build


class TestTheSwitch:
    """One setting behind both windows."""

    def test_the_outlines_are_on_by_default(self, isolated_settings):
        assert differences_shown(isolated_settings) is True

    def test_the_choice_is_stored(self, isolated_settings):
        set_differences_shown(False, isolated_settings)
        assert isolated_settings.get_bool(SHOW_DIFFERENCES_KEY, True,
                                          category=SHOW_DIFFERENCES_CATEGORY) is False

    def test_the_stored_choice_is_read_back(self, isolated_settings):
        set_differences_shown(False, isolated_settings)
        assert differences_shown(isolated_settings) is False

    def test_a_broken_settings_backend_leaves_the_outlines_on(self):
        class Exploding:
            def get_bool(self, *a, **k):
                raise RuntimeError("unreadable")

        assert differences_shown(Exploding()) is True

    def test_storing_into_a_broken_backend_does_not_raise(self):
        class Exploding:
            def set(self, *a, **k):
                raise RuntimeError("unwritable")

        set_differences_shown(True, Exploding())

    def test_the_button_caption_follows_the_state(self):
        assert toggle_button_text(True) != toggle_button_text(False)
        assert "Hide" in toggle_button_text(True)
        assert "Show" in toggle_button_text(False)


class TestTheTooltip:
    """The popup that explains one field."""

    @pytest.fixture
    def tooltip(self):
        return difference_tooltip(
            FieldDifference('ad_email', "novy@skola.cz", "stary@skola.cz"))

    def test_it_names_the_field(self, tooltip):
        assert "Email" in tooltip

    def test_it_shows_both_values(self, tooltip):
        assert "novy@skola.cz" in tooltip and "stary@skola.cz" in tooltip

    def test_it_says_which_side_is_which(self, tooltip):
        assert "In Active Directory:" in tooltip
        assert "In this application:" in tooltip

    def test_it_explains_that_the_value_is_a_snapshot(self, tooltip):
        # Point 19 b: a later change in AD is deliberately not detected.
        assert "Discover in AD" in tooltip

    def test_it_is_styled_rich_text_not_plain(self, tooltip):
        assert "<div" in tooltip and OUTLINE_COLOR.name() in tooltip

    def test_an_empty_directory_value_reads_as_not_set(self):
        tooltip = difference_tooltip(FieldDifference('ad_email', "a@b.cz", ""))
        assert "(not set)" in tooltip

    def test_markup_in_a_value_cannot_reach_the_popup(self):
        # A display name may legitimately contain '<' or '&'.
        tooltip = difference_tooltip(
            FieldDifference('ad_display_name', "<b>Jan</b> & Co", "Jan"))
        assert "<b>Jan</b> & Co" not in tooltip
        assert "&lt;b&gt;Jan&lt;/b&gt; &amp; Co" in tooltip


class TestMarkingAWidget:
    """The outline around an input field."""

    @pytest.fixture
    def line_edit(self, qapp):
        widget = QLineEdit()
        yield widget
        widget.deleteLater()

    def test_a_differing_field_gets_the_outline_colour(self, line_edit):
        mark_widget(line_edit, FieldDifference('ad_email', "a", "b"))
        assert OUTLINE_COLOR.name() in line_edit.styleSheet()

    def test_a_differing_field_gets_the_explanation(self, line_edit):
        mark_widget(line_edit, FieldDifference('ad_email', "a", "b"))
        assert "In Active Directory:" in line_edit.toolTip()

    def test_clearing_removes_both(self, line_edit):
        mark_widget(line_edit, FieldDifference('ad_email', "a", "b"))
        mark_widget(line_edit, None)
        assert line_edit.styleSheet() == "" and line_edit.toolTip() == ""

    def test_a_missing_widget_is_accepted(self):
        mark_widget(None, FieldDifference('ad_email', "a", "b"))

    def test_the_rule_names_the_widget_class(self, line_edit):
        mark_widget(line_edit, FieldDifference('ad_email', "a", "b"),
                    "QListWidget")
        assert line_edit.styleSheet().startswith("QListWidget")


class TestMarkingATableCell:
    """The flag a table cell carries."""

    def test_the_difference_is_stored_on_the_item(self, qapp):
        item = QTableWidgetItem("x")
        mark_item(item, FieldDifference('ad_email', "a", "b"))
        assert item.data(DIFFERENCE_ROLE)['field'] == 'ad_email'

    def test_the_cell_gets_the_explanation(self, qapp):
        item = QTableWidgetItem("x")
        mark_item(item, FieldDifference('ad_email', "a", "b"))
        assert "In Active Directory:" in item.toolTip()

    def test_clearing_removes_the_flag(self, qapp):
        item = QTableWidgetItem("x")
        mark_item(item, FieldDifference('ad_email', "a", "b"))
        mark_item(item, None)
        assert not item.data(DIFFERENCE_ROLE)

    def test_a_missing_item_is_accepted(self):
        mark_item(None, FieldDifference('ad_email', "a", "b"))


class TestCombinedDifference:
    """One cell can show two fields."""

    def test_two_differences_become_one(self):
        merged = combined_difference("Name", [
            FieldDifference('first_name', "Honza", "Jan"),
            FieldDifference('last_name', "Novák", "Novak"),
        ])
        assert merged.label == "Name"
        assert merged.app_value == "Honza Novák"
        assert merged.ad_value == "Jan Novak"

    def test_one_difference_is_returned_unchanged(self):
        only = FieldDifference('first_name', "Honza", "Jan")
        assert combined_difference("Name", [only]) is only

    def test_nothing_to_merge_gives_nothing(self):
        assert combined_difference("Name", [None, None]) is None


class TestTheDelegate:
    """Whether the outline would be painted at all."""

    def test_a_flagged_cell_is_painted_when_the_switch_is_on(self, qapp):
        delegate = ADDifferenceDelegate(lambda: True)
        item = QTableWidgetItem("x")
        mark_item(item, FieldDifference('ad_email', "a", "b"))
        index = _index_for(item)
        assert delegate._should_paint(index) is True

    def test_a_flagged_cell_is_skipped_when_the_switch_is_off(self, qapp):
        delegate = ADDifferenceDelegate(lambda: False)
        item = QTableWidgetItem("x")
        mark_item(item, FieldDifference('ad_email', "a", "b"))
        assert delegate._should_paint(_index_for(item)) is False

    def test_an_unflagged_cell_is_never_painted(self, qapp):
        delegate = ADDifferenceDelegate(lambda: True)
        assert delegate._should_paint(_index_for(QTableWidgetItem("x"))) is False

    def test_a_switch_that_raises_does_not_break_painting(self, qapp):
        def boom():
            raise RuntimeError("no settings")

        delegate = ADDifferenceDelegate(boom)
        item = QTableWidgetItem("x")
        mark_item(item, FieldDifference('ad_email', "a", "b"))
        assert delegate._should_paint(_index_for(item)) is False


#: Keeps the tables behind the indices below alive.  A QModelIndex does not
#: own its model, so letting the table be collected leaves a dangling pointer
#: and the next read of the index crashes the interpreter.
_INDEX_TABLES = []


def _index_for(item):
    """Put an item in a table and return its model index."""
    from PyQt6.QtWidgets import QTableWidget

    table = QTableWidget(1, 1)
    table.setItem(0, 0, item)
    _INDEX_TABLES.append(table)
    return table.model().index(0, 0)


class TestTooltipStyle:
    """The popup chrome follows the application."""

    def test_the_style_is_added_to_the_application(self, qapp):
        before = qapp.styleSheet()
        try:
            qapp.setStyleSheet("QPushButton { color: red; }")
            apply_tooltip_style(qapp)
            assert "QToolTip" in qapp.styleSheet()
            assert "QPushButton { color: red; }" in qapp.styleSheet()
        finally:
            qapp.setStyleSheet(before)

    def test_applying_it_twice_does_not_duplicate_it(self, qapp):
        before = qapp.styleSheet()
        try:
            qapp.setStyleSheet("")
            apply_tooltip_style(qapp)
            apply_tooltip_style(qapp)
            assert qapp.styleSheet().count("QToolTip") == 1
        finally:
            qapp.setStyleSheet(before)


class TestThePersonTable:
    """The outlines in the person table on the Operations tab."""

    @pytest.fixture(autouse=True)
    def _settings(self, isolated_settings, accept_dialogs):
        return isolated_settings

    @pytest.fixture
    def widget(self, qapp, source_manager):
        from operations.ad_management import ADManagementWidget
        made = ADManagementWidget(source_manager)
        yield made
        made.close()

    def source_with(self, make_class, person):
        source = Source(name="Source", source_type="manual")
        source.add_class(make_class("6.A", persons=[person]))
        return source

    def test_a_differing_cell_is_flagged(self, widget, make_class, discovered):
        widget.visible_columns = ["Email"]
        widget.set_source(self.source_with(make_class,
                                           discovered(mail="stary@skola.cz")))

        assert widget.person_table.item(0, 0).data(DIFFERENCE_ROLE)

    def test_an_identical_cell_is_not_flagged(self, widget, make_class,
                                              discovered):
        widget.visible_columns = ["Email"]
        widget.set_source(self.source_with(make_class, discovered()))

        assert not widget.person_table.item(0, 0).data(DIFFERENCE_ROLE)

    def test_a_person_who_was_never_discovered_is_not_flagged(self, widget,
                                                              make_class,
                                                              make_person):
        widget.visible_columns = ["Email"]
        widget.set_source(self.source_with(make_class,
                                           make_person(ad_email="a@b.cz")))

        assert not widget.person_table.item(0, 0).data(DIFFERENCE_ROLE)

    def test_the_flagged_cell_explains_itself(self, widget, make_class,
                                              discovered):
        widget.visible_columns = ["Email"]
        widget.set_source(self.source_with(make_class,
                                           discovered(mail="stary@skola.cz")))

        assert "stary@skola.cz" in widget.person_table.item(0, 0).toolTip()

    def test_a_differing_surname_flags_the_name_cell(self, widget, make_class,
                                                     discovered):
        widget.visible_columns = ["Name"]
        widget.set_source(self.source_with(make_class, discovered(sn="Novak")))

        flagged = widget.person_table.item(0, 0).data(DIFFERENCE_ROLE)
        # One field differs, so the cell names that field rather than the
        # column - it is the more useful of the two.
        assert flagged and flagged['label'] == "Last name"

    def test_the_name_cell_merges_both_name_fields(self, widget, make_class,
                                                   discovered):
        widget.visible_columns = ["Name"]
        widget.set_source(self.source_with(
            make_class, discovered(givenName="Honza", sn="Novak")))

        flagged = widget.person_table.item(0, 0).data(DIFFERENCE_ROLE)
        assert flagged['label'] == "Name"
        assert flagged['ad_value'] == "Honza Novak"
        assert flagged['app_value'] == "Jan Novák"

    def test_a_column_active_directory_does_not_hold_is_never_flagged(
            self, widget, make_class, discovered):
        # The directory never gives a password back, so there is nothing to
        # compare it with.
        widget.visible_columns = ["Password"]
        widget.set_source(self.source_with(make_class,
                                           discovered(mail="stary@skola.cz")))

        assert not widget.person_table.item(0, 0).data(DIFFERENCE_ROLE)

    def test_the_button_starts_in_the_stored_state(self, qapp, source_manager,
                                                   isolated_settings):
        from operations.ad_management import ADManagementWidget

        set_differences_shown(False, isolated_settings)
        made = ADManagementWidget(source_manager)
        try:
            assert made.show_ad_diff_button.isChecked() is False
        finally:
            made.close()

    def test_toggling_the_button_stores_the_choice(self, widget,
                                                   isolated_settings):
        widget.show_ad_diff_button.setChecked(False)
        assert differences_shown(isolated_settings) is False

    def test_toggling_the_button_changes_its_caption(self, widget):
        widget.show_ad_diff_button.setChecked(True)
        shown = widget.show_ad_diff_button.text()
        widget.show_ad_diff_button.setChecked(False)
        assert widget.show_ad_diff_button.text() != shown

    def test_the_delegate_follows_the_button(self, widget):
        widget.show_ad_diff_button.setChecked(True)
        assert widget.ad_differences_visible() is True
        widget.show_ad_diff_button.setChecked(False)
        assert widget.ad_differences_visible() is False

    def test_hiding_the_outlines_keeps_the_flag_on_the_cell(self, widget,
                                                            make_class,
                                                            discovered):
        # Only the painting is switched off, so switching back on needs no
        # rebuild of the table.
        widget.visible_columns = ["Email"]
        widget.set_source(self.source_with(make_class,
                                           discovered(mail="stary@skola.cz")))
        widget.show_ad_diff_button.setChecked(False)

        assert widget.person_table.item(0, 0).data(DIFFERENCE_ROLE)


class TestTheEditingWindow:
    """The outlines in the person editor."""

    @pytest.fixture(autouse=True)
    def _settings(self, isolated_settings, accept_dialogs):
        return isolated_settings

    @pytest.fixture
    def editor(self, qapp, discovered):
        from ui.property_editor import PropertyEditorDialog

        def _open(person=None):
            dialog = PropertyEditorDialog(person or discovered(), set())
            return dialog
        return _open

    def test_a_differing_field_is_outlined(self, editor, discovered):
        dialog = editor(discovered(mail="stary@skola.cz"))
        try:
            assert OUTLINE_COLOR.name() in dialog.email_input.styleSheet()
        finally:
            dialog.deleteLater()

    def test_an_identical_field_is_not_outlined(self, editor, discovered):
        dialog = editor(discovered(mail="stary@skola.cz"))
        try:
            assert dialog.username_input.styleSheet() == ""
        finally:
            dialog.deleteLater()

    def test_the_outlined_field_explains_itself(self, editor, discovered):
        dialog = editor(discovered(mail="stary@skola.cz"))
        try:
            assert "stary@skola.cz" in dialog.email_input.toolTip()
        finally:
            dialog.deleteLater()

    def test_turning_the_outlines_off_clears_them(self, editor, discovered):
        dialog = editor(discovered(mail="stary@skola.cz"))
        try:
            dialog.show_ad_diff_button.setChecked(False)
            assert dialog.email_input.styleSheet() == ""
        finally:
            dialog.deleteLater()

    def test_the_choice_survives_into_the_next_person(self, editor, discovered,
                                                      isolated_settings):
        first = editor(discovered(mail="stary@skola.cz"))
        try:
            first.show_ad_diff_button.setChecked(False)
        finally:
            first.deleteLater()

        second = editor(discovered(mail="jiny@skola.cz"))
        try:
            assert second.show_ad_diff_button.isChecked() is False
            assert second.email_input.styleSheet() == ""
        finally:
            second.deleteLater()

    def test_the_hint_counts_the_differing_fields(self, editor, discovered):
        dialog = editor(discovered(mail="stary@skola.cz",
                                   displayName="Jan N."))
        try:
            assert "2 fields differ" in dialog.ad_diff_hint.text()
        finally:
            dialog.deleteLater()

    def test_the_hint_says_when_everything_matches(self, editor, discovered):
        dialog = editor(discovered())
        try:
            assert "match" in dialog.ad_diff_hint.text()
        finally:
            dialog.deleteLater()

    def test_the_hint_says_when_the_person_was_never_discovered(self, editor,
                                                                make_person):
        dialog = editor(make_person())
        try:
            assert "not been discovered" in dialog.ad_diff_hint.text()
        finally:
            dialog.deleteLater()

    def test_a_differing_group_list_is_outlined(self, editor, discovered):
        person = discovered()
        person.add_to_group(ADGroup(name="Zaci", dn="CN=Zaci,DC=skola,DC=cz"))
        store_comparison(person, compare_person_with_ad(person))

        dialog = editor(person)
        try:
            assert OUTLINE_COLOR.name() in dialog.current_groups_list.styleSheet()
        finally:
            dialog.deleteLater()

    def test_the_group_rows_keep_their_own_tooltip_as_well(self, editor,
                                                           discovered):
        person = discovered()
        person.add_to_group(ADGroup(name="Zaci", dn="CN=Zaci,DC=skola,DC=cz"))
        store_comparison(person, compare_person_with_ad(person))

        dialog = editor(person)
        try:
            tooltip = dialog.current_groups_list.item(0).toolTip()
            assert "CN=Zaci,DC=skola,DC=cz" in tooltip
            assert "In Active Directory:" in tooltip
        finally:
            dialog.deleteLater()

    def test_toggling_twice_does_not_pile_up_the_group_tooltip(self, editor,
                                                               discovered):
        person = discovered()
        person.add_to_group(ADGroup(name="Zaci", dn="CN=Zaci,DC=skola,DC=cz"))
        store_comparison(person, compare_person_with_ad(person))

        dialog = editor(person)
        try:
            dialog.show_ad_diff_button.setChecked(False)
            dialog.show_ad_diff_button.setChecked(True)
            tooltip = dialog.current_groups_list.item(0).toolTip()
            assert tooltip.count("In Active Directory:") == 1
        finally:
            dialog.deleteLater()

    def test_the_password_field_is_never_outlined(self, editor, discovered):
        # Active Directory never gives a password back.
        dialog = editor(discovered(mail="stary@skola.cz"))
        try:
            assert dialog.password_display.styleSheet() == ""
        finally:
            dialog.deleteLater()
