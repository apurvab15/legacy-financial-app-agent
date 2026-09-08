# Legacy core mock UI (Phase 0 freeze)

This is the **screen and DOM spec** for Phase 1. Do not implement HTML in Phase 0.

The mock is a **controlled proxy** of the assignment’s “legacy web” constraints (server-rendered, frames, nested tables, non-semantic markup, no test IDs). It is **not** a replica of Jack Henry / Fiserv / FIS. Copying a real core would be the wrong move and a possible ToS/IP problem.

Demo goal the rest of the system is built around: *look up member 12345 and read their current savings balance.*

Local only. Fake data only. No real bank, no real credentials, no real PII.

---

## 1. What we are simulating (and why)

The brief names the surface. The mock exists so locator strategy, error taxonomy, and the 3.7 “surface adapter” story have something real to chew on.

| Legacy pattern | What the mock must do | Automation consequence |
| --- | --- | --- |
| **Frames / iframes** | Nav document + content document | Locators must set `frame: "content"`; parent-document CSS misses the field |
| **Nested layout tables** | Label in one cell, input in the next; **omit `<label for>`** on at least Member ID | `getByLabel` is insufficient; use role+name or nearby text |
| **Generated ids, no test ids** | `ctl00_…` ids, classes like `tb` / `btn`, **zero** `data-testid` | CSS/`#ctl00_Main_txtCIF` is allowed in HTML but **forbidden as the primary replay locator** |
| **Server-rendered postback** | Form POST; content frame reloads to a new HTML page | Checkpoint heading `Member Profile` is meaningful |
| **Runtime errors, stable chrome** | Injectable not-found / validation / timeout / permission | Classifiers on **banner text**, not weekly redesigns |

Sources to cite later in `REPORT.md` (do not copy vendor UI): HTML frames in enterprise containers (e.g. Oracle Siebel), ASP.NET WebForms naming-container ids (`ctl00_…`), WCAG notes on layout tables, the assignment’s own glossary (“legacy enterprise apps essentially never have test IDs”).

---

## 2. Shell: two documents, not one SPA

MVP implementation choice: a **named iframe shell** (HTML5-legal, Playwright-friendly). Semantically the same as a frameset: two independently loading documents.

| Frame `name` | File (Phase 1) | Role |
| --- | --- | --- |
| (parent) | `index` / shell | Gray chrome, blue title bar **Core Inquiry**, hosts the two frames |
| `nav` | `nav.html` | Left or top links: **Inquiry**, **New Sub-Account** |
| `content` | lookup / profile / banners | **All fields and results live here** |

Capability steps always target `frame: "content"` (see [`CONTRACT.md`](CONTRACT.md)). A locator that only searches the parent document is a hard fail.

No client-side router. No React. After Inquiry submit, the **content iframe** loads a new server-rendered page.

---

## 3. Screen inventory

```text
shell
├── nav: Inquiry | New Sub-Account
└── content:
    ├── lookup form  --valid 12345-->  member profile
    ├── lookup form  --99999------->  Record not found banner
    ├── lookup form  --empty/bad--->  validation banner (still on lookup)
    ├── lookup form  --expired----->  Login expired interstitial
    ├── profile      --open sub---->  confirmation
    └── confirmation --no teller--->  Not authorized banner
```

### 3.1 Lookup (default content)

Hostile 2004 intranet form:

- Outer layout `<table cellpadding="4">` wrapping an inner table
- Row: cell “Member ID” | cell `<input>` with **no** `<label for>`
- Row: Inquiry submit control (visible name **Inquiry**)
- Optional: a second unlabeled or weakly labeled field later if needed; MVP is one id + one button

Visible accessible name for the textbox should still be **Member ID** (e.g. `aria-label="Member ID"` or `title`, **not** a proper `<label for>`). That lets strategy `role` + name work, while nearby-text remains the fallback for the adjacent cell.

### 3.2 Member profile (happy path)

After POST of member `12345`:

- Heading (role `heading`, name **Member Profile**) — this is the capability **checkpoint**
- Outer layout table; inner **data** table for accounts
- Fields a teller would read: member name, member id, savings balance labeled **Savings balance**

### 3.3 Record not found

After POST of member `99999` (or any unknown id):

- A visible error **banner** whose exact text includes **`Record not found`**
- No Member Profile heading
- Known outcome classifier: `member_not_found` → replay `status: "business_outcome"` (not `failed`)

### 3.4 Validation (lookup, no navigation to profile)

Banner text locked for classifiers:

| Condition | Banner text (must be visible in content frame) |
| --- | --- |
| Empty Member ID | `Member ID is required` |
| Non-numeric / junk | `Invalid member ID` |

Replay maps these to `business_outcome` / `validation_rejected` when wired in Phase 2+. Not required for the first two-run demo (`12345` / `99999`).

### 3.5 Login expired interstitial

Injectable (query flag or hidden field), not the default path.

