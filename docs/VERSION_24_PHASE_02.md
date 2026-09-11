# UserManagerX — Version 24, Phase 02

Result of the deep analysis of every file in the project.

Every error found is documented with:

* **a)** a description of the error,
* **b)** sample code (or verification instructions) that shows how the code
  behaved **before** the fix and how it behaves **after** it.

The verification snippets are runnable. Start them from the project directory:

```bash
cd UserManagerX
python3 -c "…"      # or paste the block into a file and run it
```

Snippets that build widgets need a Qt application; on a machine without a
display, prefix them with `QT_QPA_PLATFORM=offscreen`.

---

## E01 — `ADGroupService` is used but never imported (crash during synchronisation)

**File:** `services/ad_services.py`

### a) Description

`PersonsSyncService._execute_create()` and `_execute_update()` both do:

```python
if person.group_memberships:
    group_service = ADGroupService(self.ad_client)
    group_service.sync_user_groups(person)
```

but `ADGroupService` was **never imported** in that module. The name only
resolves at runtime, so as soon as a synchronised person had at least one group
membership, the operation died with

```
NameError: name 'ADGroupService' is not defined
```

The exception was caught by the surrounding `except Exception`, so the user saw
the user as **FAILED** even though it had already been created in Active
Directory — the sync report was wrong and re-running produced "already exists"
errors.

### b) Verification

```python
# BEFORE — the name is not defined in the module
import services.ad_services as m
print(hasattr(m, "ADGroupService"))     # False  -> NameError at runtime

# AFTER
import services.ad_services as m
print(hasattr(m, "ADGroupService"))     # True
```

### Fix

`from services.ad_group_service import ADGroupService` was added, and the group
synchronisation was wrapped in its own `try/except`: a failure while assigning
groups is logged but no longer turns a successfully created user into a
reported failure.

---

## E02 — The plain-text password was written into the log file

**File:** `services/ad_services.py`

### a) Description

```python
logger.error(f"User created but password setting failed: {pwd_error} "
             f"password: {person.ad_password}")
```

Every failed password assignment wrote the student's **clear-text password**
into `student_management.log`. That file is displayed in the "5. Logs" tab, is
rotated into `.1`, `.2`, … copies, and is typically the first thing a user
sends when asking for support.

### b) Verification

```
# BEFORE — grep the log after a failed sync
2026-09-02 13:24:21 - services.ad_services - ERROR - User created but password
setting failed: … password: Xk7$mQ2p

# AFTER
2026-09-02 13:24:21 - services.ad_services - ERROR - User novakjan created but
password setting failed: …
```

### Fix

The password was removed from the message; the user name identifies the record
instead. Lazy `%s` formatting is used so nothing is built when the level is
disabled.

---

## E03 — `ldap3` was a hard dependency of the whole application

**Files:** `services/ad_client.py`, `services/ad_services.py`,
`services/ad_group_service.py` (new: `services/ldap_compat.py`)

### a) Description

All three modules imported `ldap3` at module level. `main.py` imports the
Operations tab, which imports these services, so on a computer without the
package the application aborted **before the first window appeared**:

```
ModuleNotFoundError: No module named 'ldap3'
```

even for users who only export PDFs or read encrypted files. The same code
elsewhere (`utils/ad_utils.py`) already imported `ldap3` lazily and produced a
helpful message, so the behaviour was also inconsistent.

### b) Verification

```bash
# BEFORE (with ldap3 uninstalled)
python3 -c "import main"
# ModuleNotFoundError: No module named 'ldap3'

# AFTER (with ldap3 uninstalled)
python3 -c "import main; print('application starts')"
# WARNING - ldap3 is not installed - Active Directory features are disabled
# application starts
```

### Fix

New module `services/ldap_compat.py` re-exports the required constants and sets
`LDAP3_AVAILABLE`. When the package is missing, the constants fall back to
their string values and `require_ldap3()` raises a clear
`RuntimeError("The ldap3 library is required for Active Directory operations.
Install it with: pip install ldap3")` at the moment an AD operation is actually
attempted.

---

## E04 — `PyQt6-WebEngine` was a hard dependency, and its initialisation order was fragile

**Files:** `ui/pdf_export_dialog.py`, `ui/pdf_viewer_window.py`, `main.py`

