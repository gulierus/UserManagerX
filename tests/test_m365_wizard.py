"""
Tests for the two-step Microsoft 365 import (Version 26, point 21 a).

Step 1 chooses the groups; step 2 names the classes.  The second step is what
makes the source usable on the "2. Comparison and Sync" tab, which matches
classes by name - a group called "Trida-6.A-2020" would match nothing.
"""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog

from models_m365 import M365Group, M365Member, M365Team
from ui.m365_import_wizard import (
    DEFAULT_CLASS_TEMPLATE, ClassNamingStep, GroupSelectionStep,
    M365ImportWizard,
)

pytestmark = pytest.mark.gui


def member(name="Jan Novák", object_id="u1", **overrides):
    values = dict(object_id=object_id, display_name=name,
                  user_principal_name=f"{object_id}@skola.cz")
    values.update(overrides)
    return M365Member(**values)


def group(name="Trida-6.A", object_id="g1", members=None, owners=None,
          loaded=True, **overrides):
    made = M365Group(object_id=object_id, display_name=name,
                     mail_nickname=name.lower().replace("-", "").replace(".", ""),
                     members=list(members or []), owners=list(owners or []),
                     **overrides)
    made.members_loaded = loaded
    return made


@pytest.fixture
def groups():
    return [
        group("Trida-6.A", "g1", members=[member("Jan Novák", "u1"),
                                          member("Eva Malá", "u2")],
              owners=[member("Petr Uč", "t1")]),
        group("Trida-7.B", "g2", members=[member("Ota Dvořák", "u3")]),
        group("Ucitele", "g3", members=[member("Petr Uč", "t1")]),
    ]


@pytest.fixture
def step1(qapp, groups):
    widget = GroupSelectionStep(groups)
    yield widget
    widget.deleteLater()


def tick(step, row):
    """Tick one row of step 1."""
    step.group_list.item(row).setCheckState(Qt.CheckState.Checked)


class TestStepOneListing:
    """What the left half shows."""

    def test_every_group_is_listed(self, step1, groups):
        assert step1.group_list.count() == len(groups)

    def test_nothing_is_selected_to_begin_with(self, step1):
        assert step1.selected_groups() == []

    def test_a_row_names_the_group_and_its_kind(self, step1, groups):
        text = step1.group_list.item(0).text()
        assert "Trida-6.A" in text
        assert groups[0].kind in text
        assert "2 member(s)" in text

    def test_a_group_whose_members_were_not_read_says_so(self, qapp):
        # An empty list means "not read", and "0 members" would be a lie the
        # user would act on.
        step = GroupSelectionStep([group("X", "g9", loaded=False)])
        assert "not read" in step.group_list.item(0).text()
        step.deleteLater()

    def test_a_team_is_labelled_as_one(self, qapp):
        step = GroupSelectionStep([M365Team(object_id="t", display_name="6.A")])
        assert "Team" in step.group_list.item(0).text()
        step.deleteLater()


class TestStepOneSearching:
    """The search box."""

    def test_searching_hides_what_does_not_match(self, step1):
        step1.search_input.setText("Trida")
        hidden = [step1.group_list.item(i).isHidden() for i in range(3)]
        assert hidden == [False, False, True]

    def test_the_search_is_case_insensitive(self, step1):
        step1.search_input.setText("trida-6")
        assert step1.group_list.item(0).isHidden() is False

    def test_clearing_the_search_shows_everything_again(self, step1):
        step1.search_input.setText("nothing matches this")
        step1.search_input.setText("")
        assert not any(step1.group_list.item(i).isHidden() for i in range(3))

    def test_the_count_reports_what_is_shown(self, step1):
        step1.search_input.setText("Trida")
        assert "2 of 3 shown" in step1.count_label.text()


class TestStepOneSelecting:
    """Ticking, and the two buttons."""

    def test_ticking_a_row_selects_that_group(self, step1):
        tick(step1, 0)
        assert [g.object_id for g in step1.selected_groups()] == ["g1"]

    def test_select_all_shown_ticks_only_the_visible_rows(self, step1):
        step1.search_input.setText("Trida")
        step1._set_all_shown(True)
        assert [g.object_id for g in step1.selected_groups()] == ["g1", "g2"]

    def test_clear_selection_unticks_hidden_rows_too(self, step1):
        # A user who clears the selection means all of it; leaving invisible
        # ticks behind would carry hidden choices into step 2.
        step1._set_all_shown(True)
        step1.search_input.setText("Trida")
        step1._clear_selection()
        step1.search_input.setText("")
        assert step1.selected_groups() == []

    def test_the_count_reports_the_selection(self, step1):
        tick(step1, 0)
        assert "1 selected" in step1.count_label.text()


