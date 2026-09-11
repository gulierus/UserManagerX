"""
Unit tests for :mod:`models` - Person dirty tracking, group membership,
string normalization, Class/Source containers, SourceManager signals and the
two merge strategies.

Every test states its expectation in the name; tests marked ``bug`` + ``xfail``
assert the *correct* behaviour and document a defect in the production code.
"""

import copy
import logging

import pytest

from models import (
    ADGroup,
    ADStatus,
    Class,
    Person,
    Source,
    SourceManager,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def group(name="Students", dn=None, **kwargs):
    """Build an ADGroup; the DN defaults to ``CN=<name>,DC=school,DC=local``."""
    return ADGroup(name=name, dn=dn or f"CN={name},DC=school,DC=local", **kwargs)


def collect(signal):
    """Connect a recorder to *signal* and return the list of argument tuples."""
    received = []
    signal.connect(lambda *args: received.append(args))
    return received


def synced_person(**kwargs):
    """A person that AD already knows about (has a DN and SYNCED status)."""
    kwargs.setdefault("ad_dn", "CN=Jan Novak,OU=Students,DC=school,DC=local")
    kwargs.setdefault("ad_status", ADStatus.SYNCED)
    return Person("Jan", "Novák", "6.A", **kwargs)


def source_with(name, *class_specs):
    """``source_with("L", ("6.A", [p1, p2]), ("6.B", []))``."""
    source = Source(name=name, source_type="manual")
    for class_name, persons in class_specs:
        source.add_class(Class(name=class_name, persons=list(persons)))
    return source


# ---------------------------------------------------------------------------
# Person.normalize_string
# ---------------------------------------------------------------------------

class TestNormalizeString:
    """Diacritics stripping and lower-casing used for all name comparisons."""

    @pytest.mark.parametrize("raw, expected", [
        ("Novák", "novak"),
        ("Ďáblík", "dablik"),
        ("ŽLUŤOUČKÝ", "zlutoucky"),
        ("Åsa Öberg", "asa oberg"),
        ("Mérküry", "merkury"),
        ("Łódź", "łodz"),          # stroke is not a combining mark -> kept
        ("Straße", "straße"),       # eszett has no NFKD decomposition
        ("ﬁle", "file"),            # NFKD splits the ligature
        ("①", "1"),                 # NFKD folds compatibility digits
        ("already lower", "already lower"),
    ])
    def test_normalize_string_strips_diacritics_and_lowercases(self, raw, expected):
        """Combining marks are removed, compatibility forms folded, case lowered."""
        assert Person.normalize_string(raw) == expected

    @pytest.mark.parametrize("falsy", ["", None, 0, [], {}])
    def test_normalize_string_returns_empty_string_for_falsy_input(self, falsy):
        """Anything falsy (including None) normalizes to the empty string."""
        assert Person.normalize_string(falsy) == ""

    @pytest.mark.parametrize("wrong", [123, 4.5, ["Jan"], {"a": 1}, object()])
    def test_normalize_string_raises_type_error_for_truthy_non_strings(self, wrong):
        """A truthy non-string reaches unicodedata and raises TypeError."""
        with pytest.raises(TypeError):
            Person.normalize_string(wrong)

    def test_normalize_string_is_idempotent(self):
        """Normalizing an already normalized value changes nothing."""
        once = Person.normalize_string("Přemysl Ďurčovič")
        assert Person.normalize_string(once) == once == "premysl durcovic"

    def test_normalize_string_preserves_surrounding_whitespace(self):
        """Normalization does not trim - callers must strip themselves."""
        assert Person.normalize_string("  Jan  ") == "  jan  "


# ---------------------------------------------------------------------------
# Person identity helpers
# ---------------------------------------------------------------------------

class TestPersonIdentity:
    """get_normalized_name / is_same_person / matches_search."""

    def test_get_normalized_name_normalizes_names_but_keeps_class_verbatim(self, make_person):
        """First and last name are folded; the class name is used as typed."""
        person = make_person("Jan", "Ďurčovič", "6.A")
        assert person.get_normalized_name() == ("jan", "durcovic", "6.A")

    def test_is_same_person_ignores_case_and_diacritics_of_the_name(self, make_person):
        """Two spellings of the same student in the same class are one person."""
        left = make_person("Jan", "Novák", "6.A")
        right = make_person("JAN", "NOVAK", "6.A")
        assert left.is_same_person(right) and right.is_same_person(left)

    def test_is_same_person_is_case_sensitive_on_the_class_name(self, make_person):
        """Documented deviation: the class part of the key is *not* normalized."""
        assert not make_person("Jan", "Novák", "6.A").is_same_person(
            make_person("Jan", "Novák", "6.a")
        )

    @pytest.mark.parametrize("query, expected", [
        ("novak", True),        # diacritics-insensitive
        ("NOVÁK", True),        # case-insensitive
        ("nov", True),          # prefix
        ("jan nov", True),      # spans "first last"
        ("novák jan", False),   # reversed order is not matched
        ("6.A", False),         # the class is not searched
        ("svoboda", False),
        ("", True),             # empty query matches everybody
    ])
    def test_matches_search_covers_first_last_and_full_name(self, make_person, query, expected):
        """Search folds diacritics and matches first, last or "first last"."""
        person = make_person("Jan", "Novák", "6.A", ad_username="novakjan")
        assert person.matches_search(query) is expected

    def test_matches_search_does_not_look_at_the_ad_username(self, make_person):
        """Only the human name is searched, never AD attributes."""
        assert make_person("Jan", "Novák", "6.A",
                           ad_username="xnovak99").matches_search("xnovak99") is False


# ---------------------------------------------------------------------------
# Person dirty tracking
# ---------------------------------------------------------------------------

DIRTY_FIELDS = [
    ("first_name", "Petr"),
    ("last_name", "Svoboda"),
    ("class_name", "7.B"),
    ("ad_username", "svobopet"),
    ("ad_password", "S3cret!"),
    ("ad_display_name", "Petr Svoboda"),
    ("ad_email", "petr@school.local"),
    ("ad_description", "trida 7.B"),
    ("ad_ou_path", "OU=Students,DC=school,DC=local"),
    ("home_directory", r"\\srv\home\svobopet"),
    ("home_drive", "H:"),
    ("password_must_change", True),
    ("password_cannot_change", True),
    ("password_never_expires", True),
    ("account_enabled", True),
]


class TestPersonDirtyTracking:
    """Property setters, _mark_dirty, get_changes and reset_dirty."""

    def test_freshly_constructed_person_is_clean(self, make_person):
        """Construction happens with tracking disabled, so nothing is dirty."""
        person = make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                             account_enabled=True)
        assert person.is_dirty() is False
        assert person.get_dirty_fields() == set()
        assert person.get_changes() == {}

    @pytest.mark.parametrize("field_name, new_value", DIRTY_FIELDS)
    def test_setter_marks_exactly_that_field_dirty(self, make_person, field_name, new_value):
        """Each tracked property records itself - and only itself - as changed."""
        person = make_person("Jan", "Novák", "6.A")
        setattr(person, field_name, new_value)
        assert person.get_dirty_fields() == {field_name}
        assert person.get_changes() == {field_name: new_value}
        assert getattr(person, field_name) == new_value

    @pytest.mark.parametrize("field_name, new_value", DIRTY_FIELDS)
    def test_writing_the_identical_value_leaves_the_person_clean(self, make_person,
                                                                 field_name, new_value):
        """Re-assigning the value a field already holds is not a change."""
        person = make_person("Jan", "Novák", "6.A")
        setattr(person, field_name, new_value)
        person.reset_dirty()
        setattr(person, field_name, new_value)
        assert person.is_dirty() is False

    def test_reverting_a_field_to_its_original_value_clears_the_dirty_flag(self, make_person):
        """Dirtiness is computed against the captured original, not "was touched"."""
        person = make_person("Jan", "Novák", "6.A")
        person.first_name = "Petr"
        assert person.get_dirty_fields() == {"first_name"}
        person.first_name = "Jan"
        assert person.is_dirty() is False
        assert person.get_changes() == {}

    def test_reverting_one_field_keeps_other_fields_dirty(self, make_person):
        """Only the reverted field is discarded from the dirty set."""
        person = make_person("Jan", "Novák", "6.A")
        person.first_name = "Petr"
        person.ad_email = "petr@school.local"
        person.first_name = "Jan"
        assert person.get_dirty_fields() == {"ad_email"}

    def test_changing_a_synced_person_moves_it_to_update_pending(self):
        """A synced account with local edits must be re-pushed to AD."""
        person = synced_person()
        person.ad_email = "jan@school.local"
        assert person.ad_status == ADStatus.UPDATE_PENDING

    def test_reverting_the_last_change_restores_synced_status(self):
        """Once nothing differs any more the account is synced again."""
        person = synced_person()
        person.ad_email = "jan@school.local"
        person.ad_email = None
        assert person.get_dirty_fields() == set()
        assert person.ad_status == ADStatus.SYNCED

    @pytest.mark.parametrize("status", [
        ADStatus.UNKNOWN, ADStatus.NOT_IN_AD, ADStatus.CREATE_PENDING,
        ADStatus.AMBIGUOUS,
    ])
    def test_editing_does_not_touch_a_status_other_than_synced(self, status):
        """Only SYNCED is promoted to UPDATE_PENDING; other states are kept."""
        person = Person("Jan", "Novák", "6.A", ad_status=status)
        person.first_name = "Petr"
        assert person.is_dirty() is True
        assert person.ad_status is status

    def test_get_dirty_fields_returns_a_defensive_copy(self, make_person):
        """Mutating the returned set must not corrupt the internal state."""
        person = make_person("Jan", "Novák", "6.A")
        person.first_name = "Petr"
        fields = person.get_dirty_fields()
        fields.add("ad_email")
        fields.discard("first_name")
        assert person.get_dirty_fields() == {"first_name"}

    def test_get_changes_reports_current_values_of_every_dirty_field(self, make_person):
        """The dict maps field name -> the value that should be written to AD."""
        person = make_person("Jan", "Novák", "6.A")
        person.ad_username = "novakjan"
        person.account_enabled = True
        person.password_must_change = True
        assert person.get_changes() == {
            "ad_username": "novakjan",
            "account_enabled": True,
            "password_must_change": True,
        }

    def test_reset_dirty_clears_changes_and_stamps_the_sync(self, make_person):
        """After a successful write the person is clean, synced and timestamped."""
        person = make_person("Jan", "Novák", "6.A",
                             ad_dn="CN=Jan,DC=school", ad_status=ADStatus.UPDATE_PENDING)
        person.ad_email = "jan@school.local"
        person.reset_dirty()
        assert person.is_dirty() is False
        assert person.get_changes() == {}
        assert person.ad_status == ADStatus.SYNCED
        assert person.ad_last_sync is not None

    def test_reset_dirty_without_a_dn_does_not_claim_the_person_is_synced(self, make_person):
        """A person that has no DN yet was never written, so the status stays."""
        person = make_person("Jan", "Novák", "6.A", ad_status=ADStatus.CREATE_PENDING)
        person.ad_email = "jan@school.local"
        person.reset_dirty()
        assert person.is_dirty() is False
        assert person.ad_status == ADStatus.CREATE_PENDING
        assert person.ad_last_sync is None

    def test_reset_dirty_rebaselines_the_original_values(self, make_person):
        """The new value becomes the baseline - going back to the old one is a change."""
        person = make_person("Jan", "Novák", "6.A")
        person.first_name = "Petr"
        person.reset_dirty()
        person.first_name = "Jan"
        assert person.get_changes() == {"first_name": "Jan"}

    def test_reset_dirty_is_idempotent(self, make_person):
        """Calling it twice in a row changes nothing the second time."""
        person = make_person("Jan", "Novák", "6.A", ad_dn="CN=Jan,DC=school")
        person.first_name = "Petr"
        person.reset_dirty()
        first_sync = person.ad_last_sync
        person.reset_dirty()
        assert person.is_dirty() is False
        assert person.ad_last_sync >= first_sync

    def test_ad_dn_is_deliberately_excluded_from_dirty_tracking(self, make_person):
        """The DN is assigned by AD, so writing it is not a pending local change."""
        person = make_person("Jan", "Novák", "6.A")
        person.ad_dn = "CN=Jan Novak,OU=Students,DC=school,DC=local"
        assert person.ad_dn == "CN=Jan Novak,OU=Students,DC=school,DC=local"
        assert person.is_dirty() is False
        person.mark_field_dirty("ad_dn")
        assert person.is_dirty() is False

    def test_mark_field_dirty_ignores_unknown_field_names(self, make_person):
        """An unknown name is dropped silently instead of raising KeyError."""
        person = make_person("Jan", "Novák", "6.A")
        person.mark_field_dirty("does_not_exist")
        person.mark_field_dirty("")
        assert person.is_dirty() is False

    def test_mark_field_dirty_recomputes_state_after_a_backdoor_write(self, make_person):
        """Bypassing the property and then notifying the model marks it dirty."""
        person = make_person("Jan", "Novák", "6.A")
        person._first_name = "Petr"
        assert person.is_dirty() is False
        person.mark_field_dirty("first_name")
        assert person.get_changes() == {"first_name": "Petr"}

    def test_mark_field_dirty_on_an_unchanged_field_keeps_it_clean(self, make_person):
        """It recomputes the diff rather than blindly flagging the field."""
        person = make_person("Jan", "Novák", "6.A")
        person.mark_field_dirty("first_name")
        assert person.is_dirty() is False

    def test_unicode_names_survive_the_setters_untouched(self, make_person):
        """Normalization is only for comparison - stored values keep diacritics."""
        person = make_person("Jan", "Novák", "6.A")
        person.first_name = "Přemysl"
        person.last_name = "Ďurčovič"
        assert (person.first_name, person.last_name) == ("Přemysl", "Ďurčovič")
        assert person.get_normalized_name() == ("premysl", "durcovic", "6.A")


