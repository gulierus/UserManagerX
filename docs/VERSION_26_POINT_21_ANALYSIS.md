# Version 26, point 21 — Microsoft 365: analysis, answers, and what is blocked

This point asks for an entire subsystem. Before any of it is built, here are
the answers to every question it contains, the library decision, and an honest
statement of which parts cannot be built from the material provided.

---

## 0) What arrived, and what did not

`main.cs` was **cut off at 50 000 characters**. The delivered part ends in the
middle of a comment:

```csharp
    /// <summary>
    /// Data model for group export/import
[Message truncated - exceeded 50,000 character limit]
```

That is exactly where the interesting half begins. What arrived is the
`Program` class: the scopes, the two authentication initialisers, the menu, and
the *call sites* of the workers. Every worker class is missing:

| Class | Referenced at | Delivered? |
|---|---|---|
| `GroupExporter` | `ExportGroupsAsync()` | **no** |
| `AdvancedGroupExporter` (holds `ExportGroupsInteractiveAsync`) | `AdvancedExportGroupsAsync()` | **no** |
| `GroupImporter` | `ImportGroupsAsync()` | **no** |
| `PasswordManager` | `ResetGroupMembersPasswordAsync()` | **no** |
| `TeamCreator` | `ImportAndCreateTeamAsync()` | **no** |
| `GroupSearchEngine` | `SearchGroupsAsync()` | **no** |
| the export/import data model | — | **no** |

### What that blocks

* **21 d** — *"the same logic and functionality as the `ExportGroupsInteractiveAsync`
  function in `main.cs`"*. That function is in `AdvancedGroupExporter`, which
  did not arrive. The formats it offers, the columns it writes and the options
  it asks for are unknowable from what is here. **Blocked.**
* **21 c** — *"the exact file format and import/export procedure can be found in
  the `main.cs` file"*. The format is defined by the missing data model and
  read by the missing `GroupImporter`. **Blocked on the exact format.**

  There is also a contradiction worth resolving before this is built: the point
  says the file is *"a JSON file that was exported from Microsoft 365 using the
  official export function … in the web interface after logging in to Microsoft
  365 Admin"*, but the Microsoft 365 admin centre's own export function
  produces **CSV**, not JSON. A JSON file in this shape is much more likely to
  be one `main.cs` itself wrote. Which of the two is meant changes the parser
  completely.

**To unblock both:** send the rest of `main.cs` (from `/// Data model for
group export/import` onwards), or send one real exported file of each kind.
Splitting the file across two messages, or attaching it as a file rather than
pasting it, avoids the 50 000-character cut.

Nothing below is blocked — 21 a and 21 e can be built from the Microsoft Graph
API itself, and all the questions can be answered now.

---

## 1) Which library

**Recommendation: `msgraph-sdk` together with `azure-identity`** — the official
Microsoft Graph SDK for Python.

| Option | Verdict |
|---|---|
| **`msgraph-sdk` + `azure-identity`** | The direct counterpart of the C# `Microsoft.Graph` + `Azure.Identity` pair the tool already uses, so the semantics carry over one-to-one. Typed models, paging handled for you, and the credential classes match the C# ones by name. **Chosen.** |
| `msal` + hand-written REST calls | Fewer dependencies and complete control, but every request, every paging loop and every error shape becomes ours to maintain. Reasonable only if the SDK turns out to be a problem. |
| `O365`, `pyad`, other third-party wrappers | Not maintained by Microsoft, and they lag behind Graph. Rejected. |

Two consequences for this application:

