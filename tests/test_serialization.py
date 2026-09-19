"""
Tests for the shared source serialisation and the JSON export path.

Modules under test
------------------
* ``utils.source_serialization`` - the single "Source <-> dict" implementation,
* ``utils.json_export_task``     - the task that writes the encrypted export,
* ``operations.json_export``     - the export widget.

The important promise of these modules is that an export followed by an import
is **lossless**: everything a prepared account carries (home directory, group
memberships, password policy flags, account status, metadata, source info and
class metadata) has to survive ``source_to_dict -> json.dumps -> json.loads ->
source_from_dict``.
"""

import json
import logging
from datetime import datetime

import pytest

from models import ADGroup, VerificationStatus
from utils.encryption import decrypt_file, get_available_methods
from utils.progress_tasks import TaskCancelledException
from utils.source_serialization import (
    EXPORT_FORMAT_VERSION,
    group_from_dict,
    group_to_dict,
    person_from_dict,
    person_to_dict,
    source_from_dict,
    source_to_dict,
)

import utils.json_export_task as jet
from utils.json_export_task import JSONExportTask
from operations.json_export import JSONExportWidget


AES_AVAILABLE = get_available_methods().get("aes-gcm", False)

#: Every ``Person`` field the export format is supposed to carry, with a value
#: that is different from the default so a dropped field cannot pass unnoticed.
RICH_PERSON_FIELDS = {
    "first_name": "Šárka",
    "last_name": "Nováková-Čermáková",
    "class_name": "6.A",
    "ad_username": "novakovas",
    "ad_password": "Tajné#Heslo1",
    "ad_display_name": "Šárka Nováková-Čermáková",
    "ad_email": "sarka.novakova@škola.cz",
    "ad_description": "Žákyně třídy 6.A",
    "ad_ou_path": "OU=Žáci,OU=Škola,DC=skola,DC=cz",
    "ad_dn": "CN=Šárka Nováková-Čermáková,OU=Žáci,DC=skola,DC=cz",
    "home_directory": r"\\srv01\home\novakovas",
    "home_drive": "H:",
    "password_must_change": True,
    "password_cannot_change": True,
    "password_never_expires": True,
    "account_enabled": True,
    # --- Microsoft 365 (Version 26, point 23) -------------------------------
    "m365_user_principal_name": "novakovas@skola.onmicrosoft.com",
    "m365_display_name": "Šárka Nováková-Čermáková (6.A)",
    "m365_mail_nickname": "novakovas",
    "m365_password": "M365#Tajné1",
    "m365_usage_location": "CZ",
    "m365_object_id": "6f1e9c40-0000-4000-8000-000000000001",
    "metadata": {"edupage_id": "42", "poznámka": "přeřazena z 5.B"},
}

#: A legacy export: only the nine fields the pre-refactor exporters wrote.
LEGACY_PERSON = {
    "first_name": "Jan",
    "last_name": "Novák",
    "class_name": "6.A",
    "ad_username": "novakj",
    "ad_password": "Heslo123",
    "ad_display_name": "Jan Novák",
    "ad_email": "jan@skola.cz",
    "ad_description": "Student",
    "ad_ou_path": "OU=Students,DC=skola,DC=cz",
}


def make_group(name="Žáci 6.A", dn="CN=Zaci6A,OU=Skupiny,DC=skola,DC=cz", **kwargs):
    """Build an :class:`ADGroup` with sensible, non-default values."""
    defaults = dict(description="Skupina třídy 6.A", group_type="security",
                    members_count=27, metadata={"origin": "template"})
    defaults.update(kwargs)
    return ADGroup(name=name, dn=dn, **defaults)


def export_text(source):
    """Serialise exactly the way :class:`JSONExportTask` does."""
    return json.dumps(source_to_dict(source), indent=2, ensure_ascii=False)


def roundtrip(source, **kwargs):
    """source -> dict -> JSON text -> dict -> source, the full export/import."""
    return source_from_dict(json.loads(export_text(source)), **kwargs)


@pytest.fixture
def rich_person(make_person):
    """A fully prepared account: every field set, two group memberships."""
    return make_person(
        group_memberships=[
            make_group(),
            make_group(name="Wi-Fi", dn="CN=WiFi,OU=Skupiny,DC=skola,DC=cz",
                       description=None, group_type=None, members_count=0,
                       metadata={}),
        ],
        **RICH_PERSON_FIELDS,
    )


@pytest.fixture
def rich_source(make_source, make_class, make_person, rich_person):
    """A source with metadata, source_info, class metadata and real accounts."""
    source = make_source("Škola 2024/25", source_type="edupage")
    source.metadata.update({"school": "ZŠ Ústí", "year": 2024})
    source.set_source_info("subdomain", "zsusti")
    source.set_source_info("imported", {"rows": [1, 2, 3]})

    six_a = make_class("6.A", persons=[
        rich_person,
        make_person("Petr", "Svoboda", "6.A", ad_username="svobodap"),
    ])
    six_a.metadata.update({"teacher": "Mgr. Čermák", "room": 12})
    source.add_class(six_a)
    source.add_class(make_class("IX.", 1))
    return source


