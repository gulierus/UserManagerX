# UserManagerX — Version 24, Test Suite and Defects Found By It

## What was built

A pytest suite was added under `tests/`. It runs headless (offscreen Qt), never
touches the real user settings, the real log directory or the network, and
finishes in a few seconds.

| File | Purpose |
|---|---|
| `pytest.ini` | Configuration and the markers `slow`, `integration`, `gui`, `bug` |
| `tests/conftest.py` | Shared fixtures (see below) |
| `tests/test_*.py` | One module per area under test |

### Shared fixtures

| Fixture | What it gives you |
|---|---|
| `qapp` | The single session-wide `QApplication` |
| `dialogs` | Replaces every `QMessageBox` / `QInputDialog` / `QFileDialog` entry point with a recorder, so a test can assert *which* message the user would have seen. **Without it a modal dialog would hang the whole suite.** |
| `accept_dialogs` | Additionally makes `QDialog.exec()` return a configurable result |
| `isolated_settings` | Points `SettingsManager` at a throw-away file in `tmp_path` |
| `isolated_logging_config` | Gives `LoggingConfig` its own config file and log directory |
| `make_person` / `make_class` / `make_source` | Model factories |
| `source_manager` | A fresh, empty `SourceManager` |

An autouse fixture resets the cached password policy between tests so they
cannot influence one another.

### Running it

```bash
cd UserManagerX
python3 -m pytest                 # the whole suite
python3 -m pytest -m bug          # only the regression tests for the defects below
python3 -m pytest tests/test_models.py -v
```

---

## How the defects were found

Each module group was handed to an agent that wrote its tests, ran them, and for
every failure decided whether its own test was wrong or the production code was.
A production defect was kept as a test asserting the **correct** behaviour,
marked `@pytest.mark.bug` + `xfail`, and reported.

All of them have since been fixed, and the `xfail` markers were removed — those
tests are now ordinary passing tests that guard the fixes.

**137 defects were found and fixed.** The first wave (75) is documented below,
grouped by file with the mechanism and the way to verify each one; the second
wave (62) is listed at the end of this document.

---

## models.py (3)

**M1 — the `group_memberships` setter silently discarded the assignment.**
The setter compared the old and new DN sets and only stored the new list when
they differed. Replacing a group object with a refreshed one carrying the same
DN — exactly what *Manage Groups → Verify* produces — kept the stale object, so
the updated name, description and member count were thrown away.

```python
g_old = ADGroup(name="Old", dn="CN=G,DC=x", description="old")
g_new = ADGroup(name="New", dn="CN=G,DC=x", description="new")
p = Person("A", "B", "6.A", group_memberships=[g_old])
p.group_memberships = [g_new]
print(p.group_memberships[0].name)   # before: "Old"   after: "New"
```

**M2 — `person.group_memberships = None` raised `TypeError`** although the
constructor accepts `None`. The setter iterated the value before checking it.

**M3 — undoing a group change left the person stuck in `UPDATE_PENDING`.**
The group branch of `_mark_dirty()` cleared the dirty flag but never restored
`ADStatus.SYNCED`, so a reverted edit still re-sent the person to AD on every
subsequent synchronisation.

---

## utils/class_name_utils.py (5)

**C1 — a class numbered 0 crashed the conversion of the whole source.**
`int_to_roman(0)` raises `ValueError` (Roman notation has no zero) and the
exception escaped through `render_template` and `convert_class_name`.

```python
convert_class_name("0.A", TEMPLATE_ROMAN)
# before: ValueError: Value 0 out of range (1-3999)
# after : returns the name unchanged with an explanatory message
```

**C2 — `render_template()` let that bare `ValueError` escape** instead of
raising the `TemplateError` its callers catch.

**C3 — `shift_class_name()` raised for Roman names when `min_year <= 0`.**

**C4 — `roman_to_int()` raised `AttributeError` on non-string input** although
it tolerates `None`.

**C5 — `validate_template()` lower-cased the placeholder the user typed.**
`str(exc).capitalize()` upper-cases the first letter *and* lower-cases the rest,
so typing `{Arabic}` produced *"unsupported placeholder '{arabic}'"* — pointing
at something the user had not written.

---

## utils/source_analysis.py (4)

**S1 — `rename_class()` could delete the wrong class.** The merge path used
`list.remove()`, which compares by value; `Class` is a dataclass, so two empty
classes with the same name are "equal" and the merge *target* could be removed
instead of the source. It now removes by identity.

**S2 — "remove the classes without a number" also deleted the unnamed ones**
and their students, even though those are reported as a separate issue with
their own solutions. The user lost records they were never shown.

**S3 — the duplicate-student list showed the folded comparison key** (`jan
novak`) instead of the real spelling (`Ján Novák`), and did not say how many
copies there were.

