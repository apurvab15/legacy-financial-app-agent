# Evidence

One directory per curated run. Every directory holds a single coherent run, so
`result.json` always matches the snapshots and handoff records beside it.

Per [`../docs/CONTRACT.md`](../docs/CONTRACT.md) §4 there are **no PNG/JPEG
screenshots anywhere** — evidence is a Playwright trace plus AX and DOM
snapshots. Redaction is applied on write, so member ids appear masked
(`****2345`) and no passwords or cookies are persisted.

| Directory | What it demonstrates |
| --- | --- |
| `discovery/` | Phase 4 LLM discovery against the mock: AX-tree **text** observation (`ax_text.txt`), the tool calls the model made (`tool_log.json`, transcript dropped), and the compiled capability (`capability.json`) — `success` / `ok` with `savings_balance` extracted. |
| `replay-success/` | Phase 2 happy path, member `12345`: `success` / `ok`, checkpoint heading `Member Profile` visible, `savings_balance = 1,240.55`. No LLM, no API key. |
| `replay-not-found/` | Member `99999`: `business_outcome` / `member_not_found`. The "Record not found" banner is a typed result, **not** a crash — the single most important classification in this build. |
| `replay-session_expired/` | Phase 5 HITL on the `?expired=1` login-expired screen: `escalated` / `session_expired`. Here the operator resumed *without* clearing the banner, so re-verification refused to continue and handed the session back. `handoff.json` shows all three controller flips (`human` → `automation` → `human`) and `reverify: still_session_expired`. |
| `replay-irreversible_blocked/` | Phase 5 with `irreversible.mode: require_human`: `escalated` / `irreversible_blocked` on step `s1`. Automation never clicks `Confirm open`, and on resume `reverify: human_owns_step` proves it will not retry an irreversible step either. |
| `replay-action_not_allowlisted/` | Guardrail denial: `download` is not in `allowed_actions`, so it is refused before any browser work. |
| `replay-origin_not_allowlisted/` | Guardrail denial: `--base-url http://example.com` is off the origin allowlist. |
| `replay-path_not_allowlisted/` | Guardrail denial: entry `/admin` is outside the allowed path prefixes. |

## Files you will see

| File | Contents |
| --- | --- |
| `result.json` | The locked replay result envelope: `status`, `outcome_code`, `outputs`, `failed_step`, `expected`, `observed`, `evidence_dir`. |
| `trace.zip` | Playwright trace (`screenshots=False`). Open with `playwright show-trace <path>`. |
| `ax.json` / `dom.html` | Accessibility and DOM snapshot of frame `content` at the end of the run. |
| `intervention_request.json` | HITL only. CONTRACT.md §5.2 fields; note the deliberate absence of any screenshot field. |
| `handoff.json` | HITL only. Operator name, decision, note, re-verify verdict, every controller flip, final controller. |
| `ax_before_handoff.json` / `ax_after_handoff.json` | HITL only. AX state either side of the human's work. |

## Notes

`replay-irreversible_blocked/` shows the **`require_human`** outcome
(`escalated`). With the default `policies/allowlist.yaml` (`mode: block`) the
same capability returns `failed` for the same `outcome_code`; both modes are
covered in `tests/test_guardrails.py`. The two cannot coexist as directories
because the evidence path is derived from `outcome_code`.

A successful hand-back (operator actually clears the banner, replay retries the
failed step and completes) writes its `result.json` to `replay-success/`, since
the directory follows the final status. `scripts/verify_phase5.py` exercises that
path end to end with a scripted operator.