# ---------------------------------------------------------------------------
# Lossless round trip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", sorted(RICH_PERSON_FIELDS))
def test_round_trip_preserves_every_person_field(field, rich_person, make_class,
                                                 make_source):
    """Each documented Person field comes back unchanged after a JSON round trip."""
    source = make_source("S", source_type="manual")
    source.add_class(make_class("6.A", persons=[rich_person]))

    restored = roundtrip(source).get_all_persons()[0]

    assert getattr(restored, field) == RICH_PERSON_FIELDS[field]


def test_round_trip_preserves_group_memberships(rich_source):
    """Group DNs, names, descriptions, type, member count and metadata survive."""
    restored = roundtrip(rich_source).get_all_persons()[0]
    original = rich_source.get_all_persons()[0]

    assert [g.dn for g in restored.group_memberships] == \
           [g.dn for g in original.group_memberships]
    first = restored.group_memberships[0]
    assert (first.name, first.description, first.group_type, first.members_count) == \
           ("Žáci 6.A", "Skupina třídy 6.A", "security", 27)
    assert first.metadata == {"origin": "template"}


def test_round_trip_resets_group_verification_state(make_person, make_class,
                                                    make_source):
    """Verification is machine specific: it is deliberately not carried over."""
    group = make_group()
    group.mark_verified(True)
    assert group.verification_status is VerificationStatus.VERIFIED_EXISTS

    person = make_person(group_memberships=[group])
    source = make_source("S")
    source.add_class(make_class("6.A", persons=[person]))

    restored = roundtrip(source).get_all_persons()[0].group_memberships[0]
    assert restored.verification_status is VerificationStatus.NOT_VERIFIED
    assert restored.last_verified is None
    assert restored.verification_error is None


def test_round_trip_preserves_source_metadata_and_source_info(rich_source):
    """Source.metadata and Source.source_info (e.g. the EduPage subdomain) survive."""
    restored = roundtrip(rich_source)

    assert restored.metadata == {"school": "ZŠ Ústí", "year": 2024}
    assert restored.source_info == {"subdomain": "zsusti",
                                    "imported": {"rows": [1, 2, 3]}}


def test_round_trip_preserves_class_names_metadata_and_order(rich_source):
    """Classes keep their order, their name and their metadata dictionary."""
    restored = roundtrip(rich_source)

    assert [c.name for c in restored.classes] == ["6.A", "IX."]
    assert restored.classes[0].metadata == {"teacher": "Mgr. Čermák", "room": 12}
    assert restored.classes[1].metadata == {}
    assert [len(c.persons) for c in restored.classes] == [2, 1]


def test_round_trip_preserves_person_order_inside_a_class(rich_source):
    """Students are not reordered by the export."""
    restored = roundtrip(rich_source)
    assert [(p.first_name, p.last_name) for p in restored.classes[0].persons] == \
           [("Šárka", "Nováková-Čermáková"), ("Petr", "Svoboda")]


def test_round_trip_keeps_diacritics_out_of_escape_sequences(rich_source):
    """The export is written with ensure_ascii=False, so it stays human readable."""
    text = export_text(rich_source)
    assert "Nováková-Čermáková" in text
    assert "\\u00e1" not in text


def test_round_trip_of_an_empty_source_yields_no_classes(make_source):
    """A source without classes round trips to a source without classes."""
    restored = roundtrip(make_source("Prázdný", source_type="manual"))
    assert restored.name == "Prázdný"
    assert restored.classes == []
    assert restored.get_all_persons() == []


def test_round_trip_of_an_empty_class_keeps_the_class(make_source, make_class):
    """A class without students is not silently dropped."""
    source = make_source("S")
    source.add_class(make_class("Bez žáků", 0))

    restored = roundtrip(source)
    assert [c.name for c in restored.classes] == ["Bez žáků"]
    assert restored.classes[0].persons == []


def test_round_trip_keeps_duplicate_class_names_separate(make_source, make_class,
                                                         make_person):
    """Two classes with the same name stay two classes."""
    source = make_source("S")
    source.add_class(make_class("6.A", persons=[make_person("A", "One", "6.A")]))
    source.add_class(make_class("6.A", persons=[make_person("B", "Two", "6.A")]))

    restored = roundtrip(source)
    assert len(restored.classes) == 2
    assert [p.first_name for p in restored.get_all_persons()] == ["A", "B"]


def test_round_trip_is_idempotent(rich_source):
    """Serialising the re-imported source produces the very same dictionary."""
    first = source_to_dict(rich_source)
    second = source_to_dict(source_from_dict(json.loads(json.dumps(first))))

    first.pop("export_date")
    second.pop("export_date")
    assert second == first


def test_round_trip_marks_the_imported_person_as_clean(rich_source):
    """A freshly imported account has no dirty fields - nothing to write to AD yet."""
    restored = roundtrip(rich_source).get_all_persons()[0]
    assert restored.is_dirty() is False
    assert restored.get_dirty_fields() == set()


