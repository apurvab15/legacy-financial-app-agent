# Human-in-the-loop handoff (Phase 5)

Implements [`CONTRACT.md`](CONTRACT.md) §5. The handoff is a flag flip on the
**same live browser session** — same Playwright process, same context, same page.
There is no second Playwright path and no co-browsing console.

## What triggers a handoff

| Trigger | Status / `outcome_code` | Needs an operator attached? |
| --- | --- | --- |
| Discovery model calls `escalate` | `escalated` / `hitl_required` | No |
| Replay hits `session_expired` or `locator_miss` | `escalated` / that code | **Yes** |
| Irreversible step with `irreversible.mode: require_human` | `escalated` / `irreversible_blocked` | No |

Human-recoverable *failures* only escalate when an operator surface is attached.
Without one, a locator miss stays `failed`, which keeps the §3.3 demo matrix and
Phases 2–4 behaving exactly as before. Policy-driven escalation is different: an
irreversible step under `require_human` is always `escalated`, because the run
genuinely belongs to a human whether or not anyone is listening.

`session_expired` is detected by the `Login expired` banner in frame `content`.
Without that check the mock's expired screen would surface as a bare
`locator_miss`, since the Member ID textbox is simply absent.

## The operator surface is deliberately mocked

`--operator` chooses it:

- `none` (default) — nobody is attached. Escalate, record, stop. The browser
  closes when the process exits, so use this only to observe the classification.
- `cli` — prints the intervention request to stderr and blocks on stdin until you
  type `resume` or `abort`, then asks for a free-text note. The browser stays
  open at the page where automation stopped, which is what makes the demo work.
- `auto` — a scripted operator that resumes immediately, for non-interactive runs
  and tests. `--operator-note` sets the recorded note.

A real deployment would replace this with a queue and an operator UI. The
contract only fixes the intervention request and the controller flip, so that
substitution does not touch replay.

## Enforcement

`WebSession` registers a controller gate on the page, and `src/surface.py`
checks it inside `fill` and `click`. Any caller that tries to type or click
while `controller == "human"` raises `ControllerLocked` — the check sits on the
act itself, not on the politeness of the caller. Reads (`extract`,
`detect_visible`) stay open, because re-verification has to observe the page.

A human driving the headed browser is not affected: they use the mouse, not the
Playwright API.

## Hand-back re-verifies; it never resumes on trust

On `resume`, replay flips to `automation` and then checks the page before
continuing. Order matters:

1. A `known_outcomes` banner is visible → terminal `business_outcome`.
2. The `checkpoint` is visible → `success`; the operator finished the goal by hand.
3. The step was irreversible → automation never retries it. Terminal only.
4. The original blocker is gone and the failed step's locator resolves →
   **retry that step**, not the next one.
5. Otherwise → back to `human`, terminal `escalated`, with the re-verification
   verdict recorded in `observed`.

## Evidence

Written next to the run, with no PNG/JPEG anywhere and no `screenshot` field:

| File | Contents |
| --- | --- |
| `intervention_request.json` | §5.2 fields: `session_id`, `goal`, `failed_step`, `why`, `ax_snapshot_path`, `dom_snapshot_path`, `controller` |
| `handoff.json` | Operator name, decision, note, re-verify verdict, every controller flip, final controller |
| `ax_before_handoff.json` / `ax_after_handoff.json` | AX snapshot either side of the human's work |
| `trace.zip` | The existing Playwright trace, which also captures what the human did |

Redaction still applies, so the member id in `goal` is masked (`member_id=****2345`).

One wrinkle worth knowing: handoff artifacts are written when the escalation
happens, into the escalated directory (e.g. `evidence/replay-session_expired/`).
If the operator then resumes and the run succeeds, `result.json` lands in
`evidence/replay-success/`. The log line `intervention request -> <dir>` tells
you where the handoff record is.
