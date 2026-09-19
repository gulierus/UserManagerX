# Version 26 — Phase 01

Answers to the questions asked in the request, and for every point what was
changed and how the application behaves afterwards.

> **Numbering note.** `docs/VERSION_25_PHASE_01.md` answers the *previous*
> request and uses its own numbering (its "05" is the `unwillingToPerform`
> password problem). The numbers below belong to this request only.

---

## 01) Height of "Convert Numerals... (Selected Class)"

**Question: does this button have the same height as the buttons of the
"Class Operations" group?**

No. The button sits outside that group, in a layout that gave it whatever
height the layout had left over, so it came out a few pixels shorter than
"Add Class..." and "Remove Class..." directly above it.

**Change.** The button is pinned to the natural height of its neighbours:

```python
convert_class_btn.setMinimumHeight(add_class_btn.sizeHint().height())
```

The size hint is read from a real button of the group, so the two stay equal
if the theme, the font or the platform style changes.

**Behaviour.** All class buttons in the Output Source panel now form one row
of equal height.

---

## 02) `EqualWidthTabBar` moved out of `main.py`

**Change.** The class moved verbatim into its own module,
`ui/equal_width_tab_bar.py`, and `main.py` imports it. Nothing else changed —
`main.py` is now about starting the application, not about drawing tabs.

**Behaviour.** Identical. The tab bar still gives every tab the same width.

---

## 03) Clicking empty space in the Source Manager list

**Before.** `QListWidget` clears the selection when the user clicks below the
last row. The detail panel on the right then fell back to
*"Select at least one source"*, which looked like the application had lost the
source.

**Change.** A small subclass, `SourceListWidget`, ignores presses that land on
no item:

```python
def mousePressEvent(self, event):
    if self.itemAt(event.pos()) is None:
        event.accept()      # swallow it - do not let Qt clear the selection
        return
    super().mousePressEvent(event)
```

`mouseDoubleClickEvent` is guarded the same way, because a double click on
empty space produced the same effect.

**Behaviour.** Clicking empty space does nothing at all: the selected source
stays selected and its details stay on the right. Clicking another source
still selects it, and Ctrl/Shift multi-selection is untouched.

---

## 04) Text style of the source details panel

**Before.** The panel mixed three font sizes, bold headings and normal body
text, so it did not look like the rest of the application.

**Change.** The panel is rebuilt from a single `rows_html()` helper. Every
line uses one 13 px font, nothing is bold, and the headings are separated only
by colour — the same recipe the "4. Settings" tab uses.

**Behaviour.** The panel reads as one quiet block of text in the application's
own style.

---

## 05) "read-only" / "editable" in every source selector

**Change.** All source combo boxes append the access mode to the name:

```
Students 2025            ->  Students 2025 (editable)
AD school.local          ->  AD school.local (read-only)
```

The labelling lives in one new module, `ui/source_combo.py`, so the two places
that list sources — the source selector of the "3. Operations" tab and the
three panels of the "2. Comparison and Sync" tab — cannot drift apart:

| Function | Purpose |
|---|---|
| `source_display_label(source, show_access)` | builds one row's text |
| `populate_source_combo(combo, sources)` | fills a combo box |
| `combo_source_name(combo, text=None)` | turns a label back into a source name |
| `find_source_index(combo, name)` | finds a source's row whatever its label |
| `show_access_enabled()` | reads the Settings switch |

**The important implementation detail.** The plain source name is stored in
each item's **data**, never parsed back out of the visible text. Lookups
therefore keep working no matter how the label is formatted, and a source
genuinely called `Backup (editable)` cannot be confused with a decorated one.

**The switch.** "4. Settings" → **General** → **Source Lists** →
*"Show whether a source is read-only or editable in source lists"*
(on by default). The setting is stored as
`general.show_source_access_in_lists`.

**Behaviour.** Flipping the switch relabels every open combo box immediately —
both tabs listen for the settings signal — and the source that was selected
stays selected, because the selection is remembered by name and not by label.

---

## 06) StartTLS, `ldaps://` and server certificates

**Question 1: with "Encrypt connection (StartTLS)" enabled, is `ldaps` needed
in the "Server" field?**

**No.** They are two different ways of reaching the same result:

| Address | Port | How it is encrypted |
|---|---|---|
| `ldap://dc.school.local` + StartTLS | 389 | the plain connection is opened first and then **upgraded** by the StartTLS extended operation |
| `ldaps://dc.school.local` | 636 | TLS is negotiated **before** any LDAP traffic ("implicit TLS") |

`ADClient` decides between them automatically:

```python
def _resolve_ssl(self):
    if self.use_ssl is not None:
        return bool(self.use_ssl)
    return str(self.server or "").strip().lower().startswith("ldaps://")
```

