"""
Unit tests for :mod:`services.ad_validator` and :mod:`services.ad_services`.

Neither module imports a widget or opens a dialog, so the ``dialogs`` stubs are
not needed here - but nothing in these tests may reach the network either, so
every Active Directory interaction goes through :class:`FakeADClient`, a plain
in-memory double that records the calls a real ``ADClient`` would have turned
into LDAP traffic.

The password policy is pinned to the factory default by an autouse fixture, so
the validator tests never depend on (or read) the developer's settings file.

Tests marked ``bug`` + ``xfail`` document defects found in the production code;
they assert the *correct* behaviour and are expected to fail until the defect
is fixed.
"""

import re

import pytest

from models import ADGroup, ADStatus, Person
from services.ad_validator import ADValidator, ValidationResult
from services.ad_services import (
    ConflictDetector,
    ConflictInfo,
    ConflictResolution,
    ConflictResolver,
    ConflictStrategy,
    CreateOperation,
    OperationResult,
    PersonsSyncService,
    SyncPlan,
    SyncPlanner,
    SyncResult,
    UpdateOperation,
    escape_dn_value,
    first_rdn_value,
    unescape_dn_value,
)
from services.ldap_compat import MODIFY_REPLACE
from utils.ad_utils import ValidationIssue


#: A password that satisfies every requirement of the default policy.
GOOD_PASSWORD = "Str0ng!Pass"

BASE_DN = "OU=Students,DC=skola,DC=local"


# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _default_password_policy():
    """Pin the factory-default password policy instead of reading real settings."""
    import utils.password_policy as pp

    pp.set_password_policy(pp.PasswordPolicy(), persist=False)
    yield


@pytest.fixture
def ad_person(make_person):
    """Factory for a person that is complete enough for AD."""
    def _make(**kwargs):
        values = dict(
            first_name="Jan", last_name="Novák", class_name="6.A",
            ad_username="novakjan", ad_password=GOOD_PASSWORD,
            ad_display_name="Jan Novák", ad_email="jan.novak@skola.cz",
        )
        values.update(kwargs)
        return make_person(**values)
    return _make


class FakeEntry:
    """Stand-in for ``services.ad_client.ADUserEntry`` - pure data, no LDAP."""

    def __init__(self, dn="CN=x,DC=skola,DC=local", attributes=None):
        self.dn = dn
        self.attributes = dict(attributes or {})

    def get(self, name, default=None):
        return self.attributes.get(name, default)


class FakeADClient:
    """
    In-memory Active Directory double.

    It answers exactly the six methods the services call and records every
    call, so a test can assert *what would have been sent* without a socket.
    """

    #: ``ADGroupService`` looks at this; ``None`` means "not connected".
    connection = None

    def __init__(self, users=None, existing_ous=(), create_user_result=True,
                 create_ou_result=True, modify_results=None,
                 search_results=(), timestamp="20240101000000.0Z",
                 set_password_error=None, home_directory_result=True):
        self.users = dict(users or {})
        self.existing_ous = set(existing_ous)
        self.create_user_result = create_user_result
        self.create_ou_result = create_ou_result
        #: ``{ldap_attribute: False}`` makes ``modify_user`` fail for it.
        self.modify_results = dict(modify_results or {})
        self.search_results = list(search_results)
        self.timestamp = timestamp
        self.get_user_error = None
        #: raise this from set_password() to simulate a refusal
        self.set_password_error = set_password_error
        self.home_directory_result = home_directory_result
        #: the channel is encrypted unless a test says otherwise
        self.is_secure = True
        self.tls_error = None

        self.calls = []
        self.modifications = []
        self.created_users = []
        self.created_ous = []
        self.passwords_set = []
        self.home_directories = []

    # -- the ADClient surface -------------------------------------------

    def get_user(self, dn, attributes=None):
        self.calls.append(("get_user", dn))
        if self.get_user_error is not None:
            raise self.get_user_error
        return self.users.get(dn)

    def search_users(self, base_dn, search_filter, attributes=None):
        self.calls.append(("search_users", base_dn, search_filter))
        return list(self.search_results)

    def ou_exists(self, dn):
        self.calls.append(("ou_exists", dn))
        return dn in self.existing_ous

    def create_ou(self, dn, name):
        self.calls.append(("create_ou", dn, name))
        if self.create_ou_result:
            self.existing_ous.add(dn)
            self.created_ous.append((dn, name))
        return self.create_ou_result

    def create_user(self, dn, attributes):
        self.calls.append(("create_user", dn))
        if self.create_user_result:
            self.created_users.append((dn, dict(attributes)))
            self.users[dn] = FakeEntry(
                dn, {**attributes, "modifyTimestamp": self.timestamp})
        return self.create_user_result

    def modify_user(self, dn, operations):
        self.calls.append(("modify_user", dn))
        self.modifications.append((dn, list(operations)))
        touched = {op[1] for op in operations}
        for attribute, result in self.modify_results.items():
            if attribute in touched:
                return result
        return True

    def set_password(self, dn, password):
        """Mirror of ADClient.set_password: encrypted channel, then set."""
        self.calls.append(("set_password", dn))
        if self.set_password_error is not None:
            raise self.set_password_error
        self.passwords_set.append((dn, password))
        return True

    def set_home_directory(self, dn, home_path, home_drive=None):
        self.calls.append(("set_home_directory", dn))
        if self.home_directory_result:
            self.home_directories.append((dn, home_path, home_drive))
        return self.home_directory_result

    def require_secure_channel(self, operation="this operation"):
        if not self.is_secure:
            raise RuntimeError(f"AD refuses {operation} over an unencrypted connection")

    # -- assertions helpers ---------------------------------------------

    def kinds(self):
        """The names of the calls received, in order."""
        return [call[0] for call in self.calls]

    def ops_for(self, attribute):
        """Every modification tuple that targets *attribute*."""
        return [op for _dn, ops in self.modifications
                for op in ops if op[1] == attribute]


class StubDetector:
    """ConflictDetector replacement that returns a canned answer."""

    def __init__(self, answer=None):
        self.answer = answer
        self.seen = []

    def check_conflict(self, person):
        self.seen.append(person)
        return self.answer


def make_conflict_info(person, local=None, remote=None, conflicting=None,
                       non_conflicting=None):
    """Build a ConflictInfo without going through the detector."""
    local = {"ad_email": "local@skola.cz", "last_name": "Nováková"} \
        if local is None else local
    remote = {"ad_email": "remote@skola.cz"} if remote is None else remote
    conflicting = set(local) & set(remote) if conflicting is None else conflicting
    if non_conflicting is None:
        non_conflicting = {k: v for k, v in local.items() if k not in conflicting}
    return ConflictInfo(
        person=person,
        local_changes=local,
        remote_changes=remote,
        conflicting_fields=conflicting,
        non_conflicting_local=non_conflicting,
        current_ad_state={"mail": "remote@skola.cz"},
        ad_modified_time="20240202000000.0Z",
    )


def single_class_source(make_source, make_class, persons, class_name="6.A"):
    """A one-class source holding exactly *persons*."""
    source = make_source("AD source", [])
    source.add_class(make_class(class_name, persons=persons))
    return source


def fields_of(issues):
    """The ``field`` of every issue, in order."""
    return [issue.field for issue in issues]


def rdn_count(dn):
    """Number of RDNs in *dn*, honouring backslash-escaped commas."""
    return len(re.split(r"(?<!\\),", dn))


def trailing_space_is_escaped(escaped):
    """True when *escaped* has no trailing space a directory would strip."""
    if not escaped.endswith(' '):
        return True
    body = escaped[:-1]
    return (len(body) - len(body.rstrip('\\'))) % 2 == 1


# ===========================================================================
# ADValidator.validate_person
# ===========================================================================

def test_validate_person_accepts_a_fully_populated_record(ad_person):
    """A person with every AD field filled in produces no issue at all."""
    result = ADValidator.validate_person(ad_person())
    assert result.issues == []
    assert result.is_valid is True
    assert isinstance(result, ValidationResult)


@pytest.mark.parametrize("field_name", ["first_name", "last_name", "class_name"])
@pytest.mark.parametrize("value", ["", "   ", "\t\n"])
def test_validate_person_requires_every_name_field(ad_person, field_name, value):
    """Empty *and* whitespace-only first/last/class names are blocking errors."""
    person = ad_person(**{field_name: value})
    result = ADValidator.validate_person(person)

    matching = [i for i in result.errors if i.field == field_name]
    assert len(matching) == 1
    assert matching[0].issue_type == "missing"
    assert matching[0].severity == "error"
    assert result.is_valid is False