# ---------------------------------------------------------------------------
# Person group membership
# ---------------------------------------------------------------------------

class TestPersonGroupMembership:
    """add_to_group / remove_from_group / the group_memberships property."""

    def test_constructor_copies_the_supplied_group_list(self, make_person):
        """Later mutations of the caller's list must not leak into the person."""
        supplied = [group("Students")]
        person = make_person("Jan", "Novák", "6.A", group_memberships=supplied)
        supplied.append(group("Teachers"))
        assert len(person.group_memberships) == 1
        assert person.is_dirty() is False

    def test_add_to_group_appends_and_marks_membership_dirty(self, make_person):
        """Adding a group is a pending AD change."""
        person = make_person("Jan", "Novák", "6.A")
        students = group("Students")
        person.add_to_group(students)
        assert person.group_memberships == [students]
        assert person.get_dirty_fields() == {"group_memberships"}
        assert person.get_changes()["group_memberships"] == [students]

    def test_add_to_group_ignores_a_duplicate_dn_regardless_of_case(self, make_person):
        """ADGroup identity is its DN, compared case-insensitively."""
        person = make_person("Jan", "Novák", "6.A")
        person.add_to_group(group("Students", dn="CN=Students,DC=school"))
        person.add_to_group(group("students", dn="cn=students,dc=school"))
        assert len(person.group_memberships) == 1
        assert person.get_group_dns() == ["CN=Students,DC=school"]

    def test_remove_from_group_deletes_the_membership(self, make_person):
        """Removing an existing membership is a pending AD change."""
        students = group("Students")
        person = make_person("Jan", "Novák", "6.A", group_memberships=[students])
        person.remove_from_group(students)
        assert person.group_memberships == []
        assert person.get_dirty_fields() == {"group_memberships"}

    def test_remove_from_group_matches_by_dn_not_by_object(self, make_person):
        """A freshly built ADGroup with the same DN removes the stored one."""
        person = make_person("Jan", "Novák", "6.A",
                             group_memberships=[group("Students", dn="CN=S,DC=x")])
        person.remove_from_group(group("Whatever", dn="cn=s,dc=x"))
        assert person.group_memberships == []

    def test_remove_from_group_is_a_no_op_for_an_unknown_group(self, make_person):
        """Removing something the person never had leaves the state untouched."""
        person = make_person("Jan", "Novák", "6.A",
                             group_memberships=[group("Students")])
        person.remove_from_group(group("Teachers"))
        assert len(person.group_memberships) == 1
        assert person.is_dirty() is False

    def test_add_then_remove_leaves_the_person_clean(self, make_person):
        """Membership dirtiness is a set comparison, so the round trip cancels out."""
        person = make_person("Jan", "Novák", "6.A")
        students = group("Students")
        person.add_to_group(students)
        person.remove_from_group(students)
        assert person.is_dirty() is False
        assert person.get_changes() == {}

    def test_is_in_group_and_get_group_dns_reflect_membership(self, make_person):
        """Query helpers use DN equality as well."""
        students = group("Students", dn="CN=Students,DC=x")
        teachers = group("Teachers", dn="CN=Teachers,DC=x")
        person = make_person("Jan", "Novák", "6.A", group_memberships=[students])
        assert person.is_in_group(students) is True
        assert person.is_in_group(group("x", dn="cn=students,dc=x")) is True
        assert person.is_in_group(teachers) is False
        assert person.get_group_dns() == ["CN=Students,DC=x"]

    def test_assigning_a_different_group_set_replaces_and_copies_it(self, make_person):
        """Wholesale replacement stores a copy and records the change."""
        person = make_person("Jan", "Novák", "6.A",
                             group_memberships=[group("Students")])
        replacement = [group("Teachers"), group("Staff")]
        person.group_memberships = replacement
        assert person.get_group_dns() == [g.dn for g in replacement]
        assert person.get_dirty_fields() == {"group_memberships"}
        replacement.append(group("Admins"))
        assert len(person.group_memberships) == 2

    def test_assigning_the_same_group_set_keeps_the_person_clean(self, make_person):
        """Re-assigning an equivalent set is not a change."""
        person = make_person("Jan", "Novák", "6.A",
                             group_memberships=[group("Students", dn="CN=S,DC=x")])
        person.group_memberships = [group("Students", dn="CN=S,DC=x")]
        assert person.is_dirty() is False

    @pytest.mark.bug
    def test_assigning_refreshed_group_objects_replaces_the_stored_objects(self, make_person):
        """Re-assigning groups with the same DNs but new data must store them."""
        person = make_person("Jan", "Novák", "6.A",
                             group_memberships=[group("Students", dn="CN=S,DC=x")])
        refreshed = ADGroup(name="Students", dn="cn=s,dc=x",
                            description="renamed in AD", members_count=42)
        person.group_memberships = [refreshed]
        assert person.group_memberships[0] is refreshed
        assert person.group_memberships[0].description == "renamed in AD"

    @pytest.mark.bug
    def test_assigning_none_clears_the_group_memberships(self, make_person):
        """None means "no groups", exactly as it does in the constructor."""
        person = make_person("Jan", "Novák", "6.A",
                             group_memberships=[group("Students")])
        person.group_memberships = None
        assert person.group_memberships == []
        assert person.get_dirty_fields() == {"group_memberships"}

    def test_membership_change_moves_a_synced_person_to_update_pending(self):
        """A group change has to be pushed to AD like any other change."""
        person = synced_person()
        person.add_to_group(group("Students"))
        assert person.ad_status == ADStatus.UPDATE_PENDING

    @pytest.mark.bug
    def test_undoing_a_group_change_restores_synced_status(self):
        """Same contract as the scalar fields: no differences -> synced again."""
        person = synced_person()
        students = group("Students")
        person.add_to_group(students)
        person.remove_from_group(students)
        assert person.is_dirty() is False
        assert person.ad_status == ADStatus.SYNCED

    def test_in_place_mutation_of_the_returned_list_is_only_seen_after_marking(self, make_person):
        """The getter hands out the live list; callers must notify the model."""
        person = make_person("Jan", "Novák", "6.A")
        person.group_memberships.append(group("Students"))
        assert person.is_dirty() is False
        person.mark_field_dirty("group_memberships")
        assert person.get_dirty_fields() == {"group_memberships"}

    def test_reset_dirty_rebaselines_the_group_membership(self, make_person):
        """After a sync the current groups become the new original set."""
        person = make_person("Jan", "Novák", "6.A", ad_dn="CN=Jan,DC=x")
        students = group("Students")
        person.add_to_group(students)
        person.reset_dirty()
        assert person.is_dirty() is False
        person.remove_from_group(students)
        assert person.get_dirty_fields() == {"group_memberships"}