If the address starts with `ldaps://` the connection is already encrypted and
the StartTLS step is skipped — the checkbox is then simply irrelevant, not
wrong. If the address is `ldap://` (or has no scheme at all), StartTLS does
the work. Either way `ADClient.is_secure` ends up `True`, which is what the
password code requires.

**Question 2: is a server certificate required to start StartTLS?**

**Yes — on the server.** A domain controller only offers the StartTLS
extended operation once it holds a certificate suitable for LDAP (normally
issued by AD Certificate Services, an enterprise CA, or auto-enrolled). Without
one, the upgrade is refused and the application logs

```
StartTLS was refused by the server: ...
```

and falls back to the plain connection — the connection still works, but
passwords cannot be set (see the previous request, point 05).

**The certificate does not have to be trusted by this computer.** That is a
separate question, and it is what the second checkbox controls:

```python
tls_configuration = Tls(
    validate=_ssl.CERT_REQUIRED if self.validate_certificate else _ssl.CERT_NONE
)
```

* **"Verify server certificate" off** (the default) — the channel is still
  fully encrypted, the certificate is simply not checked against a trusted
  authority. This is why a self-signed certificate on a school domain
  controller works.
* **"Verify server certificate" on** — the certificate must chain to a CA this
  machine trusts, and the name must match. A self-signed certificate then
  fails the connection.

**Change.** Both answers are now in the interface, in the tooltips of the two
checkboxes, so the next person does not have to read the source to find them.

---

## 07) `{enrollment_year}` in home-directory templates

**Change.** The placeholder was added to
`HomeDirectoryPathGenerator.AVAILABLE_PLACEHOLDERS`:

```python
'enrollment_year': 'Year the class started the first grade (6.A -> 2020)'
```

and resolved in `_get_placeholder_value()` — first from `person.enrollment_year`,
then from `person.metadata['enrollment_year']`.

The help text of both the "Home Directory Templates..." window and the
"Bulk Edit..." dialog lists it alongside the other placeholders.

**Behaviour.**

```
\\srv\students\{enrollment_year}\{username}   ->   \\srv\students\2020\novakjan
```

If the year has not been calculated yet, the template does not silently write a
wrong path — it stops with an instruction:

> This person has no enrollment year yet — calculate it with the
> 'Enrollment Years' button on the Comparison and Sync tab

`validate_template()` accepts the placeholder, so the template can be saved.

---

## 08) The enrollment year next to every class

**Change.** All three panels of the "2. Comparison and Sync" tab show the year
in the **second column** of the tree (`["Name", "Class / Enrollment year"]`)
rather than glued onto the class name.

**Why the second column and not the caption.** The class name is the
application's identity for a class — it is compared, exported, shifted and
written into AD. Appending text to it would have made "6.A" and
"6.A (enrolled 2020)" two different strings everywhere the name is used. The
second column is empty for class rows anyway, so the year costs nothing and
changes nothing.

**Behaviour.** Left, Right and Output panels all show, for example, `6.A` in
the first column and `enrolled 2020` in the second.

---

## 09) Style of the Source Manager list

**Change.** The list of sources is wrapped in the same `QFrame` and uses the
same three style sheets as the category chooser of the Settings tab, imported
from it rather than copied:

```python
from ui.settings_tab import (
    CATEGORY_PANEL_STYLE, CATEGORY_CAPTION_STYLE, CATEGORY_LIST_STYLE,
)
```

**Behaviour.** The two lists are now identical by construction — a change to
the Settings tab's look reaches the Source Manager automatically.

---

## 10) Colours of "warning" and "error" cells in "Analyze Source"

**Before.** The highlights were fully saturated (`QColor(255, 0, 0)` and
friends), so the dark theme's light text on a solid red or orange background
was unreadable.

**Change.** Both colours became soft, translucent washes:

```python
ERROR_COLOR   = QColor(200,  80,  80, 45)
WARNING_COLOR = QColor(220, 160,  60, 45)
```

The alpha of 45 lets the table's own background through, so the cell is tinted
rather than painted over.

**Behaviour.** The marking is still obvious at a glance, and the text in the
marked cell is as readable as in any other cell.

---

## 11) `.json` in the "Browse..." file filter

**Question: are `.json` files supported? Is it correct that they can be
selected? Are they encrypted?**

Three separate answers, all verified against the code rather than guessed:

1. **An encrypted file is JSON internally.** An AES-GCM export really is a
   JSON document:
   ```json
   {"version": "1.0", "algorithm": "AES-256-GCM", "salt": "...", "data": "..."}
   ```
   That is presumably why the extension ended up in the filter.