def test_imported_source_is_read_only_by_default(rich_source):
    """Files are opened read-only unless the caller says otherwise."""
    assert roundtrip(rich_source).readonly is True
    assert roundtrip(rich_source, readonly=False).readonly is False


# ---------------------------------------------------------------------------
# source_to_dict - the produced structure
# ---------------------------------------------------------------------------

def test_source_to_dict_stamps_the_format_version_and_an_iso_export_date(rich_source):
    """The on-disk contract: format_version 2 plus a parseable ISO timestamp."""
    data = source_to_dict(rich_source)

    assert data["format_version"] == 2 == EXPORT_FORMAT_VERSION
    parsed = datetime.fromisoformat(data["export_date"])
    assert abs((datetime.now() - parsed).total_seconds()) < 60


def test_source_to_dict_writes_every_person_key(rich_source):
    """
    No field of the export format is missing from the written dictionary.

    ``m365_status`` is written but is not in RICH_PERSON_FIELDS, because that
    dictionary is fed straight into ``Person(...)`` where the status is an
    enumeration member rather than the text the file holds.
    """
    person_data = source_to_dict(rich_source)["classes"][0]["persons"][0]

    assert set(person_data) == (set(RICH_PERSON_FIELDS)
                                | {"group_memberships", "m365_status"})


def test_source_to_dict_output_is_json_serialisable(rich_source):
    """The result must survive json.dumps without a custom encoder."""
    text = json.dumps(source_to_dict(rich_source), indent=2, ensure_ascii=False)
    assert json.loads(text)["name"] == "Škola 2024/25"


def test_person_to_dict_keeps_none_for_unset_optional_fields(make_person):
    """Unset optional attributes are written as JSON null, not as empty strings."""
    data = person_to_dict(make_person("Jan", "Novák", "6.A"))

    assert data["ad_username"] is None
    assert data["home_directory"] is None
    assert data["home_drive"] is None
    assert data["group_memberships"] == []
    assert data["account_enabled"] is False


def test_group_to_dict_omits_verification_state():
    """Verification is not part of the export format."""
    group = make_group()
    group.mark_verified(False)

    data = group_to_dict(group)
    assert set(data) == {"name", "dn", "description", "group_type",
                         "members_count", "metadata"}


# ---------------------------------------------------------------------------
# Legacy files
# ---------------------------------------------------------------------------

def test_legacy_person_without_new_keys_falls_back_to_defaults():
    """Files written before the format grew simply lack the newer keys."""
    person = person_from_dict(dict(LEGACY_PERSON))

    assert person.first_name == "Jan"
    assert person.ad_username == "novakj"
    assert person.home_directory is None
    assert person.home_drive is None
    assert person.group_memberships == []
    assert person.password_must_change is False
    assert person.password_cannot_change is False
    assert person.password_never_expires is False
    assert person.account_enabled is False
    assert person.metadata == {}


def test_legacy_source_without_format_version_still_loads():
    """A pre-versioning file loads; only its own fields are used."""
    data = {"name": "Starý export", "source_type": "manual",
            "classes": [{"name": "6.A", "persons": [dict(LEGACY_PERSON)]}]}

    source = source_from_dict(data)
    assert source.name == "Starý export"
    assert source.source_type == "manual"
    assert [p.ad_username for p in source.get_all_persons()] == ["novakj"]


def test_source_without_classes_key_loads_as_empty():
    """A missing 'classes' key is an empty source, not an error."""
    source = source_from_dict({"name": "Prázdný"})
    assert source.classes == []


def test_class_without_persons_key_loads_as_empty():
    """A missing 'persons' key is an empty class, not an error."""
    source = source_from_dict({"classes": [{"name": "6.A"}]})
    assert [c.name for c in source.classes] == ["6.A"]
    assert source.classes[0].persons == []


def test_unknown_keys_in_the_file_are_ignored():
    """Fields a newer version may add do not break an older reader."""
    data = {"name": "S", "future_field": 1,
            "classes": [{"name": "6.A", "future": True,
                         "persons": [dict(LEGACY_PERSON, future="x")]}]}

    source = source_from_dict(data)
    assert source.get_all_persons()[0].last_name == "Novák"


def test_missing_source_name_falls_back_to_the_default_name():
    """The file name is used when the export carries no source name."""
    assert source_from_dict({}, default_name="export.aes").name == "export.aes"
    assert source_from_dict({"name": ""}, default_name="export.aes").name == "export.aes"
    assert source_from_dict({"name": "Vlastní"}, default_name="export.aes").name == "Vlastní"


def test_missing_source_type_defaults_to_file():
    """An import without a type is treated as a file source."""
    assert source_from_dict({"name": "S"}).source_type == "file"


