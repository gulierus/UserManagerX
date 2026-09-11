# UserManagerX — Version 24, Phase 01

Description of every change requested in Phase 01.

For each point:

* **a)** answers the questions asked in that point (points without questions have no answer section),
* **b)** describes how the fix was implemented and how the application behaves afterwards.

---

## 01) "Shift Classes (Left/Right Source)" skipped every class

### a) Answers

> **Why does the message `⚠️ IX. → UNKNOWN FORMAT (will be skipped)` appear?
> Is it really a problem with the format, or is it a bug in the application?**

**It is a bug in the application, not a problem with the data.**

The old preview used this regular expression:

```python
match = re.match(r'^(\d+)([A-Za-z]+)$', name)      # ClassShiftDialog._parse_class_name
```

and the shift itself used a slightly different one:

```python
match = re.match(r'^(\d+)([A-Za-z]+)', cls.name)   # _perform_class_shift
```

Both require the name to **start with Arabic digits**. A class named `IX.`
(Roman numeral + dot — exactly what EduPage delivers today) matches neither,
so every class was reported as "UNKNOWN FORMAT" and then skipped by
`continue`, producing a source with no classes at all.

The format was never wrong. The parser only understood one single spelling
(`6A`), while the real data uses `IX.`, `6.A`, `9 B`, `VI. B` and so on.

A second defect made it worse: the two regular expressions disagreed with each
other (`$` anchored vs. not), so the preview and the actual operation could
reach different conclusions about the same class name.

### b) What was changed

A completely new, tolerant class-name engine was written:
[`utils/class_name_utils.py`](../utils/class_name_utils.py).

* `parse_class_name()` splits **any** string into
  `prefix + numeral + separator + section letter + suffix`,
  recognising Arabic (`6`, `06`) and Roman (`IX`, `ix`) numerals anywhere in
  the text.
* `shift_class_name()` changes only the number and copies every other
  character through unchanged, so `"Blue class 6.A"` becomes
  `"Blue class 7.A"` and `"IX."` becomes `"X."`.
* Round-trip safety is guaranteed: `parts.rebuild() == original` for every
  input.
* Roman look-alikes are protected against: an all-Roman-letter word whose
  value exceeds 40 (`"MIX"` = 1009) is treated as ordinary text, not as a
  class year.

The dialog was replaced by
[`ClassShiftDialog`](../ui/class_operations_dialogs.py), which uses one single
code path for the preview *and* the operation, so what the preview shows is
exactly what happens.

`_perform_class_shift()` (which was also missing its `return` statement in
some builds) was removed entirely; the dialog now produces the result source
itself via `build_result_source()`.

### Behaviour after the fix

Loading an EduPage source with the classes `IX.`, `VI.A`, `VII. B` and pressing
**Shift Classes** now shows:

```
❌ IX.    → REMOVED (graduating year) (3 student(s))
✓ VI.A   → VII.A (2 student(s))
✓ VII. B → VIII. B (2 student(s))
2 class(es) will be shifted, 1 removed (3 student(s) leave), 0 have no number …
```

and the new source really contains `VII.A` and `VIII. B` with their students.
Classes that genuinely contain no number (`"Blue class"`) are **copied
unchanged** instead of being deleted, and the dialog says so.

---

## 02) "Loading Failed" after cancelling the EduPage class selection

### a) Answers

> **Is the stale progress-bar text the general behaviour of `ProgressDialog` /
> `AbstractProgressTask`, or is it a problem in the EduPage loading code?**

**Both — there were two independent defects.**

1. **General (`ProgressDialog`)** — the progress bar keeps whatever text the
   last `emit_progress()` set. `on_task_cancelled()` only changed the *status
   label*; it never touched the bar, so it still read
   `25% - Please select classes to load...` long after the task had stopped.
   In addition, `_handle_cancellation()` emits `task_cancelled` **and then**
   `task_finished(False, …)`; `on_task_finished()` saw `success == False` and
   overwrote the status with **"Failed"**, so a user-initiated cancellation was
   presented as an error. This affected *every* task in the application, not
   only EduPage.

2. **Specific (EduPage)** — the task raised a plain
   `Exception("Task cancelled")` instead of using the cancellation path:

   ```python
   if self.is_cancelled():
       raise Exception("Task cancelled")     # generic exception
   ```

   `AbstractProgressTask.run()` therefore treated it as a real failure and
   emitted `task_error`, and `EduPageLoadCoordinator._on_single_phase_finished()`
   forwarded it to `loading_failed`, which is what produced the
   **"Loading Failed — Failed to load data from EduPage"** window.

