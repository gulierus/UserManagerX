"""
Tests for turning an Active Directory entry into a Person.

Version 26, point 20: the "1. Data Sources" tab requested five attributes from
the directory, so the home directory and the group memberships of a person
loaded from Active Directory were always empty.
"""

import pytest

from utils.ad_entry_mapping import (
    PERSON_ATTRIBUTES,
    UAC_ACCOUNT_DISABLED,
    UAC_DONT_EXPIRE_PASSWORD,
    attribute_int,
    attribute_text,
    attribute_values,
    group_from_dn,
    groups_from_entry,
    parent_dn,
    person_from_entry,
    rdn_value,
)


class FakeAttribute:
    """Stands in for one ldap3 attribute of an entry."""

    def __init__(self, value=None, values=None):
        self.value = value
        if values is not None:
            self.values = values


class FakeEntry:
    """
    Stands in for an ldap3 entry.

    ldap3 exposes attributes through ``entry['name']`` and raises ``KeyError``
    for anything that was not requested, which is the behaviour that matters
    here.
    """

    def __init__(self, entry_dn="", **attributes):
        self.entry_dn = entry_dn
        self._attributes = attributes

    def __getitem__(self, name):
        try:
            return self._attributes[name]
        except KeyError:
            raise KeyError(name)


def entry(**attributes):
    """Build a FakeEntry from plain Python values."""
    dn = attributes.pop("entry_dn", "")
    wrapped = {}
    for name, value in attributes.items():
        if isinstance(value, list):
            wrapped[name] = FakeAttribute(value=value, values=value)
        else:
            wrapped[name] = FakeAttribute(value=value)
    return FakeEntry(entry_dn=dn, **wrapped)


class TestAttributeText:
    """Reading a single-valued attribute."""

    def test_a_present_value_is_returned(self):
        assert attribute_text(entry(sn="Novák"), "sn") == "Novák"

    def test_an_attribute_the_directory_left_empty_reads_as_empty(self):
        # ldap3 sets .value to None; str() would have produced 'None'.
        assert attribute_text(entry(sn=None), "sn") == ""

    def test_an_attribute_that_was_not_requested_reads_as_empty(self):
        assert attribute_text(entry(sn="Novák"), "homeDirectory") == ""

    def test_the_first_value_of_a_list_is_used(self):
        assert attribute_text(entry(mail=["a@b.cz", "c@d.cz"]), "mail") == "a@b.cz"

    def test_an_empty_list_reads_as_empty(self):
        assert attribute_text(entry(mail=[]), "mail") == ""

    def test_a_non_string_value_is_converted(self):
        assert attribute_text(entry(userAccountControl=512),
                              "userAccountControl") == "512"


class TestAttributeValues:
    """Reading a multi-valued attribute."""

    def test_every_value_is_returned(self):
        assert attribute_values(entry(memberOf=["cn=A,dc=x", "cn=B,dc=x"]),
                                "memberOf") == ["cn=A,dc=x", "cn=B,dc=x"]

    def test_a_single_string_becomes_a_one_item_list(self):
        # A user in exactly one group: ldap3 answers with a bare string.
        assert attribute_values(entry(memberOf="cn=A,dc=x"), "memberOf") == \
            ["cn=A,dc=x"]

    def test_a_missing_attribute_gives_an_empty_list(self):
        assert attribute_values(entry(sn="Novák"), "memberOf") == []

    def test_blank_entries_are_dropped(self):
        assert attribute_values(entry(memberOf=["cn=A,dc=x", "  ", ""]),
                                "memberOf") == ["cn=A,dc=x"]


class TestAttributeInt:
    """Reading a numeric attribute."""

    def test_a_number_is_parsed(self):
        assert attribute_int(entry(pwdLastSet="0"), "pwdLastSet") == 0

    def test_a_missing_attribute_gives_none(self):
        assert attribute_int(entry(sn="Novák"), "pwdLastSet") is None

    def test_a_non_numeric_value_gives_none_instead_of_raising(self):
        assert attribute_int(entry(pwdLastSet="never"), "pwdLastSet") is None


class TestDnHelpers:
    """Reading names out of distinguished names."""

    def test_the_first_component_is_the_name(self):
        assert rdn_value("CN=Pupils,OU=Groups,DC=school,DC=local") == "Pupils"

    def test_an_escaped_comma_stays_inside_the_name(self):
        assert rdn_value(r"CN=Pupils\, year 6,DC=school") == "Pupils, year 6"

    def test_a_string_that_is_not_a_dn_is_returned_unchanged(self):
        assert rdn_value("Pupils") == "Pupils"

    def test_the_parent_is_everything_after_the_first_component(self):
        assert parent_dn("CN=Jan,OU=Trida-6A,DC=school,DC=local") == \
            "OU=Trida-6A,DC=school,DC=local"

    def test_a_single_component_has_no_parent(self):
        assert parent_dn("DC=local") == ""

    def test_an_empty_dn_has_no_parent(self):
        assert parent_dn("") == ""

    def test_an_escaped_comma_does_not_split_the_dn(self):
        assert parent_dn(r"CN=Jan\, Jr,OU=Trida-6A,DC=x") == "OU=Trida-6A,DC=x"


class TestGroups:
    """Building group objects from memberOf."""

    def test_the_group_keeps_its_name_and_its_dn(self):
        group = group_from_dn("CN=Pupils,OU=Groups,DC=school,DC=local")
        assert (group.name, group.dn) == \
            ("Pupils", "CN=Pupils,OU=Groups,DC=school,DC=local")

    def test_an_empty_dn_produces_no_group(self):
        assert group_from_dn("   ") is None

    def test_the_directory_order_is_kept(self):
        groups = groups_from_entry(entry(memberOf=["CN=B,DC=x", "CN=A,DC=x"]))
        assert [g.name for g in groups] == ["B", "A"]

    def test_a_duplicate_dn_is_listed_once(self):
        groups = groups_from_entry(entry(memberOf=["CN=A,DC=x", "cn=a,dc=x"]))
        assert [g.name for g in groups] == ["A"]

    def test_no_member_of_gives_an_empty_list(self):
        assert groups_from_entry(entry(sn="Novák")) == []


