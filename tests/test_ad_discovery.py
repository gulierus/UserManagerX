"""
Tests for "Discover in AD".

Version 26:

* point 18 - discovery must say whether the person *agrees* with Active
  Directory, not only whether an account was found;
* point 19 a - it must read every field the application shows, not five of
  them.
"""

import pytest

from models import ADGroup, ADStatus
from services.ad_client import ADUserEntry
from services.ad_comparison import AD_VALUES_METADATA_KEY, stored_differences
from services.ad_services import AD_READ_ATTRIBUTES, ADDiscoveryService

BASE_DN = "DC=skola,DC=cz"


class FakeADClient:
    """
    A directory that answers exactly what a test tells it to.

    Attributes:
        entries: ``dn -> attributes`` for :meth:`get_user`.
        matches: ``filter -> list of ADUserEntry`` for :meth:`search_users`.
        requested_attributes: every attribute list the service asked for.
    """

    def __init__(self):
        self.entries = {}
        self.matches = {}
        self.requested_attributes = []

    def get_user(self, dn, attributes=None):
        self.requested_attributes.append(attributes)
        data = self.entries.get(dn)
        return ADUserEntry(dn=dn, attributes=dict(data)) if data else None

    def search_users(self, base_dn, search_filter, attributes=None):
        self.requested_attributes.append(attributes)
        return list(self.matches.get(search_filter, []))


@pytest.fixture
def client():
    return FakeADClient()


@pytest.fixture
def service(client):
    return ADDiscoveryService(client)


@pytest.fixture
def person(make_person):
    return make_person("Jan", "Novák", "6.A", ad_username="novakjan",
                       ad_email="jan@skola.cz")


def ad_entry(dn="CN=Jan Novak,OU=Trida-6.A,DC=skola,DC=cz", **attributes):
    """An entry that matches the `person` fixture unless told otherwise."""
    data = {
        'givenName': "Jan",
        'sn': "Novák",
        'sAMAccountName': "novakjan",
        'mail': "jan@skola.cz",
        'modifyTimestamp': "20260101120000.0Z",
    }
    data.update(attributes)
    return ADUserEntry(dn=dn, attributes=data)


class TestWhatDiscoveryAsksFor:
    """Point 19 a - every field the application shows must be requested."""

    def test_the_dn_lookup_requests_the_full_attribute_list(self, service, client,
                                                            person):
        person.ad_dn = "CN=Jan Novak,DC=skola,DC=cz"
        client.entries[person.ad_dn] = ad_entry().attributes

        service.discover_persons([person], BASE_DN)

        assert client.requested_attributes[0] == AD_READ_ATTRIBUTES

    def test_the_username_search_requests_the_full_attribute_list(self, service,
                                                                  client, person):
        client.matches["(sAMAccountName=novakjan)"] = [ad_entry()]

        service.discover_persons([person], BASE_DN)

        assert client.requested_attributes[0] == AD_READ_ATTRIBUTES

    @pytest.mark.parametrize("attribute", [
        'displayName', 'mail', 'homeDirectory', 'homeDrive', 'memberOf',
    ])
    def test_the_fields_named_in_the_report_are_all_requested(self, attribute):
        assert attribute in AD_READ_ATTRIBUTES

    def test_the_modify_timestamp_is_still_requested(self):
        # The conflict detector compares it against person.ad_version.
        assert 'modifyTimestamp' in AD_READ_ATTRIBUTES

    def test_no_attribute_is_requested_twice(self):
        assert len(AD_READ_ATTRIBUTES) == len(set(AD_READ_ATTRIBUTES))