def test_validate_person_reports_missing_username_and_password_as_errors(ad_person):
    """No user name and no password are two separate blocking errors."""
    result = ADValidator.validate_person(
        ad_person(ad_username=None, ad_password=None))

    assert sorted(fields_of(result.errors)) == ["ad_password", "ad_username"]
    assert {i.issue_type for i in result.errors} == {"missing"}
    assert result.is_valid is False


def test_validate_person_treats_an_empty_username_string_as_missing(ad_person):
    """``ad_username=""`` takes the "missing" branch, not the format branch."""
    result = ADValidator.validate_person(ad_person(ad_username=""))
    username_issues = [i for i in result.issues if i.field == "ad_username"]
    assert [i.issue_type for i in username_issues] == ["missing"]


def test_validate_person_flags_a_missing_display_name_as_a_warning_only(ad_person):
    """The display name is recommended, not required - the record stays valid."""
    result = ADValidator.validate_person(ad_person(ad_display_name=None))

    assert fields_of(result.warnings) == ["ad_display_name"]
    assert result.warnings[0].severity == "warning"
    assert result.errors == []
    assert result.is_valid is True


def test_validate_person_treats_an_empty_display_name_like_a_missing_one(ad_person):
    """An empty string is falsy, so it earns the same recommendation warning."""
    result = ADValidator.validate_person(ad_person(ad_display_name=""))
    assert fields_of(result.warnings) == ["ad_display_name"]


def test_validate_person_forwards_username_format_problems(ad_person):
    """Format issues from ``validate_username`` end up in the same report."""
    result = ADValidator.validate_person(ad_person(ad_username="Novak Jan"))

    messages = " | ".join(i.message for i in result.errors)
    assert "lowercase" in messages
    assert "letters, numbers and . - _" in messages
    assert result.is_valid is False


def test_validate_person_forwards_password_policy_problems(ad_person):
    """A password below the policy minimum is reported as an invalid format."""
    result = ADValidator.validate_person(ad_person(ad_password="Ab1!"))

    password_errors = [i for i in result.errors if i.field == "ad_password"]
    assert [i.issue_type for i in password_errors] == ["invalid_format"]
    assert "too short" in password_errors[0].message


def test_validate_person_forwards_email_format_problems(ad_person):
    """A malformed e-mail is an error; the rest of the record is untouched."""
    result = ADValidator.validate_person(ad_person(ad_email="jan(at)skola"))

    assert fields_of(result.errors) == ["ad_email"]
    assert result.errors[0].issue_type == "invalid_format"


def test_validate_person_accepts_a_missing_email_without_complaint(ad_person):
    """The e-mail address is optional - its absence is not even a warning."""
    result = ADValidator.validate_person(ad_person(ad_email=None))
    assert [i for i in result.issues if i.field == "ad_email"] == []
    assert result.is_valid is True


def test_validate_person_accepts_diacritics_in_names_but_not_in_the_username(ad_person):
    """Czech names are fine; a user name carrying them is not."""
    result = ADValidator.validate_person(
        ad_person(first_name="Žofie", last_name="Čapková", ad_username="čapkovaz"))

    assert set(fields_of(result.errors)) == {"ad_username"}
    assert "diacritics" in " | ".join(i.message for i in result.errors)


def test_validate_person_reports_a_duplicate_username_as_an_error(ad_person):
    """A user name already taken is a blocking duplicate error naming it."""
    person = ad_person()
    result = ADValidator.validate_person(
        person, check_duplicates=True, existing_usernames={"novakjan"})

    duplicates = [i for i in result.issues if i.issue_type == "duplicate"]
    assert len(duplicates) == 1
    assert duplicates[0].field == "ad_username"
    assert "novakjan" in duplicates[0].message
    assert result.is_valid is False


def test_validate_person_ignores_known_usernames_when_duplicates_are_off(ad_person):
    """``check_duplicates=False`` means the set is not consulted at all."""
    result = ADValidator.validate_person(
        ad_person(), check_duplicates=False, existing_usernames={"novakjan"})
    assert result.issues == []


def test_validate_person_skips_the_duplicate_check_when_no_set_is_given(ad_person):
    """``existing_usernames=None`` disables the check even when asked for it."""
    result = ADValidator.validate_person(
        ad_person(), check_duplicates=True, existing_usernames=None)
    assert result.issues == []


def test_validate_person_matches_duplicates_exactly(ad_person):
    """Matching is exact: a differently-cased entry is not seen as taken.

    (A user name that actually contains capitals is rejected by the format
    rules anyway, so the exact match never hides a real collision.)
    """
    result = ADValidator.validate_person(
        ad_person(), check_duplicates=True, existing_usernames={"NOVAKJAN"})
    assert [i for i in result.issues if i.issue_type == "duplicate"] == []


def test_validate_person_reports_every_problem_of_a_completely_empty_record(make_person):
    """A blank person collects one error per required field plus the warning."""
    result = ADValidator.validate_person(
        make_person(first_name="", last_name="", class_name=""))

    assert sorted(fields_of(result.errors)) == [
        "ad_password", "ad_username", "class_name", "first_name", "last_name",
    ]
    assert fields_of(result.warnings) == ["ad_display_name"]
    assert result.is_valid is False


def test_validate_person_attaches_the_very_same_person_to_every_issue(make_person):
    """Each issue points back at the object that produced it, by identity."""
    person = make_person(first_name="", last_name="")
    result = ADValidator.validate_person(person)

    assert result.issues
    assert all(issue.person is person for issue in result.issues)


def test_validate_person_rejects_none_with_an_attribute_error():
    """``None`` is not a Person - the attribute access fails loudly."""
    with pytest.raises(AttributeError):
        ADValidator.validate_person(None)


def test_validation_result_errors_and_warnings_partition_the_issues(ad_person):
    """``errors`` and ``warnings`` together are exactly ``issues``."""
    result = ADValidator.validate_person(
        ad_person(ad_display_name=None, ad_email="nope"))

    assert len(result.errors) + len(result.warnings) == len(result.issues)
    assert all(i.severity == "error" for i in result.errors)
    assert all(i.severity == "warning" for i in result.warnings)


# ===========================================================================
# ADValidator.validate_source
# ===========================================================================

def test_validate_source_blames_only_the_second_holder_of_a_username(
        ad_person, make_source, make_class):
    """The first person keeps the name; the next one gets the duplicate error."""
    first = ad_person(first_name="Jan")
    second = ad_person(first_name="Josef")
    source = single_class_source(make_source, make_class, [first, second])

    result = ADValidator.validate_source(source)
    duplicates = [i for i in result.issues if i.issue_type == "duplicate"]

    assert len(duplicates) == 1
    assert duplicates[0].person is second
    assert result.is_valid is False


def test_validate_source_flags_every_repeat_after_the_first(
        ad_person, make_source, make_class):
    """Three people sharing a user name produce two duplicate errors."""
    people = [ad_person(first_name=name) for name in ("Jan", "Josef", "Jakub")]
    source = single_class_source(make_source, make_class, people)

    duplicates = [i for i in ADValidator.validate_source(source).issues
                  if i.issue_type == "duplicate"]

    assert len(duplicates) == 2
    assert [i.person for i in duplicates] == people[1:]


def test_validate_source_detects_duplicates_across_class_boundaries(
        ad_person, make_source, make_class):
    """The seen-set spans the whole source, not just one class."""
    first = ad_person(class_name="6.A")
    second = ad_person(first_name="Josef", class_name="7.B")
    source = make_source("AD source", [])
    source.add_class(make_class("6.A", persons=[first]))
    source.add_class(make_class("7.B", persons=[second]))

    duplicates = [i for i in ADValidator.validate_source(source).issues
                  if i.issue_type == "duplicate"]

    assert [i.person for i in duplicates] == [second]


def test_validate_source_does_not_flag_distinct_usernames(
        ad_person, make_source, make_class):
    """Different user names in the same class are perfectly valid."""
    source = single_class_source(make_source, make_class, [
        ad_person(ad_username="novakjan"),
        ad_person(first_name="Josef", ad_username="novakjos"),
    ])

    result = ADValidator.validate_source(source)
    assert result.issues == []
    assert result.is_valid is True


def test_validate_source_ignores_persons_without_a_username_for_duplicates(
        ad_person, make_source, make_class):
    """Two people without a user name are two "missing", never a duplicate."""
    source = single_class_source(make_source, make_class, [
        ad_person(ad_username=None), ad_person(first_name="Josef", ad_username=None),
    ])

    result = ADValidator.validate_source(source)
    assert [i.issue_type for i in result.issues] == ["missing", "missing"]


def test_validate_source_on_an_empty_source_is_valid(make_source):
    """No classes means nothing to complain about."""
    result = ADValidator.validate_source(make_source("Empty", []))
    assert result.issues == []
    assert result.is_valid is True


