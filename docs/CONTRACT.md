# Runtime contracts (Phase 0 freeze)

This file is the agreement discovery **writes** and replay **reads**. Later phases must not invent a second schema, a second result envelope, or a second Playwright launch path.

Product thesis: **the model discovers; the artifact is the capability; deterministic replay is production.**

These contracts are laptop-only, local-mock-only, and **LLM-free on replay**. There is no RL. There are **no image screenshots** to any model, and none stored as evidence.

The mock screens these contracts assume are in [`MOCK_UI.md`](MOCK_UI.md).

---

## 1. Three related contracts

`docs/CONTRACT.md` bundles three things that must not be mixed:

| Name | Requirement | What it is | When used |
| --- | --- | --- | --- |
| **Capability artifact** | 3.2 | How to do the work: steps, locators, typed params/outputs, checkpoint | Saved under `capabilities/`; production path |
| **Replay result** | 3.3 | What happened this run: `success` / `business_outcome` / `failed` / `escalated` | Returned by replay (and discovery stop); **not** stored inside the capability |
| **Session / HITL flags** | 3.6 | Who controls the live browser (`automation` vs `human`) | Runtime only; not part of the reusable skill |

A calling bank agent invokes a capability the way it would call a tool, for example `lookup-savings-balance({ member_id: "12345" })`. It does not receive a chat transcript.

JSON Schema for the capability lives at [`schemas/capability.schema.json`](../schemas/capability.schema.json). The first concrete skill (`capabilities/lookup-savings-balance.json`) is **Phase 2**, not this phase.

---

## 2. Capability artifact (requirement 3.2)

A versioned JSON skill: **steps, locators, typed params/outputs, checkpoint** — not a raw transcript.

### 2.1 Required shape

| Field | Role |
| --- | --- |
| `apiVersion` | Schema family. Locked: `capability.interface.ai.local/v1` |
| `id` | Stable tool id, e.g. `lookup-savings-balance` |
| `name` / `description` | Human-reviewable |
| `version` | Artifact revision (`"1"`, `"2"`, …). Bump on locator or step change |
| `app` | `{ surface, entry, origin_allowlist }`. MVP surface is `"web"` |
| `input_schema` / `output_schema` | JSON Schema objects so a caller can treat this as a function |
| `risk` | `safe` \| `reversible` \| `irreversible`. Irreversible steps require HITL in MVP (Phase 3/5) |
| `checkpoint` | Assert a control is visible after the happy path (do not assume a click worked) |
| `known_outcomes` | Banner/text classifiers that are **typed results**, not crashes |
| `steps[]` | Ordered `{ id, action, target, input_binding?, output_binding?, wait?, on_error? }` |
| `tenant` | Optional stub `{ app_family, overlays: [] }` for the 3.7 write-up. Empty in MVP |

**Actions in MVP:** `fill`, `click`, `extract`. Do not add `download`, `file_upload`, or free-form JS.

**Target:** `{ frame, strategies[] }`. `frame` is required on the mock because the form lives in a child document named `content` (see [`MOCK_UI.md`](MOCK_UI.md)).

**Bindings:**

- `input_binding: { from: "input", key: "member_id" }` — bind this run’s param; never bake `12345` into the skill
- `output_binding: { key: "savings_balance" }` — write an extract into `outputs`

Optional per-step `wait` (timeout / selector ready) and `on_error` (`fail` \| `escalate` \| `classify_outcome`) may be added in Phase 2 without changing `apiVersion` if they remain optional.

### 2.2 Locator priority (locked)

Replay resolves `target.strategies` **in order** and stops at the first hit. A wrong click is worse than a stop: **do not self-heal** by inventing a new locator in production.

| Priority | `strategy` | Meaning | Allowed as primary? |
| --- | --- | --- | --- |
| 1 (required first) | `role` | Accessibility role + accessible name, e.g. `{ role: "textbox", name: "Member ID" }` | **Yes — this is the primary** |
| 2 | `nearby_text` | Visible adjacent text (layout-table label in the previous cell, no `<label for>`) | Fallback |
| 3 | `accessible_name` | Name only, when role is ambiguous | Fallback |
| last | `css` | `#ctl00_Main_txtCIF` or `table table tr:nth-child(…) input` | **Never primary.** Document as weak; omit unless a later phase proves role+name cannot bind |

CSS / generated WebForms ids / `data-testid` are **not** the seam to desktop. Role+name is the same control model as OS AX / UIA (requirement 3.7).

### 2.3 What must not be in the capability JSON