# ---------------------------------------------------------------------------
# Class
# ---------------------------------------------------------------------------

class TestClass:
    """add/remove/find/search on a single class."""

    def test_add_person_appends_in_insertion_order(self, make_class, make_person):
        """The class keeps the order the students were added in."""
        cls = make_class("6.A", 0)
        first = make_person("Jan", "Novák", "6.A")
        second = make_person("Eva", "Malá", "6.A")
        cls.add_person(first)
        cls.add_person(second)
        assert cls.persons == [first, second]
        assert cls.persons[0] is first

    def test_add_person_warns_but_still_adds_on_a_class_name_mismatch(self, make_class,
                                                                     make_person, caplog):
        """A mismatch is a data-quality warning, not a rejection."""
        cls = make_class("6.A", 0)
        stray = make_person("Jan", "Novák", "9.C")
        with caplog.at_level(logging.WARNING, logger="models"):
            cls.add_person(stray)
        assert cls.persons == [stray]
        assert any("doesn't match class 6.A" in record.message for record in caplog.records)

    def test_add_person_keeps_duplicates(self, make_class, make_person):
        """The class is a plain list - deduplication is somebody else's job."""
        cls = make_class("6.A", 0)
        cls.add_person(make_person("Jan", "Novák", "6.A"))
        cls.add_person(make_person("Jan", "Novák", "6.A"))
        assert len(cls.persons) == 2

    def test_remove_person_removes_the_exact_object_not_an_equal_twin(self, make_class,
                                                                     make_person):
        """Two identical students must stay distinguishable by identity."""
        first = make_person("Jan", "Novák", "6.A")
        second = make_person("Jan", "Novák", "6.A")
        assert first == second          # dataclass equality really does collide
        cls = make_class("6.A", persons=[first, second])
        cls.remove_person(second)
        assert len(cls.persons) == 1
        assert cls.persons[0] is first

    def test_remove_person_is_a_silent_no_op_for_a_stranger(self, make_class, make_person):
        """Removing somebody who is not in the class raises nothing."""
        cls = make_class("6.A", 2)
        cls.remove_person(make_person("Kdo", "Ví", "6.A"))
        assert len(cls.persons) == 2

    def test_remove_person_removes_only_one_occurrence(self, make_class, make_person):
        """The same object listed twice is deleted once per call."""
        twin = make_person("Jan", "Novák", "6.A")
        cls = make_class("6.A", persons=[twin, twin])
        cls.remove_person(twin)
        assert cls.persons == [twin]

    def test_find_person_by_name_ignores_case_and_diacritics(self, make_class, make_person):
        """The lookup uses the normalized name on both sides."""
        target = make_person("Přemysl", "Ďurčovič", "6.A")
        cls = make_class("6.A", persons=[make_person("Eva", "Malá", "6.A"), target])
        assert cls.find_person_by_name("PREMYSL", "durcovic") is target

    def test_find_person_by_name_returns_the_first_match(self, make_class, make_person):
        """With duplicates the earliest entry wins."""
        first = make_person("Jan", "Novák", "6.A")
        second = make_person("Jan", "Novak", "6.A")
        cls = make_class("6.A", persons=[first, second])
        assert cls.find_person_by_name("Jan", "Novák") is first

    def test_find_person_by_name_returns_none_when_nobody_matches(self, make_class):
        """A miss is None, not an exception."""
        assert make_class("6.A", 3).find_person_by_name("Nikdo", "Nikdo") is None

    def test_find_person_by_name_treats_none_as_an_empty_name(self, make_class, make_person):
        """None normalizes to "" and therefore only matches an empty name."""
        blank = make_person("", "", "6.A")
        cls = make_class("6.A", persons=[make_person("Jan", "Novák", "6.A"), blank])
        assert cls.find_person_by_name(None, None) is blank

    def test_search_persons_returns_every_match_in_order(self, make_class, make_person):
        """Search is a filter over the class in its stored order."""
        a = make_person("Jan", "Novák", "6.A")
        b = make_person("Jana", "Nová", "6.A")
        c = make_person("Eva", "Malá", "6.A")
        cls = make_class("6.A", persons=[a, b, c])
        assert cls.search_persons("nov") == [a, b]
        assert cls.search_persons("jana") == [b]

    def test_search_persons_with_an_empty_query_returns_everybody(self, make_class):
        """An empty needle is contained in every name."""
        cls = make_class("6.A", 4)
        assert len(cls.search_persons("")) == 4

    def test_search_persons_returns_an_empty_list_when_nothing_matches(self, make_class):
        """No match is an empty list, never None."""
        assert make_class("6.A", 3).search_persons("zzzz") == []

    def test_repr_reports_name_and_headcount(self, make_class):
        """The repr is used in log messages and dialogs."""
        assert repr(make_class("6.A", 2)) == "Class(6.A, 2 students)"


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