def test_validate_source_on_a_source_of_empty_classes_is_valid(make_source):
    """Classes without students contribute no issues either."""
    result = ADValidator.validate_source(make_source("Empty", ["6.A", "7.B"]))
    assert result.issues == []
    assert result.is_valid is True


def test_validate_source_keeps_the_issues_in_class_and_person_order(
        ad_person, make_source, make_class):
    """Issues arrive grouped per person, in the order the source lists them."""
    broken = ad_person(first_name="", ad_username=None)
    fine = ad_person(first_name="Josef", ad_username="novakjos")
    source = make_source("AD source", [])
    source.add_class(make_class("6.A", persons=[fine]))
    source.add_class(make_class("7.B", persons=[broken]))

    issues = ADValidator.validate_source(source).issues
    assert all(i.person is broken for i in issues)
    assert fields_of(issues) == ["first_name", "ad_username"]


def test_validate_source_reports_two_field_identical_persons_separately(
        make_source, make_class, make_person):
    """Twin records compare equal yet must be validated (and counted) twice."""
    twins = [make_person(first_name="", last_name="Novák") for _ in range(2)]
    source = single_class_source(make_source, make_class, twins)

    result = ADValidator.validate_source(source)
    per_person = {id(i.person) for i in result.issues}

    assert len(per_person) == 2
    assert len([i for i in result.issues if i.field == "first_name"]) == 2


# ===========================================================================
# ADValidator.check_missing_properties
# ===========================================================================

def test_check_missing_properties_returns_nothing_for_a_complete_person(ad_person):
    """Every AD property set means an empty list."""
    assert ADValidator.check_missing_properties(ad_person()) == []


def test_check_missing_properties_lists_everything_in_a_fixed_order(make_person):
    """A bare person misses all four properties, always in the same order."""
    assert ADValidator.check_missing_properties(make_person()) == [
        "ad_username", "ad_password", "ad_display_name", "ad_email",
    ]


@pytest.mark.parametrize("attribute", [
    "ad_username", "ad_password", "ad_display_name", "ad_email",
])
@pytest.mark.parametrize("empty", [None, ""])
def test_check_missing_properties_reports_each_absent_property(
        ad_person, attribute, empty):
    """``None`` and the empty string both count as "not set"."""
    missing = ADValidator.check_missing_properties(ad_person(**{attribute: empty}))
    assert missing == [attribute]


def test_check_missing_properties_counts_whitespace_as_present(ad_person):
    """A blank-but-not-empty user name passes here - the format check catches it."""
    person = ad_person(ad_username="   ")
    assert ADValidator.check_missing_properties(person) == []
    assert not ADValidator.validate_person(person).is_valid


def test_check_missing_properties_does_not_touch_the_person(ad_person):
    """The check is read-only: no dirty flag, no status change."""
    person = ad_person(ad_email=None, ad_status=ADStatus.SYNC_SUCCEEDED)
    ADValidator.check_missing_properties(person)
    assert person.is_dirty() is False
    assert person.ad_status is ADStatus.SYNC_SUCCEEDED


# ===========================================================================
# ADValidator._count_distinct_persons
# ===========================================================================

def test_person_is_unhashable_so_grouping_must_use_identity(make_person):
    """Person is a dataclass with ``__eq__`` - putting one in a set raises."""
    with pytest.raises(TypeError):
        {make_person()}


def test_count_distinct_persons_of_an_empty_issue_list_is_zero():
    """No issues, nobody affected."""
    assert ADValidator._count_distinct_persons([]) == 0


def test_count_distinct_persons_counts_one_person_once(make_person):
    """Several issues about the same object count as a single person."""
    person = make_person()
    issues = [ValidationIssue(person, f, "missing", "m", "error")
              for f in ("first_name", "ad_username", "ad_password")]
    assert ADValidator._count_distinct_persons(issues) == 1


def test_count_distinct_persons_separates_field_identical_twins(make_person):
    """Two equal-but-distinct persons count as two (used to raise TypeError)."""
    twins = [make_person(), make_person()]
    assert twins[0] == twins[1]

    issues = [ValidationIssue(p, "ad_username", "missing", "m", "error")
              for p in twins]
    assert ADValidator._count_distinct_persons(issues) == 2


def test_count_distinct_persons_ignores_issues_without_a_person(make_person):
    """A person-less issue is skipped instead of being counted as somebody."""
    person = make_person()
    issues = [
        ValidationIssue(person, "ad_username", "missing", "m", "error"),
        ValidationIssue(None, "source", "missing", "m", "error"),
    ]
    assert ADValidator._count_distinct_persons(issues) == 1


def test_count_distinct_persons_of_only_person_less_issues_is_zero():
    """Nothing to group means zero, not one."""
    issues = [ValidationIssue(None, "source", "missing", "m", "error")]
    assert ADValidator._count_distinct_persons(issues) == 0


# ===========================================================================
# ADValidator.analyze_source
# ===========================================================================

@pytest.mark.integration
def test_analyze_source_summarises_a_mixed_source(
        ad_person, make_source, make_class):
    """Counts separate "has any issue" from "has a blocking error"."""
    clean = ad_person()
    warned = ad_person(first_name="Josef", ad_username="novakjos",
                       ad_display_name=None)
    broken = ad_person(first_name="Jakub", ad_username=None, ad_password=None,
                       ad_email=None, ad_display_name=None)
    source = single_class_source(make_source, make_class, [clean, warned, broken])

    stats = ADValidator.analyze_source(source)

    assert stats["total_persons"] == 3
    assert stats["persons_with_issues"] == 2
    assert stats["persons_with_errors"] == 1
    assert stats["total_errors"] == 2          # username + password of `broken`
    assert stats["total_warnings"] == 2        # display name of `warned` + `broken`
    assert stats["missing_username"] == 1
    assert stats["missing_password"] == 1
    assert stats["missing_email"] == 1
    assert stats["is_ready_for_sync"] is False


def test_analyze_source_of_a_clean_source_is_ready_for_sync(
        ad_person, make_source, make_class):
    """Everything filled in - zero issues and the sync gate is open."""
    source = single_class_source(make_source, make_class, [
        ad_person(), ad_person(first_name="Josef", ad_username="novakjos")])

    stats = ADValidator.analyze_source(source)

    assert stats["persons_with_issues"] == 0
    assert stats["persons_with_errors"] == 0
    assert stats["is_ready_for_sync"] is True


def test_analyze_source_of_an_empty_source_reports_zeros(make_source):
    """An empty source is trivially ready for sync."""
    stats = ADValidator.analyze_source(make_source("Empty", []))

    assert stats["total_persons"] == 0
    assert stats["persons_with_issues"] == 0
    assert stats["total_errors"] == 0
    assert stats["is_ready_for_sync"] is True


def test_analyze_source_counts_field_identical_persons_as_two(
        make_source, make_class, make_person):
    """Two twin records must be two affected persons, not one - and not a crash."""
    twins = [make_person(first_name="Jan", last_name="Novák", class_name="6.A")
             for _ in range(2)]
    source = single_class_source(make_source, make_class, twins)

    stats = ADValidator.analyze_source(source)

    assert stats["total_persons"] == 2
    assert stats["persons_with_issues"] == 2
    assert stats["persons_with_errors"] == 2
    assert stats["missing_username"] == 2


def test_analyze_source_counts_a_warning_only_person_as_affected_but_not_failing(
        ad_person, make_source, make_class):
    """A missing display name makes somebody "with issues" but not "with errors"."""
    source = single_class_source(
        make_source, make_class, [ad_person(ad_display_name=None)])

    stats = ADValidator.analyze_source(source)

    assert stats["persons_with_issues"] == 1
    assert stats["persons_with_errors"] == 0
    assert stats["is_ready_for_sync"] is True


def test_analyze_source_returns_the_validation_result_it_counted(
        ad_person, make_source, make_class):
    """The embedded result is the one the numbers were derived from."""
    source = single_class_source(
        make_source, make_class, [ad_person(ad_username=None)])

    stats = ADValidator.analyze_source(source)
    validation = stats["validation_result"]

    assert isinstance(validation, ValidationResult)
    assert len(validation.errors) == stats["total_errors"]
    assert len(validation.warnings) == stats["total_warnings"]
    assert validation.is_valid == stats["is_ready_for_sync"]


# ===========================================================================
# SyncPlanner._has_required_ad_data / _prepare_creation_attributes
# ===========================================================================

@pytest.fixture
def planner():
    """A SyncPlanner over a fake client and a detector that finds no conflict."""
    client = FakeADClient()
    detector = StubDetector(None)
    planner = SyncPlanner(client, detector)
    planner.test_client = client
    planner.test_detector = detector
    return planner