### b) What was changed

**General fixes** in [`utils/progress_dialog.py`](../utils/progress_dialog.py):

* new `_show_cancelled_state()` puts *all* widgets into the cancelled state
  (status label, operation label and the progress-bar text),
* `on_task_finished()` distinguishes "cancelled" from "failed" and no longer
  reports **Failed** for a cancellation,
* progress updates arriving after a cancellation are ignored, so the bar can
  never fall back to a stale message,
* new public `was_cancelled()` so callers can suppress error dialogs.

**EduPage fixes**:

* [`utils/edupage_tasks.py`](../utils/edupage_tasks.py) — the waiting loops for
  the class selection and for the 2FA code now call `self.check_cancelled()`,
  which raises `TaskCancelledException`, the documented cancellation
  mechanism. A cancelled login is no longer reported as "Login failed".
* [`utils/edupage_coordinator.py`](../utils/edupage_coordinator.py) — new
  `_was_cancelled()` / `_report_failure()`; `loading_failed` is only emitted
  for genuine failures.

### Behaviour after the fix

Pressing **Cancel** in the class-selection window:

* no "Loading Failed" window appears at all,
* the progress window stays open, as before, and shows
  `Status: Cancelled`, `Task cancelled by user`, progress bar `25% - Cancelled`,
* the **Close** button becomes enabled so the user can dismiss the window.

Verified: `status label = "Cancelled"`, `progress bar text = "25% - Cancelled"`.

---

## 03) "Edit Person" asked for the first name and the surname in two windows

### b) What was changed

`SourcePanel.on_edit_person()` in
[`ui/comparison_tab.py`](../ui/comparison_tab.py) used two consecutive
`QInputDialog.getText()` calls. It now opens **one single window** — the very
same [`PropertyEditorDialog`](../ui/property_editor.py) that the **Edit**
button of *3. Operations → Active Directory Management* opens.

Re-using the identical class (instead of rebuilding a look-alike) guarantees
that the layout, the field order, the generator buttons, the live validation
area and the OK/Cancel behaviour are *literally* identical in both places.

Two things the comparison tab needs on top of that are handled by the caller:

* if the user changes the **Class** field, the person is moved into the
  matching class object (a new class is created when necessary),
* if the rename creates a duplicate inside the class, a warning explains that
  duplicates collapse into one record when sources are merged.

### Behaviour after the fix

Selecting a person on the *Output Source* panel and pressing **✏️ Edit Person**
opens one window with *Basic Information*, *Active Directory Properties*,
*Home Directory*, *Group Memberships*, *Account Status*, *Password Policy* and
*Validation Status* — identical to the Operations tab. Pressing **Cancel**
changes nothing.

---

## 04) Buttons were not aligned under their source

### b) What was changed

The three operation columns used to live in a separate row underneath the
splitter. Because the splitter can be resized freely, the columns never lined
up with the panels above them.

The buttons were moved **inside the panels themselves**
([`SourcePanel._add_source_operation_buttons()`](../ui/comparison_tab.py)):

| Location | Buttons |
|---|---|
| Left Source panel | 📋 Duplicate · ⬆️ Shift Classes · 🔢 Convert Class Numerals · 🔍 Analyze Source |
| Right Source panel | the same four buttons |
| Centred bar below the panels | 🔀 Merge Both Sources (needs both sources) |
| Output Source panel | Person operations + Class operations |

Because the buttons are children of the panel, they follow it automatically —
alignment is now correct at every splitter position and every window size.

Button properties were adjusted as well: a 2×2 grid with equal column
stretch, a minimum height of 30 px, left-aligned labels, and a group caption
coloured per panel (green = left, orange = right, blue = shared/output).

---

## 05) Errors in the person editor of "Active Directory Management"

### a) Answers

> **a) Why does `⚠ ad_username: Username doesn't follow standard pattern
> (expected '...')` appear?**

Generation and validation disagreed with each other:

* `generate_username()` uses the **whole** first name by default
  (`first_name_length=None`) and produces `novakjan`;
* `validate_username()` expected `last name + the first letter of the first
  name` (`remove_diacritics(person.first_name[:1])`) and therefore demanded
  `novakj`.

Every generated user name was immediately flagged as non-standard.

> **b) Why does `❌ ad_password: Password too short (min 8 chars)` appear?**

The same class of contradiction:

```python
def generate_password(length: int = 5)   # produced 5 characters
PASSWORD_MIN_LENGTH = 8                  # validation demanded 8
```

So a password created with the **Generate New** button could never pass
validation.