### a) Description

Two separate problems:

1. Both modules imported `QWebEngineView` at module level. Without the optional
   `PyQt6-WebEngine` component the whole application failed to start
   (`ModuleNotFoundError`), even though only the PDF *preview* needs it — PDF
   *export* does not.
2. QtWebEngine requires `Qt.ApplicationAttribute.AA_ShareOpenGLContexts` to be
   set **before** the `QApplication` instance is created. It worked only by
   accident, because the import chain happened to run before
   `QApplication(sys.argv)` in `main()`. Any change to the import order would
   have produced:

   ```
   ImportError: QtWebEngineWidgets must be imported or Qt.AA_ShareOpenGLContexts
   must be set before a QCoreApplication instance is created
   ```

### b) Verification

```bash
# BEFORE (PyQt6-WebEngine uninstalled)
python3 -c "import ui.pdf_export_dialog"
# ModuleNotFoundError: No module named 'PyQt6.QtWebEngineWidgets'

# AFTER
python3 -c "import ui.pdf_export_dialog as d; print('loaded, preview available:', d.WEB_ENGINE_AVAILABLE)"
# loaded, preview available: False
```

### Fix

* The import is wrapped in `try/except ImportError` and exposes
  `WEB_ENGINE_AVAILABLE`.
* Both windows show an explanatory placeholder ("PDF preview is not available…
  Exporting the PDF works without it") instead of crashing, and every use of
  `self.web_view` is guarded.
* `main.py` sets `AA_ShareOpenGLContexts` at the very top, before any UI module
  is imported.

---

## E05 — Bulk edit was applied twice, and it wrote junk attributes onto `Person`

**Files:** `operations/ad_management.py`, `ui/bulk_edit_dialog.py`

### a) Description

`BulkEditDialog.accept_changes()` already applies every change to the selected
persons and reports the result. `ADManagementWidget.bulk_edit_persons()` then
applied `dialog.changes` **a second time**:

```python
for person in selected_persons:
    for key, value in dialog.changes.items():
        if key == 'generate_password':
            person.ad_password = generate_password()   # a DIFFERENT password
        else:
            setattr(person, key, value)
```

Consequences:

1. **"Generate new password" produced a different password than the one the
   dialog had just created and reported** — anything printed or exported from
   the dialog's result was wrong.
2. `dialog.changes` also contains *instructions*, not only fields:
   `group_action`, `groups`, `home_directory_template`, `home_drive`.
   `setattr()` created **new, meaningless attributes** on the `Person` objects
   (`person.group_action = 'add'`, `person.groups = [...]`) that no other part
   of the application understands, while the real group and home-directory
   handling had already been done correctly by the dialog.

### b) Verification

```python
from models import Person
from ui.bulk_edit_dialog import BulkEditDialog

persons = [Person(first_name="A", last_name="B", class_name="6.A")]
d = BulkEditDialog(persons)
d.change_password.setChecked(True)
d.accept_changes()
pw_after_dialog = persons[0].ad_password

# BEFORE: the widget then ran the loop again
#   -> persons[0].ad_password != pw_after_dialog          (silently different)
#   -> hasattr(persons[0], "group_action") == True        (junk attribute)

# AFTER
print(persons[0].ad_password == pw_after_dialog)          # True
print(hasattr(persons[0], "group_action"))                # False
```

### Fix

`bulk_edit_persons()` no longer re-applies anything; it only refreshes the
table and notifies the source manager.

---

## E06 — Sortable tables were addressed by row number (wrong person / corrupted export)

**Files:** `operations/ad_management.py`, `operations/pdf_export.py`

### a) Description

Both tables have `setSortingEnabled(True)` and both looked the underlying
record up **by the visual row number**:

```python
# ad_management.py
persons = self.current_source.get_all_persons()
return [persons[row] for row in selected_rows if row < len(persons)]

# pdf_export.py
self.table_data[row_idx][col_name] = item.text()
```

As soon as the user clicked a column header to sort, the visual order no longer
matched the data order. The result:

* **Active Directory Management** — "Edit", "Enable Selected Accounts" and
  "Bulk Edit" operated on **a different student** than the one highlighted.
* **PDF export** — `_sync_table_data()` wrote edited cells into the **wrong
  records**, so the exported PDF could hand one student's user name and
  password to another student. `bulk_edit_rows()` had the same defect.

### b) Verification

```python
# operations/pdf_export.py — reproduce with three students
w.load_table_from_source()
print([r['Last Name'] for r in w.table_data])          # ['Zeman', 'Adamek', 'Mala']
w.table.sortItems(1)                                   # sort by Last Name
# visual order is now ['Adamek', 'Mala', 'Zeman']
w.table.item(0, email_col).setText("adam@school.cz")   # edit the first visual row
w._sync_table_data()

# BEFORE: the e-mail landed on 'Zeman'  (table_data[0])
# AFTER:
#   Zeman    email=''
#   Adamek   email='adam@school.cz'
#   Mala     email=''
```

### Fix

Both tables now store the identity of the record on the row itself
(`item.setData(Qt.ItemDataRole.UserRole, …)`) and resolve it back through
`_person_at_row()` / `_data_index_for_row()`. Sorting can no longer desynchronise
the view from the data.

---

## E07 — Worker threads shadowed `QThread.finished`

**Files:** `ui/log_viewer_tab.py`, `operations/ad_management.py`

### a) Description

Four `QThread` subclasses declared their own signal named `finished`:

```python
class LogLoaderThread(QThread):
    finished = pyqtSignal(str, dict)        # shadows QThread.finished()
```

`QThread` already provides `finished()` and emits it when the thread ends. A
subclass signal with the same name (and a different signature) collides with
it: connections can be resolved against the wrong overload, and Qt's own
emission reaches slots that expect arguments. It is a well-known source of
intermittent, hard to reproduce failures.

### b) Verification

```python
from PyQt6.QtCore import QThread
from ui.log_viewer_tab import LogLoaderThread

# BEFORE: LogLoaderThread.finished was the custom 2-argument signal and
#         QThread's own finished() was no longer reachable by that name.
# AFTER:
print(LogLoaderThread.load_finished is not None)   # custom signal, clear name
t = LogLoaderThread("x", {})
print(hasattr(t, "finished"))                      # True - Qt's own signal intact
```

### Fix

Renamed to `load_finished`, `filter_finished` (twice) and
`discovery_finished`, and every `connect()` / `emit()` was updated.

---

## E08 — `wait()` without a timeout blocked the user interface

**Files:** `ui/log_viewer_tab.py`, `operations/ad_management.py`

### a) Description

```python
if self.loader_thread and self.loader_thread.isRunning():
    self.loader_thread.wait()          # no timeout
```

`QThread.wait()` without an argument blocks **the GUI thread** until the worker
finishes. For the AD discovery thread that means the whole application is frozen
until an LDAP request to an unreachable server times out (up to a minute or
more); for the log loader it means a second click while a large file is still
being read freezes the window.

### b) Verification

Point the AD server field at an unreachable address, press *Discover in AD*,
then press it again: **before** the fix the window stopped repainting and
Windows marked it "Not responding"; **after** the fix it stays responsive and
the log shows `Previous AD discovery did not stop within 5 seconds`.

### Fix

All blocking waits now have a timeout (5 s for AD discovery, 2 s for the log
loader). The log loader additionally calls `requestInterruption()` and
disconnects the abandoned thread so a late result cannot overwrite the new one.

---

## E09 — `source_to_json()` never returned its result

**File:** `operations/json_export.py`

### a) Description

The method built the complete dictionary and then simply ended:

```python
logger.debug(f"Converted source '{source.name}' to JSON …")
# <- no "return data"
```

Every caller received `None`. The method was not on the active export path
(the export task had its own copy), which is exactly why the defect stayed
unnoticed — any future use would have failed with
`TypeError: 'NoneType' object is not subscriptable`.

### b) Verification

```python
from operations.json_export import JSONExportWidget
from models import SourceManager, Source
w = JSONExportWidget(SourceManager())
result = w.source_to_json(Source(name="s", source_type="manual"))
print(result)          # BEFORE: None      AFTER: {'format_version': 2, 'name': 's', …}
```

### Fix

See E10 — the method now delegates to the shared serialiser and returns it.

---

## E10 — Export/import lost half of the person data

**Files:** `operations/json_export.py`, `utils/json_export_task.py`,
`sources/file_source.py` (new: `utils/source_serialization.py`)

### a) Description

The conversion "Source → dictionary" existed **three times**, and all three
copies wrote only nine fields. Everything the application had gained since then
was silently dropped on export and, of course, could not be read back:

* `home_directory`, `home_drive`
* `group_memberships`
* `password_must_change`, `password_cannot_change`, `password_never_expires`
* `account_enabled`
* `Source.source_info` (e.g. the EduPage subdomain)

Exporting a fully prepared source and loading it again therefore produced
accounts without groups, without a home directory and — because
`account_enabled` defaults to `False` — **disabled**.

### b) Verification

```python
from models import Source, Class, Person, ADGroup
from utils.source_serialization import source_to_dict, source_from_dict

s = Source(name="orig", source_type="manual")
c = Class(name="6.A")
c.add_person(Person(first_name="Jan", last_name="Novák", class_name="6.A",
                    home_directory=r"\\srv\home\novakjan", home_drive="H:",
                    group_memberships=[ADGroup(name="Students", dn="CN=Students,DC=x")],
                    password_must_change=True, account_enabled=True))
s.add_class(c)

back = source_from_dict(source_to_dict(s))
p = back.get_all_persons()[0]
print(p.home_directory, p.home_drive, [g.dn for g in p.group_memberships],
      p.password_must_change, p.account_enabled)

# BEFORE: None None [] False False
# AFTER : \\srv\home\novakjan H: ['CN=Students,DC=x'] True True
```

### Fix

One single implementation in `utils/source_serialization.py`
(`source_to_dict()` / `source_from_dict()`), used by all three places. Files
written by older versions still load: missing keys fall back to the defaults of
a freshly created person.

---

## E11 — "Replace it?" did not replace anything

**File:** `sources/file_source.py`

### a) Description

When the chosen source name already existed, the application asked
*"Source 'X' already exists. Replace it?"*. Answering **Yes** ran:

```python
source.name = name
break
…
self.source_manager.add_source(source)
```

Nothing was replaced — a **second** source with the same name was added. Since
`get_source_by_name()` returns the first match, every drop-down, every
operation and every subsequent lookup kept using the **old** data, and the newly
loaded file was unreachable while still occupying memory.

### b) Verification

```python
from models import SourceManager, Source
sm = SourceManager()
sm.add_source(Source(name="dup", source_type="file"))
sm.add_source(Source(name="dup", source_type="file"))   # what the old code did
print(sm.get_source_names())                            # ['dup', 'dup']
print(sm.get_source_by_name("dup") is sm.sources[0])    # True - the OLD one wins
```

After the fix, confirming the replacement removes the existing source first, so
the list contains exactly one `dup`, and it is the freshly loaded one.

---

## E12 — Removing a person / class / source deleted the wrong object

**File:** `models.py`

### a) Description

```python
def remove_person(self, person: Person):
    if person in self.persons:
        self.persons.remove(person)      # matches by VALUE
```

`Person`, `Class` and `Source` are `@dataclass` types, so Python generates a
field-by-field `__eq__`. Two different students carrying the same data — exactly
the duplicates that the new source analysis reports — compare **equal**, and
`list.remove()` deletes the *first* match instead of the object the user
selected. The same applied to `Source.remove_class()` and
`SourceManager.remove_source()`.

### b) Verification

```python
from models import Class, Person
c = Class(name="6.A")
a = Person(first_name="Jan", last_name="Novak", class_name="6.A")
b = Person(first_name="Jan", last_name="Novak", class_name="6.A")
c.add_person(a); c.add_person(b)
print(a == b)                  # True  (dataclass equality)

c.remove_person(b)             # remove the SECOND record
print(c.persons[0] is a)       # BEFORE: False (the first one was deleted)
                               # AFTER : True
```

### Fix

All three methods now compare with `is` and delete by index.

---

## E13 — The merged source shared its persons with the input sources

**File:** `models.py`

### a) Description

`merge_sources()` put the **original** `Person` objects into the result:

```python
merged_persons = dict(left_persons)          # references, not copies
…
class_map[class_name].add_person(person)
```

The merged source therefore aliased its inputs: editing a student in the result
(or generating credentials for it) silently changed the same student inside the
left *and* right source, including read-only EduPage sources.

Two smaller defects came with it: duplicates inside one source disappeared
without any notice, and the class order of the result was the arbitrary order of
a dictionary.

### b) Verification

```python
from models import SourceManager
merged, stats = SourceManager.merge_sources_detailed(left, right, "union", "M")
merged.get_all_persons()[0].first_name = "CHANGED"
print(left.get_all_persons()[0].first_name)
# BEFORE: 'CHANGED'   (the input source was modified!)
# AFTER : 'Jan'
print(stats['left_duplicates_collapsed'], stats['in_both'], stats['result_persons'])
```

### Fix

New `merge_sources_detailed()` deep-copies every person, returns statistics
(unique records, collapsed duplicates, only-left / only-right / in-both counts)
and produces a deterministic class order. `merge_sources()` still exists and
delegates to it, so existing callers keep working.

---

## E14 — Path validation rejected every absolute path (settings were not saved)

**Files:** `utils/settings_manager.py`, `utils/logging_config.py`

### a) Description

Both modules validated paths like this:

```python
if '..' in path or path.startswith('/') or path.startswith('\\'):
    return False
if any(c in path for c in '<>:"|?*'):     # ':' kills every Windows drive letter
    return False
```

This rejected **every** absolute path — including every folder the user can
pick with the *Browse…* button (`C:\Logs`, `/var/log/app`,
`\\server\share\logs`). `update_logging_config()` then returned `False` and the
change was discarded. Because the caller ignored the return value, the settings
dialog still said *"Settings saved successfully."*

It also rejected legitimate names such as `my..logs` (the `..` check matched a
substring rather than a path segment).

### b) Verification

```python
from utils.settings_manager import SettingsManager
v = SettingsManager._validate_path
for p in ["logs", "/var/log/app", r"C:\Users\James\Logs", r"\\srv\share\logs",
          "my..logs", "../secret", "bad<name>", "x:y:z"]:
    print(f"{p!r:32} {v(p)}")

# BEFORE: only 'logs' and 'my..logs' were accepted
# AFTER : logs True | /var/log/app True | C:\Users\James\Logs True
#         \\srv\share\logs True | my..logs True
#         ../secret False | bad<name> False | x:y:z False
```

### Fix

Both validators were rewritten: absolute paths are accepted, `..` is only
rejected as a complete path segment, the Windows-invalid characters `<>"|?*` are
rejected, and a colon is allowed only as the drive separator. See also point 20
of Phase 01, which added the user-facing handling.

---

## E15 — The "5. Logs" tab looked at a different configuration than the Settings tab

**Files:** `ui/settings_tab.py`, `utils/logging_config.py`

### a) Description

The application keeps two logging configurations:

* `SettingsManager` (category `logging`, written by the Settings tab), and
* `LoggingConfig` (`logging_config.json`, used by the Logs tab to *find* the
  log files).

The Settings tab wrote only to the first one and re-created the handlers
itself. After changing the log directory, the Logs tab therefore kept listing
the files of the **previous** location — and reported "no log files" when that
folder no longer existed.

### b) Verification

1. *4. Settings → Logging* → set a different **Base path** → *Save All Settings*.
2. Switch to *5. Logs → Historical Logs*.

**Before:** the file list and the "📂 Log folder:" line still show the old
directory. **After:** both show the new one.

### Fix

`SettingsTab._setup_logging_handlers()` now pushes the same values into the
`LoggingConfig` singleton and saves it, so both views agree. A relative
directory is resolved against the application directory first.

---

## E16 — Group list: rows were mapped to a list index

**File:** `ui/group_management_dialog.py`

### a) Description

```python
groups = [self.groups[self.group_list.row(i)] for i in selected]
```

The selected `QListWidgetItem`s were translated into indices of `self.groups`.
That only holds while the widget and the list are perfectly in sync; any
filtering, sorting or partial refresh makes it **delete or verify the wrong
group**, and an index beyond the end raises `IndexError`. `remove_selected_groups()`
additionally used `list.remove()`, which matches `ADGroup` by DN, so a duplicate
DN could remove a different object than the one selected.

### b) Verification

```python
groups = [ADGroup(name=f"G{i}", dn=f"CN=G{i},DC=x") for i in range(4)]
d = GroupManagementDialog(initial_groups=groups)
d.group_list.item(1).setSelected(True)
d.group_list.item(3).setSelected(True)
d.remove_selected_groups()
print([g.name for g in d.groups])      # ['G0', 'G2']  - G1 and G3 removed
```

### Fix

Each item carries its `ADGroup` in `Qt.ItemDataRole.UserRole`; the new helper
`_groups_from_items()` reads it back, and removal filters by identity.

---

## E17 — Group verification updated only the first group of a template

**File:** `ui/group_management_dialog.py`

### a) Description

```python
for i, group in enumerate(template.groups):
    if group.dn.lower() in updated_dns:
        …
        template.groups[i] = updated
        template.mark_verified()
        break                     # <- stops after the FIRST match
```

A template containing several verified groups kept the **stale** data of all but
one of them, while `mark_verified()` claimed the whole template had been
verified.

### b) Verification

```python
tpl = GroupTemplate(name="T", groups=[ADGroup(name="old1", dn="CN=G0,DC=x"),
                                      ADGroup(name="old2", dn="CN=G2,DC=x")])
d = GroupManagementDialog(initial_groups=[], initial_templates=[tpl])
d._update_templates_with_groups([ADGroup(name="new1", dn="CN=G0,DC=x"),
                                 ADGroup(name="new2", dn="CN=G2,DC=x")])
print([g.name for g in d.template_manager.get_all_templates()[0].groups])
# BEFORE: ['new1', 'old2']
# AFTER : ['new1', 'new2']
```

### Fix

The `break` was removed, the lookup uses a DN dictionary, and
`mark_verified()` is called once after the loop — and only when something
actually changed.

---

## E18 — Passwords were generated with a non-cryptographic random generator

**File:** `utils/ad_utils.py`

### a) Description

```python
import random
password = [random.choice(lowercase), …]
random.shuffle(password)
```

`random` is a Mersenne Twister seeded from the system clock. Its output is
predictable: an observer who sees a few generated passwords can reconstruct the
internal state and derive all the others. For values that become Active
Directory account passwords this is not acceptable.

### b) Verification

```python
import inspect, utils.ad_utils as m
src = inspect.getsource(m.generate_password)
print("secrets" in src, "random.choice" in src)
# BEFORE: False True      AFTER: True False
```

### Fix

`generate_password()` now uses `secrets.SystemRandom()` (the operating system's
cryptographic entropy source) for the character choice **and** the shuffle.

---

## E19 — A damaged USRX file produced a raw `KeyError`

**File:** `utils/encryption.py`

### a) Description

```python
salt  = base64.b64decode(metadata['salt'])
nonce = base64.b64decode(metadata['nonce'])
tag   = base64.b64decode(metadata['tag'])
```

For a truncated container, or one written by another tool, this raised
`KeyError: 'salt'`, which reached the user as a meaningless message. Invalid
Base64 produced an equally unhelpful `binascii.Error`.

### b) Verification

```python
from utils.usrx_format import write_usrx_file
from utils.encryption import decrypt_file_with_format

write_usrx_file("/tmp/damaged.usrx", b"garbage", "aes-gcm", {"note": "no parameters"})
try:
    decrypt_file_with_format("/tmp/damaged.usrx", "pw")
except Exception as e:
    print(type(e).__name__, e)

# BEFORE: KeyError 'salt'
# AFTER : RuntimeError The USRX file is damaged or was not written by this
#         application - missing encryption parameter(s): salt, nonce, tag
```

### Fix

The parameters are checked before use and both failure modes are converted into
a `RuntimeError` with an explanation. The temporary-file cleanup handlers no
longer use a bare `except:` either.

---

## E20 — The comparison panel kept a pointer to a deleted source

**File:** `ui/comparison_tab.py`

### a) Description

`refresh_sources()` remembered the selected text, rebuilt the combo box and
restored the selection:

```python
index = self.source_combo.findText(current)
if index >= 0:
    self.source_combo.setCurrentIndex(index)
self.source_combo.blockSignals(False)
```

When the selected source had been removed, `findText()` returned `-1`, so the
combo silently fell back to "(Select Source)" — but `self.current_source` still
referenced the **deleted** `Source` object and the tree still displayed it.
Because the signals were blocked, `on_source_changed()` never ran to clear it.
Any following operation (add person, rename class, shift) then modified an
orphaned object whose changes could never be seen again.

### b) Verification

1. Select a source in the *Left Source* panel.
2. Load a file that replaces it (or remove it), so the name disappears.
3. **Before:** the tree still shows the old data and editing appears to work.
   **After:** the panel clears itself and the statistics line becomes empty.

### Fix

`refresh_sources()` now clears `current_source`, the tree and the statistics
label when the previously selected source is gone.

---

## E21 — Deleting persons could silently delete nothing

**File:** `ui/comparison_tab.py`

### a) Description

```python
for person in persons_to_delete:
    cls = self.current_source.find_class_by_name(person.class_name)
    if cls:
        cls.remove_person(person)
…
QMessageBox.information(self, "Success", f"Deleted {len(persons_to_delete)} person(s)")
```

The class was located through `person.class_name`. When that value did not
match the class the person is actually stored in — a state the new analysis
explicitly detects and reports — `find_class_by_name()` returned `None` (or the
wrong class) and **nothing was deleted**, while the success message still
claimed the students were gone.

### b) Verification

```python
person.class_name = "does-not-exist"    # simulate the mismatch
panel.on_delete_persons()
# BEFORE: "Deleted 1 person(s)" - but the person is still in the tree
# AFTER : the person is really removed; the message reports the real count
```

### Fix

The person is now searched for in **all** classes and removed by identity, and
the message reports the number of records actually removed.

---

## E22 — Other robustness improvements

| File | Improvement |
|---|---|
| `main.py` | The fatal-error handler used a bare `except:`, which also swallows `KeyboardInterrupt` and `SystemExit`; it now catches `Exception`. |
| `utils/ad_sync_task.py` | Closing the AD connection swallowed every error with `except Exception: pass`; the problem is now logged. |
| `ui/log_viewer_tab.py` | `LogLoaderThread` did not handle `OSError` (an unreadable device or a network drop reached the user as "Unexpected error"). |
| `ui/comparison_tab.py` | Refreshing after an external change discarded the active search filter; the filter is now preserved. |
| `ui/comparison_tab.py` | "Add Person" treated an empty entry like *Cancel*; empty values are now reported, and cancelling is distinguished from entering nothing. |
| `ui/comparison_tab.py` | Renaming a class to an existing name is now offered as a merge instead of being refused. |
| `services/ad_services.py` | A failure while assigning groups no longer turns a successfully created user into a reported failure. |
| `ui/import_dialog.py` | Unavailable decryption backends are disabled instead of failing later with a confusing message. |

---

## Summary

| ID | Severity | Effect before the fix |
|---|---|---|
| E01 | **Crash** | `NameError` during every synchronisation involving groups |
| E02 | **Security** | Clear-text passwords written into the log file |
| E03 | **Crash** | The application did not start without `ldap3` |
| E04 | **Crash** | The application did not start without `PyQt6-WebEngine` |
| E05 | **Data** | Bulk edit applied twice; wrong password; junk attributes |
| E06 | **Data** | Wrong student edited / exported after sorting a table |
| E07 | Robustness | Custom signals shadowed `QThread.finished` |
| E08 | Usability | The interface froze on a blocking `wait()` |
| E09 | Latent | `source_to_json()` returned `None` |
| E10 | **Data** | Export/import lost groups, home directory, policy flags, account status |
| E11 | **Data** | "Replace it?" created a second, unreachable source |
| E12 | **Data** | Removal deleted the wrong person / class / source |
| E13 | **Data** | The merged source shared objects with its inputs |
| E14 | **Data** | Logging settings silently not saved, success reported anyway |
| E15 | Usability | The Logs tab looked at the previous log folder |
| E16 | **Data** | Group list could verify or delete the wrong group |
| E17 | **Data** | Only the first group of a template was refreshed |
| E18 | **Security** | Passwords generated with a predictable RNG |
| E19 | Usability | A damaged USRX file produced a raw `KeyError` |
| E20 | **Data** | Panel kept editing a deleted source |
| E21 | **Data** | Deleting persons could silently delete nothing |
| E22 | Robustness | Assorted error-handling improvements |