@pytest.mark.parametrize("overrides, expected", [
    ({}, True),
    ({"first_name": ""}, False),
    ({"last_name": ""}, False),
    ({"ad_username": None}, False),
    ({"ad_password": None}, False),
    ({"ad_username": ""}, False),
    ({"ad_password": ""}, False),
    ({"ad_email": None, "ad_display_name": None}, True),   # both optional
    ({"class_name": ""}, True),                            # not part of the check
])
def test_has_required_ad_data_demands_names_username_and_password(
        planner, ad_person, overrides, expected):
    """Only the four creation essentials are required, and empty counts as absent."""
    assert planner._has_required_ad_data(ad_person(**overrides)) is expected


def test_prepare_creation_attributes_maps_every_populated_field(planner, ad_person):
    """Populated optional fields become ``mail`` and ``description``."""
    attrs = planner._prepare_creation_attributes(
        ad_person(ad_description="Student 6.A"))

    assert attrs["givenName"] == "Jan"
    assert attrs["sn"] == "Novák"
    assert attrs["sAMAccountName"] == "novakjan"
    assert attrs["displayName"] == "Jan Novák"
    assert attrs["mail"] == "jan.novak@skola.cz"
    assert attrs["description"] == "Student 6.A"


def test_prepare_creation_attributes_omits_absent_optional_fields(planner, ad_person):
    """Without e-mail or description the attributes simply do not appear."""
    attrs = planner._prepare_creation_attributes(
        ad_person(ad_email=None, ad_description=None))

    assert "mail" not in attrs
    assert "description" not in attrs


def test_prepare_creation_attributes_falls_back_to_the_full_name(planner, ad_person):
    """A missing display name is replaced by "first last"."""
    attrs = planner._prepare_creation_attributes(ad_person(ad_display_name=None))
    assert attrs["displayName"] == "Jan Novák"


# ===========================================================================
# SyncPlanner.create_sync_plan
# ===========================================================================

def test_create_sync_plan_schedules_a_complete_new_person_for_creation(
        planner, ad_person, make_source, make_class):
    """An unknown, complete person becomes one CreateOperation in her class OU."""
    person = ad_person()
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)

    assert len(plan.create_operations) == 1
    operation = plan.create_operations[0]
    assert operation.person is person
    assert operation.target_ou == f"OU=Trida-6.A,{BASE_DN}"
    assert operation.attributes["sAMAccountName"] == "novakjan"
    assert plan.update_operations == []
    assert plan.incomplete_persons == []


def test_create_sync_plan_parks_an_incomplete_person_instead_of_creating_her(
        planner, ad_person, make_source, make_class):
    """Without a password there is nothing to create - she needs user action."""
    person = ad_person(ad_password=None)
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)

    assert plan.create_operations == []
    assert plan.incomplete_persons == [person]
    assert plan.statistics["requires_user_action"] == 1


@pytest.mark.parametrize("status", [ADStatus.UNKNOWN, ADStatus.NOT_FOUND_IN_AD])
def test_create_sync_plan_treats_unknown_and_absent_persons_as_creations(
        planner, ad_person, make_source, make_class, status):
    """Both "never looked" and "confirmed absent" lead to a create."""
    person = ad_person(ad_status=status, ad_dn="CN=Jan,DC=stale")
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)
    assert [op.person for op in plan.create_operations] == [person]


def test_create_sync_plan_updates_a_dirty_person_that_exists_in_ad(
        planner, ad_person, make_source, make_class):
    """EXISTS_IN_AD plus local edits gives an UpdateOperation with those edits."""
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn="CN=Jan,DC=skola")
    person.ad_email = "novy@skola.cz"
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)

    assert plan.create_operations == []
    assert len(plan.update_operations) == 1
    operation = plan.update_operations[0]
    assert operation.person is person
    assert operation.changes == {"ad_email": "novy@skola.cz"}
    assert operation.has_conflict is False
    assert operation.conflict_info is None


def test_create_sync_plan_skips_an_unchanged_person_that_exists_in_ad(
        planner, ad_person, make_source, make_class):
    """Nothing dirty means no update operation, even for a known AD user."""
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn="CN=Jan,DC=skola")
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)

    assert plan.update_operations == []
    assert plan.create_operations == []


def test_create_sync_plan_skips_a_clean_synced_person_entirely(
        planner, ad_person, make_source, make_class):
    """A clean SYNCED person is short-circuited before the conflict detector runs."""
    person = ad_person(ad_status=ADStatus.SYNC_SUCCEEDED, ad_dn="CN=Jan,DC=skola")
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)

    assert plan.statistics["total_operations"] == 0
    assert plan.incomplete_persons == []
    assert planner.test_detector.seen == []
    assert planner.test_client.calls == []


def test_create_sync_plan_creates_a_person_that_claims_ad_but_has_no_dn(
        planner, ad_person, make_source, make_class):
    """Without a DN there is nothing to modify, so the person is (re)created."""
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn=None)
    person.ad_email = "novy@skola.cz"
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)

    assert [op.person for op in plan.create_operations] == [person]
    assert plan.update_operations == []


def test_create_sync_plan_marks_the_operation_when_the_detector_finds_a_conflict(
        ad_person, make_source, make_class):
    """A ConflictInfo from the detector is carried into the UpdateOperation."""
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn="CN=Jan,DC=skola")
    person.ad_email = "novy@skola.cz"
    conflict = make_conflict_info(person)
    planner = SyncPlanner(FakeADClient(), StubDetector(conflict))
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)

    operation = plan.update_operations[0]
    assert operation.has_conflict is True
    assert operation.conflict_info is conflict
    assert plan.statistics["conflicts"] == 1


@pytest.mark.integration
def test_create_sync_plan_walks_every_class_in_order(
        planner, ad_person, make_source, make_class):
    """All classes contribute, and each person lands in exactly one bucket."""
    new = ad_person(class_name="6.A")
    incomplete = ad_person(first_name="Josef", class_name="6.A", ad_password=None)
    existing = ad_person(first_name="Jakub", class_name="7.B",
                         ad_username="novakjak",
                         ad_status=ADStatus.MATCHES_AD, ad_dn="CN=Jakub,DC=skola")
    existing.ad_description = "sedmák"

    source = make_source("AD source", [])
    source.add_class(make_class("6.A", persons=[new, incomplete]))
    source.add_class(make_class("7.B", persons=[existing]))

    plan = planner.create_sync_plan(source, BASE_DN)

    assert [op.person for op in plan.create_operations] == [new]
    assert plan.incomplete_persons == [incomplete]
    assert [op.person for op in plan.update_operations] == [existing]
    assert plan.statistics == {
        "total_operations": 2, "creates": 1, "updates": 1,
        "conflicts": 0, "incomplete": 1, "requires_user_action": 1,
    }


def test_create_sync_plan_of_an_empty_source_is_an_empty_plan(planner, make_source):
    """No classes, no operations - and the fake client stays untouched."""
    plan = planner.create_sync_plan(make_source("Empty", []), BASE_DN)

    assert plan.statistics["total_operations"] == 0
    assert planner.test_client.calls == []


@pytest.mark.bug
def test_create_sync_plan_plans_an_update_for_an_edited_already_synced_person(
        planner, ad_person, make_source, make_class):
    """Editing a SYNCED person (-> UPDATE_PENDING) must still reach Active Directory."""
    person = ad_person(ad_status=ADStatus.SYNC_SUCCEEDED, ad_dn="CN=Jan,DC=skola")
    person.ad_email = "novy@skola.cz"
    assert person.ad_status is ADStatus.DIFFERS_FROM_AD     # set by the model
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)

    assert [op.person for op in plan.update_operations] == [person]


@pytest.mark.bug
def test_create_sync_plan_never_creates_an_ambiguous_person(
        planner, ad_person, make_source, make_class):
    """A person matching several AD accounts must not get yet another one."""
    person = ad_person(ad_status=ADStatus.MULTIPLE_AD_MATCHES)
    source = single_class_source(make_source, make_class, [person])

    plan = planner.create_sync_plan(source, BASE_DN)

    assert not any(op.person is person for op in plan.create_operations)


@pytest.mark.bug
def test_create_sync_plan_derives_the_upn_suffix_from_the_base_dn(
        planner, ad_person, make_source, make_class):
    """The UPN must live in the domain we are writing to, not in "domain.local"."""
    source = single_class_source(make_source, make_class, [ad_person()])

    plan = planner.create_sync_plan(source, "OU=Students,DC=skola,DC=local")

    assert plan.create_operations[0].attributes["userPrincipalName"] == \
        "novakjan@skola.local"


@pytest.mark.bug
def test_create_sync_plan_escapes_a_class_name_containing_a_comma(
        planner, ad_person, make_source, make_class):
    """A comma in the class name must not split the OU into two components."""
    person = ad_person(class_name="6,A")
    source = single_class_source(make_source, make_class, [person], class_name="6,A")

    plan = planner.create_sync_plan(source, "DC=skola,DC=local")

    assert rdn_count(plan.create_operations[0].target_ou) == 3