# ---------------------------------------------------------------------------
# group_from_dict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", [
    None, [], ["dn"], "CN=Students", 42, ("dn",),
], ids=["none", "empty-list", "list", "string", "int", "tuple"])
def test_group_from_dict_returns_none_for_a_non_dict_entry(entry):
    """Anything that is not a dictionary is an unusable group entry."""
    assert group_from_dict(entry) is None


@pytest.mark.parametrize("entry", [
    {}, {"name": "Students"}, {"dn": ""}, {"dn": None},
], ids=["empty", "name-only", "empty-dn", "null-dn"])
def test_group_from_dict_rejects_an_entry_without_a_dn(entry):
    """The DN is the identity of a group - without it the entry is dropped."""
    assert group_from_dict(entry) is None


def test_group_from_dict_derives_the_name_from_the_dn_when_missing():
    """A group entry without a name is named after the first DN component."""
    group = group_from_dict({"dn": "CN=Žáci 6.A,OU=Skupiny,DC=skola,DC=cz"})
    assert group.name == "Žáci 6.A"
    assert group.dn == "CN=Žáci 6.A,OU=Skupiny,DC=skola,DC=cz"


def test_group_from_dict_derives_the_name_when_the_stored_name_is_empty():
    """An empty name is treated the same way as a missing one."""
    assert group_from_dict({"dn": "CN=WiFi,DC=x", "name": ""}).name == "WiFi"


def test_group_from_dict_defaults_optional_fields():
    """Only the DN is mandatory; the rest gets neutral defaults."""
    group = group_from_dict({"dn": "CN=WiFi,DC=x"})

    assert group.description is None
    assert group.group_type is None
    assert group.members_count == 0
    assert group.metadata == {}
    assert group.verification_status is VerificationStatus.NOT_VERIFIED


@pytest.mark.parametrize("raw, expected", [
    (None, 0), (0, 0), ("12", 12), (7, 7), (3.9, 3), (True, 1),
], ids=["none", "zero", "numeric-string", "int", "float", "bool"])
def test_group_from_dict_coerces_members_count_to_an_int(raw, expected):
    """member counts arrive as numbers or numeric strings and become ints."""
    group = group_from_dict({"dn": "CN=WiFi,DC=x", "members_count": raw})
    assert group.members_count == expected


# ---------------------------------------------------------------------------
# person_from_dict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing", ["first_name", "last_name", "class_name"])
def test_person_from_dict_raises_for_each_missing_required_field(missing):
    """Every required field is reported by name in the raised ValueError."""
    data = {"first_name": "Jan", "last_name": "Novák", "class_name": "6.A"}
    del data[missing]

    with pytest.raises(ValueError,
                       match=f"Person is missing required '{missing}' field"):
        person_from_dict(data)


def test_person_from_dict_raises_for_an_empty_dictionary():
    """The first missing field wins, so the message names first_name."""
    with pytest.raises(ValueError, match="first_name"):
        person_from_dict({})


def test_person_from_dict_accepts_empty_strings_as_names():
    """An empty name is data, not a missing field - it must not raise."""
    person = person_from_dict({"first_name": "", "last_name": "", "class_name": ""})
    assert (person.first_name, person.last_name, person.class_name) == ("", "", "")


@pytest.mark.parametrize("flag", ["password_must_change", "password_cannot_change",
                                  "password_never_expires", "account_enabled"])
@pytest.mark.parametrize("raw, expected", [
    (True, True), (False, False), (None, False), ("", False), ("yes", True),
    (1, True), (0, False),
], ids=["true", "false", "null", "empty-string", "string", "one", "zero"])
def test_person_from_dict_coerces_policy_flags_to_bool(flag, raw, expected):
    """Policy flags are always real booleans, whatever the file contains."""
    data = {"first_name": "Jan", "last_name": "Novák", "class_name": "6.A", flag: raw}
    assert getattr(person_from_dict(data), flag) is expected


def test_person_from_dict_skips_unusable_group_entries_and_keeps_the_rest(caplog):
    """One broken group entry must not cost the person its other memberships."""
    data = {
        "first_name": "Jan", "last_name": "Novák", "class_name": "6.A",
        "group_memberships": [
            {"name": "no dn here"},
            {"dn": "CN=Students,DC=x"},
            None,
            {"dn": "CN=WiFi,DC=x"},
        ],
    }

    with caplog.at_level(logging.WARNING, logger="utils.source_serialization"):
        person = person_from_dict(data)

    assert person.get_group_dns() == ["CN=Students,DC=x", "CN=WiFi,DC=x"]
    assert sum("Skipping unusable group entry" in r.message for r in caplog.records) == 2


@pytest.mark.parametrize("raw", [None, [], "", {}], ids=["null", "empty", "empty-string", "empty-dict"])
def test_person_from_dict_treats_empty_group_memberships_as_no_groups(raw):
    """Every falsy value for the membership list means 'no groups'."""
    data = {"first_name": "Jan", "last_name": "Novák", "class_name": "6.A",
            "group_memberships": raw}
    assert person_from_dict(data).group_memberships == []


