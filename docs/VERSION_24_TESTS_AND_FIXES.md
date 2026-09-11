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

**75 defects were found and fixed**, and a further **55 are documented** by
failing-but-expected tests at the end of this document. Below are the fixed
ones, grouped by file, each with the mechanism and the way to verify it.

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
python3 -m pytest              # 3592 passed, 62 xfailed
python3 -m pytest -m bug       # only the regression tests for the defects above
python3 -m pyflakes $(find . -name "*.py" -not -path "./tests/*" -not -name "resources_rc.py") | grep -i "undefined name"
                               # no output - was 1 before (G1)
```

---

# Outstanding defects (documented, not yet fixed)

The second wave of test modules found a further **55 distinct defects**
(62 xfailed test runs — some are parametrised over several inputs). Each one is
already pinned by a test that asserts the *correct* behaviour and is marked

```python
@pytest.mark.bug
@pytest.mark.xfail(reason="BUG: ...", strict=False)
```

so the suite stays green while the defect stays visible. List them at any time
with:

```bash
python -m pytest -m bug -rx
```

They are grouped by the area they live in.


## utils/progress_tasks.py, utils/progress_dialog.py

*Covered by `tests/test_progress.py`*

* a successful indeterminate task leaves the progress bar sweeping forever
* durations are rendered as because the seconds are rounded independently
* progress updates between the cancel click and task_cancelled overwrite the feedback

## utils/home_directory_utils.py, utils/font_manager.py

*Covered by `tests/test_home_font.py`*

* bare {last_name} keeps only the last part of a compound surname
* a second face of the same family overwrites the first ReportLab registration
* cleanup() leaks the temp file orphaned by the second face of a family

## services/ad_validator.py, services/ad_services.py

*Covered by `tests/test_ad_validator_services.py`*

* UPDATE_PENDING persons are dropped from the plan
* AMBIGUOUS persons are scheduled for creation
* class name is not DN-escaped in the target OU
* home_directory changes are silently dropped
* DN is not recorded when the password step fails

## ui/comparison_tab.py and the class-operation dialogs

*Covered by `tests/test_comparison_tab.py`*

* moving a student into a class that already holds the same name raises no duplicate warning
* delete matches persons by value, so the first equal record is removed instead of the selected one

## ui/settings_tab.py, ui/log_directory_widget.py

*Covered by `tests/test_settings_ui.py`*

* a relative base path is never resolved against the application directory
* the base-path reset button ignores the default_base_path given to the constructor
* a NUL byte in the folder name or log file name is not reported; the status line claims the path is usable
* save_all_settings() ignores the False returned by SettingsManager.set() and still claims the other settings were saved
* _setup_logging_handlers() clears the root handlers without closing them, leaking the open log file

## ui/log_viewer_tab.py

*Covered by `tests/test_log_viewer.py`*

* real-time view is never trimmed, lineCount() stays 1
* every message lands in one paragraph, so the view cannot be trimmed and each append re-lays it out
* the stats counter is not updated for hidden messages
* the early return leaves the UI in the filtering state
* a log entry without raises KeyError in the slot
* consecutive progress updates append instead of replace

## operations/ad_management.py, ui/bulk_edit_dialog.py, ui/property_editor.py

*Covered by `tests/test_ad_management.py`*

* refresh_person_table leaks one Edit button per person on every repaint
* edit_person compares persons by value, so a field-identical twin
* generate_missing_credentials re-generates the user name of a person that only lacks a password
* PropertyEditorDialog.generate_username raises ValueError for a name without ASCII letters
* the generators append a confirmation that the validate() call right after erases
* group changes are written to the Person immediately and survive Cancel

## operations/pdf_export.py, utils/pdf_generator.py and the PDF dialogs

*Covered by `tests/test_pdf_export.py`*

* empty load leaves the previous rows on screen
* generate_to_file truncates the target before the PDF exists
* cell text is parsed as markup, is lost
* unescaped makes the whole export raise
* pikepdf passes validation although it is missing
* saved column selection is ignored on restore
* saved column widths are reset to auto

## sources/*.py, utils/edupage_*.py

*Covered by `tests/test_sources_edupage.py`*

* cancelling the 2FA dialog is logged as a login error
* an attribute the directory did not return reads as the string , so nameless accounts are kept
* user entries are read without requesting distinguishedName
* the case-insensitive LDAP match is thrown away by a case-sensitive startswith
* the Search Format combo box is never read
* the ImportError branch closes a progress dialog that was never created

## ui/import_dialog.py, ui/group_management_dialog.py, utils/group_tasks.py

*Covered by `tests/test_groups_import.py`*

* ExportDialog never checks whether the chosen encryption backend is installed
* a DN without a usable RDN yields an empty group name instead of the fallback
* an escaped comma inside a CN is treated as an RDN separator
* group_verified_label is built once and never updated, so it always reads
* template rows contain raw HTML entities (&#9888;) which a QListWidget renders literally
* button captions carry raw HTML entities (&#10133;) which QPushButton renders literally
* a file holding two groups with the same DN is imported twice, breaking the one-group-per-DN rule
* a file holding two templates of the same name silently drops one but the summary counts both

## main.py, ui/operations_tab.py, ui/source_selection_tab.py

*Covered by `tests/test_integration.py`*

* closeEvent never closes the tabs, so LogViewerTab keeps its handler on the root logger and its 5 s refresh timer
* OperationsTab keeps the deleted source in current_source and in all three operation widgets
* OperationsTab does not listen to source_modified, so the PDF class list and the JSON statistics go stale
* the operation list has no current row while the stack already shows the AD management page


**Total: 55 distinct defects documented, not yet fixed (62 xfailed runs).**