@pytest.mark.parametrize("value", [
    "6.A", "6,A", "Novák, Jan", "a+b", "a=b", 'q"x', "a;b", "<a>",
    "#první", " mezera", "mezera ", " ", "  ",
    "zpetne\\lomitko", "konec\\", "konec\\ ",
])
def test_an_escaped_dn_value_is_one_rdn_and_reads_back_unchanged(value):
    """Whatever is escaped into an RDN must come out of it unchanged."""
    escaped = escape_dn_value(value)
    dn = f"OU={escaped},DC=skola,DC=local"

    assert unescape_dn_value(escaped) == value
    assert first_rdn_value(dn) == value          # the name create_ou is given
    assert trailing_space_is_escaped(escaped)    # a server strips a bare one
    assert escaped[:1] not in ('#', ' ')         # a bare lead is significant


# ===========================================================================
# ConflictDetector.check_conflict
# ===========================================================================

@pytest.fixture
def synced_person(ad_person):
    """A person known to AD, with a recorded version and AD snapshot."""
    person = ad_person(
        ad_status=ADStatus.MATCHES_AD,
        ad_dn="CN=Jan Novák,OU=Trida-6.A,DC=skola,DC=local",
        ad_version="20240101000000.0Z",
        metadata={"ad_current_values": {
            "mail": "jan.novak@skola.cz", "sn": "Novák",
            "displayName": "Jan Novák",
        }},
    )
    return person


def test_check_conflict_returns_none_for_a_person_without_local_changes(synced_person):
    """Nothing changed locally - AD is never even queried."""
    client = FakeADClient()
    assert ConflictDetector(client).check_conflict(synced_person) is None
    assert client.calls == []


def test_check_conflict_returns_none_for_a_person_without_a_dn(ad_person):
    """A person that was never matched to an AD object cannot conflict."""
    person = ad_person(ad_dn=None)
    person.ad_email = "novy@skola.cz"
    client = FakeADClient()

    assert ConflictDetector(client).check_conflict(person) is None
    assert client.calls == []


def test_check_conflict_returns_none_when_the_entry_disappeared(synced_person):
    """A DN that no longer resolves is reported as "no conflict", not a crash."""
    synced_person.ad_email = "novy@skola.cz"
    client = FakeADClient(users={})

    assert ConflictDetector(client).check_conflict(synced_person) is None
    assert client.kinds() == ["get_user"]


def test_check_conflict_returns_none_when_ad_was_not_touched(synced_person):
    """Same modifyTimestamp as at discovery time - the local edit is safe."""
    synced_person.ad_email = "novy@skola.cz"
    entry = FakeEntry(synced_person.ad_dn, {
        "mail": "jan.novak@skola.cz", "modifyTimestamp": "20240101000000.0Z"})
    client = FakeADClient(users={synced_person.ad_dn: entry})

    assert ConflictDetector(client).check_conflict(synced_person) is None


def test_check_conflict_returns_none_when_the_person_has_no_recorded_version(
        synced_person):
    """Without a baseline version there is nothing to compare against."""
    synced_person.ad_version = None
    synced_person.ad_email = "novy@skola.cz"
    entry = FakeEntry(synced_person.ad_dn, {
        "mail": "someone.else@skola.cz", "modifyTimestamp": "20240202000000.0Z"})
    client = FakeADClient(users={synced_person.ad_dn: entry})

    assert ConflictDetector(client).check_conflict(synced_person) is None


def test_check_conflict_ignores_remote_edits_to_untouched_fields(synced_person):
    """AD changed the surname, we changed the e-mail - no overlap, no conflict."""
    synced_person.ad_email = "novy@skola.cz"
    entry = FakeEntry(synced_person.ad_dn, {
        "mail": "jan.novak@skola.cz", "sn": "Novotný",
        "modifyTimestamp": "20240202000000.0Z"})
    client = FakeADClient(users={synced_person.ad_dn: entry})

    assert ConflictDetector(client).check_conflict(synced_person) is None


def test_check_conflict_reports_the_field_changed_on_both_sides(synced_person):
    """The overlapping field is the conflict; the rest stays applicable."""
    synced_person.ad_email = "novy@skola.cz"
    synced_person.last_name = "Nováková"
    entry = FakeEntry(synced_person.ad_dn, {
        "mail": "admin@skola.cz", "sn": "Novák",
        "modifyTimestamp": "20240202000000.0Z"})
    client = FakeADClient(users={synced_person.ad_dn: entry})

    conflict = ConflictDetector(client).check_conflict(synced_person)

    assert isinstance(conflict, ConflictInfo)
    assert conflict.person is synced_person
    assert conflict.conflicting_fields == {"ad_email"}
    assert conflict.remote_changes == {"ad_email": "admin@skola.cz"}
    assert conflict.non_conflicting_local == {"last_name": "Nováková"}
    assert conflict.local_changes["ad_email"] == "novy@skola.cz"
    assert conflict.ad_modified_time == "20240202000000.0Z"
    assert conflict.current_ad_state is entry.attributes


def test_check_conflict_ignores_fields_absent_from_the_fresh_entry(synced_person):
    """An attribute AD does not return at all cannot be a remote change."""
    synced_person.ad_email = "novy@skola.cz"
    entry = FakeEntry(synced_person.ad_dn,
                      {"modifyTimestamp": "20240202000000.0Z"})
    client = FakeADClient(users={synced_person.ad_dn: entry})

    assert ConflictDetector(client).check_conflict(synced_person) is None


def test_check_conflict_swallows_client_errors_and_reports_no_conflict(synced_person):
    """An LDAP failure must not abort the whole plan."""
    synced_person.ad_email = "novy@skola.cz"
    client = FakeADClient()
    client.get_user_error = RuntimeError("connection reset")

    assert ConflictDetector(client).check_conflict(synced_person) is None


@pytest.mark.parametrize("field_name, expected", [
    ("first_name", "givenName"),
    ("last_name", "sn"),
    ("ad_username", "sAMAccountName"),
    ("ad_email", "mail"),
    ("ad_display_name", "displayName"),
    ("ad_description", "description"),
    ("home_directory", "home_directory"),      # unknown -> passed through
    ("", ""),
])
def test_map_to_ad_attribute_translates_known_fields_and_passes_the_rest(
        field_name, expected):
    """The detector's mapping falls back to the field name itself."""
    assert ConflictDetector(FakeADClient())._map_to_ad_attribute(field_name) == expected


# ===========================================================================
# ConflictResolver
# ===========================================================================

@pytest.fixture
def resolver():
    """The (stateless) conflict resolver."""
    return ConflictResolver()


def test_resolve_local_wins_keeps_every_local_change(resolver, make_person):
    """LOCAL_WINS overwrites AD with all local edits and skips nothing."""
    info = make_conflict_info(make_person())
    resolution = resolver.resolve(info, ConflictStrategy.LOCAL_WINS)

    assert isinstance(resolution, ConflictResolution)
    assert resolution.merged_changes == info.local_changes
    assert resolution.skipped_fields == set()


def test_resolve_local_wins_returns_a_copy_not_the_original_dict(resolver, make_person):
    """Mutating the resolution must not corrupt the stored conflict."""
    info = make_conflict_info(make_person())
    resolution = resolver.resolve(info, ConflictStrategy.LOCAL_WINS)

    resolution.merged_changes["ad_email"] = "tampered@skola.cz"
    assert info.local_changes["ad_email"] == "local@skola.cz"


def test_resolve_remote_wins_discards_every_local_change(resolver, make_person):
    """REMOTE_WINS writes nothing and records all local fields as skipped."""
    info = make_conflict_info(make_person())
    resolution = resolver.resolve(info, ConflictStrategy.REMOTE_WINS)

    assert resolution.merged_changes == {}
    assert resolution.skipped_fields == {"ad_email", "last_name"}


def test_resolve_auto_merge_refuses_when_a_field_really_conflicts(resolver, make_person):
    """AUTO_MERGE gives up (returns None) as soon as a field clashes."""
    info = make_conflict_info(make_person())
    assert resolver.resolve(info, ConflictStrategy.AUTO_MERGE) is None


def test_resolve_auto_merge_applies_everything_when_nothing_clashes(
        resolver, make_person):
    """Without conflicting fields AUTO_MERGE behaves like LOCAL_WINS."""
    info = make_conflict_info(
        make_person(), local={"ad_description": "6.A"}, remote={},
        conflicting=set(), non_conflicting={"ad_description": "6.A"})

    resolution = resolver.resolve(info, ConflictStrategy.AUTO_MERGE)

    assert resolution.merged_changes == {"ad_description": "6.A"}
    assert resolution.skipped_fields == set()