- The LLM chat transcript, chain-of-thought, raw AX dumps, or discovery timestamps
- The concrete member id from discovery (`12345`) — that is a **parameter**
- Secrets, cookies, passwords, full account numbers, SSN, or other PII
- Image screenshots or pixel coordinates
- CSS / `ctl00_…` as the **primary** locator

Compiler job (Phase 4): map LLM actions → this schema, replace run-specific values with `{member_id}`, **drop the transcript**.

### 2.4 Locked example — `lookup-savings-balance`

Hand-written in Phase 2; the Phase 4 compiler must emit this **same shape**.

```json
{
  "apiVersion": "capability.interface.ai.local/v1",
  "id": "lookup-savings-balance",
  "name": "Lookup member savings balance",
  "version": "1",
  "description": "Open Core Inquiry, look up a member by ID, return current savings balance.",
  "app": {
    "surface": "web",
    "entry": "/",
    "origin_allowlist": ["http://127.0.0.1"]
  },
  "input_schema": {
    "type": "object",
    "required": ["member_id"],
    "properties": {
      "member_id": { "type": "string", "pattern": "^[0-9]+$" }
    }
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "member_name": { "type": "string" },
      "savings_balance": { "type": "string" }
    }
  },
  "risk": "safe",
  "checkpoint": {
    "kind": "role_name_visible",
    "role": "heading",
    "name": "Member Profile"
  },
  "known_outcomes": [
    {
      "code": "member_not_found",
      "detect": { "kind": "text_visible", "text": "Record not found" }
    }
  ],
  "steps": [
    {
      "id": "s1",
      "action": "fill",
      "target": {
        "frame": "content",
        "strategies": [
          { "strategy": "role", "role": "textbox", "name": "Member ID" },
          { "strategy": "nearby_text", "text": "Member ID" }
        ]
      },
      "input_binding": { "from": "input", "key": "member_id" }
    },
    {
      "id": "s2",
      "action": "click",
      "target": {
        "frame": "content",
        "strategies": [
          { "strategy": "role", "role": "button", "name": "Inquiry" }
        ]
      }
    },
    {
      "id": "s3",
      "action": "extract",
      "target": {
        "frame": "content",
        "strategies": [
          { "strategy": "nearby_text", "text": "Savings balance" }
        ]
      },
      "output_binding": { "key": "savings_balance" }
    }
  ]
}
```

Why this shape:

- **Versioned** (`apiVersion` + `version`) so a human can review and a later tenant overlay can specialize without silently breaking callers.
- **Typed params/outputs** so an agent can call it like a function; `12345` is data, not part of the skill.
- **Ordered locators**, role+name first, because `ctl00_Main_txtCIF` will not survive a vendor patch.
- **Checkpoint** so replay does not assume the Inquiry click worked.
- **`known_outcomes`** so “Record not found” is a result, not an exception.
- **`frame: "content"`** because the mock (and Siebel-class apps) put the form in a child document.

MVP extract is `savings_balance`. `member_name` is allowed by `output_schema` and may be filled by an extra extract step later; it is not required for the first replay demo.

---

## 3. Replay result (requirement 3.3)

Replay loads the artifact, binds this run’s params, and executes steps **with no LLM decisions**. If the Inquiry button is missing, replay does not invent a plan.

### 3.1 Terminal statuses (locked)

Exactly four values of `status`. Do not add a fifth.

| `status` | Meaning | Typical `outcome_code` | Caller should treat as |
| --- | --- | --- | --- |
| `success` | Checkpoint met; outputs filled | `ok` | Done; use `outputs` |
| `business_outcome` | Expected domain result; automation worked | `member_not_found`, `validation_rejected`, `not_authorized` | Done; **not a crash** |
| `failed` | Hard failure: locator miss, unexpected dialog, missing frame, timeout after retries | `locator_miss`, `unexpected_dialog`, `session_expired`, `timeout` | Stop; inspect evidence |
| `escalated` | Control handed to a human on the **same** session | `hitl_required`, `irreversible_blocked` | Operator owns the browser until resume |

**Business outcome ≠ failure.** Member `99999` → `status: "business_outcome"`, `outcome_code: "member_not_found"`. Returning `failed` for that banner is the most common design mistake in this assignment.

**Recoverable** is not a terminal status. Known interstitials (slow load) wait/retry **inside** a step, then continue. If retries exhaust, the run becomes `failed` (or `escalated` if policy says so).

### 3.2 Result JSON (locked)