2. **A plain, unencrypted JSON file is rejected.** `detect_encryption_method()`
   does not recognise it and the load fails.
3. **The application never writes a `.json` file here.** Exports are saved as
   `.aes` or `.gpg`.

So the filter offered an extension that the application neither produces nor
accepts. It was **not** correct, and the files it invited the user to pick are
**not** encrypted.

**Change.**

* `.json` was removed from the Browse filters (with a comment explaining why,
  so it is not "helpfully" added back). `All Files (*)` stays, so a file with
  an unusual extension can still be picked deliberately.
* When a plain JSON file is loaded anyway, the error now says what is wrong:

  > This looks like a plain, unencrypted JSON file. Only encrypted files can be
  > loaded here...

**Behaviour.** The dialog offers the formats that actually work, and the one
way to still arrive with a plain JSON file ends in a sentence that explains it.

---

## 12) "Encrypted File" renamed to "Encrypted JSON File"

**Change.** The source type is called **"Encrypted JSON File"** on the
"1. Data Sources" tab.

**Behaviour.** Cosmetic, and it matches point 11: the file is an encrypted
JSON document, and the name now says so.

---

## 13) The "Enrollment Years" buttons froze the interface

**Cause.** The button resolved the current school year **on the GUI thread**,
and resolving it can involve a network lookup. Every repaint, every click and
the window itself were blocked until the lookup finished or timed out — which
on a school network without internet access meant several seconds of a frozen,
"not responding" window.

**Change.** The work moved into a background task, in a new module
`utils/enrollment_task.py`:

| Class | Runs | Does |
|---|---|---|
| `EnrollmentYearTask(AbstractProgressTask)` | when the button is pressed | resolves the year **and** computes the per-class changes in a worker thread, behind the standard `ProgressDialog` |
| `YearWarmupTask` | once at start-up | resolves the year in the background so the first button press is instant |

`EnrollmentService` gained `is_year_cached()`, and the silent/automatic path
(`update_source_enrollment_years(..., silent=True)`, used when a source is
added) now calls `resolve_current_year(use_internet=False)` when the cache is
cold, so adding a source never blocks either.

**Behaviour.** The button opens a progress dialog with a working Cancel button;
the window stays responsive throughout. In practice the dialog flashes past,
because the warm-up has usually finished long before.

---

## 14) "Bulk Edit..." moved into "Operations"

**Change.** The button moved from the "Persons" group into the "Operations"
group, and the now-empty "Persons" caption was removed.

**Behaviour.** One group fewer, and "Bulk Edit..." sits with the other actions
that work on the selection.

---

## 15) One button to hide the connection and operation panels

**Change.** A checkable button hides `self.connection_group` **and**
`self.operations_group` together:

```python
self.toggle_panels_button.setCheckable(True)
```

The "Columns..." button lives outside both groups, so it is deliberately
unaffected and stays reachable while the panels are hidden.

**Behaviour.** One click collapses both panels and the person table grows into
the space; the same button brings them back. The button's own text and tooltip
follow its state.

---

## 16) "Home Directory" column in the person table

**Change.** `ALL_COLUMNS` gained `"Home Directory"`. The cell shows the drive
letter and the path together (`H: \\srv\students\novakjan`), and the full path
is repeated in the cell's tooltip for paths too long for the column.

**Behaviour.** The column is off by default and is switched on in the
"Columns..." window like any other.

---

## 17) "Groups" column in the person table

**Change.** `ALL_COLUMNS` gained `"Groups"`. The cell lists the group **names**
(short and readable); the tooltip lists the full **distinguished names**, one
per line, for when the exact group matters.

**Behaviour.** As with point 16 — off by default, switched on in the
"Columns..." window.

---

## 18) `ADStatus` — one member, one meaning

### Table 1 — what the members meant **before** the change

| Member | Where it was set | What it meant | Problem |
|---|---|---|---|
| `UNKNOWN` | the default on every new `Person` | nothing has been looked up | — |
| `NOT_IN_AD` | discovery, no match | not found in the searched area | — |
| `EXISTS_IN_AD` | discovery, exactly one match; **and** immediately after `create_user()` succeeded | "an account was found" — and nothing at all about whether the values agreed | covers **two** of the required situations (c and d) and could not tell them apart |
| `CREATE_PENDING` | **nowhere** | — | dead: no code path ever assigned it, so it could only ever appear in a file written by hand |
| `UPDATE_PENDING` | a local edit of a person who was `SYNCED`; **and** after a synchronisation in which one or more steps failed | "there is something to write" **or** "the write partly failed" | covers **two** unrelated situations — a pending local edit and a failed synchronisation |
| `SYNCED` | after a fully successful create/update; and by `reset_dirty()` | the last synchronisation succeeded | — |
| `AMBIGUOUS` | discovery, more than one match by username | several accounts match | correct, but the name says nothing about *what* is ambiguous |