class TestSource:
    """Class container, person aggregation, deep_copy and statistics."""

    def test_find_class_by_name_matches_exactly_and_is_case_sensitive(self, make_source):
        """Class names are identifiers, compared verbatim."""
        source = make_source("S", [("6.A", 1), ("IX.", 2)])
        assert source.find_class_by_name("6.A") is source.classes[0]
        assert source.find_class_by_name("6.a") is None
        assert source.find_class_by_name(" 6.A ") is None

    def test_find_class_by_name_returns_none_for_a_missing_class(self, make_source):
        """A miss is None."""
        assert make_source("S", [("6.A", 1)]).find_class_by_name("9.Z") is None

    def test_find_class_by_name_returns_the_first_of_two_equal_names(self, make_class):
        """Duplicated class names resolve to the earlier object."""
        source = Source(name="S", source_type="manual")
        first, second = make_class("6.A", 1), make_class("6.A", 2)
        source.add_class(first)
        source.add_class(second)
        assert source.find_class_by_name("6.A") is first

    def test_get_all_persons_flattens_the_classes_in_order(self, make_class):
        """Students come back class by class, in class order."""
        a = make_class("6.A", 2)
        b = make_class("6.B", 1)
        source = Source(name="S", source_type="manual")
        source.add_class(a)
        source.add_class(b)
        assert source.get_all_persons() == a.persons + b.persons

    def test_get_all_persons_returns_a_new_list_that_does_not_write_back(self, make_source):
        """The aggregation is a snapshot; appending to it must not add students."""
        source = make_source("S", [("6.A", 2)])
        persons = source.get_all_persons()
        persons.append("not a person")
        assert len(source.get_all_persons()) == 2

    def test_get_all_persons_is_empty_for_a_source_without_classes(self):
        """An empty source aggregates to an empty list."""
        assert Source(name="S", source_type="manual").get_all_persons() == []

    def test_remove_class_removes_the_exact_object(self, make_class):
        """Identity again: two identical classes stay distinguishable."""
        source = Source(name="S", source_type="manual")
        first, second = make_class("6.A", 0), make_class("6.A", 0)
        source.add_class(first)
        source.add_class(second)
        source.remove_class(second)
        assert source.classes == [first]
        assert source.classes[0] is first

    def test_remove_class_ignores_a_class_from_another_source(self, make_source, make_class):
        """Removing a foreign class is a no-op."""
        source = make_source("S", [("6.A", 1)])
        source.remove_class(make_class("6.A", 1))
        assert len(source.classes) == 1

    def test_search_persons_spans_every_class(self, make_class, make_person):
        """The source-wide search concatenates the per-class results."""
        a = make_class("6.A", persons=[make_person("Jan", "Novák", "6.A")])
        b = make_class("6.B", persons=[make_person("Jana", "Nová", "6.B"),
                                       make_person("Eva", "Malá", "6.B")])
        source = Source(name="S", source_type="manual")
        source.add_class(a)
        source.add_class(b)
        assert source.search_persons("nov") == [a.persons[0], b.persons[0]]

    def test_source_info_round_trips_with_a_default_for_unknown_keys(self, make_source):
        """set_source_info / get_source_info behave like a dict with a default."""
        source = make_source("S")
        source.set_source_info("file", "/tmp/x.csv")
        assert source.get_source_info("file") == "/tmp/x.csv"
        assert source.get_source_info("missing") is None
        assert source.get_source_info("missing", "fallback") == "fallback"

    def test_deep_copy_produces_a_fully_independent_source(self, make_source, make_person):
        """Editing the copy - or the original - must never affect the other."""
        source = make_source("S", [("6.A", 2)])
        source.set_source_info("file", "/tmp/x.csv")
        source.classes[0].persons[0].add_to_group(group("Students"))
        clone = source.deep_copy()

        assert clone is not source
        assert clone.name == source.name and clone.readonly == source.readonly
        assert clone.get_statistics() == source.get_statistics()
        assert clone.classes[0] is not source.classes[0]
        assert clone.classes[0].persons[0] is not source.classes[0].persons[0]
        assert (clone.classes[0].persons[0].group_memberships[0]
                is not source.classes[0].persons[0].group_memberships[0])

        clone.classes[0].persons[0].first_name = "Změněný"
        clone.classes[0].persons.pop()
        clone.source_info["file"] = "/tmp/other.csv"
        assert source.classes[0].persons[0].first_name == "First0"
        assert len(source.classes[0].persons) == 2
        assert source.get_source_info("file") == "/tmp/x.csv"

    def test_deep_copy_preserves_dirty_state(self, make_source):
        """A copy of an edited source is still edited."""
        source = make_source("S", [("6.A", 1)])
        source.classes[0].persons[0].ad_email = "a@school.local"
        clone = source.deep_copy()
        assert clone.classes[0].persons[0].get_changes() == {"ad_email": "a@school.local"}

    def test_deep_copy_of_an_empty_source_works(self):
        """No classes is not a special case."""
        clone = Source(name="S", source_type="csv", readonly=True).deep_copy()
        assert clone.classes == [] and clone.readonly is True and clone.source_type == "csv"

    def test_get_statistics_counts_classes_and_students(self, make_source):
        """The statistics feed the UI header, so the shape matters."""
        source = make_source("S", [("6.A", 2), ("IX.", 3)])
        assert source.get_statistics() == {
            "total_classes": 2,
            "total_persons": 5,
            "classes": [{"name": "6.A", "count": 2}, {"name": "IX.", "count": 3}],
        }

    def test_get_statistics_of_an_empty_source_is_all_zeros(self):
        """An empty source reports zeros and an empty class list."""
        assert Source(name="S", source_type="manual").get_statistics() == {
            "total_classes": 0, "total_persons": 0, "classes": [],
        }

    def test_get_statistics_counts_an_empty_class(self, make_source):
        """A class without students still counts as a class."""
        stats = make_source("S", [("6.A", 0), ("6.B", 1)]).get_statistics()
        assert stats["total_classes"] == 2 and stats["total_persons"] == 1