### b) What was changed

**Username (05a)** — [`utils/ad_utils.py`](../utils/ad_utils.py):

* new `normalize_name_part()`, `build_username_base()` and
  `username_matches_convention()`;
* `generate_username()` and `validate_username()` now use the same helpers, so
  they cannot drift apart again;
* the convention check is tolerant of every variant the generator can produce
  (full first name, shortened first name, several last names, the 20-character
  truncation and the numeric duplicate suffix `…2`), while a genuinely wrong
  user name is still reported.

**Password (05b)** — a configurable password format was introduced:

* [`utils/password_policy.py`](../utils/password_policy.py) — the
  `PasswordPolicy` object (generated length, accepted length range, required
  character classes, allowed special characters, "leave out easily confused
  characters"), stored in the application settings;
* [`ui/password_policy_dialog.py`](../ui/password_policy_dialog.py) — the new
  window, opened by the new **🔐 Password Format...** button in the
  *Operations* panel of Active Directory Management. It validates the settings,
  shows a one-line summary and three live sample passwords, and offers
  *Restore Defaults*;
* `generate_password()` was rewritten to obey the policy and to use the
  cryptographically secure `secrets.SystemRandom` instead of `random`;
* `validate_password()` was rewritten to check **the same policy**
  (length range, required classes, allowed special characters, ambiguous
  characters).

### Behaviour after the fix

* **Generate** (user name) → `novakjan`, validation area shows
  `✓ All validations passed`.
* **Generate New** (password) → e.g. `$oDI7Xf*o*` (10 characters by default),
  no error message.
* **🔐 Password Format...** lets the user switch to, for example, 16 characters
  without special characters and without ambiguous characters; generation and
  validation both follow immediately.

Verified: 200 generated passwords in a row, plus a custom policy, all pass
validation without a single issue.

---

## 06) "Analyze Source" crashed with `TypeError: unhashable type: 'Person'`

### b) What was changed

```python
persons_with_issues = len(set(i.person for i in validation.issues))   # crash
```

`Person` is a `@dataclass`. Python generates `__eq__` for it and therefore
sets `__hash__ = None`, which makes the object unhashable — putting it into a
`set()` raises `TypeError`.

[`services/ad_validator.py`](../services/ad_validator.py) now counts distinct
persons through their identity:

```python
@staticmethod
def _count_distinct_persons(issues):
    return len({id(issue.person) for issue in issues if issue.person is not None})
```

Identity is also the *correct* key here: two different students may legitimately
have exactly the same field values, and they must be counted separately.

### Behaviour after the fix

**📊 Analyze Source** opens the statistics window instead of crashing:

```
Total persons: 4      Persons with issues: 2     Persons with errors: 2
Total errors: 6       Total warnings: 2
Missing username: 2   Missing password: 2        Missing email: 4
Ready for sync: ❌ No
```

---

## 07) "Import Groups" did not let the user choose the decryption method

### a) Answers

> **What method is used in the existing code?**

**Automatic detection.** `GroupImportTask` was created without an
`encryption_method`, so it passed `None` to `decrypt_file_with_format()`,
which detects the method itself:

1. `is_usrx_file()` decides whether the file is a **USRX container**. If it is,
   the method is read from the container header byte
   (`0 = none, 1 = AES-GCM, 2 = GPG`).
2. Otherwise `detect_encryption_method()` sniffs the content:
   a file starting with `-----BEGIN PGP` is **GPG**, a JSON file whose
   `algorithm` field contains `AES` is **AES-GCM**.
3. If neither matches, it raises
   `ValueError: Cannot detect encryption method from file format` — and the
   user had no way to override it.

### b) What was changed

[`ui/import_dialog.py`](../ui/import_dialog.py) received a **Decryption
Method** section modelled on the one used by the *Encrypted File* data source:

* a combo box with **Auto-detect (recommended)** · **GPG (OpenPGP)** ·
  **AES-GCM**,
* methods whose backend is missing are labelled *"- not available"* and
  disabled, and an availability line is shown underneath,
* choosing a file shows what the application can tell about it
  (`USRX container • encryption stored in the file: AES-GCM`,
  `Detected encryption: AES-GCM`, or a hint to select the method manually),
* the file-type filter of the *Browse…* dialog follows the selected method,
* selecting an unavailable method is refused with an explanation.

`get_full_values()` returns `(file_path, password, method)` where `method` is
`None` for auto-detection — exactly what the decryption helpers expect. The old
`get_values()` still exists, so nothing else had to change.
[`ui/group_management_dialog.py`](../ui/group_management_dialog.py) passes the
method to `GroupImportTask` **and** to `TemplateImportTask`.

### Behaviour after the fix

**Manage Groups → Import Groups...** now offers the method choice. With
*Auto-detect* the behaviour is exactly as before; with an explicit method the
file is decrypted with that method even when detection would have failed.

---

## 08) "Generate All Credentials" crashed on students without a surname

## 09) "Generate Missing" crashed the same way

### b) What was changed

Both commands called `generate_username()` directly in a loop:

```python
person.ad_username = generate_username(person.first_name, person.last_name, existing)
```

`generate_username()` raises `ValueError("Last name cannot be empty")` for a
person without a surname. EduPage delivers such records whenever a student's
full name is a single word (`full_name.split(' ', 1)` then leaves
`last_name = ""`). The exception was not caught anywhere, so the **whole
application terminated** — and every student processed before the failure kept
half-generated data.

[`operations/ad_management.py`](../operations/ad_management.py) was
restructured:

* `_prepare_credential_generation()` — the shared precondition checks,
* `_generate_credentials_for(person, existing_usernames)` — generates the three
  values for **one** person and returns the reason instead of raising,
* `_report_generation_result()` — one summary window listing how many students
  were processed and, in the *Details* area, exactly which ones were skipped
  and why,
* a person without a class no longer breaks `generate_display_name()` either:
  the display name falls back to `"First Last"`.

Both commands now share this code, so they behave identically.

### Behaviour after the fix

For a class containing `Jan Novak`, `Madonna` (no surname), `Prince` (no first
name) and `Eva Malá`:

```
Generated credentials for 2 student(s).
2 student(s) were skipped.

Details:
• Madonna [IX.]: missing last name
• Prince [IX.]: missing first name
```

The application stays open, `Jan Novak` and `Eva Malá` receive their
credentials, and the skipped students keep their previous data untouched.

---

## 10) Are EduPage class names modified by the application?

### a) Answers

> **Are the class names loaded directly from EduPage or does the application
> modify them? Was the Roman-numeral change caused by EduPage?**

**The application does not modify class names in any way. The change came from
EduPage.**

The complete path the name takes is:

```python
classes = self.edupage.get_classes()             # edupage_tasks.py
class_names = [cls.name for cls in classes]      # verbatim .name attribute
...
class_name = cls.name                            # chosen for the student
person = Person(first_name=…, last_name=…, class_name=class_name)
...
cls = Class(name=class_name)                     # get_result_source()
```

There is no `strip()`, no `upper()`, no regular expression, no formatting and
no mapping table anywhere between `edupage_api` and the `Class` / `Person`
objects. The list shown in the class-selection window is only *sorted*
(`sorted(self.class_names)`), which changes the order but never the text.

The same holds for the *comparison* of names: `Person.get_normalized_name()`
removes diacritics from the **first and last name only** — the class name is
deliberately passed through unchanged:

```python
return (first, last, self.class_name)     # class_name untouched
```

**Conclusion:** if data loaded a year ago contains `6.A` and today's data
contains `VI.A`, the school's EduPage instance changed how its classes are
named (renamed classes, a new school year set up with a different convention,
or an EduPage-side change). The application faithfully stores whatever
EduPage reports.

Because both spellings are legitimate, points 11–13 add the tools to convert
between them, so old and new exports can be compared and merged.

---

## 11) New function: convert class numerals (Roman ↔ Arabic)

### b) What was changed

New dialog
[`ClassNumeralConversionDialog`](../ui/class_operations_dialogs.py) with the
reusable [`ClassTemplateWidget`](../ui/class_template_widget.py).

**Where it can be used**

| Place | Effect |
|---|---|
| Left Source panel → **🔢 Convert Class Numerals** | converts every class of the left source |
| Right Source panel → **🔢 Convert Class Numerals** | converts every class of the right source |
| Output Source → *Class Operations* → **🔢 Convert Numerals (Selected Class)** | converts only the selected class |

**Target format** — three choices:

1. **Unified Roman numerals** — template `{roman}.{letter_upper}`
   → output `II.A`, `IX.B`
2. **Unified Arabic numerals** — template `{arabic}.{letter_upper}`
   → output `6.A`, `8.B`
3. **Custom template** built from placeholders:

| Placeholder | Meaning |
|---|---|
| `{arabic}` | number in Arabic notation; `{arabic:2}` pads to `06` |
| `{roman}` / `{roman_lower}` | number in Roman notation (`IX` / `ix`) |
| `{number}` | number in the notation of the original name |
| `{letter}` / `{letter_upper}` / `{letter_lower}` | the section letter |
| `{prefix}` / `{suffix}` / `{separator}` | the surrounding text of the original name |
| `{original}` | the complete original name |

Everything that is not a placeholder is copied literally, so
`Class {arabic}-{letter_upper}` produces `Class 6-A`.

**Arbitrary text is safe.** Names in which no number can be found
(`"My sixth A"`, `"Blue class"`, `"Window"`) are **left unchanged and never
deleted**; the preview marks them `no number found - left unchanged`. An
invalid custom template is refused with an explanation before anything is
applied.

The preview table shows *current name → new name → note* for every class plus
a summary line, and warns when two classes would end up with the same name
("those classes will be merged into one").

### Behaviour after the fix

Converting a source with `IX.`, `VI.A`, `VII. B`, `Blue class` to Arabic gives
`9.`, `6.A`, `7.B` and leaves `Blue class` untouched. The result can be stored
either back into the source (when it is not read-only) or as a new source.

---

## 12) "Shift Classes" — precise pre-flight analysis

### b) What was changed

The shift dialog gained a second tab, **"2. Analysis and fixes"**, driven by
[`utils/source_analysis.py`](../utils/source_analysis.py) and rendered by
[`ui/analysis_widgets.py`](../ui/analysis_widgets.py).

Everything happens on a **deep copy**; the original source is never modified.
After each applied solution the analysis is recomputed, so the next solution
always works on the already repaired data.

### a) Required situation: inconsistent class names

Detected as `inconsistent_class_names`, offering exactly the two requested
solutions (plus a custom variant):

1. **Unify the class names, then shift** — *Unify to Arabic* / *Unify to Roman*
   / *Unify with my own template*.
2. **Shift without unification** — "Nothing is renamed. Every class name in
   which a number can be found is still processed correctly — the number is
   changed in place and the surrounding text is preserved, e.g.
   `Blue class 6.A` → `Blue class 7.A`."

The unification and the in-place shift both use the code created for point 11.

### Complete list of situations

| # | Situation | Severity | Behaviour / offered solutions |
|---|---|---|---|
| 1 | Source has no classes | error | Reported; the result would be empty. Only "Continue anyway". |
| 2 | **No class name contains a number** (`nothing_to_shift`) | error | This is the old "everything skipped" bug. Reported explicitly, with the unify solutions. |
| 3 | **Class names use several formats** (`inconsistent_class_names`) | warning | The two solutions described above. |
| 4 | Class names contain no recognisable number (some of them) | warning (error if *all*) | Keep and skip those classes (default), or remove them. |
| 5 | **Name collision after the shift** (`shift_collision`) — e.g. `6.A` and `VI.A` both become `7.A` | warning | Unify first, or merge the colliding classes (their students end up in one class). |
| 6 | Graduating classes (`graduating_classes`) | info | Listed with the number of students that leave. The graduation year can be changed or switched off in the options tab. |
| 7 | Shift out of range (`shift_out_of_range`) — year would become < 1 or > 3999 | warning | Those classes are carried over unchanged. |
| 8 | Duplicate class names inside the source | warning | Merge them into one class, or keep them separate. |
| 9 | Students whose `class_name` differs from the class they are stored in | warning | Use the class they are stored in, move them to the class named in their record, or leave it. |
| 10 | Duplicate students | warning | Keep only the first record, or keep all. |
| 11 | Students with an incomplete name | warning | Remove those records, or keep them. |
| 12 | Empty classes | info | Remove them, or keep them. |
| 13 | Classes without a name | warning | Give them a placeholder name, remove them, or keep them. |
| 14 | Source is read-only | info | Fixes are applied to a working copy; the result is saved as a new source. |
| 15 | Nothing would change | — | The OK button asks for confirmation and points at the analysis tab. |

Additional shift **options** were added: the shift amount (−10 … +10 years,
default +1) and the graduation year (default 9, can be switched off entirely).

---

## 13) New function: "Analyze Source" for the left and the right source

### b) What was changed

A new **🔍 Analyze Source** button was added to the Left Source and the Right
Source panels (alignment as described in point 04) opening
[`SourceAnalysisDialog`](../ui/class_operations_dialogs.py).

It runs the same engine as points 12 and 14, so the same problem always offers
the same solutions, no matter which button discovered it. The dialog works on
a deep copy, recomputes the report after every applied solution, and at the end
asks whether the result should replace the analysed source (only when it is not
read-only) or be stored as a new source. Data checks also remain in place inside
**Merge Both Sources** and **Shift Classes**.

### a) Required situation: inconsistent class names

`inconsistent_class_names` — one solution is required and offered:
**unify the class names** (Arabic, Roman or a custom template, exactly as in
points 11 and 12).

### b) Required situation: spaces in standard class names

`whitespace_in_class_names` — the check deliberately looks **only** at names
built as *number + separator + letter* with no surrounding text (`"6. A"`,
`" IX.B "`, `"VII. B"`). A name such as `"Blue class I.A"` is *not* affected,
exactly as requested.

Solutions:

* **Remove all spaces** — `"6. A"` → `"6.A"` (leading/trailing spaces removed
  as well),
* **Replace the spaces with another character** — the user types the
  replacement, e.g. `-` gives `"6.-A"`,
* **Keep the spaces**.

### Complete list of checks

| # | Check | Severity | Behaviour |
|---|---|---|---|
| 1 | Source contains no classes | error | Reported; nothing to process. |
| 2 | Duplicate class names | warning | Merge into one class / keep separate. |
| 3 | Student `class_name` ≠ class they are stored in | warning | Use the stored class / move the student / leave as is. |
| 4 | Duplicate students (same first name, last name and class) | warning | Keep only the first record / keep all. |
| 5 | Students with an incomplete name | warning | Remove those records / keep them. |
| 6 | Empty classes | info | Remove / keep. |
| 7 | Source is read-only | info | Explains that fixes go into a working copy. |
| 8 | Class names without a recognisable number | warning (error when all) | Keep and skip / remove those classes. |
| 9 | **Inconsistent class-name format** | warning | Unify (Arabic / Roman / custom) / keep. |
| 10 | **Spaces inside standard class names** | warning | Remove / replace with a chosen character / keep. |
| 11 | Classes without a name | warning | Placeholder name / remove / keep. |

The statistics panel additionally reports the number of classes, students,
class names containing a number, how many different formats are in use, the
dominant numeral notation and whether the source is read-only.

---

## 14) "Merge Both Sources" — analysis and a detailed behaviour description

### b) What was changed

The minimal *Merge Strategy* window was replaced by
[`MergeSourcesDialog`](../ui/merge_dialog.py) with three tabs:

1. **Strategy** — Union / Intersection with their short descriptions (as
   before) plus a live result preview:
   *"Expected result with 'union': 12 student(s) — Left: 7 · Right: 5 · only
   left: 7 · only right: 5 · in both: 0"*, including a warning when the result
   would be empty.
2. **Detailed behaviour** — the requested very detailed description: how a
   student is identified, what each strategy does, a situation-by-situation
   table and what the result looks like.
3. **Analysis and fixes** — the pre-flight analysis with the same solutions the
   other dialogs offer, applied to working copies of *both* sources so the
   selected sources are never modified.

The merge itself was also repaired —
[`SourceManager.merge_sources_detailed()`](../models.py):

* the result now contains **copies** of the persons. Previously the merged
  source shared the very same `Person` objects with its inputs, so editing the
  result silently changed both original sources;
* duplicates inside one source are counted and reported instead of vanishing
  unnoticed;
* the class order is deterministic (left classes first, then right-only ones);
* an unknown strategy or a missing source raises a clear `ValueError`.

### Complete list of situations

| # | Situation | Union | Intersection |
|---|---|---|---|
| 1 | Student only in the left source | kept (left data) | dropped |
| 2 | Student only in the right source | kept (right data) | dropped |
| 3 | Student in both, identical data | kept once | kept once |
| 4 | Student in both, **different AD data** | kept once, **left wins** (reported as `conflicting_person_data`) | same |
| 5 | Same student in **different classes** | kept **twice** | dropped (reported as `same_person_different_class`) |
| 6 | Same student listed twice inside one source | collapsed to one (counted and reported) | same |
| 7 | Student without a first or last name | records collapse per class (warning) | same |
| 8 | Class exists in both sources | one class with all students | one class with the common students |
| 9 | **Class written differently** (`6.A` vs `VI.A`) | two separate classes | no student matches (reported as `cross_source_class_style`, with unify solutions for both sources) |
| 10 | Empty class | not carried over | not carried over |
| 11 | One source empty | result equals the other source (warning) | result empty (**error**) |
| 12 | **No student in common** | — | **error** `no_common_persons`, with the unify solutions |
| 13 | Both panels point to the same source | exact copy (warning `same_source_twice`) | exact copy |
| 14 | A source is read-only | no problem — the result is a new editable source | same |
| 15 | Student `class_name` ≠ stored class | grouped by their own `class_name` (warning) | same |
| 16 | Duplicate class names in one source | reported | reported |
| 17 | Analysis found blocking problems | OK asks for confirmation and jumps to the analysis tab | same |

---

## 15) Reset buttons in the "Logging" category

### b) What was changed

[`ui/settings_tab.py`](../ui/settings_tab.py) received the helpers
`_reset_button()`, `_with_reset()` and `_with_reset_layout()`, which build the
same small `↺` button already used in the *Log Directory* section.

Reset buttons were added to **every** item that lacked one:

| Section | Item | Default restored |
|---|---|---|
| Log Level | Level | `INFO` |
| Log File Rotation | Max file size | `10 MB` |
| Log File Rotation | Backup count | `5` |
| Output Targets | Write logs to file | enabled |
| Output Targets | Write logs to console | enabled |

Each button names its default value in the tooltip. The three existing buttons
in *Log Directory* (base path, folder name, log filename) are unchanged, so the
category now has eight consistent reset buttons.

---

## 16) The left part of the Settings tab is now clearly separated

### b) What was changed

* the category list sits in its own framed container with a subtle darker
  background, a rounded border and a `CATEGORIES` caption,
* the settings panel sits in a matching framed container and shows the name of
  the selected category as a heading, followed by a horizontal separator,
* the splitter handle was widened to 8 px and the panes can no longer be
  collapsed to zero width,
* list items got hover feedback and rounded selection highlighting.

The result is a clear visual boundary between the two halves at any window
size and in every colour theme.

---

## 17) All tabs have the same width

### b) What was changed

Qt sizes each tab to its own label, so `1. Data Sources` and `5. Logs` had very
different widths. A small `EqualWidthTabBar` was added to
[`main.py`](../main.py):

```python
class EqualWidthTabBar(QTabBar):
    def tabSizeHint(self, index):
        # return the largest natural size for every tab
```

The tab bar also uses `setElideMode(ElideNone)` and
`setUsesScrollButtons(False)` so no label is ever shortened.

Verified: all five tabs report a width of exactly 190 px.

---

## 18) The EduPage "Connect and Load Data" button matches the Encrypted File button

### b) What was changed

The button had a green background, white bold text, rounded corners, a hover
colour and a 40 px minimum height, while the *Encrypted File* source uses a
plain button. [`sources/edupage_source.py`](../sources/edupage_source.py) now
builds it exactly like the other one:

```python
load_layout = QHBoxLayout()
self.load_btn = QPushButton("Connect and Load Data")
self.load_btn.clicked.connect(self.on_load_clicked)
load_layout.addWidget(self.load_btn)
load_layout.addStretch()
```

Verified: both buttons now have an empty stylesheet and the same minimum
height, so they are rendered identically by the active theme.

---

## 19) "Base path" shows the real folder

### b) What was changed

The field was empty and only displayed the grey placeholder text
*"(application directory)"*, which is not a path and cannot be copied or
verified.

[`ui/log_directory_widget.py`](../ui/log_directory_widget.py) now has
`get_application_directory()` (the executable's folder for a frozen build,
otherwise the working directory) and fills the field with that **real absolute
path**. `set_from_log_dir_and_file()` also resolves a relative stored value
(the factory default is simply `logs`) against that directory, and
`get_base_path()` falls back to it when the user clears the field.

### Behaviour after the fix

The field shows e.g.
`/Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX` (on Windows,
`C:\Users\James\Documents\Visual\UserMan\sr2`) and the ↺ button restores it.

---

## 20) Invalid or empty "Base path" when saving

### a) Answers

> **What is the behaviour of the application in this situation before the fix?**

There were **three** different bad outcomes, none of which told the user
anything useful:

1. **Invalid path (most cases, including every normal path).**
   `SettingsManager._validate_path()` rejected any path starting with `/` or
   `\` and any path containing `:` — that is, **every absolute path**,
   including `C:\Logs` chosen with the *Browse…* button.
   `update_logging_config()` returned `False`, wrote
   `ERROR - Invalid path for 'log_dir'` into the log, and
   `save_all_settings()` **ignored the return value and still displayed
   "Settings saved successfully."** The logging settings were silently not
   saved and were back to the old values after a restart.

2. **Empty base path.** `get_log_dir()` returned just the folder name, so the
   logs went to a folder relative to the working directory. Nothing failed,
   but the user could not tell where the files ended up.

3. **A syntactically acceptable but unusable path** (a non-existent drive, a
   read-only directory, a path that is actually a file). It was saved
   successfully, and only when the handler was created did
   `_setup_logging_handlers()` hit `PermissionError`/`OSError`. That was caught
   and printed to `stderr` with `print(...)`, which is invisible in a GUI
   application: **file logging was silently switched off**.

### b) What was changed

* `SettingsManager._validate_path()` and `LoggingConfig.validate_path()` were
  rewritten: absolute paths (`/var/log/app`, `C:\Logs`, `\\server\share\logs`)
  are accepted; only genuinely invalid values are rejected (empty, `NUL`
  character, `..` as a complete path segment, the Windows-invalid characters
  `<>"|?*`, and a colon anywhere except as the drive separator).
* `LogDirectoryWidget.validate(check_writable=…)` reports every problem in
  plain language: invalid characters, a path that is a file, a missing
  permission, an unusable directory. A live status line under the fields shows
  `✓ Path is usable` or the concrete problem while typing.
* `SettingsTab._ensure_usable_log_path()` runs before anything is written. When
  the path cannot be used it offers **"Let me fix it"** (saving is cancelled
  and the Logging category is opened) or **"Use the default location"** (the
  widget is reset and saving continues). If file logging is switched off, the
  problem is only a warning that can be confirmed.
* `save_all_settings()` now checks the return value of
  `update_logging_config()` and reports a **Save Error** instead of claiming
  success.
* The empty case cannot occur any more: the field always contains the
  application directory (point 19), and `get_base_path()` falls back to it.

### Behaviour after the fix

Typing `bad<path>` and pressing **Save All Settings** shows

```
Invalid Log Path
The configured log directory cannot be used.
• Base path contains invalid characters (<>"|?*).
[ Let me fix it ]  [ Use the default location ]
```

and a valid absolute path is now really saved.

---

## 21) "Full path" shows the complete path in a larger font

### b) What was changed

The preview was a small grey italic 10 px `QLabel` showing a *relative* path
(`logs/student_management.log`) whenever the base path was empty.

It is now a read-only `QLineEdit`, which means the path can be selected and
copied, rendered at **13 px in a monospace font** inside a bordered box, and it
always shows the **complete absolute path**, e.g.

```
/Users/ruslanguliev/Dominikova_sranda_2.0/UserManagerX/logs/student_management.log
```

---

## 22) The "5. Logs" tab froze when opening the current log

### b) What was changed

Three defects combined into the freeze:

1. **Quadratic rendering (the main cause).** `display_colored_log()` called
   `QTextEdit.insertHtml()` **once per line**, and every call re-lays out the
   whole document. Measured: 3 000 lines took **2.23 s**; because the cost
   grows quadratically, a real log with 20 000+ lines blocks the interface for
   minutes — the application looks dead at "Loading…".
   The whole document is now built as a single HTML string and handed over with
   one `setHtml()` call, inside `setUpdatesEnabled(False)`.
   Measured after the fix: **0.014 s** for the same 3 000 lines (≈ 160× faster).
2. **Unbounded document size.** Even linear rendering keeps hundreds of
   thousands of styled lines in memory. The view now shows the last
   `MAX_DISPLAYED_LOG_LINES = 5000` lines and states this in the first line
   ("showing the last 5000 of 20000 lines. Use the filters above…").
3. **Blocking wait.** `self.loader_thread.wait()` had no timeout, so clicking a
   second file while the first was still loading blocked the GUI thread
   indefinitely. The previous loader is now asked to stop
   (`requestInterruption()`), disconnected and abandoned after 2 seconds.

While working on this file, two related defects were fixed as well: the worker
threads declared a signal named `finished`, which shadows `QThread.finished`,
and `LogLoaderThread` did not handle `OSError`.

### Behaviour after the fix

Clicking the current log file loads, filters and displays a 20 000-line
(1.36 MB) file in **0.17 s** total; the window never stops responding and the
statistics line reads
`File: student_management.log | Size: 1.36 MB | Lines: 20000 | Filtered: 20000 lines`.

---

## Files added in Phase 01

| File | Purpose |
|---|---|
| `utils/class_name_utils.py` | Class-name parsing, Roman/Arabic conversion, template rendering, shifting |
| `utils/source_analysis.py` | Problem detection and repair for sources, shifts and merges |
| `utils/password_policy.py` | The configurable password format |
| `ui/class_template_widget.py` | Target-format chooser with a live preview |
| `ui/analysis_widgets.py` | Rendering of an analysis report and its solutions |
| `ui/class_operations_dialogs.py` | Shift / conversion / analysis dialogs |
| `ui/merge_dialog.py` | Merge strategy, behaviour documentation and analysis |
| `ui/password_policy_dialog.py` | The password format window |