class TestPersonFromEntry:
    """The whole mapping."""

    @pytest.fixture
    def full_entry(self):
        return entry(
            entry_dn="CN=Jan Novak,OU=Trida-6A,DC=school,DC=local",
            givenName="Jan", sn="Novák",
            displayName="Jan Novák (6.A)",
            sAMAccountName="novakjan",
            userPrincipalName="novakjan@school.local",
            mail="novakjan@school.local",
            description="Pupil of 6.A",
            homeDirectory=r"\\srv\students\novakjan",
            homeDrive="H:",
            memberOf=["CN=Pupils,DC=school,DC=local"],
            distinguishedName="CN=Jan Novak,OU=Trida-6A,DC=school,DC=local",
            userAccountControl="512",
            pwdLastSet="132000000000000000",
            whenChanged="20260101120000.0Z",
        )

    def test_the_names_are_mapped(self, full_entry):
        person = person_from_entry(full_entry, "6.A")
        assert (person.first_name, person.last_name, person.class_name) == \
            ("Jan", "Novák", "6.A")

    def test_the_account_fields_are_mapped(self, full_entry):
        person = person_from_entry(full_entry, "6.A")
        assert person.ad_username == "novakjan"
        assert person.ad_display_name == "Jan Novák (6.A)"
        assert person.ad_email == "novakjan@school.local"
        assert person.ad_description == "Pupil of 6.A"

    def test_the_home_directory_is_mapped(self, full_entry):
        person = person_from_entry(full_entry, "6.A")
        assert person.home_directory == r"\\srv\students\novakjan"
        assert person.home_drive == "H:"

    def test_the_groups_are_mapped(self, full_entry):
        person = person_from_entry(full_entry, "6.A")
        assert [g.name for g in person.group_memberships] == ["Pupils"]

    def test_the_dn_and_its_parent_are_mapped(self, full_entry):
        person = person_from_entry(full_entry, "6.A")
        assert person.ad_dn == "CN=Jan Novak,OU=Trida-6A,DC=school,DC=local"
        assert person.ad_ou_path == "OU=Trida-6A,DC=school,DC=local"

    def test_the_metadata_keeps_the_reference_values(self, full_entry):
        person = person_from_entry(full_entry, "6.A")
        assert person.metadata["ad_user_principal_name"] == "novakjan@school.local"
        assert person.metadata["ad_when_changed"] == "20260101120000.0Z"
        assert person.metadata["ad_dn"] == person.ad_dn

    def test_an_entry_without_a_first_name_is_not_a_person(self):
        assert person_from_entry(entry(sn="Novák"), "6.A") is None

    def test_an_entry_without_a_surname_is_not_a_person(self):
        assert person_from_entry(entry(givenName="Jan"), "6.A") is None

    def test_a_disabled_account_is_reported_as_disabled(self):
        person = person_from_entry(
            entry(givenName="Jan", sn="Novák",
                  userAccountControl=str(512 | UAC_ACCOUNT_DISABLED)), "6.A")
        assert person.account_enabled is False

    def test_an_enabled_account_is_reported_as_enabled(self):
        person = person_from_entry(
            entry(givenName="Jan", sn="Novák", userAccountControl="512"), "6.A")
        assert person.account_enabled is True

    def test_a_missing_user_account_control_does_not_disable_the_account(self):
        # 'Unknown' must not read as 'disabled' for an account the directory
        # is actively serving.
        person = person_from_entry(entry(givenName="Jan", sn="Novák"), "6.A")
        assert person.account_enabled is True

    def test_a_never_expiring_password_is_recognised(self):
        person = person_from_entry(
            entry(givenName="Jan", sn="Novák",
                  userAccountControl=str(512 | UAC_DONT_EXPIRE_PASSWORD)), "6.A")
        assert person.password_never_expires is True

    def test_pwd_last_set_zero_means_change_at_next_logon(self):
        person = person_from_entry(
            entry(givenName="Jan", sn="Novák", pwdLastSet="0"), "6.A")
        assert person.password_must_change is True

    def test_an_ordinary_pwd_last_set_does_not_force_a_change(self, full_entry):
        assert person_from_entry(full_entry, "6.A").password_must_change is False

    def test_the_entry_dn_is_used_when_the_attribute_is_missing(self):
        person = person_from_entry(
            entry(entry_dn="CN=Jan,OU=Trida-6A,DC=x", givenName="Jan",
                  sn="Novák"), "6.A")
        assert person.ad_dn == "CN=Jan,OU=Trida-6A,DC=x"

    def test_empty_optional_fields_become_none_not_empty_strings(self):
        person = person_from_entry(entry(givenName="Jan", sn="Novák"), "6.A")
        assert person.ad_email is None
        assert person.home_directory is None
        assert person.ad_description is None


class TestAttributeList:
    """The request list itself."""

    def test_every_editable_field_has_a_source_attribute(self):
        for attribute in ("givenName", "sn", "displayName", "sAMAccountName",
                          "mail", "description", "homeDirectory", "homeDrive",
                          "memberOf", "distinguishedName"):
            assert attribute in PERSON_ATTRIBUTES

    def test_no_attribute_is_requested_twice(self):
        assert len(PERSON_ATTRIBUTES) == len(set(PERSON_ATTRIBUTES))