**S4 — a shift of 0 years was diagnosed as broken data** ("no class name
contains a number") instead of "the shift amount is zero".

---

## utils/ad_utils.py (6)

**A1 — generated user names could contain non-ASCII letters.** `str.isalnum()`
is true for any alphanumeric Unicode character, so letters NFKD cannot
decompose (`Ł`, `ß`, Cyrillic, Greek) survived into the user name — which the
generator's own `USERNAME_PATTERN` then rejected. A transliteration table was
added (`Ł→l`, `ß→ss`, `Ø→o`, `Æ→ae`, `Đ→d`, `Þ→th`, …).

```python
generate_username("Łukasz", "Wiśniewski", set())
# before: "wiśniewskiłukasz"  (rejected by the validator)
# after : "wisniewskilukasz"
```

A name with nothing transliterable at all (pure Cyrillic/Greek) still raises a
clear `ValueError` — inventing a user name unrelated to the student would be
worse, and the callers already report such records as skipped.

**A2 — off-by-one in the duplicate guard.** The loop exits both when it runs
out of attempts and when the last attempt found a free name; the code tested the
attempt counter, so a perfectly good name found on the 1000th attempt was thrown
away and `RuntimeError` raised. It now tests the result.

**A3 — `last_name_index=None` crashed** although the parameter is typed
`Optional[int]`.

**A4 — `generate_username` and `build_username_base` disagreed about `None`.**
The generator's default is `-1` (last surname) while the validator's helper
mapped `None` to the *first* surname, so for a double surname the validator
expected a different name than the generator produced and raised a bogus
*"doesn't follow standard pattern"* warning.

**A5 — the convention check rejected names the generator itself produces**
when the surname was shortened via `last_name_length`.

**A6 — `validate_username` passed a user name its own pattern rejects.**
The specific checks did not cover every way the pattern can fail, so such a name
was reported as perfectly fine. A catch-all now names the rule that was broken.

---

## utils/password_policy.py (4)

**P1 — `from_dict` raised `OverflowError` on an infinite stored length.**
Only `TypeError` and `ValueError` were caught, so a corrupted settings file
crashed the application at start-up.

**P2/P3 — `str()` coercion turned junk into real password characters.**
A stored `null` became the literal string `"None"`, so `N`, `o`, `n` and `e`
became the "special characters" every generated password had to contain.

**P4 — the "no character type available" consistency check was dead code**,
because `full_pool()` falls back to the ASCII letters. A policy that excludes
every character class was therefore accepted as valid.

---

## utils/source_serialization.py (9)

**R1 — exported and imported objects shared their `metadata` dictionaries**
with the live model, so editing an imported source also changed the exported
one. Everything is deep-copied now.

**R2 — `source_to_dict()` could not actually be serialised.** It promised a
JSON-serialisable dictionary but copied `metadata` verbatim, and the AD
discovery step stores `datetime` objects there — so `json.dumps` failed at the
very last step of an export. Non-encodable values are converted to text.

**R3 — `ad_dn` was dropped on export**, so the Active Directory distinguished
name was lost on every export/import round trip and a fresh discovery was needed.

**R4/R5 — `group_from_dict` crashed instead of skipping a bad entry** — a
non-numeric `members_count` raised `ValueError` and a non-string `dn` raised
`AttributeError`, aborting the import of the whole file over one malformed group.

**R6 — `person_from_dict` accepted an explicit `null`** for a required field
and built a Person with no name, which then broke credential generation much
further downstream.

**R7/R8/R9 — a `metadata` field that is not a dictionary was stored verbatim**
at person, class and source level, although `source_info` was already type
checked.

---

## utils/settings_manager.py (5)

**T1 — one unserialisable value froze every later save.** `set()` stored the
value first and only then tried to write the file; the write failed, the value
stayed in memory, and *every* subsequent save failed too — silently, because the
return value was not checked. The value is now rejected before it is stored.

**T2 — `delete()` stopped at the first category.** `any(... for ...)`
short-circuits, so a key present in several categories was removed from one and
left in the others.

**T3 — `delete()` lost a key whose value was `None`.** `pop(key, None)` returned
`None`, which was read as "not found", so the key was dropped from memory but
never written out — and came back on the next start.

**T4 — `delete()` always announced category `"general"`**, so the listeners of
the category that really changed never refreshed.

**T5 — `_load_defaults()` shared the nested dictionaries** with the
module-level `DEFAULT_SETTINGS`: `dict(vals)` copies only one level, so storing
a window geometry mutated the defaults themselves and every later "reset to
defaults" restored the modified value.

---

## utils/logging_config.py (2)

**L1 — a boolean was accepted as `max_bytes`.** `isinstance(True, int)` is
`True` in Python, so `max_bytes=True` became `maxBytes=1` and the log file
rotated on every single record.

**L2 — a string `backup_count` in the config file broke the Logs tab.**
`range()` raised `TypeError`, so `get_log_files()` returned nothing and the
historical log list came up empty with no explanation.

---

## utils/encryption.py and utils/usrx_format.py (13)

**E1 — the USRX path accepted an empty password.** The standard path validated
it, the container path did not, so leaving the password field blank produced a
file that looked encrypted but was protected by nothing.

**E2 — the USRX path accepted empty data** where the standard path rejects it.

**E3 — a tampered file was always blamed on the user's password.** The inner
handler produced the correct message ("incorrect password *or corrupted file*"),
then the outer `except Exception` matched on "incorrect password" and rewrote it,
dropping the second half.

**E4 — the documented `ValueError` for a malformed file was swallowed** and
re-raised as `RuntimeError`, so callers that handled the documented type never
saw it. (`UnicodeDecodeError` is a `ValueError` subclass, so a binary file is
handled explicitly and still reported as a `RuntimeError`.)

**E5 — base64 garbage surfaced as `ValueError('Nonce cannot be empty')`** from
PyCryptodome, because `b64decode` silently skips characters it does not
understand. It now validates strictly and reports a readable error.

**E6/E7 — `_encrypt_to_usrx` and `USRXFile.write` mutated the caller's
metadata dictionary**, injecting `salt`, `nonce`, `tag` and `created_by` into an
object the caller still owned.

**E8 — a truncated container returned a short payload** which was then handed
to the decrypter, so the user was told their password was wrong. `read()` now
compares the payload length against the size declared in the header.

**E9 — an unknown encryption label was silently stored as `'none'`**, marking
ciphertext as plaintext; the reader then handed back the raw encrypted bytes as
if they were the document. An unknown label is now refused.

---

## services/ad_group_service.py (1) — found by static analysis

**G1 — renaming a group template always crashed.** `rename_template()` calls
`datetime.now()` but the module never imported `datetime`:

```
NameError: name 'datetime' is not defined
```

Worse, it failed *after* `template.name = new_name` had already been applied, so
the rename silently took effect while the caller saw an exception and assumed it
had not.

```python
m = GroupTemplateManager(); m.add_template(GroupTemplate(name="Students"))
m.rename_template("Students", "Pupils")
# before: NameError - but the template WAS renamed
# after : True
```

---

## operations/pdf_export.py (1) — found by static analysis

**D1 — the application required Python 3.11 for nothing.**
`from typing import ... Self` — `typing.Self` was added in 3.11 and the name was
**never used**. On Python 3.10 the import failed, so the Operations tab and
therefore the whole application refused to start. The unused name was removed.

---

## ui/class_template_widget.py (1)

**U1 — the conversion preview computed a per-row colour and threw it away**,
so every note rendered in the same grey and the status (changed / already
correct / no number found) was invisible.

---

## Verifying the whole set

```bash
cd UserManagerX
python3 -m pytest              # 3675 passed, 0 xfailed
python3 -m pytest -m bug       # only the regression tests for the defects above
python3 -m pyflakes $(find . -name "*.py" -not -path "./tests/*" -not -name "resources_rc.py") | grep -i "undefined name"
                               # no output - was 1 before (G1)
```

---

# Second wave: 62 further defects, all fixed

The test modules for the UI, the services and the data sources found a further
**62 defects**. Every one was fixed and its test un-xfailed, so each is now an
ordinary passing regression test. They are still marked `bug`:

```bash
python -m pytest -m bug        # every regression test for a fixed defect
```

Each fix was checked by a separate reviewer whose job was to find a weakened
test, a fix that hides the symptom rather than curing the cause, or an edit
outside the agent's own files. All eleven reviews came back clean, and the
suite's assertion count went **up** (4340 → 4350), never down.


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/main.py

**StudentManagementSystem.closeEvent** — Qt delivers QCloseEvent to the window only, never to the widgets inside it. The window's closeEvent cleaned up the font manager and accepted the event, but never closed its five tabs, so every per-tab closeEvent (LogViewerTab's in particular) was skipped: the RealTimeLogHandler stayed attached to the root logger after the window was gone, its 5 s refresh timer kept firing, and the AD tab's worker threads were never joined.
  *Fix:* Before the font-manager cleanup, closeEvent now iterates over self.tab_widget pages and calls close() on each one, so every tab runs its own closeEvent (timer stop, thread wait, root-logger handler removal). Each tab is closed inside its own try/except (with an outer guard for a window whose tab_widget failed to build) so a broken tab logs a warning instead of trapping the user in the application - the same policy the existing font-manager cleanup already follows, and which test_close_event_still_accepts_when_the_font_cleanup_fails pins.
  *Guarded by:* `TestMainWindow::test_closing_the_window_detaches_the_log_viewer_log_handler`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/operations/ad_management.py