class TestTheStatusDiscoverySets:
    """Points 18 b, c, d and g."""

    def test_a_person_who_is_not_there_is_reported_as_not_found(self, service,
                                                                person):
        service.discover_persons([person], BASE_DN)
        assert person.ad_status is ADStatus.NOT_FOUND_IN_AD

    def test_an_account_whose_values_all_agree_is_reported_as_identical(
            self, service, client, person):
        client.matches["(sAMAccountName=novakjan)"] = [ad_entry()]

        service.discover_persons([person], BASE_DN)

        assert person.ad_status is ADStatus.MATCHES_AD

    def test_an_account_whose_values_differ_is_reported_as_differing(
            self, service, client, person):
        client.matches["(sAMAccountName=novakjan)"] = [
            ad_entry(mail="stary@skola.cz")]

        service.discover_persons([person], BASE_DN)

        assert person.ad_status is ADStatus.DIFFERS_FROM_AD

    def test_a_differing_home_directory_is_enough_to_differ(self, service,
                                                            client, person):
        person.home_directory = r"\\srv\students\novakjan"
        client.matches["(sAMAccountName=novakjan)"] = [ad_entry()]

        service.discover_persons([person], BASE_DN)

        assert person.ad_status is ADStatus.DIFFERS_FROM_AD

    def test_a_differing_group_list_is_enough_to_differ(self, service, client,
                                                        person):
        person.add_to_group(ADGroup(name="Zaci", dn="CN=Zaci,DC=skola,DC=cz"))
        client.matches["(sAMAccountName=novakjan)"] = [ad_entry()]

        service.discover_persons([person], BASE_DN)

        assert person.ad_status is ADStatus.DIFFERS_FROM_AD

    def test_several_matching_accounts_are_reported_as_ambiguous(self, service,
                                                                 client, person):
        client.matches["(sAMAccountName=novakjan)"] = [
            ad_entry(dn="CN=A,DC=skola,DC=cz"),
            ad_entry(dn="CN=B,DC=skola,DC=cz"),
        ]

        service.discover_persons([person], BASE_DN)

        assert person.ad_status is ADStatus.MULTIPLE_AD_MATCHES

    def test_a_person_the_search_explodes_on_is_not_left_half_updated(
            self, service, client, person, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("the directory went away")

        monkeypatch.setattr(client, "search_users", boom)

        results = service.discover_persons([person], BASE_DN)

        # The search failure is swallowed per base, so the person is simply
        # not found rather than crashing the whole discovery run.
        assert results[0].found_in_ad is False
        assert person.ad_status is ADStatus.NOT_FOUND_IN_AD


class TestWhatDiscoveryRecords:
    """The snapshot and the comparison the rest of the UI reads."""

    def test_the_dn_and_the_version_are_recorded(self, service, client, person):
        client.matches["(sAMAccountName=novakjan)"] = [ad_entry()]

        service.discover_persons([person], BASE_DN)

        assert person.ad_dn == "CN=Jan Novak,OU=Trida-6.A,DC=skola,DC=cz"
        assert person.ad_version == "20260101120000.0Z"

    def test_the_directory_values_are_kept_for_later(self, service, client,
                                                     person):
        client.matches["(sAMAccountName=novakjan)"] = [
            ad_entry(homeDirectory=r"\\srv\students\novakjan")]

        service.discover_persons([person], BASE_DN)

        snapshot = person.metadata[AD_VALUES_METADATA_KEY]
        assert snapshot['homeDirectory'] == r"\\srv\students\novakjan"

    def test_the_differing_fields_are_recorded(self, service, client, person):
        client.matches["(sAMAccountName=novakjan)"] = [
            ad_entry(mail="stary@skola.cz", displayName="Jan N.")]
        person.ad_display_name = "Jan Novák"

        service.discover_persons([person], BASE_DN)

        assert {d.field for d in stored_differences(person)} == \
            {'ad_email', 'ad_display_name'}

    def test_an_identical_person_records_no_differences(self, service, client,
                                                        person):
        client.matches["(sAMAccountName=novakjan)"] = [ad_entry()]

        service.discover_persons([person], BASE_DN)

        assert stored_differences(person) == []

    def test_a_second_discovery_replaces_the_first_comparison(self, service,
                                                              client, person):
        client.matches["(sAMAccountName=novakjan)"] = [
            ad_entry(mail="stary@skola.cz")]
        service.discover_persons([person], BASE_DN)
        assert stored_differences(person)

        client.matches["(sAMAccountName=novakjan)"] = [ad_entry()]
        service.discover_persons([person], BASE_DN)

        assert stored_differences(person) == []
        assert person.ad_status is ADStatus.MATCHES_AD