class TestStepOneDetails:
    """What the right half shows for the clicked group."""

    def test_clicking_a_group_lists_its_people(self, step1):
        step1.group_list.setCurrentRow(0)
        texts = [step1.people_list.item(i).text()
                 for i in range(step1.people_list.count())]
        assert any("Jan Novák" in text for text in texts)
        assert any("Petr Uč" in text for text in texts)

    def test_owners_are_marked_as_owners(self, step1):
        step1.group_list.setCurrentRow(0)
        owner_row = step1.people_list.item(0)
        assert owner_row.toolTip() == "Owner"

    def test_the_details_name_the_group(self, step1):
        step1.group_list.setCurrentRow(0)
        assert "Trida-6.A" in step1.details_text.toHtml()

    def test_an_unread_group_says_so_instead_of_showing_nobody(self, qapp):
        step = GroupSelectionStep([group("X", "g9", loaded=False)])
        step.group_list.setCurrentRow(0)
        assert "have not been read" in step.people_list.item(0).text()
        step.deleteLater()

    def test_an_empty_group_says_it_is_empty(self, qapp):
        step = GroupSelectionStep([group("X", "g9")])
        step.group_list.setCurrentRow(0)
        assert "no owners and no members" in step.people_list.item(0).text()
        step.deleteLater()


@pytest.fixture
def step2(qapp, groups):
    widget = ClassNamingStep()
    widget.set_groups(groups[:2])
    yield widget
    widget.deleteLater()


class TestStepTwoNaming:
    """Turning a group name into a class name."""

    def test_one_row_per_selected_group(self, step2):
        assert step2.table.rowCount() == 2

    def test_the_default_template_strips_the_school_prefix(self, step2):
        assert [s.class_name for s in step2.result()] == ["6.A", "7.B"]

    def test_the_template_can_be_changed_and_reapplied(self, step2):
        step2.template_input.setText("{display_name}")
        step2.apply_template(only_empty=False)
        assert [s.class_name for s in step2.result()] == ["Trida-6.A", "Trida-7.B"]

    def test_a_name_can_be_typed_by_hand(self, step2):
        step2.table.item(0, 2).setText("Sixth A")
        assert step2.result()[0].class_name == "Sixth A"

    def test_the_preview_shows_what_the_template_produces(self, step2):
        step2.template_input.setText("{display_name|after:Trida-}")
        assert "6.A" in step2.preview_label.text()

    def test_a_broken_template_previews_the_reason(self, step2):
        step2.template_input.setText("{nope}")
        assert "⚠" in step2.preview_label.text()

    def test_a_broken_template_leaves_the_names_empty_not_wrong(self, step2):
        # Writing an error message into a class name would create a class
        # called "⚠ Unknown placeholder".
        step2.template_input.setText("{nope}")
        step2.apply_template(only_empty=False)
        assert [s.class_name for s in step2.result()] == ["", ""]

    def test_apply_to_all_overwrites_a_hand_typed_name(self, step2):
        # The button says "apply to all", so it has to mean all.
        step2.table.item(0, 2).setText("Sixth A")
        step2.apply_template(only_empty=False)
        assert step2.result()[0].class_name == "6.A"

    def test_the_people_count_is_shown(self, step2):
        assert step2.table.item(0, 1).text() == "2"

    def test_a_group_with_unread_members_is_marked(self, qapp):
        step = ClassNamingStep()
        step.set_groups([group("X", "g9", loaded=False)])
        assert step.table.item(0, 1).text() == "?"
        step.deleteLater()


class TestStepTwoOwners:
    """Whether the owners become pupils."""

    def test_owners_are_excluded_by_default(self, step2):
        # In a school group the owner is the teacher.
        assert step2.table.item(0, 1).text() == "2"

    def test_including_the_owners_raises_the_count(self, step2):
        step2.include_owners_check.setChecked(True)
        assert step2.table.item(0, 1).text() == "3"

    def test_the_choice_reaches_the_result(self, step2):
        step2.include_owners_check.setChecked(True)
        assert all(s.include_owners for s in step2.result())


