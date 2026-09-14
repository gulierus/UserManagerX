# Version 25 — Phase 01

Answers to the questions, and what changed for each point.

---

## 05) Passwords could not be set — `unwillingToPerform`

### Why it happened

`ADClient.connect()` opened a **plain, unencrypted `ldap://` connection**. The
LDAPS code was present but commented out:

```python
# tls_conf = Tls(validate=CERT_NONE)
# server_obj = Server(self.server, port=636, use_ssl=True, get_info=ALL)
server_obj = Server(self.server, get_info=ALL)          # <- plain LDAP
```

Active Directory **refuses to write the `unicodePwd` attribute over an
unencrypted channel**. It is not a permission problem and not a password-policy
problem: the directory rejects the operation before it looks at either. The
answer it sends is exactly the one in the report:

```
result 53, unwillingToPerform,
"0000001F: SvcErr: DSID-031A1260, problem 5003 (WILL_NOT_PERFORM)"
```

That is why it failed **for every user**, and why creating accounts still
worked — creating an object needs no encryption, only the password does.

The two log lines that look contradictory are two different calls:

```
INFO  - Modified user: CN=Karel Atav (8.A),...      <- the attribute update, succeeded
ERROR - Failed to modify user CN=Karel Atav (8.A)   <- the password, refused
```

### The fix

* `connect()` honours an `ldaps://` address and otherwise upgrades the channel
  with **StartTLS**; the outcome is recorded in `ADClient.is_secure`.
* A new `ADClient.set_password()` uses ldap3's
  `extend.microsoft.modify_password`, which builds the quoted UTF-16-LE value
  AD expects, and **refuses up front** with an explanatory message when the
  channel is not encrypted — instead of letting the server answer with an
  opaque error code.
* The connection panel gained **“Encrypt connection (StartTLS)”** (on by
  default) and **“Verify server certificate”** (off by default, because school
  domain controllers commonly use a self-signed certificate). The settings
  reach every connection the tab opens.

### After the fix