**Answer to 18 g — what `AMBIGUOUS` meant and whether to keep it.** It was set
in exactly one place: discovery searched by `sAMAccountName` and got **more
than one** account back. It is read in exactly one place, `create_sync_plan()`,
which leaves that person out of the plan entirely:

```python
if person.ad_status == ADStatus.AMBIGUOUS:
    # Several AD accounts match this person, so we do not know which one is
    # hers.  Creating a user would add yet another duplicate ...
    continue
```

**It is kept.** Removing it would leave two choices, both bad: treat the person
as found (and write to an account that may belong to somebody else) or treat
her as not found (and create a *third* duplicate account). It was renamed to
`MULTIPLE_AD_MATCHES`, which says what is actually ambiguous.

### Table 2 — what the members mean **after** the change

| Member | Shown as | Set when | Means |
|---|---|---|---|
| `UNKNOWN` | *Not checked* | the default on a new person; a status that cannot be read back | a) Active Directory has not been asked about this person yet |
| `NOT_FOUND_IN_AD` | *Not in AD* | discovery finished with no match | b) not present in the **searched area** — a narrower search scope can produce this for a person who does exist elsewhere |
| `DIFFERS_FROM_AD` | *Differs* | discovery found exactly one account **and** `compare_person_with_ad()` reported at least one difference; a field is edited on a person who was settled; the account has just been created and the remaining steps have not run yet | c) found, and at least one field differs from what AD held at discovery |
| `MATCHES_AD` | *Identical* | discovery found exactly one account **and** the comparison found nothing; every local edit is undone on a person who was `MATCHES_AD` | d) found, and every comparable field is identical |
| `SYNC_SUCCEEDED` | *Synchronised* | a create or update finished with an empty failure list; `reset_dirty()` on a person who has a DN | e) the last synchronisation finished with no errors |
| `SYNC_INCOMPLETE` | *Synchronised with errors* | a create or update finished with a non-empty failure list (the password, the account flags, the groups or the home directory did not apply) | f) the synchronisation ran, but one or more steps failed |
| `MULTIPLE_AD_MATCHES` | *Several matches* | discovery matched more than one account | g) we do not know which account is hers; she is left out of every synchronisation until this is resolved |

### What had to change for the table to be true

Renaming was the smaller half. `MATCHES_AD` and `DIFFERS_FROM_AD` are a promise
that the two sides were actually compared, and nothing in the application did
that. A new module, `services/ad_comparison.py`, now does:

* It knows which person fields exist in the directory
  (`FIELD_TO_AD_ATTRIBUTE`) — nine of them, from the first name to the group
  list. The password is deliberately absent: Active Directory never gives one
  back. The class name is absent too: it is a position in the tree, not an
  attribute.
* It compares tolerantly, so the status does not flicker on noise. `None`, `""`
  and a missing attribute all mean "not set"; surrounding whitespace is
  ignored; a single-valued attribute returned as a one-item list still matches;
  and groups are compared as a **set of DNs, case-insensitively**, because the
  order a directory lists them in is meaningless.
* It stores the result on the person (`metadata['ad_differences']`) as plain
  dictionaries, so the comparison survives to be painted by the table — and so
  `metadata` stays made of simple types, because it is copied and serialised
  elsewhere.

`_refresh_ad_state()`, which re-reads an account after writing to it, now
refreshes the comparison as well; otherwise the table would keep marking the
fields the synchronisation had just brought into agreement.

### Other consequences

* **The status column shows the label, not the raw value.** *Identical*,
  *Differs*, *Synchronised with errors* — with the full sentence in the cell's
  tooltip. The colours were extended to all seven states (green: nothing to do,
  amber: something outstanding, orange-red: partly failed, red: missing,
  purple: ambiguous).
* **The PDF export prints the label too.** `synced` became *Synchronised*: a
  PDF is read by a person.
* **Undoing an edit restores the right status.** Editing a person who was
  *Identical* makes her *Differs*; undoing every edit puts her back to
  *Identical*, and a person who was *Synchronised* goes back to
  *Synchronised* — the status held before the first edit is remembered rather
  than guessed.
* **Old values still load.** `ADStatus.from_value()` maps every value an
  earlier version wrote onto a current member, and anything unrecognised
  becomes `UNKNOWN` — a status that cannot be read must not stop a file from
  loading. `exists_in_ad` is read back as `DIFFERS_FROM_AD`, because that is
  the reading that cannot cause a real difference to be missed.