def test_person_from_dict_keeps_the_person_independent_of_the_group_list():
    """The Person copies the membership list it is handed."""
    groups = [{"dn": "CN=Students,DC=x"}]
    person = person_from_dict({"first_name": "Jan", "last_name": "Novák",
                               "class_name": "6.A", "group_memberships": groups})
    groups.append({"dn": "CN=Later,DC=x"})

    assert person.get_group_dns() == ["CN=Students,DC=x"]


def test_person_from_dict_rejects_a_non_dictionary():
    """Passing something that is not a mapping is a programming error."""
    with pytest.raises((TypeError, AttributeError, ValueError)):
        person_from_dict(None)


# ---------------------------------------------------------------------------
# source_from_dict - validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    None, [], ["a"], "not json", 42, 3.5, True,
], ids=["none", "empty-list", "list", "string", "int", "float", "bool"])
def test_source_from_dict_rejects_anything_but_an_object(payload):
    """The top level of the export file has to be a JSON object."""
    with pytest.raises(ValueError, match="does not contain a JSON object"):
        source_from_dict(payload)


@pytest.mark.parametrize("classes", [
    None, {}, {"6.A": []}, "6.A", 3,
], ids=["null", "empty-dict", "dict", "string", "int"])
def test_source_from_dict_rejects_a_classes_field_that_is_not_a_list(classes):
    """'classes' must be a list - a null or an object is a corrupt file."""
    with pytest.raises(ValueError, match="'classes' field must be a list"):
        source_from_dict({"name": "S", "classes": classes})


@pytest.mark.parametrize("entry", ["6.A", None, 7, ["6.A"]],
                         ids=["string", "none", "int", "list"])
def test_source_from_dict_rejects_a_class_that_is_not_a_dictionary(entry):
    """Every element of 'classes' has to be an object."""
    with pytest.raises(ValueError, match="Each class must be a dictionary"):
        source_from_dict({"classes": [entry]})


def test_source_from_dict_rejects_a_class_without_a_name():
    """A class without a name cannot be shown or merged."""
    with pytest.raises(ValueError, match="Class is missing required 'name' field"):
        source_from_dict({"classes": [{"persons": []}]})


@pytest.mark.parametrize("persons", [None, {}, "Jan", 5],
                         ids=["null", "dict", "string", "int"])
def test_source_from_dict_rejects_a_persons_field_that_is_not_a_list(persons):
    """The error names the offending class so the user can find it."""
    with pytest.raises(ValueError,
                       match=r"'persons' field in class '6\.A' must be a list"):
        source_from_dict({"classes": [{"name": "6.A", "persons": persons}]})


@pytest.mark.parametrize("entry", ["Jan Novák", None, 7, ["Jan"]],
                         ids=["string", "none", "int", "list"])
def test_source_from_dict_rejects_a_person_that_is_not_a_dictionary(entry):
    """Every element of 'persons' has to be an object."""
    with pytest.raises(ValueError, match="Each person must be a dictionary"):
        source_from_dict({"classes": [{"name": "6.A", "persons": [entry]}]})


def test_source_from_dict_propagates_a_broken_person_from_a_valid_class():
    """A person missing a required field aborts the import with a clear message."""
    data = {"classes": [{"name": "6.A", "persons": [{"first_name": "Jan"}]}]}
    with pytest.raises(ValueError, match="Person is missing required 'last_name' field"):
        source_from_dict(data)


def test_source_from_dict_ignores_a_source_info_that_is_not_a_dictionary():
    """A corrupt source_info is skipped instead of poisoning the source."""
    source = source_from_dict({"name": "S", "source_info": ["a", "b"]})
    assert source.source_info == {}


def test_source_from_dict_imports_a_person_whose_class_name_disagrees(caplog):
    """A mismatching class_name is kept and reported, not silently rewritten."""
    data = {"classes": [{"name": "6.A", "persons": [
        {"first_name": "Jan", "last_name": "Novák", "class_name": "7.B"}]}]}

    with caplog.at_level(logging.WARNING):
        source = source_from_dict(data)

    person = source.classes[0].persons[0]
    assert person.class_name == "7.B"
    assert source.classes[0].name == "6.A"


# ---------------------------------------------------------------------------
# JSONExportTask
# ---------------------------------------------------------------------------

def test_task_source_to_json_delegates_to_the_shared_serialiser(monkeypatch,
                                                                rich_source):
    """The task must not keep a private copy of the conversion."""
    seen = []
    sentinel = {"format_version": "sentinel"}
    monkeypatch.setattr(jet, "source_to_dict",
                        lambda src: (seen.append(src), sentinel)[1])

    task = JSONExportTask(rich_source, "/does/not/exist.aes", "pw", "aes-gcm")
    result = task._source_to_json()

    assert result is sentinel
    assert len(seen) == 1 and seen[0] is rich_source