| Connection | Account created | Password set | Groups applied |
|---|---|---|---|
| plain `ldap://` | yes | **no** (AD's rule) — reported clearly | **yes** |
| `ldaps://` or StartTLS | yes | **yes** | yes |

---

## 06) Users were created but never added to groups

**Yes — this was caused by 05.** The two are the same bug seen from two sides.

`_execute_create()` and `_execute_update()` were written as a straight line, and
the password step returned from the method when it failed:

```python
if person.ad_password:
    try:
        self._set_password(dn, person.ad_password, person)
    except Exception as pwd_error:
        return OperationResult(..., message="... but password setting failed")   # <- returns

# ... the group synchronisation lives HERE, after that return
if person.group_memberships:
    ADGroupService(self.ad_client).sync_user_groups(person)
```

Since the password failed for *every* user, the group step never executed for
any of them. The application showed the memberships because it had them in
memory; nothing had ever been written to the directory.

### The fix

Every per-user step is now independent. Password, account flags, group
memberships and the home directory each run regardless of what the previous one
did, and the failures are collected:

```
Created user atavkarel, but: password not set (Active Directory refused ...)
```

A person with outstanding work is left `UPDATE_PENDING` instead of being marked
`SYNCED`, so the next synchronisation picks the remaining work up rather than
believing it is done.

This is the general behaviour you asked for: **one failed step no longer
cancels the rest of the synchronisation for that person.**

---

## 07) What the “Base DN” field does

### a) Where people are searched

**Before this change: the whole subtree, to any depth.**

`ADClient.search_users()` always passed `search_scope=SUBTREE`:

```python
self.connection.search(
    search_base=base_dn,
    search_filter=search_filter,
    search_scope=SUBTREE,        # <- everything below, at any depth
    attributes=attributes,
)
```

So with

```
Base DN = OU=Zaci,OU=Uzivatele,OU=SKOLA,DC=skola,DC=fdok,DC=cz
```

a person was looked for in that OU **and in every organisational unit nested
inside it, however deep** — `OU=Trida-6.A`, `OU=Trida-6.A/OU=Something`, and so
on. Organisational units are therefore *not* ignored; they were all included,
which is the opposite problem: the search is broader than a per-class layout
needs, and a pupil who shares a name with somebody in another branch can be
found there.

The Base DN is used for three other things as well:

| Used by | How |
|---|---|
| `SyncPlanner.create_sync_plan` | Builds the target OU of a new account: `OU=Trida-<class>,<base dn>` |
| `_prepare_creation_attributes` | Derives the UPN suffix from the `DC=` parts (`DC=skola,DC=fdok,DC=cz` → `@skola.fdok.cz`) |
| Group discovery | The OU the group search starts from |

### The new options

The connection panel gained a **“Search in:”** choice:

| Option | Meaning |
|---|---|
| **Whole subtree below the Base DN** | The previous behaviour, still the default |
| **Only the OUs directly in the Base DN** | One level only — nothing nested deeper |
| **Only the organisational units I name** | Only the OUs you list |

For the third option you list OU names, separated by a comma or a semicolon.
They are **templates**, resolved per person, so one entry covers every class:

| Placeholder | For `6.A` |
|---|---|
| `{class_name}` | `6.A` |
| `{grade}` | `6` |
| `{roman}` | `VI` |
| `{letter}` | `A` |
| `{enrollment_year}` | `2020` — the year that class started grade 1 (point 17) |

```
Trida-{class_name}   ->  OU=Trida-6.A,OU=Zaci,OU=Uzivatele,OU=SKOLA,DC=skola,DC=fdok,DC=cz
Rocnik-{grade}       ->  OU=Rocnik-6,...
{enrollment_year}    ->  OU=2020,...
```

Values are DN-escaped, so a class called `6,A` produces `OU=Trida-6\,A,...`
rather than a broken DN. A template that cannot be resolved (a class with no
number, say) is skipped rather than producing a nonsense search base, and the
Base DN itself is always appended as a fallback so a pupil whose class has no
matching OU is still found instead of being silently reported as missing.

### b) Which field is searched

Four strategies are tried **in order**, and the first one that returns exactly
one match wins:

| # | Field | LDAP filter |
|---|---|---|
| 1 | The stored DN | direct read, no search — only when the person already has `ad_dn` |
| 2 | **User name** | `(sAMAccountName=<ad_username>)` |
| 3 | **E-mail** | `(mail=<ad_email>)` |
| 4 | **First + last name** | `(&(givenName=<first_name>)(sn=<last_name>))` |

More than one match at step 2 or 4 marks the person `AMBIGUOUS` rather than
picking one.

**Example — Karel Atav, class 6.A, user name `atavkarel`, no e-mail:**

1. No `ad_dn` yet → skipped.
2. `(sAMAccountName=atavkarel)` searched under
   `OU=Zaci,OU=Uzivatele,OU=SKOLA,DC=skola,DC=fdok,DC=cz`.
   One hit → **found**, and the search stops here. His DN
   (`CN=Karel Atav (6.A),OU=Trida-6.A,...`) is stored for next time.
3. Had he no user name, the e-mail would have been tried.
4. Had that failed too, `(&(givenName=Karel)(sn=Atav))` would have been tried —
   which is the step that most often returns several matches, because two
   pupils can share a name.

---

## 08) What `exists_in_ad` means

### a) It means only “an account was found”

`exists_in_ad` says **nothing** about whether the values match. Discovery sets
it the moment one of the four searches above returns a single entry:

```python
if result.found_in_ad and not result.ambiguous:
    person.ad_status = ADStatus.EXISTS_IN_AD
    person.ad_dn = result.ad_dn
    person.metadata['ad_current_values'] = result.ad_attributes
```

Display Name, Email and every other field may differ completely; the status is
unchanged. It answers *“is there an account for this person?”*, not *“is this
person in sync?”*.

The values that were read **are** stored, in
`person.metadata['ad_current_values']`, but only as a baseline for later
conflict detection — they are not compared with the application's own values at
this point.

### b) Where values are and are not compared

| Comparison | Where | What it means |
|---|---|---|
| Application value vs. AD value | **nowhere** | The application never asks “does my Display Name differ from the one in AD?” |
| AD value *now* vs. AD value *at discovery* | `ConflictDetector.check_conflict` | Detects that **somebody else** changed AD since you looked |
| Application value *now* vs. application value *when loaded* | `Person._dirty_fields` | Detects that **you** changed something |

A field is written to AD when **you** changed it (it is dirty), not when it
differs from AD. So a Display Name edited directly in Active Directory by
somebody else is *not* detected or overwritten — unless you also edit it here,
which is exactly when the conflict detector steps in and asks.

There is deliberately **no** “compare everything with AD and show the
differences” function. If you want one, say so — the pieces are all there
(`ad_current_values` already holds what AD returned).

---

## 12) b) An illogical drive letter

**Before:** the field accepted anything. Typing `sfsdf` wrote
`person.home_drive = "sfsdf"` for every selected person, and the value went on
to AD's `homeDrive` attribute, where Windows silently fails to map it. Nothing
warned, and the bulk edit reported success.

**After:** the field warns inline as you type (`⚠ use a letter and a colon,
e.g. H:`) and, if you confirm anyway, the dialog asks first:

> `sfsdf` is not a drive letter. Windows can only map a single letter followed
> by a colon (for example H:). `sfsdf` will be written to Active Directory but
> will not work. Use it anyway?

It is still possible to force it — the directory is yours — but not by accident.