---

## 19) Showing what Active Directory holds

### a) "Discover in AD" now reads every field

**Before.** The discovery searches asked for `'*'` and nothing else:

```python
if attributes is None:
    attributes = ['*', 'modifyTimestamp', 'createTimestamp']
```

`'*'` means "the attributes this entry carries", and a directory is free to
leave constructed attributes such as `memberOf` out of it. Relying on it is a
gamble that costs exactly the fields the report names.

**Change.** Discovery and the post-write refresh both ask for a named list:

```python
AD_READ_ATTRIBUTES = list(dict.fromkeys(
    PERSON_ATTRIBUTES + ['*', 'modifyTimestamp', 'createTimestamp']
))
```

`PERSON_ATTRIBUTES` is the same list the "1. Data Sources" loader uses
(point 20), so the display name, the email, the description, the home
directory, the home drive and `memberOf` are always requested by name.

### b) Two answers, then the two ways of showing a value

**Question: is it true that detection happens only in
`ConflictDetector.check_conflict`?**

**No — and it was never quite what that method did.** There were two separate
things going on, and only one of them was a comparison:

| Where | What it compared | When it ran |
|---|---|---|
| `Person._mark_dirty()` | the person **against herself** — the value now versus the value when she was loaded | on every edit |
| `ConflictDetector.check_conflict()` | the person's *local changes* against **fresh** values read from the directory | only from the synchronisation path, and only for a person who is dirty **and** has a DN |

Neither of them ever asked "does this person agree with Active Directory?".
`_mark_dirty` does not know the directory exists. `check_conflict` starts with

```python
if not person.is_dirty() or not person.ad_dn:
    return None
```

so a person who was never edited was never compared with anything, and even for
an edited person it only looks at the fields that were edited — a field that
differs because *Active Directory* holds something else was invisible.

That is why this point needed a new comparison rather than a new view of an
existing one: `services.ad_comparison.compare_person_with_ad()` (see point 18).
It now runs in discovery, for every person, over every comparable field.

**Question: why does `ConflictDetector` exist?**

It solves a different problem: **the lost update**. Two people can work on the
same account. The sequence it protects against is:

1. "Discover in AD" reads Jan's account and remembers `modifyTimestamp`.
2. A colleague changes Jan's email directly in Active Directory.
3. Meanwhile this application's user also edits Jan's email.
4. The synchronisation writes — and silently destroys the colleague's change.

`check_conflict()` re-reads the account at step 4, notices that
`modifyTimestamp` no longer matches `person.ad_version`, and reports the fields
that were changed **on both sides** (`conflicting_fields`) separately from
those changed only locally (`non_conflicting_local`), so the user can be asked
what to do instead of one write quietly winning.

So the two have different jobs and both are needed:

| | `ad_comparison` (new) | `ConflictDetector` (existing) |
|---|---|---|
| Question | do the two sides agree *right now, as far as we know*? | has AD changed **since we looked**, in a way that clashes with our edits? |
| Data | the snapshot from "Discover in AD" | a **fresh** read at synchronisation time |
| Runs | on every discovery, for every person | during synchronisation, only for dirty persons with a DN |
| Used for | the status, the outlines, the tooltips | asking the user how to resolve a clash |

**And yes — the outlines deliberately do not notice later changes in AD.** The
request asks for exactly that, and it is the right behaviour: the values shown
are the ones "Discover in AD" read. If somebody changes the directory
afterwards, the application keeps showing the discovered value until discovery
is run again. A view that silently re-read the directory would make the table
move under the user's hands, and it would put an LDAP round trip behind every
repaint.

### The two ways of showing a value

Both are built from one module, `ui/ad_difference_view.py`, so they cannot
drift apart: one colour, one popup, one switch.

**1. In the editing window.** Every field whose value differs from the
directory is outlined in amber, and hovering it shows the popup:

> **Email** differs from Active Directory
> In Active Directory: `stary@skola.cz`
> In this application: `novy@skola.cz`
> *Read by "Discover in AD". Press it again to refresh.*

A line above the form says what the outlines mean right now — *"2 fields differ
from Active Directory"*, *"Every field matches …"*, or *"This person has not
been discovered in Active Directory yet."*, which is the distinction a bare
absence of outlines could not make.

Nine fields are covered: first name, last name, username, display name, email,
description, home directory, home drive and the group list. The **password is
deliberately not** — Active Directory never gives one back, so there is nothing
to compare it with.

The group list needed one extra step: a tooltip set on a `QListWidget` is only
shown over its *empty* area, because hovering a row shows that row's own
tooltip. The explanation is therefore appended to each row's tooltip as well,
below the group's DN — and the row's own text is remembered, so toggling the
button repeatedly cannot pile the explanation up on top of itself.

