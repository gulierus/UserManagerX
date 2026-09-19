# UserManagerX

A desktop application for managing school student data — importing it from
EduPage or encrypted files, comparing and merging data sets between school
years, and provisioning the matching Active Directory accounts.

Built with **Python 3** and **PyQt6**.

---

## What it does

| Tab | Purpose |
|---|---|
| **1. Data Sources** | Load students from EduPage (with 2FA), from an encrypted file, from Active Directory, or from Microsoft 365 |
| **2. Comparison and Sync** | Compare two sources side by side, analyse and repair the data, shift class years, convert class numerals, merge sources |
| **3. Operations** | Active Directory management (credentials, groups, synchronisation), Microsoft 365 management, encrypted PDF export, encrypted JSON export |
| **4. Settings** | Appearance, logging configuration, general options |
| **5. Logs** | Live log view and historical log files |

### Highlights

**Class-name handling.** Class names are free-form strings, because that is what
the upstream systems deliver. The application parses Arabic and Roman numerals
anywhere in the name and preserves everything around them, so `6.A`, `IX.`,
`9 B`, `VI. B` and `Blue class 6.A` are all understood — and
`Blue class 6.A` shifts to `Blue class 7.A`.

**Pre-flight analysis.** Before shifting or merging, the data is checked for
every problem that could make the operation produce a wrong result — mixed
naming formats, stray spaces, duplicate classes, students whose class does not
match the class they are stored in, name collisions after a shift, and so on.
Each problem comes with the solutions you can apply, and fixes chain: each one
operates on the data the previous one produced. Nothing is changed until you say
so, and the input sources are never modified.

**Encryption.** Data can be exported to AES-GCM or GPG encrypted files, either
in the standard format or in the `.usrx` binary container.

---

## Requirements

```
Python 3.10+
PyQt6
```

Optional, per feature:

| Package | Needed for |
|---|---|
| `ldap3` | Active Directory features |
| `pycryptodome` | AES-GCM encryption |
| `python-gnupg` + GnuPG | GPG encryption |
| `reportlab` | PDF export |
| `PyQt6-WebEngine` | PDF preview |
| `edupage-api` | EduPage import |
| `pikepdf` | PDF password handling |
| `msgraph-sdk` + `azure-identity` | Microsoft 365 features |

Missing optional packages disable only their own feature — the application
still starts and everything else keeps working.

```bash
pip install PyQt6
pip install ldap3 pycryptodome reportlab PyQt6-WebEngine edupage-api  # optional
pip install msgraph-sdk azure-identity                                # optional, Microsoft 365
```

---

## Running

```bash
python main.py
```

---

## Tests

```bash
pip install pytest
python -m pytest                 # the whole suite
python -m pytest -m bug          # regression tests for the 137 fixed defects
python -m pytest tests/test_models.py -v
```

The suite runs headless (offscreen Qt) and never touches the real user
settings, the real log directory or the network. See
[`docs/VERSION_24_TESTS_AND_FIXES.md`](docs/VERSION_24_TESTS_AND_FIXES.md) for
what it covers and for the defects it found.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/VERSION_24_PHASE_01.md`](docs/VERSION_24_PHASE_01.md) | The 22 requested changes of version 24, each with the reasoning and the resulting behaviour |
| [`docs/VERSION_24_PHASE_02.md`](docs/VERSION_24_PHASE_02.md) | Defects found by a full review of the code base, with reproductions |
| [`docs/VERSION_24_TESTS_AND_FIXES.md`](docs/VERSION_24_TESTS_AND_FIXES.md) | The test suite and the defects it exposed |

---

## Project layout

```
main.py                  application entry point, main window, tab bar
models.py                Person / Class / Source / SourceManager + merging
sources/                 data source widgets (EduPage, encrypted file, AD)
operations/              AD management, PDF export, JSON export
services/                Active Directory client, services, validator
ui/                      dialogs and tab widgets
utils/                   class names, analysis, encryption, tasks, settings
tests/                   pytest suite
docs/                    change and defect documentation
```

---

## A note on data protection

This application processes personal data about children. Keep in mind that:

* log files can contain student names — `logs/` is git-ignored for that reason;
* exported files may contain names **and account passwords**; always export
  them encrypted and treat the password accordingly;
* `settings.json` stores the last used AD server and user name (never a
  password) and is git-ignored as well.