```json
{
  "status": "success|business_outcome|failed|escalated",
  "outcome_code": "ok|member_not_found|...",
  "outputs": {},
  "failed_step": null,
  "expected": "",
  "observed": "",
  "evidence_dir": "evidence/..."
}
```

| Field | Rules |
| --- | --- |
| `outputs` | Filled on `success` (and optionally on some business outcomes). Empty object otherwise |
| `failed_step` | Step id on `failed` / `escalated`; `null` on `success` and on classified `business_outcome` |
| `expected` / `observed` | Short human strings for checkpoints and classifiers (e.g. expected heading `Member Profile`, observed banner `Record not found`) |
| `evidence_dir` | Directory of logs + Playwright trace + AX/DOM snapshot. **No PNG/JPEG screenshots** |

Must demo in Phase 2: success replay **and** `member_not_found` as `business_outcome` (not `failed`).

### 3.3 Demo matrix (lookup capability)

| Invocation | Param | Expected `status` | `outcome_code` | `outputs` |
| --- | --- | --- | --- | --- |
| Run A | `member_id=12345` | `success` | `ok` | `savings_balance` = `1,240.55` (and optional `member_name`) |
| Run B | `member_id=99999` | `business_outcome` | `member_not_found` | `{}` |
| Locator miss / missing frame | any | `failed` | `locator_miss` (or similar) | `{}` |
| Stuck / irreversible / HITL | any | `escalated` | `hitl_required` | `{}` until resume |

---

## 4. Observation and evidence (no screenshots)

Locked for every later phase:

- **Observation** for the discovery LLM is **compact AX-tree text** (interactive nodes with stable refs) of the relevant frame. Never pixels, never a screenshot tool, never a vision computer-use API.
- **Evidence** on failure (and for `/evidence/` runs) is a Playwright trace plus an AX snapshot and/or DOM snapshot. Optional: structured logs. A screen recording is skipped.
- **Do not** write screenshots into `/evidence/`, into `intervention_request.json`, or into capability files.
- Replay never calls an LLM.

This is cheaper, avoids PII-in-pixels, and is the same control model that extends off the browser (requirement 3.7).

---

## 5. HITL session flags (requirement 3.6)

Handoff is the **same live browser session**, not “open a new window.” Operator UI may be a tiny local page or CLI (Phase 5). Do not build a co-browsing console.

### 5.1 Controller flag (locked)

Session runtime state includes:

```json
{
  "controller": "automation | human",
  "session_id": "…",
  "paused": false
}
```

| `controller` | Who may drive Playwright actions |
| --- | --- |
| `automation` | Replay or discovery agent |
| `human` | Operator in the headed browser; automation must not click/type until resume |

Required flip on a real handoff: `automation` → `human` → `automation` (or mark complete while still `human` if the operator finished the goal).

### 5.2 Intervention request (written on escalate; not part of the capability)

Minimum fields (Phase 5):

```json
{
  "session_id": "…",
  "goal": "look up member 12345 and read their current savings balance",
  "failed_step": "s2",
  "why": "Inquiry button not found in content frame",
  "ax_snapshot_path": "evidence/…/ax.json",
  "dom_snapshot_path": "evidence/…/dom.html",
  "controller": "human"
}
```

No screenshot field.

While paused: keep the browser open; do not create a second Playwright session.

---

## 6. Safety notes (implemented in Phase 3, specified now)

- Origin allowlist: local mock only (`http://127.0.0.1` / `http://localhost`), path prefixes as needed.
- Allowed actions: `click`, `fill`, `extract` (and waits). Not: `download`, `file_upload`, arbitrary navigation off-allowlist.
- `risk: irreversible` (open sub-account confirm) → block or `require_human` in MVP.
- Redact digit runs and never persist passwords, cookies, or raw PII in artifacts or logs. Skipping screenshots is part of this policy.

---

## 7. Still stubbed after Phase 0

| Later phase | What this contract is waiting on |
| --- | --- |
| Phase 1 | Local mock pages that actually show these banners and frames |
| Phase 2 | Hand-written `capabilities/lookup-savings-balance.json` + replay CLI that returns this result JSON |
| Phase 3 | `policies/allowlist.yaml` and redaction middleware |
| Phase 4 | LLM observe→act loop that compiles to **this same** artifact shape |
| Phase 5 | Pause/resume that flips `controller` |
| Phase 6 | `/evidence/` runs, `README.md`, `REPORT.md` |

No mock app, Playwright, or LLM loop exists yet. That is intentional.
