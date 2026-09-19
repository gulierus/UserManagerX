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
| `utils/ad_entry_mapping.py` | which AD attributes are read, and how they become a person (20) |
| `utils/source_naming.py` | readable default names for sources (26) |
| `tests/test_source_combo.py` | 43 tests for the labelling and the Settings switch |
| `tests/test_source_naming.py` | 28 tests for the name builder and the deduplicator |
| `tests/test_ad_entry_mapping.py` | 43 tests for the attribute request and the mapping |
| `tests/test_ad_search_scope.py` | 62 tests for the search scope, which had none before |

---

## Still open

Points **18**, **19**, **21**, **22** and **23** are not part of this phase
yet. Point 21 in particular is blocked — see the note in the reply
about the truncated `main.cs`.