def test_task_source_to_json_matches_the_shared_serialiser(rich_source):
    """Its output is byte for byte what source_to_dict produces."""
    task = JSONExportTask(rich_source, "/does/not/exist.aes", "pw", "aes-gcm")

    produced = task._source_to_json()
    expected = source_to_dict(rich_source)
    produced.pop("export_date")
    expected.pop("export_date")

    assert produced == expected
    assert produced["classes"][0]["persons"][0]["home_directory"] == \
        RICH_PERSON_FIELDS["home_directory"]


def test_task_rejects_an_unavailable_encryption_method(monkeypatch, rich_source,
                                                       tmp_path):
    """GPG without python-gnupg fails loudly before anything is written."""
    monkeypatch.setattr(jet, "get_available_methods",
                        lambda: {"gpg": False, "aes-gcm": True})
    out = tmp_path / "export.gpg"
    task = JSONExportTask(rich_source, str(out), "Correct#Horse9", "gpg")

    with pytest.raises(RuntimeError, match="Encryption method 'gpg' is not available"):
        task.execute()

    assert task.success is False
    assert not out.exists()


def test_task_writes_the_complete_person_payload(monkeypatch, rich_source, tmp_path):
    """The plaintext handed to the encrypter carries every field of the account."""
    captured = {}

    def fake_encrypt(data, output_path, password, method="aes-gcm"):
        captured["data"] = data
        captured["password"] = password
        captured["method"] = method
        open(output_path, "w").write("encrypted")

    monkeypatch.setattr(jet, "get_available_methods",
                        lambda: {"gpg": False, "aes-gcm": True})
    monkeypatch.setattr(jet, "encrypt_file", fake_encrypt)

    out = tmp_path / "export.aes"
    task = JSONExportTask(rich_source, str(out), "Correct#Horse9", "aes-gcm")
    message = task.execute()

    payload = json.loads(captured["data"])
    person = payload["classes"][0]["persons"][0]
    assert captured["method"] == "aes-gcm"
    assert captured["password"] == "Correct#Horse9"
    assert person["home_directory"] == RICH_PERSON_FIELDS["home_directory"]
    assert person["account_enabled"] is True
    assert [g["dn"] for g in person["group_memberships"]] == \
        ["CN=Zaci6A,OU=Skupiny,DC=skola,DC=cz", "CN=WiFi,OU=Skupiny,DC=skola,DC=cz"]
    assert payload["source_info"] == {"subdomain": "zsusti",
                                      "imported": {"rows": [1, 2, 3]}}
    assert task.success is True
    assert message == f"Exported 3 persons to {out}"


def test_task_reports_progress_up_to_one_hundred(monkeypatch, rich_source, tmp_path):
    """The dialog gets a monotonic 25/50/75/100 progression."""
    monkeypatch.setattr(jet, "get_available_methods",
                        lambda: {"gpg": False, "aes-gcm": True})
    monkeypatch.setattr(jet, "encrypt_file",
                        lambda data, path, pw, method="aes-gcm": open(path, "w").write("x"))

    task = JSONExportTask(rich_source, str(tmp_path / "e.aes"), "pw12345678", "aes-gcm")
    seen = []
    task.progress_changed.connect(lambda value, message: seen.append(value))
    task.execute()

    assert seen == sorted(seen)
    assert seen[-1] == 100


def test_task_fails_when_the_output_file_is_missing(monkeypatch, rich_source,
                                                    tmp_path):
    """A silent encrypter that writes nothing is caught by the verify step."""
    monkeypatch.setattr(jet, "get_available_methods",
                        lambda: {"gpg": False, "aes-gcm": True})
    monkeypatch.setattr(jet, "encrypt_file",
                        lambda data, path, pw, method="aes-gcm": None)

    task = JSONExportTask(rich_source, str(tmp_path / "missing.aes"), "pw", "aes-gcm")
    with pytest.raises(RuntimeError, match="Output file was not created"):
        task.execute()
    assert task.success is False


def test_task_stops_when_cancelled_before_it_starts(monkeypatch, rich_source,
                                                    tmp_path):
    """A cancel request is honoured at the first checkpoint."""
    monkeypatch.setattr(jet, "get_available_methods",
                        lambda: {"gpg": False, "aes-gcm": True})
    out = tmp_path / "export.aes"
    task = JSONExportTask(rich_source, str(out), "pw12345678", "aes-gcm")
    task.request_cancel()

    with pytest.raises(TaskCancelledException):
        task.execute()

    assert task.success is False
    assert not out.exists()


@pytest.mark.integration
@pytest.mark.skipif(not AES_AVAILABLE, reason="PyCryptodome is not installed")
def test_task_export_can_be_decrypted_and_imported_without_loss(rich_source,
                                                                tmp_path):
    """End to end: export -> encrypted file -> decrypt -> import, nothing lost."""
    out = tmp_path / "export.aes"
    task = JSONExportTask(rich_source, str(out), "Correct#Horse9", "aes-gcm")
    task.execute()

    restored = source_from_dict(json.loads(decrypt_file(str(out), "Correct#Horse9")),
                                default_name="export.aes")
    person = restored.get_all_persons()[0]

    assert out.stat().st_size > 0
    assert restored.name == rich_source.name
    assert restored.source_info == rich_source.source_info
    assert person.home_directory == RICH_PERSON_FIELDS["home_directory"]
    assert person.account_enabled is True
    assert person.password_never_expires is True
    assert person.get_group_dns() == rich_source.get_all_persons()[0].get_group_dns()
    assert person.metadata == RICH_PERSON_FIELDS["metadata"]