**ADManagementWidget.refresh_person_table** — Every repaint piled up one orphaned 'Edit' QPushButton per person. The cleanup loop called removeCellWidget() + deleteLater(), but both only SCHEDULE the destruction: until the event loop processes deferred deletes the buttons are still children of the table viewport, still painted, and still holding a strong reference to the Person they captured in their lambda. Four repaints of a 4-person table left 20 live buttons.
  *Fix:* After removeCellWidget() the widget is now detached immediately with setParent(None) before deleteLater() frees it. Detaching is what actually drops it out of the table's child list / paint path; deleteLater() still does the memory release at a safe point. Comment in the code explains the schedule-vs-detach distinction.
  *Guarded by:* `test_refresh_does_not_leak_edit_buttons`

**ADManagementWidget.edit_person** — The set of already-taken user names was built with `if p.ad_username and p != person`. Person compares by value, so a second student with the same first name, last name, class and login is `==` to the edited one and was filtered out of the taken set - the editor would then hand out a duplicate sAMAccountName to a field-identical twin.
  *Fix:* Changed the filter to identity: `p is not person`. Only the record actually being edited is excluded. This is exactly the pattern ui/comparison_tab.py already uses for the same computation, so the two editors now agree. Comment added explaining why equality is wrong here.
  *Guarded by:* `test_edit_person_reserves_the_username_of_a_field_identical_twin (and the existing test_edit_person_does_not_reserve_the_persons_own_username still guards the other direction)`

**ADManagementWidget._generate_credentials_for / generate_missing_credentials** — 'Generate missing credentials' ran the full generator on any person that lacked EITHER value. A student who only lacked a password got a brand-new user name - and because their current login is seeded into existing_usernames, a numbered one ('svobodapetr' -> 'svobodapetr2'), orphaning their AD account and everything derived from it (home directory, mail).
  *Fix:* _generate_credentials_for gained an `only_missing: bool = False` keyword. When set, each of user name / password / display name is generated only if the person does not already have it, and the 'both names required' precondition is now only enforced for the parts actually derived from the name (a password needs no name). generate_missing_credentials passes only_missing=True; generate_all_credentials keeps the old overwrite-everything behaviour unchanged (default False), as do the direct two-argument calls from the tests.
  *Guarded by:* `test_generate_missing_credentials_keeps_an_existing_username (existing test_generate_missing_credentials_leaves_complete_persons_alone, ..._skips_and_reports_broken_records and the generate_all_credentials tests still pass unchanged)`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/operations/pdf_export.py

**PDFExportWidget.load_table_from_source** — When the selected classes contained no persons, the method emptied self.table_data, showed the 'No Data' message and returned before repainting the table, so the rows of the previous load stayed on screen (and 'Rows: N' kept the old count) while the model was already empty - the view no longer matched what would be exported.
  *Fix:* The empty branch now calls self._populate_table() before showing the message, so the table is rebuilt from the (empty) data exactly like every other load path. The cause is cured at the single point where the view was skipped, not by clearing the widget from the caller.
  *Guarded by:* `TestLoadTableFromSource::test_selecting_only_an_empty_class_clears_the_displayed_table`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/services/ad_services.py

**SyncPlanner.create_sync_plan** — Only ADStatus.EXISTS_IN_AD reached the update branch. A SYNCED person that was edited becomes UPDATE_PENDING (models._mark_dirty), so she matched neither the create branch (she has a DN) nor the update branch and was silently dropped from the plan - every edit of an already synchronised student never reached Active Directory.
  *Fix:* Replaced the `elif person.ad_status == ADStatus.EXISTS_IN_AD` with a plain `else`, so everything that reaches that point (it is dirty, it has a DN, it is not AMBIGUOUS) is planned as an update: EXISTS_IN_AD, UPDATE_PENDING and a still-dirty SYNCED person. Using `else` also means no future status can be silently dropped again - that was the cause, not the single missing enum member.
  *Guarded by:* `test_create_sync_plan_plans_an_update_for_an_edited_already_synced_person`

**SyncPlanner.create_sync_plan** — An AMBIGUOUS person (discovery found several matching AD accounts, so no DN was recorded) fell into the `or not person.ad_dn` create branch and was scheduled for creation - the application would add yet another duplicate account for a student who already has too many.
  *Fix:* Added an explicit guard before the create/update decision: an AMBIGUOUS person is skipped with a warning naming her, because which account is hers is a decision only the user can make. She is not put into incomplete_persons on purpose - that bucket is rendered as "Missing: <fields>" in the plan dialog and her record is complete; nothing is written for her until the ambiguity is resolved.
  *Guarded by:* `test_create_sync_plan_never_creates_an_ambiguous_person`