@pytest.mark.parametrize("choices", [None, {}])
def test_resolve_user_prompt_without_answers_skips_the_person(
        resolver, make_person, choices):
    """USER_PROMPT needs answers; without them the person is left alone."""
    info = make_conflict_info(make_person())
    assert resolver.resolve(info, ConflictStrategy.USER_PROMPT, choices) is None


def test_resolve_user_prompt_local_choice_applies_the_local_value(
        resolver, make_person):
    """"local" writes our value and keeps the non-conflicting change too."""
    info = make_conflict_info(make_person())
    resolution = resolver.resolve(
        info, ConflictStrategy.USER_PROMPT, {"ad_email": "local"})

    assert resolution.merged_changes == {
        "last_name": "Nováková", "ad_email": "local@skola.cz"}
    assert resolution.skipped_fields == set()


def test_resolve_user_prompt_remote_choice_leaves_the_field_untouched(
        resolver, make_person):
    """"remote" means "do not write this field" - it is reported as skipped."""
    info = make_conflict_info(make_person())
    resolution = resolver.resolve(
        info, ConflictStrategy.USER_PROMPT, {"ad_email": "remote"})

    assert resolution.merged_changes == {"last_name": "Nováková"}
    assert resolution.skipped_fields == {"ad_email"}


def test_resolve_user_prompt_custom_choice_uses_the_typed_value(resolver, make_person):
    """A "custom:" answer wins over both sides, prefix stripped."""
    info = make_conflict_info(make_person())
    resolution = resolver.resolve(
        info, ConflictStrategy.USER_PROMPT, {"ad_email": "custom:jn@skola.cz"})

    assert resolution.merged_changes["ad_email"] == "jn@skola.cz"
    assert resolution.skipped_fields == set()


def test_resolve_user_prompt_custom_choice_may_be_empty(resolver, make_person):
    """"custom:" with nothing behind it clears the attribute rather than skipping."""
    info = make_conflict_info(make_person())
    resolution = resolver.resolve(
        info, ConflictStrategy.USER_PROMPT, {"ad_email": "custom:"})

    assert resolution.merged_changes["ad_email"] == ""
    assert resolution.skipped_fields == set()


@pytest.mark.parametrize("answer", ["", "whatever", "CUSTOM:x", "Local"])
def test_resolve_user_prompt_skips_fields_with_an_unusable_answer(
        resolver, make_person, answer):
    """Anything that is not local/remote/custom: leaves the field untouched."""
    info = make_conflict_info(make_person())
    resolution = resolver.resolve(
        info, ConflictStrategy.USER_PROMPT,
        {"ad_email": answer, "last_name": "local"})

    assert "ad_email" not in resolution.merged_changes
    assert resolution.skipped_fields == {"ad_email"}


def test_resolve_user_prompt_skips_a_conflicting_field_nobody_answered(
        resolver, make_person):
    """An answer for another field does not accidentally apply this one."""
    info = make_conflict_info(make_person())
    resolution = resolver.resolve(
        info, ConflictStrategy.USER_PROMPT, {"first_name": "local"})

    assert resolution.merged_changes == {"last_name": "Nováková"}
    assert resolution.skipped_fields == {"ad_email"}


def test_resolve_user_prompt_handles_several_conflicts_independently(
        resolver, make_person):
    """Each conflicting field follows its own answer."""
    info = make_conflict_info(
        make_person(),
        local={"ad_email": "l@skola.cz", "ad_display_name": "Jan N.",
               "ad_description": "6.A"},
        remote={"ad_email": "r@skola.cz", "ad_display_name": "Jan Novak"},
    )

    resolution = resolver.resolve(
        info, ConflictStrategy.USER_PROMPT,
        {"ad_email": "local", "ad_display_name": "remote"})

    assert resolution.merged_changes == {
        "ad_description": "6.A", "ad_email": "l@skola.cz"}
    assert resolution.skipped_fields == {"ad_display_name"}


def test_resolve_rejects_a_strategy_that_is_not_the_enum(resolver, make_person):
    """The bare string value of a strategy is not accepted - nothing is written."""
    info = make_conflict_info(make_person())
    assert resolver.resolve(info, "local_wins") is None


# ===========================================================================
# PersonsSyncService._build_uac_flags / _map_field_to_ldap
# ===========================================================================

@pytest.fixture
def sync_service():
    """A sync service over a fake client and a real resolver."""
    client = FakeADClient()
    service = PersonsSyncService(client, ConflictResolver())
    service.test_client = client
    return service


@pytest.mark.parametrize("enabled, never_expires, expected", [
    (True, False, 0x0200),                     # 512   NORMAL_ACCOUNT
    (False, False, 0x0200 | 0x0002),           # 514   + ACCOUNTDISABLE
    (True, True, 0x0200 | 0x10000),            # 66048 + DONT_EXPIRE_PASSWORD
    (False, True, 0x0200 | 0x0002 | 0x10000),  # 66050 both
])
def test_build_uac_flags_covers_every_combination(
        sync_service, ad_person, enabled, never_expires, expected):
    """NORMAL_ACCOUNT is always set; the other two bits follow the person."""
    person = ad_person(account_enabled=enabled,
                       password_never_expires=never_expires)
    assert sync_service._build_uac_flags(person) == expected


def test_build_uac_flags_ignores_the_cannot_change_password_setting(
        sync_service, ad_person):
    """``password_cannot_change`` is an ACL, not a UAC bit - flags stay at 512."""
    person = ad_person(account_enabled=True, password_cannot_change=True)
    assert sync_service._build_uac_flags(person) == 0x0200


def test_build_uac_flags_disables_a_default_person(sync_service, make_person):
    """``account_enabled`` defaults to False, so a fresh person is created disabled."""
    assert sync_service._build_uac_flags(make_person()) == 514


@pytest.mark.parametrize("field_name, expected", [
    ("first_name", "givenName"),
    ("last_name", "sn"),
    ("ad_username", "sAMAccountName"),
    ("ad_email", "mail"),
    ("ad_display_name", "displayName"),
    ("ad_description", "description"),
    ("ad_password", None),
    ("class_name", None),
    ("group_memberships", None),
    ("unknown_field", None),
])
def test_map_field_to_ldap_returns_none_for_unmapped_fields(
        sync_service, field_name, expected):
    """Unlike the detector's mapping, an unknown field yields None here."""
    assert sync_service._map_field_to_ldap(field_name) == expected


# ===========================================================================
# PersonsSyncService.execute_sync
# ===========================================================================

@pytest.fixture
def no_group_service(monkeypatch):
    """Replace ADGroupService so group sync never reaches the (fake) client."""
    import services.ad_services as ad_services

    class RecordingGroupService:
        instances = []

        def __init__(self, client):
            self.client = client
            RecordingGroupService.instances.append(self)

        def sync_user_groups(self, person):
            RecordingGroupService.synced.append(person)
            if RecordingGroupService.raises:
                raise RuntimeError("group server down")
            return True

    RecordingGroupService.instances = []
    RecordingGroupService.synced = []
    RecordingGroupService.raises = False
    monkeypatch.setattr(ad_services, "ADGroupService", RecordingGroupService)
    return RecordingGroupService


@pytest.mark.integration
def test_execute_sync_creates_the_user_sets_the_password_and_marks_it_synced(
        sync_service, ad_person, no_group_service):
    """The happy path: OU, user, password, then a fresh version from AD."""
    person = ad_person(account_enabled=True)
    client = sync_service.test_client
    target_ou = f"OU=Trida-6.A,{BASE_DN}"
    plan = SyncPlan(create_operations=[CreateOperation(
        person, target_ou,
        SyncPlanner(client, StubDetector())._prepare_creation_attributes(person))])

    result = sync_service.execute_sync(plan)

    assert result.statistics == {"total": 1, "successful": 1, "failed": 0,
                                 "skipped": 0}
    assert client.created_ous == [(target_ou, "Trida-6.A")]
    dn, attributes = client.created_users[0]
    assert dn == f"CN=Jan Novák,{target_ou}"
    assert attributes["userAccountControl"] == "512"
    # The UTF-16-LE encoding is ldap3's job now (extend.microsoft.modify_password);
    # the service only has to delegate, over an encrypted channel.
    assert client.passwords_set == [(dn, GOOD_PASSWORD)]
    assert person.ad_dn == dn
    assert person.ad_status is ADStatus.SYNC_SUCCEEDED
    assert person.is_dirty() is False
    assert person.ad_version == client.timestamp
    assert result.duration >= 0


def test_execute_sync_does_not_recreate_an_existing_ou(
        sync_service, ad_person, no_group_service):
    """An OU that is already there is used as is."""
    target_ou = f"OU=Trida-6.A,{BASE_DN}"
    client = sync_service.test_client
    client.existing_ous.add(target_ou)
    plan = SyncPlan(create_operations=[
        CreateOperation(ad_person(), target_ou, {"sAMAccountName": "novakjan"})])

    sync_service.execute_sync(plan)

    assert client.created_ous == []
    assert "create_ou" not in client.kinds()