# ---------------------------------------------------------------------------
# JSONExportWidget
# ---------------------------------------------------------------------------

@pytest.mark.gui
def test_widget_source_to_json_returns_the_real_dictionary(dialogs, source_manager,
                                                           rich_source):
    """Regression: the widget used to build the dictionary and return None."""
    widget = JSONExportWidget(source_manager)

    data = widget.source_to_json(rich_source)

    assert isinstance(data, dict)
    assert data["name"] == "Škola 2024/25"
    assert data["format_version"] == EXPORT_FORMAT_VERSION
    assert [c["name"] for c in data["classes"]] == ["6.A", "IX."]
    assert data["classes"][0]["persons"][0]["ad_username"] == "novakovas"
    assert dialogs.calls == []


@pytest.mark.gui
def test_widget_and_task_agree_on_the_exported_structure(dialogs, source_manager,
                                                         rich_source):
    """Both call sites must produce the identical structure."""
    widget = JSONExportWidget(source_manager)
    task = JSONExportTask(rich_source, "/does/not/exist.aes", "pw", "aes-gcm")

    from_widget = widget.source_to_json(rich_source)
    from_task = task._source_to_json()
    from_widget.pop("export_date")
    from_task.pop("export_date")

    assert from_widget == from_task


@pytest.mark.gui
def test_widget_source_to_json_survives_an_empty_source(dialogs, source_manager,
                                                        make_source):
    """An empty source exports to an empty class list, not to None."""
    widget = JSONExportWidget(source_manager)

    data = widget.source_to_json(make_source("Prázdný", source_type="manual"))

    assert data["classes"] == []
    assert data["name"] == "Prázdný"


@pytest.mark.gui
def test_widget_export_button_follows_the_selected_source(dialogs, source_manager,
                                                          rich_source):
    """set_source(None) disables the export; a real source enables it."""
    widget = JSONExportWidget(source_manager)
    assert widget.export_btn.isEnabled() is False

    widget.set_source(rich_source)
    assert widget.export_btn.isEnabled() is True
    assert "Škola 2024/25" in widget.source_info_label.text()

    widget.set_source(None)
    assert widget.export_btn.isEnabled() is False
    assert "No source selected" in widget.source_info_label.text()


@pytest.mark.gui
def test_widget_export_without_a_source_warns_instead_of_crashing(dialogs,
                                                                  source_manager):
    """Pressing Export with nothing selected shows a warning and stops."""
    widget = JSONExportWidget(source_manager)

    widget.on_export()

    assert dialogs.kinds() == ["warning"]
    assert dialogs.saw("select a source")


@pytest.mark.gui
def test_widget_auto_filename_is_derived_from_the_source_name(dialogs,
                                                              source_manager,
                                                              rich_source):
    """The generated name keeps the readable part of the source name."""
    widget = JSONExportWidget(source_manager)
    widget.set_source(rich_source)

    name = widget._make_auto_name()

    assert name.startswith("Škola")
    assert name.endswith(".aes")
    assert "/" not in name


# ---------------------------------------------------------------------------
# Defects found in the production code
# ---------------------------------------------------------------------------

@pytest.mark.bug
def test_group_from_dict_returns_none_for_a_non_numeric_members_count():
    """An unusable group entry must be dropped, not blow up the whole import."""
    group = group_from_dict({"dn": "CN=Students,DC=x", "members_count": "many"})
    assert group is None or group.members_count == 0


@pytest.mark.bug
def test_import_survives_a_group_with_a_non_numeric_members_count():
    """One malformed group field must not cost the user every account in the file."""
    data = {"name": "S", "classes": [{"name": "6.A", "persons": [{
        "first_name": "Jan", "last_name": "Novák", "class_name": "6.A",
        "group_memberships": [{"dn": "CN=Students,DC=x", "members_count": "many"}],
    }]}]}

    source = source_from_dict(data)
    assert [p.last_name for p in source.get_all_persons()] == ["Novák"]


@pytest.mark.bug
@pytest.mark.parametrize("dn", [12345, ["CN=x"], {"cn": "x"}],
                         ids=["int", "list", "dict"])
def test_group_from_dict_returns_none_for_a_non_string_dn(dn):
    """A DN that is not a string is an unusable entry, not a crash."""
    assert group_from_dict({"dn": dn}) is None


@pytest.mark.bug
def test_import_does_not_share_metadata_with_the_exported_source(rich_source):
    """The rebuilt source must own its metadata, not alias the exporter's."""
    data = source_to_dict(rich_source)
    restored = source_from_dict(data)

    restored.metadata["school"] = "jiná škola"
    assert rich_source.metadata["school"] == "ZŠ Ústí"