# ---------------------------------------------------------------------------
# SourceManager
# ---------------------------------------------------------------------------

class TestSourceManager:
    """Registry behaviour and the three Qt signals."""

    def test_add_source_registers_it_and_emits_source_added(self, source_manager, make_source):
        """The signal carries the source object itself."""
        received = collect(source_manager.source_added)
        source = make_source("EduPage")
        source_manager.add_source(source)
        assert source_manager.sources == [source]
        assert received == [(source,)]

    def test_add_source_allows_duplicate_names(self, source_manager, make_source):
        """The manager does not deduplicate - callers check source_name_exists first."""
        first, second = make_source("Dup"), make_source("Dup")
        source_manager.add_source(first)
        source_manager.add_source(second)
        assert len(source_manager.sources) == 2
        assert source_manager.get_source_by_name("Dup") is first

    def test_remove_source_emits_the_name_of_the_removed_source(self, source_manager,
                                                                make_source):
        """source_removed carries the name, not the object."""
        source = make_source("EduPage")
        source_manager.add_source(source)
        received = collect(source_manager.source_removed)
        source_manager.remove_source(source)
        assert source_manager.sources == []
        assert received == [("EduPage",)]

    def test_remove_source_removes_the_exact_object(self, source_manager, make_source):
        """Two sources with the same name stay distinguishable by identity."""
        first, second = make_source("Dup"), make_source("Dup")
        source_manager.add_source(first)
        source_manager.add_source(second)
        source_manager.remove_source(second)
        assert source_manager.sources == [first]
        assert source_manager.sources[0] is first

    def test_remove_source_of_an_unregistered_source_emits_nothing(self, source_manager,
                                                                   make_source):
        """A stranger is ignored silently - no exception, no signal."""
        source_manager.add_source(make_source("A"))
        received = collect(source_manager.source_removed)
        source_manager.remove_source(make_source("Stranger"))
        source_manager.remove_source(None)
        assert len(source_manager.sources) == 1
        assert received == []

    def test_notify_source_modified_emits_the_given_name(self, source_manager):
        """The notification is a pure signal - it does not verify the name."""
        received = collect(source_manager.source_modified)
        source_manager.notify_source_modified("EduPage")
        source_manager.notify_source_modified("Ghost")
        assert received == [("EduPage",), ("Ghost",)]

    @pytest.mark.parametrize("name, expected", [
        ("EduPage", True), ("edupage", False), ("EduPage ", False), ("", False),
    ])
    def test_source_name_exists_compares_exactly(self, source_manager, make_source,
                                                 name, expected):
        """Name comparison is case- and whitespace-sensitive."""
        source_manager.add_source(make_source("EduPage"))
        assert source_manager.source_name_exists(name) is expected

    def test_get_source_by_name_returns_none_for_an_unknown_name(self, source_manager,
                                                                 make_source):
        """A miss is None."""
        source_manager.add_source(make_source("EduPage"))
        assert source_manager.get_source_by_name("Bakaláři") is None

    def test_get_source_names_lists_names_in_registration_order(self, source_manager,
                                                               make_source):
        """The order drives the combo box in the UI."""
        for name in ("EduPage", "CSV", "Bakaláři"):
            source_manager.add_source(make_source(name))
        assert source_manager.get_source_names() == ["EduPage", "CSV", "Bakaláři"]

    def test_a_fresh_manager_is_empty(self, source_manager):
        """No sources, no names, nothing exists."""
        assert source_manager.sources == []
        assert source_manager.get_source_names() == []
        assert source_manager.source_name_exists("anything") is False