**2. In the person table.** The same outline around the differing cells, drawn
by a `QStyledItemDelegate` (a `QTableWidgetItem` cannot carry a border of its
own), with the same popup on hover. The `Name` cell covers the first and last
name and the `Home Directory` cell covers the drive and the path, so a cell
showing two fields is explained by both: if only one of them differs the popup
names that field, and if both differ it merges them into one before/after pair.

Turning the outlines off only stops the **painting** — the flags stay on the
cells, so switching back on is a repaint rather than a rebuild of every row.

### The switch is a setting, not a window's mood

The request asks that the choice made in one person's editing window still
apply when the next person is opened. It is therefore stored as
`general.show_ad_differences` rather than kept on a dialog:

* the editing window reads it when it opens and writes it when the button is
  clicked;
* the person table does the same;
* so the two windows always agree, and the choice also survives a restart.

The button's caption follows its state (*Show AD differences* /
*Hide AD differences*), so it is obvious what clicking it will do.

### The popup's design

It is a Qt tooltip — it appears where the user expects, near the pointer, and
disappears by itself — but its **content is rich text built by the
application**, with the field name in the outline colour, the two values
labelled, and a grey footnote explaining where the value came from. The tooltip
chrome (background, border, radius, padding, font size) is styled once for the
whole application in `main.py`, so it stops looking like a bare system hint.
Everything taken from the data is HTML-escaped, because a display name may
legitimately contain `<` or `&`.

---

## 20) Which fields the Active Directory *source* loads

**Question: when data is loaded from Active Directory on the "1. Data Sources"
tab, are all fields loaded — the home directory, the list of groups? Why is the
"HomeDirectory" field empty in the Edit window, and why are the groups not
visible?**

**Answer: no, and the reason is one line of code.** The loader asked the
directory for exactly six attributes:

```python
attributes=['givenName', 'sn', 'displayName', 'sAMAccountName',
            'mail', 'distinguishedName']
```

`ldap3` only fills in attributes that were **requested**. An attribute left out
of that list is not "empty in the directory" — it was never asked for, and the
entry simply does not carry it. So the complete picture before the change was:

| Field in the application | Attribute | Loaded before? |
|---|---|---|
| First name | `givenName` | yes |
| Last name | `sn` | yes |
| AD username | `sAMAccountName` | yes |
| Display name | `displayName` | yes |
| Email | `mail` | yes |
| DN | `distinguishedName` | yes |
| **Description** | `description` | **no** |
| **Home directory** | `homeDirectory` | **no** |
| **Home drive** | `homeDrive` | **no** |
| **Groups** | `memberOf` | **no** |
| **Account enabled** | `userAccountControl` | **no** |
| **Must change password** | `pwdLastSet` | **no** |

That is the whole explanation of the reported procedure: load → copy on the
"2. Comparison and Sync" tab → "Edit" on the "3. Operations" tab. The copy is
faithful (`deepcopy`), the editor is correct — there was nothing to show
because nothing was ever read.

**Change.** A new module, `utils/ad_entry_mapping.py`, owns both halves of the
problem: the list of attributes to request (`PERSON_ATTRIBUTES`) and the
mapping from an entry to a `Person` (`person_from_entry`). One place decides,
so the request and the mapping cannot drift apart again.

Details worth naming:

* **`memberOf` is not always a list.** A user in exactly one group is answered
  with a bare string on some `ldap3` versions. `attribute_values()` accepts
  both shapes.
* **Group objects are built from the DN**, not fetched one by one — reading
  every group separately would turn loading one class into dozens of extra
  round trips. The name is the first component of the DN, and an escaped comma
  stays inside it, so a group called `Pupils, year 6` keeps its name.
* **`userAccountControl` is read for two flags only.** "Account disabled"
  (`0x0002`) and "password never expires" (`0x10000`). *"User cannot change
  password"* is deliberately **not** read from it: in Active Directory that
  setting is two access control entries on the object, not a UAC bit, so
  reading UAC for it would always report `False` — a wrong answer is worse than
  no answer.
* **A missing `userAccountControl` does not mean "disabled".** An account the
  directory is actively serving is reported as enabled.
* **`pwdLastSet == 0`** is how AD expresses "must change at next logon".
* Entries without a first *or* last name are still skipped — those are service
  accounts and computer objects, and the user search now also excludes
  `objectClass=computer` explicitly.

**Behaviour.** After loading from Active Directory, the "Edit" window shows the
home directory, the home drive, the description and the group list, and the new
"Home Directory" and "Groups" columns (points 16 and 17) have something to
display. The values survive the copy made on the "2. Comparison and Sync" tab.