@pytest.mark.bug
def test_imported_person_owns_its_metadata(rich_source):
    """Editing an imported account must not reach back into the source it came from."""
    restored = source_from_dict(source_to_dict(rich_source))

    restored.get_all_persons()[0].metadata["poznámka"] = "změněno"
    assert rich_source.get_all_persons()[0].metadata["poznámka"] == "přeřazena z 5.B"


@pytest.mark.bug
@pytest.mark.parametrize("field", ["first_name", "last_name", "class_name"])
def test_person_from_dict_rejects_a_null_required_field(field):
    """'first_name': null is as unusable as a missing first_name."""
    data = {"first_name": "Jan", "last_name": "Novák", "class_name": "6.A",
            field: None}

    with pytest.raises(ValueError, match=field):
        person_from_dict(data)


@pytest.mark.bug
def test_a_corrupt_metadata_field_is_ignored_at_every_level():
    """source_info is type checked; metadata has to be too."""
    data = {"name": "S", "metadata": "junk", "classes": [{
        "name": "6.A", "metadata": 7,
        "persons": [{"first_name": "Jan", "last_name": "Novák",
                     "class_name": "6.A", "metadata": "nope"}]}]}

    source = source_from_dict(data)

    assert source.metadata == {}
    assert source.classes[0].metadata == {}
    assert source.classes[0].persons[0].metadata == {}


@pytest.mark.bug
def test_round_trip_preserves_the_ad_distinguished_name(make_person, make_class,
                                                        make_source):
    """ad_dn is what the group sync uses; losing it silently disables the sync."""
    person = make_person("Jan", "Novák", "6.A",
                         ad_dn="CN=Jan Novák,OU=Žáci,DC=skola,DC=cz")
    source = make_source("S")
    source.add_class(make_class("6.A", persons=[person]))

    assert roundtrip(source).get_all_persons()[0].ad_dn == \
        "CN=Jan Novák,OU=Žáci,DC=skola,DC=cz"


@pytest.mark.bug
def test_source_to_dict_is_json_serialisable_after_an_ad_check(make_person,
                                                               make_class,
                                                               make_source):
    """services.ad_services puts raw AD attributes into person.metadata."""
    person = make_person("Jan", "Novák", "6.A")
    person.metadata["ad_current_values"] = {"whenChanged": datetime(2024, 1, 2, 3, 4, 5)}
    source = make_source("S")
    source.add_class(make_class("6.A", persons=[person]))

    text = json.dumps(source_to_dict(source), indent=2, ensure_ascii=False)
    assert "2024" in text


# ---------------------------------------------------------------------------
# Version 26, point 23 - the Microsoft 365 fields in the JSON export
# ---------------------------------------------------------------------------

def test_the_microsoft_365_status_survives_a_round_trip(make_source, make_class,
                                                        make_person):
    """The status is stored by value and read back as the same member."""
    from models_m365 import M365Status

    person = make_person(m365_status=M365Status.SYNC_INCOMPLETE)
    source = make_source("S", source_type="manual")
    source.add_class(make_class("6.A", persons=[person]))

    restored = roundtrip(source).get_all_persons()[0]
    assert restored.m365_status is M365Status.SYNC_INCOMPLETE


def test_the_microsoft_365_status_is_written_as_text_not_an_enum(make_person):
    """A JSON file has to hold something JSON can express."""
    from utils.source_serialization import person_to_dict

    written = person_to_dict(make_person())['m365_status']
    assert isinstance(written, str)


def test_a_file_without_the_microsoft_365_fields_still_loads(make_person):
    """A file written before this version simply lacks the keys."""
    from models_m365 import M365Status
    from utils.source_serialization import person_from_dict

    restored = person_from_dict({"first_name": "Jan", "last_name": "Novák",
                                 "class_name": "6.A"})
    assert restored.m365_user_principal_name is None
    assert restored.m365_status is M365Status.UNKNOWN


def test_an_unreadable_microsoft_365_status_does_not_stop_the_load(make_person):
    """A status this version does not know becomes 'not checked'."""
    from models_m365 import M365Status
    from utils.source_serialization import person_from_dict

    restored = person_from_dict({"first_name": "Jan", "last_name": "Novák",
                                 "class_name": "6.A",
                                 "m365_status": "something_from_the_future"})
    assert restored.m365_status is M365Status.UNKNOWN


def test_the_two_directories_are_stored_separately(make_person):
    """
    A person can be in both directories with different values.

    Folding them into one set of fields would lose one of them.
    """
    from utils.source_serialization import person_from_dict, person_to_dict

    person = make_person(ad_display_name="Jan Novák",
                         m365_display_name="Jan Novák (6.A)")
    restored = person_from_dict(person_to_dict(person))

    assert restored.ad_display_name == "Jan Novák"
    assert restored.m365_display_name == "Jan Novák (6.A)"