- Banner / interstitial text includes **`Login expired`**
- Blocks Inquiry until dismissed or session “restored”
- Default replay treatment: hard `failed` with `outcome_code: "session_expired"` unless a later recoverable dismiss is added

### 3.6 Open sub-account confirmation (safety / HITL fodder)

From profile, nav link **New Sub-Account** loads a confirmation page in `content`:

- Copy that this will **open a sub-account** (irreversible class)
- Submit button visible name along the lines of **Confirm open**
- Artifact `risk: irreversible` → Phase 3 blocks or Phase 5 requires human

MVP lookup capability does **not** include this submit in its steps. The page exists so guardrails have a real control to refuse.

### 3.7 Not authorized

When confirmation is submitted without a mock “teller” role (default):

- Banner text includes **`Not authorized`**
- `business_outcome` / `not_authorized` if a future capability classifies it; otherwise unused by lookup replay

### 3.8 Slow query

Optional `slow=1` (or equivalent) delays the lookup POST by several seconds so Phase 2 can wait/retry. Not a separate screen.

---

## 4. Demo member data (fake, not PII)

Only two members matter for the assignment demo:

| Member ID | Name | Savings balance | Result |
| --- | --- | --- | --- |
| `12345` | Jane Member | `1,240.55` | Profile + checkpoint |
| `99999` | — | — | `Record not found` |

Do not add real SSNs. If a masked identifier is shown for flavor, use something obviously fake (e.g. `***-**-0000`) and **do not persist** it in artifacts or logs.

No other production-like customer file. Stretch “tenant B” (label **CIF Number** instead of **Member ID**) is out of scope until asked.

---

## 5. Generated ids and classes (present in HTML, unused as primary locators)

Use WebForms-style naming-container prefixes so a CSS-first agent would be tempted and wrong.

Locked examples (Phase 1 may add more in the same pattern):

| Control | `id` (hostile) | Visible name / nearby text |
| --- | --- | --- |
| Member ID field | `ctl00_Main_txtCIF` | Member ID |
| Inquiry button | `ctl00_Main_btnInquiry` | Inquiry |
| Confirm open | `ctl00_Main_btnConfirmOpen` | Confirm open |

- Classes: `tb`, `btn`, `hdr`, `err` — generic, not semantic
- **Zero** `data-testid`, `data-test`, `data-cy`
- Do not rely on stable `nth-child` structure in the capability

What a CSS person would try (and replay must not use as primary): `#ctl00_Main_txtCIF`, `table table tr:nth-child(2) input`.

---

## 6. Error banners — classifier strings (locked)

Exact substrings for `known_outcomes` / result classification. Phase 1 HTML must include these characters as visible text.

| Banner substring | Typical `outcome_code` | Typical `status` |
| --- | --- | --- |
| `Record not found` | `member_not_found` | `business_outcome` |
| `Member ID is required` | `validation_rejected` | `business_outcome` |
| `Invalid member ID` | `validation_rejected` | `business_outcome` |
| `Not authorized` | `not_authorized` | `business_outcome` |
| `Login expired` | `session_expired` | `failed` (default) |

Lookup capability `known_outcomes` in Phase 0/2 is required to include **`Record not found`**. The others may be added as the mock and classifier grow.

---

## 7. Visible chrome (dated on purpose)

- Gray page background, ~8pt default font
- Layout tables with `cellpadding="4"`
- Blue title bar: **Core Inquiry**
- Looks like a 2004 intranet so reviewers see Section 1 of the brief was taken seriously

No modern design system. No screenshots of this UI are sent to any model in later phases.

---

## 8. How this mock causes each requirement

| Requirement | What this UI forces |
| --- | --- |
| **3.1 Discovery** | LLM sees a compact AX tree of the **content** frame; type member id, click Inquiry, notice Member Profile |
| **3.2 Artifact** | Store `frame: "content"` + `{ role: textbox, name: "Member ID" }`, params `member_id`, output `savings_balance`, checkpoint heading Member Profile |
| **3.3 Replay** | `12345` → success + balance; `99999` → `business_outcome` `member_not_found`; missing frame → `failed` |
| **3.4 Safety** | Allowlist localhost only; Confirm open is irreversible |
| **3.5 Evidence** | Trace + AX/DOM of the content frame; no screenshots |
| **3.6 HITL** | Hide Inquiry or inject an unexpected dialog; human clicks in the **same** headed browser |
| **3.7 Write-up** | Frames + tables = web adapter; desktop cores = swap adapter, keep `{ role, name }` |

---

## 9. What we are not claiming

- Not “this HTML is Jack Henry Symitar / SilverLake / DNA.” Those internals are not public; much of the real work is desktop or wrapped 5250.
- Not that every credit union still ships `<frameset>` in 2026.
- The mock’s job is to make **locator + error + handoff** design visible on a laptop.

---

## 10. Still stubbed after Phase 0

There is **no** `apps/legacy_core/` yet. No server, no HTML, no browser to open.

Phase 1 should implement this spec as a local FastAPI or Flask app so a human can type `12345` vs `99999` by hand. Not this phase.