# ---------------------------------------------------------------------------
# merge_sources / merge_sources_detailed
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestMergeSources:
    """Union / intersection merging, statistics and input independence."""

    @staticmethod
    def _person(first, last, class_name, **kwargs):
        return Person(first, last, class_name, **kwargs)

    @pytest.mark.parametrize("left_missing, right_missing", [
        (True, False), (False, True), (True, True),
    ])
    def test_merge_requires_both_sources(self, make_source, left_missing, right_missing):
        """A missing side is a ValueError with an explicit message."""
        left = None if left_missing else make_source("L")
        right = None if right_missing else make_source("R")
        with pytest.raises(ValueError, match="Both a left and a right source are required"):
            SourceManager.merge_sources_detailed(left, right, "union", "M")

    @pytest.mark.parametrize("strategy", ["difference", "Union", "UNION", "", "symmetric"])
    def test_merge_rejects_an_unknown_strategy(self, make_source, strategy):
        """Strategy names are matched exactly and echoed back in the message."""
        with pytest.raises(ValueError, match=f"Unknown merge strategy: {strategy}"):
            SourceManager.merge_sources_detailed(
                make_source("L"), make_source("R"), strategy, "M")

    def test_missing_source_is_checked_before_the_strategy(self, make_source):
        """With both problems the missing source is reported first."""
        with pytest.raises(ValueError, match="Both a left and a right source are required"):
            SourceManager.merge_sources_detailed(None, make_source("R"), "bogus", "M")

    def test_union_keeps_everybody_from_both_sides(self):
        """Union = left students plus the right-only ones."""
        left = source_with("L", ("6.A", [self._person("Jan", "Novák", "6.A")]))
        right = source_with("R", ("6.A", [self._person("Eva", "Malá", "6.A")]))
        result, stats = SourceManager.merge_sources_detailed(left, right, "union", "M")
        assert sorted(p.first_name for p in result.get_all_persons()) == ["Eva", "Jan"]
        assert stats["result_persons"] == 2

    def test_union_lets_the_left_source_win_for_shared_students(self):
        """The left spelling and the left AD data survive the merge."""
        left = source_with("L", ("6.A", [
            self._person("Jan", "Novák", "6.A", ad_username="left-user")]))
        right = source_with("R", ("6.A", [
            self._person("JAN", "NOVAK", "6.A", ad_username="right-user")]))
        result, stats = SourceManager.merge_sources_detailed(left, right, "union", "M")
        merged = result.get_all_persons()
        assert len(merged) == 1
        assert (merged[0].first_name, merged[0].last_name) == ("Jan", "Novák")
        assert merged[0].ad_username == "left-user"
        assert stats["in_both"] == 1

    def test_intersection_keeps_only_students_present_on_both_sides(self):
        """Everything that exists on just one side is dropped."""
        left = source_with("L", ("6.A", [
            self._person("Jan", "Novák", "6.A"),
            self._person("Eva", "Malá", "6.A")]))
        right = source_with("R", ("6.A", [
            self._person("jan", "novak", "6.A"),
            self._person("Petr", "Sova", "6.A")]))
        result, stats = SourceManager.merge_sources_detailed(
            left, right, "intersection", "M")
        assert [p.first_name for p in result.get_all_persons()] == ["Jan"]
        assert (stats["only_left"], stats["only_right"], stats["in_both"]) == (1, 1, 1)

    def test_intersection_of_disjoint_sources_is_empty(self):
        """No overlap means no classes at all in the result."""
        left = source_with("L", ("6.A", [self._person("Jan", "Novák", "6.A")]))
        right = source_with("R", ("9.C", [self._person("Eva", "Malá", "9.C")]))
        result, stats = SourceManager.merge_sources_detailed(
            left, right, "intersection", "M")
        assert result.get_all_persons() == [] and result.classes == []
        assert stats["result_persons"] == 0 and stats["result_classes"] == 0

    def test_the_same_name_in_a_different_class_is_a_different_student(self):
        """The class is part of the identity key, so no false intersection."""
        left = source_with("L", ("6.A", [self._person("Jan", "Novák", "6.A")]))
        right = source_with("R", ("6.B", [self._person("Jan", "Novák", "6.B")]))
        union, _ = SourceManager.merge_sources_detailed(left, right, "union", "M")
        intersection, _ = SourceManager.merge_sources_detailed(
            left, right, "intersection", "M")
        assert len(union.get_all_persons()) == 2
        assert intersection.get_all_persons() == []

    def test_merge_result_contains_copies_and_never_aliases_the_inputs(self):
        """Editing the merged source must not corrupt either input source."""
        students = group("Students")
        left_person = self._person("Jan", "Novák", "6.A", group_memberships=[students])
        right_person = self._person("Eva", "Malá", "6.B")
        left = source_with("L", ("6.A", [left_person]))
        right = source_with("R", ("6.B", [right_person]))

        result, _ = SourceManager.merge_sources_detailed(left, right, "union", "M")
        merged = {p.first_name: p for p in result.get_all_persons()}
        assert merged["Jan"] is not left_person
        assert merged["Eva"] is not right_person
        assert merged["Jan"].group_memberships[0] is not students
        assert result.classes[0] is not left.classes[0]

        merged["Jan"].first_name = "Přepsaný"
        merged["Eva"].ad_username = "changed"
        result.classes[0].persons.clear()
        assert left_person.first_name == "Jan"
        assert right_person.ad_username is None
        assert len(left.classes[0].persons) == 1

    def test_merge_does_not_modify_the_input_sources(self):
        """Neither the class lists nor the students of the inputs are touched."""
        left = source_with("L", ("6.A", [self._person("Jan", "Novák", "6.A")]))
        right = source_with("R", ("9.C", [self._person("Eva", "Malá", "9.C")]))
        before = (left.get_statistics(), right.get_statistics())
        SourceManager.merge_sources_detailed(left, right, "union", "M")
        assert (left.get_statistics(), right.get_statistics()) == before

    def test_merge_keeps_the_left_class_order_and_appends_right_only_classes(self):
        """Left order first, then whatever only the right source has."""
        left = source_with(
            "L",
            ("6.A", [self._person("A", "A", "6.A")]),
            ("6.B", [self._person("B", "B", "6.B")]),
        )
        right = source_with(
            "R",
            ("9.C", [self._person("C", "C", "9.C")]),
            ("6.A", [self._person("D", "D", "6.A")]),
        )
        result, stats = SourceManager.merge_sources_detailed(left, right, "union", "M")
        assert [cls.name for cls in result.classes] == ["6.A", "6.B", "9.C"]
        assert [p.first_name for p in result.classes[0].persons] == ["A", "D"]
        assert stats["result_classes"] == 3

    def test_a_class_that_no_student_belongs_to_is_dropped(self):
        """Documented behaviour: the result is rebuilt from students only."""
        left = source_with("L", ("6.A", []), ("6.B", [self._person("B", "B", "6.B")]))
        right = source_with("R")
        result, stats = SourceManager.merge_sources_detailed(left, right, "union", "M")
        assert [cls.name for cls in result.classes] == ["6.B"]
        assert stats["result_classes"] == 1

    def test_students_are_filed_under_their_own_class_name(self):
        """A student whose class_name disagrees with its container is re-filed."""
        left = source_with("L", ("6.A", [self._person("Jan", "Novák", "9.Z")]))
        result, _ = SourceManager.merge_sources_detailed(
            left, source_with("R"), "union", "M")
        assert [cls.name for cls in result.classes] == ["9.Z"]

    def test_duplicates_inside_one_source_are_collapsed_and_counted(self):
        """The same student twice on one side is merged into one and reported."""
        left = source_with("L", ("6.A", [
            self._person("Jan", "Novák", "6.A", ad_username="first"),
            self._person("JAN", "NOVAK", "6.A", ad_username="second"),
            self._person("Eva", "Malá", "6.A")]))
        right = source_with("R", ("6.A", [
            self._person("Petr", "Sova", "6.A"),
            self._person("Petr", "Sova", "6.A")]))
        result, stats = SourceManager.merge_sources_detailed(left, right, "union", "M")

        assert stats["left_persons"] == 3 and stats["left_unique"] == 2
        assert stats["left_duplicates_collapsed"] == 1
        assert stats["right_persons"] == 2 and stats["right_unique"] == 1
        assert stats["right_duplicates_collapsed"] == 1
        assert stats["result_persons"] == 3
        names = [p.ad_username for p in result.get_all_persons()]
        assert "first" in names and "second" not in names

    def test_statistics_report_every_documented_key(self):
        """The full statistics dict for a mixed union."""
        left = source_with("L", ("6.A", [
            self._person("Jan", "Novák", "6.A"),
            self._person("Eva", "Malá", "6.A")]))
        right = source_with("R", ("6.A", [self._person("Jan", "Novák", "6.A")]),
                            ("9.C", [self._person("Petr", "Sova", "9.C")]))
        _, stats = SourceManager.merge_sources_detailed(left, right, "union", "M")
        assert stats == {
            "strategy": "union",
            "left_persons": 2,
            "right_persons": 2,
            "left_unique": 2,
            "right_unique": 2,
            "left_duplicates_collapsed": 0,
            "right_duplicates_collapsed": 0,
            "only_left": 1,
            "only_right": 1,
            "in_both": 1,
            "result_persons": 3,
            "result_classes": 2,
        }

    def test_statistics_of_two_empty_sources_are_all_zeros(self, make_source):
        """Merging nothing with nothing yields an empty, valid result."""
        result, stats = SourceManager.merge_sources_detailed(
            make_source("L"), make_source("R"), "union", "Empty")
        assert result.get_all_persons() == [] and result.classes == []
        assert stats["result_persons"] == 0
        assert stats["only_left"] == stats["only_right"] == stats["in_both"] == 0

    @pytest.mark.parametrize("strategy", ["union", "intersection"])
    def test_the_merged_source_is_a_fresh_editable_manual_source(self, strategy):
        """Whatever the inputs were, the result is editable and named as asked."""
        left = source_with("L", ("6.A", [self._person("Jan", "Novák", "6.A")]))
        right = copy.deepcopy(left)
        right.name = "R"
        right.readonly = True
        left.readonly = True
        result, stats = SourceManager.merge_sources_detailed(
            left, right, strategy, "Sloučený zdroj")
        assert result.name == "Sloučený zdroj"
        assert result.source_type == "manual"
        assert result.readonly is False
        assert stats["strategy"] == strategy

    @pytest.mark.parametrize("strategy", ["union", "intersection"])
    def test_merging_a_source_with_itself_is_a_deduplicated_copy(self, strategy):
        """Both strategies degenerate to "the source itself" for equal inputs."""
        source = source_with("S", ("6.A", [
            self._person("Jan", "Novák", "6.A"),
            self._person("Eva", "Malá", "6.A")]))
        result, stats = SourceManager.merge_sources_detailed(
            source, source, strategy, "Self")
        assert sorted(p.first_name for p in result.get_all_persons()) == ["Eva", "Jan"]
        assert stats["in_both"] == 2
        assert stats["only_left"] == stats["only_right"] == 0

    def test_merging_is_idempotent(self):
        """Merging the merge result with itself changes nothing."""
        left = source_with("L", ("6.A", [self._person("Jan", "Novák", "6.A")]))
        right = source_with("R", ("9.C", [self._person("Eva", "Malá", "9.C")]))
        once, _ = SourceManager.merge_sources_detailed(left, right, "union", "M")
        twice, _ = SourceManager.merge_sources_detailed(once, once, "union", "M")
        assert once.get_statistics() == twice.get_statistics()

    def test_merge_sources_returns_only_the_source(self, make_source):
        """The convenience wrapper drops the statistics tuple."""
        left = source_with("L", ("6.A", [self._person("Jan", "Novák", "6.A")]))
        result = SourceManager.merge_sources(left, make_source("R"), "union", "M")
        assert isinstance(result, Source)
        assert [p.first_name for p in result.get_all_persons()] == ["Jan"]

    def test_merge_sources_propagates_the_validation_errors(self, make_source):
        """The wrapper does not swallow the ValueError of the detailed merge."""
        with pytest.raises(ValueError, match="Unknown merge strategy"):
            SourceManager.merge_sources(make_source("L"), make_source("R"), "nope", "M")

    def test_merge_is_available_on_an_instance_too(self, source_manager, make_source):
        """Both static methods are reachable through a SourceManager instance."""
        left = source_with("L", ("6.A", [self._person("Jan", "Novák", "6.A")]))
        result = source_manager.merge_sources(left, make_source("R"), "union", "M")
        assert len(result.get_all_persons()) == 1

    def test_unicode_names_survive_the_merge_unchanged(self):
        """Diacritics are only folded for the comparison key, never stored folded."""
        left = source_with("L", ("6.A", [self._person("Přemysl", "Ďurčovič", "6.A")]))
        right = source_with("R", ("6.A", [self._person("PREMYSL", "DURCOVIC", "6.A")]))
        result, stats = SourceManager.merge_sources_detailed(left, right, "union", "M")
        merged = result.get_all_persons()
        assert len(merged) == 1
        assert (merged[0].first_name, merged[0].last_name) == ("Přemysl", "Ďurčovič")
        assert stats["in_both"] == 1