* **It is asynchronous.** `msgraph-sdk` is built on `httpx` and every call is a
  coroutine. That fits the request ("some operations must be performed
  asynchronously"): each task gets its own `asyncio` event loop **inside** the
  worker thread of an `AbstractProgressTask`, exactly as the EduPage tasks do,
  and the GUI thread never awaits anything. Point 25 of this same request is
  the reminder of why that rule matters — anything touching a widget from a
  worker thread corrupts memory.
* **It must be an optional dependency.** `ldap3` was once a hard import and
  stopped the whole application from starting for users who only export PDFs
  (version 24, point E03). The fix was `services/ldap_compat.py`. The Graph SDK
  gets the same treatment: a thin compatibility module, a `GRAPH_AVAILABLE`
  flag, and a clear message instead of a `ModuleNotFoundError` at start-up.

### Which operations should be asynchronous

| Operation | Background task? | Why |
|---|---|---|
| Signing in / acquiring a token | **yes** | a network round trip, and the device-code flow waits for the user |
| Listing groups and teams | **yes** | paged, hundreds of groups on a school tenant |
| Reading the owners and members of a group | **yes** | one request per group, and the wizard needs it while the user browses |
| Creating groups, adding members, creating teams | **yes** | one request per object, and a class of 30 is 30 round trips |
| Resetting passwords | **yes** | one request per user, plus polling a long-running operation |
| Building `Class` objects from the selected groups (step 2) | no | pure computation on data already in memory |
| Rendering a placeholder into a class name | no | pure string work |

---

## 2) Authentication — and the honest answer about 2FA

### The question: *"Will 2FA be supported? … Can 2FA support be implemented?"*

**With username and password: no. Not as a limitation of this application, but
because the flow itself has no way to ask for a second factor.**

Signing in with a username and a password from inside a program means the OAuth
2.0 **Resource Owner Password Credentials** flow — `UsernamePasswordCredential`
in `azure-identity`. The program sends the two strings and receives a token.
There is no step in that exchange at which a code, a push notification or a
security key could be requested. When the account has MFA enforced, Microsoft
answers with an error instead of a token:

```
AADSTS50076: Due to a configuration change made by your administrator ...
             you must use multi-factor authentication to access '...'.
```

ROPC additionally does not work for federated accounts, for guest accounts, or
when a Conditional Access policy requires a compliant or hybrid-joined device.
It also requires the app registration to have *"Allow public client flows"*
switched on.

And the premise in the question is right: when signing in to the Microsoft 365
admin centre in a browser, MFA is effectively mandatory for an administrator —
Microsoft's security defaults require it for privileged roles, and most tenants
have Conditional Access on top. So for exactly the accounts this tool needs,
username-and-password will usually fail.

### What can be done instead

| Method | In-app? | Works with MFA? | Needs a browser? |
|---|---|---|---|
| **Username + password** (ROPC) | yes | **no** | no |
| **Device code** | yes — the app shows a code and a short URL | **yes** | the *second factor* happens on any device, including a phone |
| **App-only / client credentials** (Client ID + Client Secret) | yes | **not applicable** — there is no user, so there is no second factor | no |
| Interactive browser (what `main.cs` does) | no | yes | yes |

**Recommendation, and what will be built:**

1. **Username and password**, as asked for — with the limitation stated in the
   connection panel *before* the user tries it, not as an error afterwards. If
   the tenant allows it (a dedicated service account exempted from MFA), it is
   the simplest thing there is.
2. **App-only authentication** with Client ID, Client Secret and Tenant ID, as
   asked for. This is the method that genuinely delivers *"integrate everything
   into the application"*: no user, no browser, no second factor, and an
   administrator consents to the permissions once.
3. **Device code** as a third option, because it is the only way to sign in as
   a real administrator **with** MFA and **without** the application driving a
   browser. It costs very little next to the other two, and it is the honest
   answer to *"can 2FA support be implemented?"* — yes, this way.

### The permissions each method needs

`main.cs` asks for these four scopes, and they are the right set:

```
User.ReadWrite.All
Group.ReadWrite.All
UserAuthenticationMethod.ReadWrite.All
Directory.ReadWrite.All
```

Delegated (methods 1 and 3) they are *scopes* the signing-in administrator must
already be entitled to; app-only (method 2) they are *application permissions*
that need one-off admin consent in Entra ID. Note that they are **not
interchangeable** — see the password section below for where that bites.

> These permission names and behaviours should be confirmed against Microsoft's
> current Graph documentation before release; Microsoft changes them.

---

## 3) *"Does each source have specific fields for storing data about its origin?"*

**Yes — two of them, and they are already used.**

`models.Source` carries:

```python
source_info: Dict[str, Any] = field(default_factory=dict)   # + set/get helpers
metadata:    Dict          = field(default_factory=dict)
```

`set_source_info(key, value)` / `get_source_info(key, default)` exist for
exactly this purpose. A Microsoft 365 source will record at least:

| Key | Example |
|---|---|
| `tenant_id` | `4f1e…-…-…` |
| `tenant_domain` | `skola.onmicrosoft.com` |
| `auth_method` | `app_only`, `username_password`, `device_code` |
| `signed_in_as` | `admin@skola.cz` (empty for app-only) |
| `client_id` | the app registration used |
| `loaded_at` | when the data was read |
| `group_count` | how many groups became classes |

**No secret is ever stored there.** A source is serialised into exports and
shown in the Source Manager; a client secret or a password in it would leak.

---

## 4) *"Is it true that the application maps `Class` to Active Directory organizational units?"*

**Yes, and yes — the user cannot choose the name.** One line settles both
halves (`services/ad_services.py`, in `create_sync_plan`):

```python
target_ou = (f"OU=Trida-{escape_dn_value(person.class_name)}"
             f",{base_dn}")
```

So:

* one organisational unit per class, directly under the Base DN;
* the name is **hard-coded** as `Trida-` plus the class name — there is no
  setting, no template and no dialog for it;
* if the unit does not exist, `execute_sync` creates it
  (`ou_exists()` → `create_ou()`) and then creates the persons inside it.

The prefix is the same convention the "1. Data Sources" Active Directory loader
searches for, so the two halves agree — but only by both hard-coding the same
string. Making it a template, the way the Microsoft 365 group name is required
to be in this very point, would be a small and worthwhile change. It is **not**
part of this request, so it has not been made; say the word and it will be.

---

## 5) *"How are group owners set when creating a group?"*

Through Graph, in one of two places:

**At creation**, as OData binding arrays in the same request that creates the
group — up to 20 of each:

```jsonc
POST /v1.0/groups
{
  "displayName": "6.A",
  "mailNickname": "trida-6a",
  "mailEnabled": true,
  "securityEnabled": false,
  "groupTypes": ["Unified"],
  "owners@odata.bind": [ "https://graph.microsoft.com/v1.0/users/{owner-id}" ],
  "members@odata.bind": [ "https://graph.microsoft.com/v1.0/users/{member-id}" ]
}
```

**Afterwards**, one at a time:

```
POST /v1.0/groups/{group-id}/owners/$ref
{ "@odata.id": "https://graph.microsoft.com/v1.0/users/{owner-id}" }
```

Two things matter for this application:

1. **A Microsoft 365 group should not be left without an owner.** When a group
   is created by a *signed-in user* (delegated), that user becomes the owner
   automatically. When it is created **app-only**, there is no signed-in user —
   so unless owners are supplied explicitly, the group is created ownerless and
   nobody can manage it in the web interface. The connection panel will
   therefore offer a "group owner" setting, and app-only mode will insist on it.
2. **A team needs an owner before it can be created.** Turning a group into a
   team (`PUT /groups/{id}/team`) fails on an ownerless group.

---

## 6) *"What exactly is required to remotely set a person's password in Microsoft 365?"*

There are **two different APIs**, they need different permissions, and only one
of them works app-only. This is the part most likely to surprise.

### Option A — `passwordProfile` on the user

```
PATCH /v1.0/users/{id}
{ "passwordProfile": { "password": "…",
                       "forceChangePasswordNextSignIn": true } }
```

* Permission: `User.ReadWrite.All` (delegated) or
  `Directory.AccessAsUser.All`; app-only needs the `User.ReadWrite.All`
  **application** permission.
* The caller must hold a role that may reset that user's password — Password
  Administrator, User Administrator, or Global Administrator.
* **A user holding a higher privileged role cannot have their password reset
  by a lower-privileged caller.** Resetting a Global Administrator's password
  requires Global Administrator.
* Works **app-only** for ordinary users — which is what a school needs for
  pupils.

### Option B — the authentication-methods API

```
POST /v1.0/users/{id}/authentication/passwordMethods/
     28c10230-6103-485e-b985-444c60001490/resetPassword
{ "newPassword": "…" }
```

* Permission: `UserAuthenticationMethod.ReadWrite.All` — the third scope
  `main.cs` asks for.
* That GUID is the documented well-known identifier of the password method; it
  is the same for every user.
* The call returns **202 Accepted** with a `Location` header: it is a
  long-running operation that must be **polled** until it reports `succeeded`.
  Treating the 202 as success is a mistake that looks like it worked.
* **Delegated only.** This endpoint does not support app-only access, so a tool
  signed in with a client secret cannot use it.

### What this application will do

| Signed in as | Password reset uses | Notes |
|---|---|---|
| App-only (client secret) | **Option A** | works for pupils; refuses, with a clear message, for accounts holding an admin role |
| User (device code, or username+password) | **Option B**, falling back to A | B is the API Microsoft points at, and the one `main.cs` asks scopes for |

Either way, the practical checklist for the administrator is:

1. An app registration in Entra ID with the four permissions above, **granted
   admin consent**.
2. For app-only: a client secret, and the app's service principal assigned a
   directory role that may reset passwords.
3. For delegated: an account that actually holds Password Administrator or
   higher.
4. The new password must satisfy the tenant's password policy, or Graph
   rejects it — the application's own password generator will be pointed at the
   Microsoft 365 rules, not the Active Directory ones.
5. `forceChangePasswordNextSignIn` decides whether the pupil must change it at
   first sign-in. Note that with MFA registration enforced, a forced change can
   collide with the registration prompt; for pupil accounts it is usually left
   off.

---

## 7) Plan for what is not blocked

| Part | State |
|---|---|
| Graph compatibility layer + the three credentials, as an optional dependency | to build |
| **21 a** — "Microsoft 365 From Web" source, two-step group/team → class wizard, placeholders in the class name, automatic enrollment year | to build |
| **21 e** — "Microsoft 365 Management" operation, mirroring the Active Directory one | to build |
| **21 c** — "Microsoft 365 From File" | **blocked** on the file format |
| **21 d** — export operation | **blocked** on `ExportGroupsInteractiveAsync` |
| **22 / 23** — PDF and JSON export of the Microsoft 365 fields | follows 21 a/e, since those define the fields |

Dedicated classes for groups and teams — not `Class` — as the point requires:
`M365Group` and `M365Team` carry what Microsoft 365 holds (id, display name,
mail nickname, description, visibility, group types, owners, members), and a
`Class` is built **from** one of them in step 2 of the wizard, where the user
names it.