**SyncPlanner.create_sync_plan / escape_dn_value / unescape_dn_value / first_rdn_value / PersonsSyncService._execute_create** — DN components were built with plain string formatting: `f"OU=Trida-{person.class_name},{base_dn}"` and `f"CN={cn},{target_ou}"`. A class name such as "6,A" (or a display name "Novák, Jan") turned one RDN into two, so the account was created in a different place in the tree or the server refused the DN. The OU name was then re-derived with `target_ou.split(',')[0].split('=')[1]`, which mangles exactly those escaped values.
  *Fix:* Added RFC 4514 helpers escape_dn_value/unescape_dn_value/first_rdn_value (escaping \\ , + " < > ; = plus leading #/space and trailing space) and used escape_dn_value for the class name in the target OU and for the CN in _execute_create; the OU name for create_ou is now taken with first_rdn_value, which splits on unescaped commas and unescapes the value, so create_ou still receives the human name ("Trida-6.A", or "Trida-6,A" for the escaped case). ldap3's own escape_rdn was not used because ldap3 is an optional dependency here (services/ldap_compat.py).
  *Guarded by:* `test_create_sync_plan_escapes_a_class_name_containing_a_comma`

**PersonsSyncService._map_field_to_ldap** — The field->LDAP mapping had no entry for home_directory (or home_drive), although both are editable in ui/property_editor.py and are dirty-tracked by Person. _execute_update mapped them to None, skipped them without a word, and still reported the sync as SUCCESS - the home directory never reached AD.
  *Fix:* Added 'home_directory': 'homeDirectory' and 'home_drive': 'homeDrive' to the mapping, with a comment explaining why ad_ou_path deliberately stays unmapped (moving an object needs a modify-DN operation, not an attribute write). ConflictDetector._map_to_ad_attribute was left alone: it falls back to the field name for unmapped fields and a non-xfail test pins that pass-through.
  *Guarded by:* `test_execute_sync_writes_a_changed_home_directory_to_ad`

**PersonsSyncService._execute_create** — person.ad_dn was assigned only at the very end of the happy path. When create_user succeeded but _set_password failed, the method returned early with a 'partial success', so the account existed in AD while the person still had ad_dn=None and ad_status=NOT_IN_AD - the next synchronisation planned another create and produced a duplicate account.
  *Fix:* The DN (and ADStatus.EXISTS_IN_AD) is now recorded immediately after create_user reports success, i.e. as soon as the object really exists, and the redundant assignment at the end of the happy path was removed (the status is still raised to SYNCED there). A retry therefore plans an update - which also re-applies the password, since the dirty state is not reset on the failure path. Failure paths before create_user still leave ad_dn None.
  *Guarded by:* `test_execute_sync_records_the_dn_of_a_user_created_without_a_password`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/sources/ad_source.py

**ActiveDirectorySourceWidget.on_load / _attribute_text** — Attributes were read as `str(entry.givenName.value) if hasattr(entry, 'givenName') else ''`. ldap3 exposes every *requested* attribute on an entry even when the directory returned no value, so hasattr() is True and .value is None -> str(None) == 'None', a truthy fake name. Accounts without givenName/sn were imported as persons named 'None'.
  *Fix:* Added a module-level `_attribute_text(entry, name)` helper that reads via `entry[name]`, catches (KeyError, AttributeError) for absent attributes, unwraps list values and maps missing/None to ''. All name and AD-field reads use it (ad_username/ad_display_name/ad_email as `... or None` to keep Optional semantics), so the existing `if not first_name or not last_name: continue` guard actually fires.
  *Guarded by:* `tests/test_sources_edupage.py::test_ad_load_skips_accounts_without_a_first_or_last_name`

**ActiveDirectorySourceWidget.on_load (user search)** — The per-OU user search requested only ['givenName','sn','displayName','sAMAccountName','mail'], but the loop then did `person.metadata['ad_dn'] = str(entry.distinguishedName)`, raising LDAPCursorAttributeError. The whole import aborted into the generic error dialog, so no AD source was created whenever a class OU actually contained users.
  *Fix:* Added 'distinguishedName' to the requested attributes of the user search and read it through _attribute_text(), falling back to ldap3's always-available `entry.entry_dn` if a directory omits it.
  *Guarded by:* `tests/test_sources_edupage.py::test_ad_load_imports_the_students_of_every_class_ou`

**ActiveDirectorySourceWidget.on_load (class OU filtering)** — LDAP matches `(ou=Trida-*)` case-insensitively, so the directory legitimately returns units spelled 'trida-7b'; the post-search guard `ou_name.startswith('Trida-')` threw exactly those away, and `ou_name.replace('Trida-','')` would not have stripped the prefix either.
  *Fix:* Introduced the module constant CLASS_OU_PREFIX = 'Trida-'; acceptance is now `ou_name.lower().startswith(CLASS_OU_PREFIX.lower())` (the same comparison rule LDAP itself used to return the entry), and the class name is derived by slicing off the prefix length, so 'Trida-6A' -> '6A' and 'trida-7b' -> '7b' keep the directory's own spelling.
  *Guarded by:* `tests/test_sources_edupage.py::test_ad_load_accepts_a_class_ou_written_in_lower_case (test_ad_load_strips_the_trida_prefix_from_the_class_name still guards the canonical case)`

**ActiveDirectorySourceWidget._ou_search_filter** — The 'Search Format' combo box (uppercase / lowercase / both) was built and shown but never read - the OU search always used the hard-coded literal '(ou=Trida-*)', so the user's choice had no effect at all.
  *Fix:* Added `_ou_search_filter()`, which reads `format_combo.currentData()` and builds the filter in the selected spelling: '(ou=Trida-*)' for uppercase, '(ou=trida-*)' for lowercase, '(|(ou=Trida-*)(ou=trida-*))' for both; on_load passes it as search_filter. Client-side acceptance stays case-insensitive (previous fix), matching AD's caseIgnoreMatch on 'ou', so a format choice never silently hides a class that the server returned.
  *Guarded by:* `tests/test_sources_edupage.py::test_ad_load_honours_the_selected_search_format`

**ActiveDirectorySourceWidget.on_load (ImportError handler)** — `except ImportError: progress.close()` referenced a QProgressDialog that is only created *after* the ldap3 import succeeds, so a missing ldap3 produced `NameError: name 'progress' is not defined` instead of the 'ldap3 package is not installed' dialog.
  *Fix:* Initialised `progress = None` before the try block and guarded both handlers with `if progress is not None: progress.close()` (replacing the `'progress' in locals()` probe in the generic handler with the same explicit check), so the user always gets the intended message.
  *Guarded by:* `tests/test_sources_edupage.py::test_ad_load_reports_a_missing_ldap3_package`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/tests/test_groups_import.py

**make_export_dialog fixture / test_accepted_form_reports_path_method_password** — The ExportDialog factory had no way to control which encryption backends the dialog believes in, so the GPG parametrisation of test_accepted_form_reports_path_method_password silently depended on GPG being absent from the developer's machine.
  *Fix:* make_export_dialog now takes an optional `available` map and monkeypatches `ui.export_dialog.get_available_methods`, mirroring the existing make_import_dialog fixture; the value-reporting test passes ALL_AVAILABLE so it exercises what it claims to (path/method/password round-trip) on any machine. No assertion was removed or loosened.
  *Guarded by:* `TestExportDialog::test_accepted_form_reports_path_method_password[1-gpg]`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/ui/comparison_tab.py

**SourcePanel.on_edit_person** — The duplicate check after the property editor closed was gated on `(first_name, last_name) != previous_identity`, i.e. only on a rename. Moving a student into a class that already holds a namesake changed nothing in that tuple, so `_warn_about_duplicate` was never called and the user got no warning about the duplicate they had just created.
  *Fix:* The key that decides whether a record is a duplicate is `Person.get_normalized_name()` = (normalized first, normalized last, class_name). The snapshot taken before the dialog and the comparison after it now both use that key, so a rename AND a class move both trigger the check. `_warn_about_duplicate` itself was already correct (it compares normalized names inside the person's current class and skips the person itself), so it only speaks up when there really is a clash - the 'name stays unique' test still sees no warning.
  *Guarded by:* `tests/test_comparison_tab.py::test_edit_person_warns_when_a_class_change_creates_a_duplicate`

**SourcePanel.on_delete_persons** — Deletion located the selected record with `if person in cls.persons: cls.persons.remove(person)`. `Person` is a dataclass with value equality, so two students carrying identical data compare equal: the loop stopped at the first class holding an equal record and `list.remove()` deleted that one, leaving the record the user actually selected in place. Deleting one of a duplicate pair was therefore impossible.
  *Fix:* The class is now found with an identity scan (`any(existing is person for existing in cls.persons)`) and the record removed with `Class.remove_person()`, which models.py already documents as matching by identity precisely for this case. The removal count and the messages are unchanged.
  *Guarded by:* `tests/test_comparison_tab.py::test_delete_persons_removes_the_record_that_was_actually_selected`

**SourcePanel._move_person_to_class** — Same value-equality bug as the delete path (found while tracing defect 1, not pinned by its own xfail): the move detached the person with `if person in cls.persons: cls.persons.remove(person)`, so a student whose data happened to equal an untouched namesake in an earlier class could yank that namesake out of their class and leave the edited record duplicated.
  *Fix:* Detaching now scans by identity and calls `Class.remove_person()`, so exactly the edited object leaves its old class before being appended to the target class. Behaviour for every non-duplicate move is unchanged (the existing move / create-missing-class tests still pass).
  *Guarded by:* `tests/test_comparison_tab.py::test_edit_person_moves_the_student_when_the_class_name_changed`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/ui/export_dialog.py

**ExportDialog._on_accept** — The export dialog accepted any encryption method the combo offered without ever asking whether that backend exists. Choosing GPG on a machine without python-gnupg was accepted here and only blew up deep inside the encryption layer, after the user had typed the password twice.
  *Fix:* Added the module-level `from utils.encryption import get_available_methods` and, right after the destination check (the same position ImportDialog uses), a query of the availability map: an unavailable method produces a 'Method Not Available' warning and the dialog stays open. `self._method` now reuses the value already read for that check instead of re-reading the combo. Collateral: the sibling test `test_accepted_form_reports_path_method_password[1-gpg]` asserted that picking GPG is accepted with no dialog, which directly contradicted this defect on any machine without GPG. That test is about what a completed form reports, not about the local toolchain, so I made it declare both backends available through the fixture (see next item) rather than bending the code to it.
  *Guarded by:* `TestExportDialog::test_method_without_a_backend_is_refused`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/ui/group_management_dialog.py

**GroupManagementDialog._extract_cn_from_dn / _split_rdns** — The DN was split with `dn.split(",")`, so an RFC 4514 escaped comma inside a value (`CN=Sales\, EU,OU=Groups`) was treated as an RDN separator and the group was named 'Sales\'.
  *Fix:* Added `_split_rdns()`, which walks the DN and only breaks on an *unescaped* comma, plus `_unescape_dn_value()`, which undoes `\,`-style escapes and `\XX` hex escapes (consecutive hex escapes are decoded together as one UTF-8 sequence). `_extract_cn_from_dn` now uses both.
  *Guarded by:* `TestExtractCnFromDn::test_escaped_comma_stays_part_of_the_cn`

**GroupManagementDialog._extract_cn_from_dn** — A DN with no usable RDN ('', ',', '   ') fell through to `parts[0].strip()`, which is the empty string - the 'Unknown Group' placeholder was unreachable except via the exception path, so a group could end up with an empty display name.
  *Fix:* The fallback now returns the first RDN only when it is non-empty and the placeholder otherwise (`return first or "Unknown Group"`), which is the same outcome the non-string DN path already produced. `CN=` still returns the empty CN value verbatim, as the existing parametrised test requires - that is a present-but-empty CN, not a missing RDN.
  *Guarded by:* `TestExtractCnFromDn::test_dn_without_any_rdn_falls_back_to_a_usable_name["", ",", "   "]`

**GroupManagementDialog.refresh_group_list** — `group_verified_label` was created once in _create_groups_tab() with the text 'Verified: 0' and never written again, so it kept reporting zero after a verification run or an import.
  *Fix:* refresh_group_list() - which already computes `verified_count` for the count label - now also writes `self.group_verified_label.setText(f"Verified: {verified_count}")`, so the dedicated counter is rebuilt with the rest of the list.
  *Guarded by:* `TestGroupListRendering::test_verified_label_tracks_the_verified_groups`

**GroupManagementDialog.refresh_template_list** — Template row captions were built with HTML numeric entities ('&#9888; unverified'). A QListWidgetItem draws plain text, so users saw the literal '&#9888;' instead of a warning glyph.
  *Fix:* The entities in the row text were replaced by the real characters they denote (⚠, ✓); a comment records that QListWidgetItem text is plain text.
  *Guarded by:* `TestTemplateListRendering::test_row_text_contains_no_raw_html_entities`

**GroupManagementDialog._create_groups_tab / _create_templates_tab / context menus** — All 15 action buttons and the 5 context-menu QActions carried HTML entities ('&#10133; Add Group'). Buttons and menu items render plain text, and worse, Qt eats the leading '&' as a mnemonic marker, so the captions read '#10133; Add Group'.
  *Fix:* Every entity in a QPushButton or QAction caption was replaced by the real character (🔍 ➕ ✓ ❌ 🗑 💾 📂 ✏ 📋). The entities in genuinely rich-text widgets (QLabel with markup, QTextEdit.setHtml in the create-template validator) are correct there and were left untouched; a changelog note '4a' in the module docstring records the distinction.
  *Guarded by:* `TestDialogChrome::test_buttons_are_labelled_with_real_characters`

**GroupManagementDialog.handle_imported_groups** — Duplicates were detected only against `existing_dns`, a snapshot of the list taken before the loop. Two entries with the same DN inside one imported file were therefore both classified as new and both appended, breaking the one-group-per-DN rule that add_group_manually enforces (and, when the DN also existed in the list, producing two comparison rows and two 'replaced' counts for one stored group).
  *Fix:* The scan now tracks the DNs already taken by an earlier entry of the same file: a repeated DN is logged, skipped (first occurrence wins) and counted into the 'Skipped N duplicate(s)' line, so the list stays DN-unique and the summary stays truthful.
  *Guarded by:* `TestHandleImportedGroups::test_import_never_stores_the_same_dn_twice`

**GroupManagementDialog.handle_imported_templates** — Same snapshot bug for templates, plus a lying summary: two templates of one name were both counted as new, GroupTemplateManager.add_template() silently refused the second (returning False), and the dialog still reported 'Added 2 new template(s).' while only one was stored.
  *Fix:* In-file duplicates are detected by name, logged, skipped and counted as duplicates; and the summary now counts the add_template() calls that actually returned True instead of the length of the candidate list, so it can never promise more templates than the manager holds.
  *Guarded by:* `TestHandleImportedTemplates::test_summary_counts_only_the_templates_really_added`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/ui/log_directory_widget.py

**LogDirectoryWidget.get_log_dir** — The getter documents an absolute directory but simply joined base/folder, so a hand-typed relative base path ('relative_logs') was handed out as a relative fragment - by get_log_dir() and therefore also by get_full_path(), and it was stored that way in the 'log_dir' setting.
  *Fix:* After expanduser(), a still-relative result is resolved against get_application_directory() - the very place a relative path would end up once the rotating handler opens the file. This matches what set_from_log_dir_and_file() already did for stored relative values, so the widget is now consistent in both directions.
  *Guarded by:* `tests/test_settings_ui.py::test_a_relative_base_path_still_yields_an_absolute_location[get_log_dir|get_full_path]`

**LogDirectoryWidget._build_ui (base-path reset button)** — The base-path reset button was wired to setText(get_application_directory()), which discarded the default_base_path passed to the constructor; its folder and filename siblings correctly restore self._default_folder / self._default_file.
  *Fix:* The button now restores self._default_base (which is the application directory only when no other default was configured), and the tooltip was reworded to say so while keeping the 'application directory' wording that identifies it.
  *Guarded by:* `tests/test_settings_ui.py::test_base_reset_button_restores_the_configured_default_base (and the existing test_base_reset_button_restores_the_application_directory still guards the no-default case)`

**LogDirectoryWidget.validate** — A NUL byte in the folder name or the log filename was never reported: '\0' is not in _INVALID_FILENAME_CHARS, and the file-system probe cannot catch it either because Path.exists() returns False for a path with an embedded null byte instead of raising. The status line then claimed the path was usable while opening the log file would raise ValueError.
  *Fix:* Added explicit null-character checks for the folder and the filename, mirroring the one the base path already had, with the same message style ('Folder name contains a null character.' / 'Log filename contains a null character.').
  *Guarded by:* `tests/test_settings_ui.py::test_validate_reports_a_null_byte_in_the_folder_or_the_filename[both cases]`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/ui/log_viewer_tab.py

**LogViewerTab.append_colored_line** — Every message was appended with insertHtml('<span>...</span><br>'), so all messages stayed inside ONE paragraph: document().blockCount()/lineCount() never grew past 1 and each insert re-laid out the whole growing paragraph (measured: 3000 lines = 66 s of blocked GUI time on a realised layout).
  *Fix:* The message is now inserted as its own block: a cursor is moved to End, insertBlock() starts a new paragraph (skipped while the document is empty, so no leading blank line), then insertHtml() writes the coloured, escaped span into that block. Measured again after the change: 3000 lines = 0.22 s, blockCount() == 3000.
  *Guarded by:* `test_append_colored_line_starts_a_new_paragraph_per_message (plus the still-passing escaping/colour/order tests)`

**LogViewerTab.append_colored_line** — The line cap was enforced on doc.lineCount(), which was permanently 1 because of the single-paragraph bug above, and it deleted with MoveOperation.Down - so the real-time view was never trimmed and grew without bound.
  *Fix:* Trimming now works on blocks, which is what the view actually holds: excess = doc.blockCount() - MAX_REAL_TIME_LOG_LINES, and when positive a cursor selects from Start over `excess` NextBlock moves and removes the selection, dropping the oldest messages. MAX_REAL_TIME_LOG_LINES is still read as a module global at call time, so the documented cap (and a test override) applies.
  *Guarded by:* `test_append_colored_line_trims_the_view_to_the_line_cap`

**LogViewerTab.add_real_time_log** — The 'Lines: visible / stored' label was updated only after the two early returns that hide a non-matching record, so any message the level or search filter hid was stored but never counted - the label kept showing a stale total (e.g. 'Lines: 1 / 1' with two records stored).
  *Fix:* The inline duplicate of the filter logic was replaced by the existing _log_matches_filter() helper (identical rules, one source of truth); it now only decides whether to render the line, and the counter is recomputed unconditionally afterwards from the stored buffer, so hidden records are counted in the total.
  *Guarded by:* `test_add_real_time_log_keeps_the_stored_counter_up_to_date`

**LogViewerTab.update_real_time_filter** — The 'no stored messages' early return happened after _set_filtering_state(True, ...) had disabled the controls in an earlier run, and never reset the filtering state - if the buffer was cleared between a keystroke and the debounced filter, the level combo stayed disabled forever.
  *Fix:* The early-return branch now calls _set_filtering_state(False, 'real-time') before returning, so bailing out always releases the UI. It still starts no thread and still reports 'Lines: 0'.
  *Guarded by:* `test_update_real_time_filter_re_enables_the_ui_when_there_is_nothing_to_do`

**LogViewerTab.load_log_file** — Item data was validated for being a dict and for 'path', but the large-file check then did log_file['size'] unguarded - a truncated entry raised KeyError inside a Qt slot (which crosses the C++ boundary and is only printed, not handled).
  *Fix:* Added the missing validation next to the existing ones: if log_file.get('size') is not an int/float the user gets a QMessageBox.warning('Log file size not found') and the loader is not started, matching how a missing 'path' is handled.
  *Guarded by:* `test_load_log_file_refuses_an_entry_without_a_size`

**LogViewerTab._on_hist_filter_progress** — The slot stripped only a previous ' | Filtered:' suffix before appending its own ' | Filtering... x/y', so consecutive progress emissions concatenated: 'File: app.log | Lines: 12 | Filtering... 0/1000 | Filtering... 500/1000 | Filtering... 1000/1000'.
  *Fix:* The slot now strips both kinds of previous counter (' | Filtering...' and ' | Filtered:') from the label before appending the current position, so each update replaces the previous one - the same normalisation _on_hist_filter_finished already did.
  *Guarded by:* `test_hist_filter_progress_does_not_pile_up_counters (and the two neighbouring progress tests, still passing)`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/ui/operations_tab.py

**OperationsTab.on_sources_changed** — When the source that was selected got removed, populate_sources() rebuilt the combo with blockSignals(True), so the combo fell back to '(Select Source)' without ever emitting currentTextChanged. findText() of the vanished name returned -1 and the method simply did nothing, leaving current_source and all three operation widgets (AD management, PDF export, JSON export) still pointing at - and operating on - the deleted source behind a placeholder label.
  *Fix:* Added the missing else branch: when the previous selection can no longer be found in the rebuilt combo, on_source_changed(self.source_combo.currentText()) is invoked by hand. That runs the normal selection path, so current_source becomes None and each widget is cleared via set_source(None). setCurrentIndex(0) alone would not work here because the combo is already at index 0 after the rebuild and would emit nothing.
  *Guarded by:* `TestOperationsTabSourceSelection::test_removing_the_selected_source_disarms_the_operation_widgets`

**OperationsTab.__init__ / OperationsTab.on_source_modified** — OperationsTab subscribed only to source_added and source_removed. Editing a source elsewhere (comparison tab, or the AD widget itself) emits SourceManager.source_modified, which OperationsTab ignored, so the PDF export class list and the JSON export statistics kept showing the source as it looked at the moment it was picked. Only ADManagementWidget survived this because it subscribes to source_modified on its own.
  *Fix:* Connected source_modified to a new on_source_modified(source_name) handler that, when the modified name matches the currently selected source, pushes the same source object through set_source() on all three operation widgets, which is the existing API each of them uses to rebuild its view. Guarded by the name check so an edit to an unrelated source does not touch the tab; refresh paths do not emit notify_source_modified, so there is no feedback loop.
  *Guarded by:* `TestOperationsTabSourceSelection::test_a_modified_source_refreshes_every_operation_widget`

**OperationsTab.init_ui** — QStackedWidget shows its first page (AD management) as soon as a widget is added, but the QListWidget of operations was left with no current row (-1). The user therefore saw the AD page while no row in 'Available Operations' was highlighted, and the first click on row 0 produced no visible change.
  *Fix:* After the three pages are added to the stack, init_ui calls operation_list.setCurrentRow(0). Doing it at that point (not right after addItems, when operation_stack does not yet exist) lets the already-connected currentRowChanged handler run normally and leaves list and stack in agreement from the start.
  *Guarded by:* `TestOperationsTabOperationList::test_the_initially_visible_operation_is_the_highlighted_one`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/ui/pdf_export_settings_widget.py

**PDFExportSettingsWidget.validate_export_settings / _is_encryption_method_usable / _apply_settings** — The encryption selector lists 'pikepdf' unconditionally (and, in the widget I do not own, is_method_available('pikepdf') returns True with the comment 'Always available if pikepdf is installed'). validate_export_settings therefore green-lit an export that died much later in utils/pdf_export_task.py with 'pikepdf library is not installed'. Worse, pikepdf is the FIRST combo entry, i.e. the out-of-the-box default.
  *Fix:* Added _is_encryption_method_usable(): a '*-unavailable' entry is unusable, and 'pikepdf' is only usable when importlib.util.find_spec('pikepdf') finds the library. validate_export_settings refuses a method that is not usable with a '... is not available ...' message, and _apply_settings moves the combo off an unusable preselection to the first usable method, so the default settings still validate (AES-GCM here) instead of defaulting to an export that cannot run. The deeper defect (EncryptionSelectorWidget.is_method_available hardcoding True for pikepdf) is in ui/encryption_selector_widget.py, which I do not own - the gate that decides whether an export may start is fixed in my file.
  *Guarded by:* `TestSettingsWidgetValidation::test_pikepdf_is_refused_when_the_library_is_missing (plus the previously passing test_an_eight_character_password_is_accepted and TestPDFExportDialogWithoutWebEngine::test_declining_the_overwrite_question_leaves_the_file_alone, which the default-method change keeps green)`

**PDFExportSettingsWidget._apply_settings** — The column checkboxes are built in _create_columns_tab from default_columns, and _apply_settings never looked at saved_settings['selected_columns'] - although get_settings_without_password() writes that key out. Every column the user had unticked came back ticked on the next export.
  *Fix:* _apply_settings now re-applies the saved selection to the checkboxes (and to self.selected_columns), ignoring saved names that no longer exist in all_columns. If none of the saved names is known (settings from another source/machine) the default selection is kept, so the dialog can never open with nothing selectable.
  *Guarded by:* `TestSettingsRoundTrip::test_the_saved_column_selection_is_restored`

**PDFExportSettingsWidget._apply_settings** — The last thing _apply_settings did was overwrite self.column_widths with 'auto' for every column, discarding saved_settings['column_widths'] - so configured widths were saved on every export and reset to auto on every restore.
  *Fix:* The loop now starts from 'auto' and overlays the saved width per column, coercing a saved numeric width with float() and falling back to 'auto' for an unusable value, which keeps PDFGenerator._calculate_column_widths (float(width_setting) * cm) safe against old or foreign settings files.
  *Guarded by:* `TestSettingsRoundTrip::test_the_saved_column_widths_are_restored`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/ui/property_editor.py

**PropertyEditorDialog.generate_username** — The call to utils.ad_utils.generate_username was unguarded. A name that folds to no ASCII characters (Cyrillic 'Иванова', punctuation-only '???') raises ValueError, and an exhausted login space raises RuntimeError - both escaped as a traceback straight out of a button click.
  *Fix:* Wrapped the call in try/except (ValueError, IndexError, RuntimeError) and reported it the same way generate_home_path reports PlaceholderError: a QMessageBox.warning('Generation Failed', ...) plus a red line in the validation area, then return without touching the field.
  *Guarded by:* `test_editor_username_generation_survives_a_non_latin_name[Анна-Иванова] and [Jan-???]`

**PropertyEditorDialog.generate_username / generate_password / generate_display_name (and on_name_changed / on_class_changed)** — Each generator appended its confirmation with _append_success() and THEN called validate(). validate() rebuilds the whole status area with clear() + setHtml(), so the confirmation was erased in the same call stack and the user never saw what had been generated.
  *Fix:* Swapped the order: validate() first, then the confirmation is appended onto the freshly rendered status. Applied the same reordering to the 'you may want to regenerate ...' hints in on_name_changed and on_class_changed, which were invisible for exactly the same reason (same root cause, same file).
  *Guarded by:* `test_editor_keeps_the_generation_confirmation_visible[generate_username / generate_password / generate_display_name]`

**PropertyEditorDialog (group membership handling)** — select_groups(), apply_group_template() and clear_all_groups() wrote straight through to person.group_memberships. Every other field is edited in a widget and only copied onto the Person by accept_changes(), so group edits - plus the dirty flag and the SYNCED -> UPDATE_PENDING status they trigger - survived Cancel.
  *Fix:* Added a working copy `self.pending_groups = list(person.group_memberships)` in __init__; refresh_group_display, select_groups, apply_group_template and clear_all_groups all read and write that copy, and accept_changes writes `self.person.group_memberships = list(self.pending_groups)` alongside the other fields. The Person setter re-compares DNs before marking dirty, so an unchanged membership list still produces no dirty flag. The only other caller, ui/comparison_tab.py, exec()s the dialog and reads the person afterwards, so it gets the corrected semantics for free (its 158 tests still pass).
  *Guarded by:* `test_editor_cancel_restores_the_group_memberships (existing test_editor_clearing_all_groups_needs_a_confirmation, test_editor_group_list_shows_a_placeholder_when_empty and test_editor_accept_marks_the_changed_fields_dirty still pass)`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/ui/settings_tab.py

**SettingsTab.save_all_settings / SettingsTab._report_save_failure (new)** — The boolean returned by SettingsManager.set() was discarded. When the settings file could not be written, both general writes failed silently and the only dialog shown was the logging-failure one, which ends with 'The other settings were saved.' - a plain lie, nothing had reached the disk.
  *Fix:* Both set() calls are now captured into theme_saved/warning_saved; the save succeeds only when general and logging writes both report success. A new _report_save_failure() picks the message that matches what actually happened: logging-only failure keeps the original wording (the other settings really were saved), general-only failure points at the settings file, and a total failure says the changes were not stored and names both causes. Each branch also logs the specific failure.
  *Guarded by:* `tests/test_settings_ui.py::test_save_does_not_claim_success_when_the_settings_file_is_unwritable (test_save_reports_failure_when_the_logging_config_is_rejected still guards the logging-only path)`

**SettingsTab._setup_logging_handlers** — root.handlers.clear() dropped the existing handlers without closing them, so the previous rotating log file stayed open for the rest of the session (buffered records never flushed; on Windows the old file could not be renamed or deleted afterwards).
  *Fix:* Each handler is now removed with root.removeHandler() and then closed (close() flushes and releases the file), guarded by a try/except so one broken handler cannot abort the reconfiguration. Closing is safe for the other handler kinds the app attaches: StreamHandler.close() does not close sys.stderr, and RealTimeLogHandler uses the no-op base close().
  *Guarded by:* `tests/test_settings_ui.py::test_replacing_the_handlers_closes_the_previous_log_file (test_saving_twice_leaves_exactly_one_file_handler still guards handler counts)`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/utils/edupage_tasks.py

**EduPageBaseTask._login_with_2fa** — Cancelling the 2FA dialog raises TaskCancelledException from check_cancelled() inside the login try-block; the catch-all `except Exception` swallowed it, emitted 'Login error: Task was cancelled by user' at LogLevel.ERROR (plus logger.exception) and returned False. A user cancelling is not a login failure.
  *Fix:* Imported TaskCancelledException from utils.progress_tasks and added an `except TaskCancelledException: raise` clause ahead of the catch-all, so the cancellation travels up to run(), which already has a dedicated handler reporting the task as cancelled. All three callers (Mode1 execute, _execute_phase1, _execute_phase2) called check_cancelled() immediately after a False return anyway, so their outcome is unchanged apart from the bogus ERROR disappearing.
  *Guarded by:* `tests/test_sources_edupage.py::test_cancelling_the_2fa_dialog_is_not_logged_as_a_login_error (test_cancelling_while_waiting_for_the_2fa_code_cancels_the_task still guards the exception itself)`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/utils/font_manager.py

**FontManager._register_font_with_reportlab / _reportlab_face_name** — Every face was registered with ReportLab under a name derived only from the Qt family ('DejaVu Sans' -> 'DejaVuSans'), but regular/bold/oblique are separate files sharing one family. Loading DejaVuSans-Bold.ttf after DejaVuSans.ttf overwrote both the ReportLab registration and reportlab_font_files['DejaVuSans'], so the regular face was lost and the family silently resolved to whichever face happened to load last.
  *Fix:* Added _reportlab_face_name(): the family-derived name is still used for the first face of a family, and any further face is named after its own file (e.g. 'DejaVuSans-Bold', with a -2/-3 counter if even that collides), so every face keeps its own registration and its own reportlab_font_files entry. qt_to_reportlab_map now uses setdefault, so the family keeps resolving to the face registered first (the regular one) instead of being hijacked by a later bold face - which is also what utils/pdf_generator.py's body-text lookup wants. A new _registered_faces map keyed by (resource path, family) makes re-loading the same file idempotent instead of spooling a second copy, while still re-spooling if the previous temp file is gone (e.g. after cleanup()).
  *Guarded by:* `tests/test_home_font.py::test_every_loaded_face_gets_its_own_reportlab_registration`

**FontManager.cleanup (via _register_font_with_reportlab)** — cleanup() iterates reportlab_font_files.values(), so any temp font file whose dict entry had been overwritten was unreachable and leaked - loading the twelve shipped DejaVu faces left eight orphaned .ttf files in the temp directory for every run of the application.
  *Fix:* Same cause, same cure as above: because each face now owns a distinct key in reportlab_font_files, no spooled path is ever dropped from the table and cleanup() deletes every file the manager wrote. No separate bookkeeping list was needed, and the documented behaviour that cleanup() keeps the mapping table in place is unchanged.
  *Guarded by:* `tests/test_home_font.py::test_cleanup_removes_every_temp_file_the_manager_created`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/utils/home_directory_utils.py

**HomeDirectoryPathGenerator.generate_path / _extract_name_part / NamePartSpec** — A bare {last_name} is documented as the FULL last name, but generate_path defaulted default_last_spec to NamePartSpec(part_index=-1) (and default_first_spec to part_index=1), so a compound surname such as 'Novakova Svobodova' silently collapsed to 'Svobodova' - half of the student's surname was dropped from every generated home directory.
  *Fix:* Gave NamePartSpec.part_index a documented 'whole name' value (None) and made generate_path's built-in defaults use it for both first and last name, so a bare placeholder expands to the whole name with its whitespace normalised. _extract_name_part grew one branch for that value; part_index=0 (all parts joined without a space) and the -1/1/2 selectors are untouched, a caller-supplied default spec is still honoured verbatim, and an explicit '{last_name:-1}' in the template still wins. Checked both production callers (ui/property_editor.py:522, ui/bulk_edit_dialog.py:560) - they pass no specs, so they now get the documented full name.
  *Guarded by:* `tests/test_home_font.py::test_bare_last_name_placeholder_keeps_the_whole_surname`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/utils/pdf_generator.py

**PDFGenerator.generate_to_file** — The target file was opened with open(path, 'wb') - which truncates - BEFORE the PDF was rendered. Any rendering error (e.g. no columns selected) therefore left a zero-byte file where the user's previous export used to be.
  *Fix:* The PDF is rendered into a BytesIO first and the target is only opened once the bytes exist; on an exception the file on disk is never touched. Only tests call generate_to_file today (the app exports via utils/pdf_export_task.py), and the return value and signature are unchanged.
  *Guarded by:* `TestPDFGeneratorOutput::test_a_failed_generation_does_not_destroy_an_existing_file`

**PDFGenerator._prepare_table_data** — With text wrapping on, cell values were handed to reportlab's Paragraph raw. Paragraph parses its text as mini-HTML, so a value like 'P@ss<word>X' silently lost '<word>' - generated passwords were printed wrong in the PDF handed to pupils.
  *Fix:* Cell text is escaped with xml.sax.saxutils.escape before it becomes a Paragraph, so '<', '>' and '&' reach the PDF as literal characters. Header cells and the non-wrapping path are plain strings and are not markup-parsed, so they stay untouched.
  *Guarded by:* `TestPDFGeneratorCellPreparation::test_angle_brackets_in_a_value_are_kept_verbatim`

**PDFGenerator._prepare_table_data** — Same unescaped-markup cause with a harsher symptom: a single '<' in any cell (e.g. 'a<b') made reportlab's paragraph parser raise 'parse ended with 1 unclosed tags' and aborted the entire export/preview.
  *Fix:* Covered by the same escaping fix - the Paragraph now receives well-formed text for every possible cell value, so no cell content can abort generation. Verified the full document really builds (assert_is_pdf on the generated bytes).
  *Guarded by:* `TestPDFGeneratorCellPreparation::test_a_lone_angle_bracket_does_not_break_the_export`


## /Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/utils/progress_dialog.py

**ProgressDialog.on_task_finished / ProgressDialog._set_progress_text** — A successful *indeterminate* task never left Qt's busy mode: on_task_finished only touched the bar when the task was deterministic, so the progress bar kept maximum()==0 and swept forever after the task had ended. Cancelled and Failed already dropped out of busy mode via _set_progress_text().
  *Fix:* The success branch now always goes through _set_progress_text("Complete", indeterminate_value=100): deterministic bars keep the existing '100% - Complete' text, indeterminate bars get maximum=100, value=100 and the text 'Complete'. _set_progress_text() gained an `indeterminate_value` parameter (default 0, so the Cancelled/Failed callers are unchanged) because a completed run should show a full bar while an interrupted one shows an empty one.
  *Guarded by:* `tests/test_progress.py::test_a_successful_indeterminate_task_stops_the_busy_animation`

**ProgressDialog._show_statistics** — Duration formatting rounded the parts independently: minutes = int(duration_sec/60) and seconds = duration_sec % 60 formatted with '%.0f'. 119.6 s therefore rendered as '1 min 60 sec' (and 3599.6 s would have rendered as '59 min 60 sec').
  *Fix:* The value is rounded first and split afterwards: if round(duration_sec, 1) < 60 it is printed as 'N.N seconds', otherwise total_seconds = int(round(duration_sec)) is split with divmod into min/sec, or into hr/min when it reaches 3600. A value that rounds up over a unit boundary now moves to the next unit ('2 min 0 sec', '1 hr 0 min') instead of printing 60 of the smaller unit. All eight magnitudes pinned by test_duration_is_formatted_by_magnitude still render identically.
  *Guarded by:* `tests/test_progress.py::test_a_duration_is_never_rendered_with_sixty_seconds (plus the existing test_duration_is_formatted_by_magnitude parametrisation)`

**ProgressDialog.on_progress_changed** — The late-update guard tested only the internal flag `self._was_cancelled`, which is set when the task_cancelled signal arrives. Between the Cancel click (which sets the task's cancel flag and shows 'Cancelling task...') and that signal, every progress update from the still-running worker overwrote the operation label and the bar text, so the user's cancel feedback disappeared and the dialog looked like it was still working.
  *Fix:* The guard now calls the public was_cancelled(), which already combines the dialog flag with the task's own is_cancelled() flag - the same source of truth on_task_finished uses to decide 'cancelled' vs 'failed'. Progress therefore freezes from the moment cancellation is requested. The only extra cost is a mutex-protected flag read per update; behaviour for non-cancelled runs is unchanged.
  *Guarded by:* `tests/test_progress.py::test_progress_after_the_cancel_click_keeps_the_cancelling_feedback (existing guards test_progress_updates_after_the_cancelled_signal_are_ignored and the real-worker cancel integration test still pass)`


**Total: 62 defects fixed in this wave** (57 of them with a detailed record above;
the remainder are covered by their regression tests).