---

## 24) One search logic for both tabs

**What the "Search Format" field actually did.** It offered three options:

| Option | Filter it produced |
|---|---|
| `Trida-6X (uppercase X)` | `(ou=Trida-*)` |
| `Trida-6x (lowercase x)` | `(ou=trida-*)` |
| `Both` | `(\|(ou=Trida-*)(ou=trida-*))` |

**All three return the same organisational units.** LDAP compares attribute
values case-insensitively, so a directory answers `(ou=Trida-*)` with
`Trida-6A` *and* `trida-7b`. The field asked the user to make a choice that
could not change the result — and the code carried a comment saying exactly
that, because a case-*sensitive* `startswith()` in Python had once dropped the
lower-case units after the server had correctly returned them.

**Change.** The field was replaced by the two controls from the
"Active Directory Management" operation, built from the same `SearchScope`
definitions:

* **"Search in:"** — `Whole subtree below the Base DN (default)` /
  `Only the OUs directly in the Base DN` / `Only the organisational units I name`
* **"Organisational units:"** — enabled for the third scope only, the same
  comma/semicolon separated list, with the same placeholder hint underneath.

The shared module `utils/ad_search_scope.py` gained the discovery half of the
problem, because the two tabs ask *different questions of the same
configuration*:

| Tab | Question | Function |
|---|---|---|
| 3. Operations | "where do I look for **this person**?" | `build_search_bases()`, `ldap_scope()` |
| 1. Data Sources | "where do I look for the **class units**?" | `build_class_ou_filter()`, `class_ou_search_scope()` |

**How a placeholder behaves during discovery.** A placeholder means "whatever
this class is called". On the Operations tab that is resolved per person; on
the Data Sources tab there is no person yet, so it becomes an LDAP wildcard:

| Typed | Used as | Effect |
|---|---|---|
| `Trida-{class_name}` | `(ou=Trida-*)` | every class unit |
| `Rocnik-{grade}` | `(ou=Rocnik-*)` | every year unit |
| `Zaci` | `(ou=Zaci)` | exactly that unit |
| `Trida-{clas_name}` | *(refused)* | a typo is neither "match nothing" nor "match everything" |