class TestStepTwoValidation:
    """What must be true before the wizard can finish."""

    def test_a_complete_step_has_no_problems(self, step2):
        assert step2.problems() == []

    def test_a_missing_name_is_a_problem(self, step2):
        step2.table.item(0, 2).setText("")
        assert any("no class name" in problem for problem in step2.problems())

    def test_two_classes_with_one_name_is_a_problem(self, step2):
        # They would be indistinguishable everywhere a class is looked up by
        # name.
        step2.table.item(1, 2).setText("6.A")
        assert any("same name" in problem for problem in step2.problems())

    def test_the_duplicate_check_ignores_case(self, step2):
        step2.table.item(1, 2).setText("6.a")
        assert any("same name" in problem for problem in step2.problems())

    def test_no_groups_at_all_is_a_problem(self, qapp):
        step = ClassNamingStep()
        assert step.problems() == ["No group was selected in step 1."]
        step.deleteLater()

    def test_the_problems_are_shown_under_the_table(self, step2):
        step2.table.item(0, 2).setText("")
        assert step2.problem_label.text() != ""


@pytest.fixture
def wizard(qapp, groups):
    made = M365ImportWizard(groups)
    yield made
    made.deleteLater()


class TestTheWizard:
    """Moving between the two steps."""

    def test_it_starts_on_step_one(self, wizard):
        assert wizard.steps.currentIndex() == 0
        assert wizard.back_button.isEnabled() is False

    def test_it_refuses_to_advance_with_nothing_selected(self, wizard):
        wizard.go_next()
        assert wizard.steps.currentIndex() == 0
        assert "at least one" in wizard.message_label.text()

    def test_selecting_a_group_lets_it_advance(self, wizard):
        tick(wizard.selection_step, 0)
        wizard.go_next()
        assert wizard.steps.currentIndex() == 1
        assert wizard.next_button.text() == "Finish"

    def test_going_back_returns_to_step_one(self, wizard):
        tick(wizard.selection_step, 0)
        wizard.go_next()
        wizard.go_back()
        assert wizard.steps.currentIndex() == 0

    def test_a_typed_name_survives_a_trip_back_to_step_one(self, wizard):
        # Losing it would punish the user for checking something.
        tick(wizard.selection_step, 0)
        wizard.go_next()
        wizard.naming_step.table.item(0, 2).setText("Sixth A")
        wizard.go_back()
        wizard.go_next()
        assert wizard.naming_step.result()[0].class_name == "Sixth A"

    def test_adding_a_group_keeps_the_names_of_the_others(self, wizard):
        tick(wizard.selection_step, 0)
        wizard.go_next()
        wizard.naming_step.table.item(0, 2).setText("Sixth A")
        wizard.go_back()
        tick(wizard.selection_step, 1)
        wizard.go_next()
        names = [s.class_name for s in wizard.naming_step.result()]
        assert names[0] == "Sixth A" and names[1] != ""

    def test_finishing_with_a_problem_is_refused(self, wizard):
        tick(wizard.selection_step, 0)
        wizard.go_next()
        wizard.naming_step.table.item(0, 2).setText("")
        wizard.go_next()
        assert wizard.result() != QDialog.DialogCode.Accepted
        assert wizard.message_label.text() != ""

    def test_finishing_a_complete_step_accepts_the_dialog(self, wizard):
        tick(wizard.selection_step, 0)
        wizard.go_next()
        wizard.go_next()
        assert wizard.result() == QDialog.DialogCode.Accepted

    def test_the_result_carries_the_groups_and_their_names(self, wizard):
        tick(wizard.selection_step, 0)
        tick(wizard.selection_step, 1)
        wizard.go_next()
        selections = wizard.result_selections()
        assert [s.group.object_id for s in selections] == ["g1", "g2"]
        assert [s.class_name for s in selections] == ["6.A", "7.B"]

    def test_the_default_template_is_the_documented_one(self):
        assert DEFAULT_CLASS_TEMPLATE == "{display_name|after:Trida-}"