def test_execute_sync_fails_the_person_when_the_ou_cannot_be_created(
        sync_service, ad_person, no_group_service):
    """No OU means no user - and the failure names the OU."""
    client = sync_service.test_client
    client.create_ou_result = False
    target_ou = f"OU=Trida-6.A,{BASE_DN}"
    person = ad_person()
    plan = SyncPlan(create_operations=[
        CreateOperation(person, target_ou, {"sAMAccountName": "novakjan"})])

    result = sync_service.execute_sync(plan)

    assert result.statistics["failed"] == 1
    assert target_ou in result.failed[0].message
    assert client.created_users == []
    assert person.ad_dn is None


def test_execute_sync_reports_a_refused_creation_as_failed(
        sync_service, ad_person, no_group_service):
    """``create_user`` returning False is a failure, not an exception."""
    client = sync_service.test_client
    client.create_user_result = False
    person = ad_person()
    plan = SyncPlan(create_operations=[
        CreateOperation(person, f"OU=Trida-6.A,{BASE_DN}", {})])

    result = sync_service.execute_sync(plan)

    assert [r.status for r in result.failed] == ["FAILED"]
    assert result.failed[0].message == "Failed to create user in AD"
    assert person.ad_status is not ADStatus.SYNC_SUCCEEDED


def test_execute_sync_turns_a_client_exception_into_a_failed_result(
        sync_service, ad_person, no_group_service):
    """The raised exception is attached to the result instead of escaping."""
    client = sync_service.test_client
    boom = RuntimeError("LDAP server is down")

    def explode(dn, attributes):
        raise boom

    client.create_user = explode
    plan = SyncPlan(create_operations=[
        CreateOperation(ad_person(), f"OU=Trida-6.A,{BASE_DN}", {})])

    result = sync_service.execute_sync(plan)

    assert result.failed[0].error is boom
    assert result.failed[0].message == "LDAP server is down"


def test_execute_sync_reports_a_created_user_whose_password_failed(
        sync_service, ad_person, no_group_service):
    """The account exists, so the operation counts as a (partial) success."""
    client = sync_service.test_client
    client.set_password_error = RuntimeError("unwillingToPerform")
    person = ad_person()
    plan = SyncPlan(create_operations=[
        CreateOperation(person, f"OU=Trida-6.A,{BASE_DN}", {})])

    result = sync_service.execute_sync(plan)

    assert result.statistics["successful"] == 1
    assert "password not set" in result.successful[0].message
    assert client.created_users


def test_execute_sync_sets_pwd_last_set_when_the_password_must_change(
        sync_service, ad_person, no_group_service):
    """``password_must_change`` adds the pwdLastSet=0 modification."""
    person = ad_person(password_must_change=True)
    plan = SyncPlan(create_operations=[
        CreateOperation(person, f"OU=Trida-6.A,{BASE_DN}", {})])

    sync_service.execute_sync(plan)

    assert sync_service.test_client.ops_for("pwdLastSet") == [
        (MODIFY_REPLACE, "pwdLastSet", ["0"])]


def test_execute_sync_keeps_a_created_user_when_group_sync_explodes(
        sync_service, ad_person, no_group_service):
    """A failing group service is logged, never rolled back onto the user."""
    no_group_service.raises = True
    person = ad_person()
    person.add_to_group(ADGroup(name="Zaci", dn="CN=Zaci,DC=skola,DC=local"))
    plan = SyncPlan(create_operations=[
        CreateOperation(person, f"OU=Trida-6.A,{BASE_DN}", {})])

    result = sync_service.execute_sync(plan)

    assert result.statistics["successful"] == 1
    assert no_group_service.synced == [person]
    # The account exists, so this is not a failure - but the groups are still
    # outstanding, so claiming a clean synchronisation would hide real work.
    assert person.ad_status is ADStatus.SYNC_INCOMPLETE
    assert "groups not applied" in result.successful[0].message


def test_execute_sync_skips_group_sync_for_a_person_without_groups(
        sync_service, ad_person, no_group_service):
    """No memberships, no group service instance at all."""
    plan = SyncPlan(create_operations=[
        CreateOperation(ad_person(), f"OU=Trida-6.A,{BASE_DN}", {})])

    sync_service.execute_sync(plan)

    assert no_group_service.instances == []


def test_set_password_rejects_an_empty_password(sync_service, ad_person):
    """An empty password is refused before anything is sent to AD."""
    with pytest.raises(ValueError, match="empty"):
        sync_service._set_password("CN=Jan,DC=skola", "", ad_person())
    assert sync_service.test_client.modifications == []


def test_set_password_raises_when_the_client_refuses_the_change(
        sync_service, ad_person):
    """A refusal from the client propagates to the caller."""
    sync_service.test_client.set_password_error = RuntimeError(
        "Failed to set the password: unwillingToPerform")
    with pytest.raises(RuntimeError, match="unwillingToPerform"):
        sync_service._set_password("CN=Jan,DC=skola", GOOD_PASSWORD, ad_person())


def test_set_password_refuses_an_unencrypted_channel(sync_service, ad_person):
    """AD will not set a password over plain LDAP - say so instead of failing
    with an opaque 'unwillingToPerform' from the server."""
    client = sync_service.test_client
    client.is_secure = False
    client.set_password_error = RuntimeError(
        "AD refuses a password change over an unencrypted connection")
    with pytest.raises(RuntimeError, match="unencrypted"):
        sync_service._set_password("CN=Jan,DC=skola", GOOD_PASSWORD, ad_person())


def test_set_password_passes_unicode_passwords_through_unchanged(
        sync_service, ad_person):
    """The service must not mangle the password; encoding belongs to ldap3."""
    sync_service._set_password("CN=Jan,DC=skola", "Přílíš1!", ad_person())

    assert sync_service.test_client.passwords_set == [("CN=Jan,DC=skola", "Přílíš1!")]


def test_set_password_still_applies_the_must_change_flag(sync_service, ad_person):
    """pwdLastSet=0 forces a change at next logon and must survive the move."""
    person = ad_person(password_must_change=True)
    sync_service._set_password("CN=Jan,DC=skola", GOOD_PASSWORD, person)

    assert sync_service.test_client.ops_for("pwdLastSet") == [
        (MODIFY_REPLACE, "pwdLastSet", ["0"])]


def test_a_failed_must_change_flag_does_not_undo_the_password(
        sync_service, ad_person):
    """The password is already set; a failing flag must not look like failure."""
    sync_service.test_client.modify_results = {"pwdLastSet": False}
    sync_service._set_password("CN=Jan,DC=skola", GOOD_PASSWORD,
                               ad_person(password_must_change=True))

    assert sync_service.test_client.passwords_set == [("CN=Jan,DC=skola", GOOD_PASSWORD)]


@pytest.mark.integration
def test_execute_sync_updates_mapped_attributes_and_clears_the_dirty_state(
        sync_service, ad_person, no_group_service):
    """A changed e-mail becomes one MODIFY_REPLACE on ``mail``."""
    dn = "CN=Jan,OU=Trida-6.A,DC=skola,DC=local"
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn=dn)
    person.ad_email = "novy@skola.cz"
    client = sync_service.test_client
    client.users[dn] = FakeEntry(dn, {"mail": "novy@skola.cz",
                                      "modifyTimestamp": "20240303000000.0Z"})
    plan = SyncPlan(update_operations=[
        UpdateOperation(person=person, changes=person.get_changes())])

    result = sync_service.execute_sync(plan)

    assert result.statistics == {"total": 1, "successful": 1, "failed": 0,
                                 "skipped": 0}
    assert client.ops_for("mail") == [(MODIFY_REPLACE, "mail", ["novy@skola.cz"])]
    assert person.ad_status is ADStatus.SYNC_SUCCEEDED
    assert person.is_dirty() is False
    assert person.ad_version == "20240303000000.0Z"
    assert person.metadata["ad_current_values"]["mail"] == "novy@skola.cz"


def test_execute_sync_clears_an_attribute_with_an_empty_value_list(
        sync_service, ad_person, no_group_service):
    """Setting a field to ``None`` sends an empty value list, i.e. "delete"."""
    dn = "CN=Jan,DC=skola"
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn=dn)
    plan = SyncPlan(update_operations=[
        UpdateOperation(person=person, changes={"ad_description": None})])

    sync_service.execute_sync(plan)

    assert sync_service.test_client.ops_for("description") == [
        (MODIFY_REPLACE, "description", [])]