**Safety.** Everything the user types is escaped for RFC 4515 before it reaches
a filter — `(`, `)`, `\` and NUL — with `*` kept only where this module put it.
A unit named `A)(objectClass=*` cannot break out of the filter it is placed in.

**Behaviour.**

* The default is unchanged: every `Trida-*` unit below the Base DN, at any
  depth, exactly as before.
* `Only the OUs directly in the Base DN` no longer walks a large tree.
* `Only the organisational units I name` loads exactly the units named — a unit
  that does not follow the `Trida-` convention keeps its own name as the class
  name (`Zaci` loads as the class `Zaci`), and a unit matched by two entries is
  loaded once.
* Naming no unit at all is refused **before** the connection is opened, with
  *"No organisational unit was named, so nothing would be searched."* — the old
  behaviour would have reported an empty directory.

---

## 25) Intermittent freeze and crash when synchronising *(high priority)*

**Cause — proven, not guessed.** The stack in the report is the whole story:

```
Windows fatal exception: access violation
  File "ui\log_viewer_tab.py", line 736 in append_colored_line
  File "ui\log_viewer_tab.py", line 764 in add_real_time_log
  File "ui\log_viewer_tab.py", line 214 in emit          <- logging handler
  ...
  File "utils\progress_tasks.py", line 141 in run        <- WORKER THREAD
```

`RealTimeLogHandler` was a plain `logging.Handler` that called a GUI callback
**directly from whichever thread logged the message**. The synchronisation task
logs from its worker thread, so `QTextDocument` was being mutated from two
threads at once. Qt forbids that: widgets and their documents belong to the GUI
thread. The result is memory corruption, which is *why the failure was
intermittent* — it only crashes when the two threads happen to overlap, and it
sometimes corrupts memory quietly instead, producing the freeze with no message
at all.

**Change.** The handler became a `QObject` and hands every record to the GUI
thread through a signal, which Qt delivers as a queued connection:

```python
class RealTimeLogHandler(QObject, logging.Handler):
    record_logged = pyqtSignal(str, int, object)

    def __init__(self, callback):
        QObject.__init__(self)
        logging.Handler.__init__(self)
        self._callback = callback
        self.record_logged.connect(self._deliver)

    def _deliver(self, msg, level, record):
        try:
            self._callback(msg, level)
        except Exception:
            self.handleError(record)          # never let an exception escape a slot

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.record_logged.emit(msg, record.levelno, record)
```

Two details that matter:

* **`_deliver` swallows exceptions.** PyQt6 turns an unhandled exception in a
  connected slot into `qFatal()` — the process aborts. Guarding the callback
  turns a formatting bug in the log viewer back into an ordinary
  `handleError()`, which is what `logging` expects.
* **The `LogRecord` travels with the signal** so `handleError(record)` still
  has the record it needs on the other side.

**Verification.** A stress test issues 400 log calls from a worker thread while
the GUI thread appends 400 lines of its own, and asserts that the document was
only ever touched from the GUI thread. It passes; before the change the same
test corrupted the document.

**Behaviour.** Synchronisation no longer crashes or freezes, and the log viewer
still shows messages from background work in real time.

---

## 26) A readable name for an Active Directory source

**Before.**

```python
source = Source(name=f"AD-{server}", ...)
```

which produced names such as `AD-ldaps://dc01.school.local:636` — long, full of
punctuation, and, worse, **identical for two different branches of the same
directory**. Because the AD loader also never asked the user for a name and
never checked for duplicates, loading twice created two sources with the same
name, and every lookup by name found only the first one: the freshly loaded
data was unreachable.

**Change.** A new module, `utils/source_naming.py`, builds the name from the
two things that actually distinguish one AD source from another:

* the **directory** — its DNS domain, rebuilt from the `DC=` components of the
  base DN, falling back to the server host name;
* the **branch** — the left-most non-`DC=` component of the base DN, which is
  what makes two sources from the same server different.

| Server | Base DN | Suggested name |
|---|---|---|
| `ldaps://dc01.school.local:636` | `OU=Students,DC=school,DC=local` | `AD school.local - Students` |
| `ldap://192.168.1.10` | `DC=school,DC=local` | `AD school.local` |
| `dc01.school.local` | *(empty)* | `AD dc01.school.local` |
| `ldap://[fe80::1]:389` | `OU=Zaci,DC=zs,DC=cz` | `AD zs.cz - Zaci` |

Edge cases handled: the scheme and the port are stripped (including IPv6
literals in brackets); escaped commas stay inside a value, so a unit really
called `6.A\, second group` survives; malformed DN components are skipped
rather than raising; and the result is truncated at 60 characters on a word
boundary.

`unique_source_name()` then appends ` (2)`, ` (3)`, … if the name is taken,
comparing case-insensitively — two sources differing only in case are
indistinguishable to a person reading the list.

**The loader also asks now.** After the data is loaded, the AD source shows the
same naming dialog the EduPage and file loaders use, pre-filled with the
suggestion. A name that is already taken raises the usual *"Source 'X' already
exists. Replace it?"* question, and **Yes now actually replaces** the old
source instead of adding a second one with the same name. Cancelling the dialog
discards the load.

**Behaviour.** Loading `OU=Students,DC=school,DC=local` from
`ldaps://dc01.school.local:636` suggests `AD school.local - Students`; loading
`OU=Teachers` from the same server suggests `AD school.local - Teachers`; doing
either one twice suggests `... (2)`.

---

## Files added in this phase

| File | Purpose |
|---|---|
| `ui/equal_width_tab_bar.py` | the tab bar extracted from `main.py` (02) |
| `ui/source_combo.py` | read-only / editable labelling of source combos (05) |
| `utils/enrollment_task.py` | background enrollment-year tasks (13) |
| `services/ad_comparison.py` | comparing a person with the discovered AD values (18, 19) |
| `ui/ad_difference_view.py` | the outlines, the popup and the shared switch (19 b) |
| `utils/ad_entry_mapping.py` | which AD attributes are read, and how they become a person (20) |
| `utils/source_naming.py` | readable default names for sources (26) |
| `tests/test_source_combo.py` | 43 tests for the labelling and the Settings switch |
| `tests/test_source_naming.py` | 28 tests for the name builder and the deduplicator |
| `tests/test_ad_entry_mapping.py` | 43 tests for the attribute request and the mapping |
| `tests/test_ad_search_scope.py` | 62 tests for the search scope, which had none before |
| `tests/test_ad_status.py` | 71 tests pinning every meaning in table 2 |
| `tests/test_ad_comparison.py` | 36 tests for the field-by-field comparison |
| `tests/test_ad_discovery.py` | 21 tests for discovery, which had none before |
| `tests/test_ad_difference_view.py` | 55 tests for the outlines, the popup and the switch |

---

## Still open

Points **21**, **22** and **23** are not part of this phase yet. Point 21 is
blocked - see the note in the reply about the truncated `main.cs` - and 22/23
describe exports of the Microsoft 365 fields that point 21 would introduce. Point 21 in particular is blocked — see the note in the reply
about the truncated `main.cs`.