def test_execute_sync_skips_an_update_without_changes(
        sync_service, ad_person, no_group_service):
    """An empty change set is skipped and nothing is sent to AD."""
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn="CN=Jan,DC=skola")
    plan = SyncPlan(update_operations=[UpdateOperation(person=person, changes={})])

    result = sync_service.execute_sync(plan)

    assert result.statistics == {"total": 1, "successful": 0, "failed": 0,
                                 "skipped": 1}
    assert result.skipped[0].message == "No changes to apply"
    assert sync_service.test_client.modifications == []


def test_execute_sync_fails_an_update_the_client_refuses(
        sync_service, ad_person, no_group_service):
    """A rejected attribute write is a failure and stops the operation."""
    dn = "CN=Jan,DC=skola"
    sync_service.test_client.modify_results = {"mail": False}
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn=dn)
    plan = SyncPlan(update_operations=[
        UpdateOperation(person=person, changes={"ad_email": "novy@skola.cz"})])

    result = sync_service.execute_sync(plan)

    assert "attributes were not updated" in result.failed[0].message
    assert person.ad_status is ADStatus.SYNC_INCOMPLETE


def test_execute_sync_skips_a_conflict_nobody_answered(
        sync_service, ad_person, no_group_service):
    """USER_PROMPT without choices for that person leaves the record alone."""
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn="CN=Jan,DC=skola")
    plan = SyncPlan(update_operations=[UpdateOperation(
        person=person, changes={"ad_email": "l@skola.cz"},
        has_conflict=True, conflict_info=make_conflict_info(person))])

    result = sync_service.execute_sync(plan)

    assert result.skipped[0].message == "Conflict not resolved"
    assert sync_service.test_client.modifications == []


@pytest.mark.integration
def test_execute_sync_applies_the_user_choices_keyed_by_person_id(
        sync_service, ad_person, no_group_service):
    """Choices are looked up under ``str(id(person))`` and then applied."""
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn="CN=Jan,DC=skola")
    conflict = make_conflict_info(person)
    plan = SyncPlan(update_operations=[UpdateOperation(
        person=person, changes=dict(conflict.local_changes),
        has_conflict=True, conflict_info=conflict)])

    result = sync_service.execute_sync(
        plan, ConflictStrategy.USER_PROMPT,
        {str(id(person)): {"ad_email": "remote"}})

    assert result.statistics["successful"] == 1
    assert sync_service.test_client.ops_for("mail") == []
    assert sync_service.test_client.ops_for("sn") == [
        (MODIFY_REPLACE, "sn", ["Nováková"])]


def test_execute_sync_with_local_wins_writes_every_conflicting_field(
        sync_service, ad_person, no_group_service):
    """LOCAL_WINS needs no user input at all."""
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn="CN=Jan,DC=skola")
    conflict = make_conflict_info(person)
    plan = SyncPlan(update_operations=[UpdateOperation(
        person=person, changes=dict(conflict.local_changes),
        has_conflict=True, conflict_info=conflict)])

    result = sync_service.execute_sync(plan, ConflictStrategy.LOCAL_WINS)

    assert result.statistics["successful"] == 1
    assert sync_service.test_client.ops_for("mail") == [
        (MODIFY_REPLACE, "mail", ["local@skola.cz"])]


def test_execute_sync_updates_account_flags_for_a_policy_change(
        sync_service, ad_person, no_group_service):
    """A policy-only change goes through userAccountControl, not a plain attribute."""
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn="CN=Jan,DC=skola",
                       account_enabled=True, password_never_expires=True)
    plan = SyncPlan(update_operations=[
        UpdateOperation(person=person, changes={"account_enabled": True})])

    sync_service.execute_sync(plan)

    assert sync_service.test_client.ops_for("userAccountControl") == [
        (MODIFY_REPLACE, "userAccountControl", ["66048"])]


def test_execute_sync_counts_creates_and_updates_together(
        sync_service, ad_person, no_group_service):
    """``total_processed`` covers both phases; incomplete persons are not processed."""
    creating = ad_person()
    updating = ad_person(first_name="Josef", ad_status=ADStatus.MATCHES_AD,
                         ad_dn="CN=Josef,DC=skola")
    plan = SyncPlan(
        create_operations=[CreateOperation(creating, f"OU=Trida-6.A,{BASE_DN}", {})],
        update_operations=[UpdateOperation(updating, {"ad_email": "j@skola.cz"})],
        incomplete_persons=[ad_person(first_name="Jakub")],
    )

    result = sync_service.execute_sync(plan)

    assert result.total_processed == 2
    assert result.statistics == {"total": 2, "successful": 2, "failed": 0,
                                 "skipped": 0}


@pytest.mark.parametrize("status, bucket", [
    ("SUCCESS", "successful"),
    ("FAILED", "failed"),
    ("SKIPPED", "skipped"),
    ("anything else", "skipped"),
])
def test_categorize_result_files_every_status(sync_service, make_person, status, bucket):
    """Unknown statuses land in "skipped" rather than being dropped."""
    sync_result = SyncResult(total_processed=1)
    op_result = OperationResult(person=make_person(), status=status, message="m")

    sync_service._categorize_result(sync_result, op_result)

    assert getattr(sync_result, bucket) == [op_result]
    assert sync_result.statistics[bucket] == 1


@pytest.mark.bug
def test_execute_sync_writes_a_changed_home_directory_to_ad(
        sync_service, ad_person, no_group_service):
    """A home directory edited in the UI must reach AD, not vanish on "success"."""
    dn = "CN=Jan,DC=skola"
    person = ad_person(ad_status=ADStatus.MATCHES_AD, ad_dn=dn)
    person.home_directory = r"\\srv01\home\novakjan"
    plan = SyncPlan(update_operations=[
        UpdateOperation(person=person, changes=person.get_changes())])

    result = sync_service.execute_sync(plan)

    assert result.statistics["successful"] == 1
    assert sync_service.test_client.ops_for("homeDirectory") == [
        (MODIFY_REPLACE, "homeDirectory", [r"\\srv01\home\novakjan"])]


@pytest.mark.bug
def test_execute_sync_records_the_dn_of_a_user_created_without_a_password(
        sync_service, ad_person, no_group_service):
    """The account exists in AD, so the person must remember its DN."""
    client = sync_service.test_client
    client.set_password_error = RuntimeError("unwillingToPerform")
    person = ad_person()
    target_ou = f"OU=Trida-6.A,{BASE_DN}"
    plan = SyncPlan(create_operations=[CreateOperation(person, target_ou, {})])

    sync_service.execute_sync(plan)

    assert person.ad_dn == f"CN=Jan Novák,{target_ou}"


# ===========================================================================
# SyncPlan.statistics / SyncResult.statistics
# ===========================================================================

def test_sync_plan_statistics_of_an_empty_plan_are_all_zero():
    """A fresh plan has no operations and needs no user action."""
    assert SyncPlan().statistics == {
        "total_operations": 0, "creates": 0, "updates": 0,
        "conflicts": 0, "incomplete": 0, "requires_user_action": 0,
    }


def test_sync_plan_statistics_count_conflicts_and_incomplete_persons(make_person):
    """``requires_user_action`` is conflicts plus incomplete, not the total."""
    person = make_person()
    plan = SyncPlan(
        create_operations=[CreateOperation(person, "OU=x", {})],
        update_operations=[
            UpdateOperation(person, {"ad_email": "a"}, has_conflict=True,
                            conflict_info=make_conflict_info(person)),
            UpdateOperation(person, {"ad_email": "b"}),
        ],
        incomplete_persons=[person, person],
    )

    assert plan.statistics == {
        "total_operations": 3, "creates": 1, "updates": 2,
        "conflicts": 1, "incomplete": 2, "requires_user_action": 3,
    }


def test_sync_plan_statistics_exclude_incomplete_persons_from_the_operations(
        make_person):
    """Incomplete persons are not operations - nothing would be sent for them."""
    plan = SyncPlan(incomplete_persons=[make_person(), make_person()])
    assert plan.statistics["total_operations"] == 0
    assert plan.statistics["requires_user_action"] == 2


def test_sync_result_statistics_report_the_three_buckets(make_person):
    """``total`` is what was attempted, the buckets are what happened."""
    person = make_person()
    result = SyncResult(
        total_processed=4,
        successful=[OperationResult(person, "SUCCESS", "ok")],
        failed=[OperationResult(person, "FAILED", "no"),
                OperationResult(person, "FAILED", "no")],
        skipped=[OperationResult(person, "SKIPPED", "later")],
    )

    assert result.statistics == {"total": 4, "successful": 1, "failed": 2,
                                 "skipped": 1}


def test_sync_result_statistics_start_empty():
    """A result that processed nothing reports zeros, not None."""
    assert SyncResult(total_processed=0).statistics == {
        "total": 0, "successful": 0, "failed": 0, "skipped": 0}
    assert SyncResult(total_processed=0).duration == 0.0
