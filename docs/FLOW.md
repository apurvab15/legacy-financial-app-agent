# Code-file logic flow

The model discovers; the artifact is the capability; deterministic replay is production.
Replay never calls an LLM. Observation is AX-tree **text**, never pixels.

```mermaid
flowchart LR
  CLI["src/cli.py"] --> DISCOVER["src/discover.py"]
  CLI --> REPLAY["src/replay.py"]
  DISCOVER --> ARTIFACT["capabilities/*.json"]
  ARTIFACT --> REPLAY
  REPLAY --> RESULT["src/result.py"]
```

---

## 1. Files at a glance

```mermaid
flowchart TB
  subgraph entry ["Entry"]
    MAIN["src/__main__.py"]
    CLI["src/cli.py"]
  end

  subgraph paths ["Two paths"]
    DISCOVER["src/discover.py<br/>LLM observe-decide-act"]
    COMPILER["src/compiler.py<br/>trace → capability JSON"]
    LLM["src/llm.py<br/>Gemini tools, text only"]
    OBSERVE["src/observe.py<br/>AX-tree text + refs"]
    REPLAY["src/replay.py<br/>no LLM"]
  end

  subgraph runtime ["Shared runtime"]
    SESSION["src/session.py<br/>one Playwright session"]
    SURFACE["src/surface.py<br/>locators + controller gate"]
    GUARD["src/guardrails.py<br/>allowlist / irreversible"]
    CLASSIFY["src/classify.py<br/>checkpoint vs banner"]
    HITL["src/hitl.py<br/>operator + handoff files"]
    EVIDENCE["src/evidence.py<br/>AX/DOM/trace, no PNG"]
    REDACT["src/redact.py"]
    RESULT["src/result.py"]
    MOCK["src/mock_server.py"]
  end

  subgraph data ["Contracts and data"]
    SCHEMA["schemas/capability.schema.json"]
    CAP["capabilities/*.json"]
    POLICY["policies/allowlist*.yaml"]
    APP["apps/legacy_core/app.py"]
    EVDIR["evidence/"]
  end

  MAIN --> CLI
  CLI -->|"discover --goal"| DISCOVER
  CLI -->|"default / --capability"| REPLAY
  DISCOVER --> LLM
  DISCOVER --> OBSERVE
  DISCOVER --> COMPILER
  COMPILER --> CAP
  CAP --> REPLAY
  SCHEMA -. validates .-> CAP
  CLI --> POLICY
  POLICY --> GUARD
  DISCOVER --> SESSION
  REPLAY --> SESSION
  SESSION --> APP
  MOCK --> APP
  SESSION --> SURFACE
  REPLAY --> CLASSIFY
  REPLAY --> HITL
  DISCOVER --> HITL
  REPLAY --> EVIDENCE
  DISCOVER --> EVIDENCE
  EVIDENCE --> EVDIR
  EVIDENCE --> RESULT
  REDACT -.->|"filters logs + evidence"| EVIDENCE
```

| File | Job |
| --- | --- |
| `src/__main__.py` | `python -m src` → `cli.main` |
| `src/cli.py` | Parse args; route discover vs replay vs `--demo-action` |
| `src/discover.py` | LLM loop: observe → tool call → act → compile |
| `src/llm.py` | Gemini chat/completions; tools `fill/click/extract/done/escalate` |
| `src/observe.py` | Compact AX text of iframe `content`; stable refs for one observation |
| `src/compiler.py` | Recorded tool calls → capability JSON; drop transcript; parameterize `member_id` |
| `src/replay.py` | Bind params, run steps, classify, HITL resume; **no LLM** |
| `src/session.py` | One Chromium context; tracing; `controller` flag; network guard |
| `src/surface.py` | Ordered locators (role → nearby_text → accessible_name → css); fill/click/extract |
| `src/guardrails.py` | Origin/path/action allowlist; irreversible block vs `require_human` |
| `src/classify.py` | Known banner (`business_outcome`) vs checkpoint (`success`) |
| `src/hitl.py` | Operator (none/cli/auto); intervention request; session-expired detect |
| `src/evidence.py` | `result.json` + AX + DOM + Playwright trace |
| `src/result.py` | Envelope: `success` / `business_outcome` / `failed` / `escalated` |
| `src/redact.py` | Mask digit runs, cookies, passwords in logs and evidence |
| `src/mock_server.py` | Start Flask mock on `127.0.0.1:8000` if nothing is listening |
| `apps/legacy_core/app.py` | Frameset mock: lookup, not-found, expired, confirm open |

---

## 2. CLI routing (`src/cli.py`)

```mermaid
flowchart TD
  START["python -m src"] --> PARSE["cli.main<br/>RedactFilter on logging"]
  PARSE --> POLICY["guardrails.load_policy<br/>policies/allowlist.yaml"]
  POLICY --> Q1{discover subcommand<br/>or --goal?}

  Q1 -->|yes| NEED["require --goal and --member-id"]
  NEED --> DISC["discover.run_discovery"]
  DISC --> PRINT["print ReplayResult JSON"]

  Q1 -->|no| Q2{"--demo-action set?"}
  Q2 -->|yes| DEMO["guardrails.assert_action_allowed<br/>write evidence/replay-action_not_allowlisted"]
  DEMO --> PRINT

  Q2 -->|no| LOAD["replay.load_capability"]
  LOAD --> MOCK{"host is 127.0.0.1 / localhost?"}
  MOCK -->|yes| STARTMOCK["mock_server.ensure_mock"]
  MOCK -->|no| RUN
  STARTMOCK --> RUN["replay.run_replay"]
  RUN --> PRINT
```

Exit code `0` for `success` / `business_outcome`, `2` otherwise.

---

## 3. Discovery path (LLM once)

Thesis: the model drives the mock **once**. The compiler writes a reusable skill. Production never sees the transcript.

```mermaid
flowchart TD
  D["discover.run_discovery"] --> KEY["llm.load_llm_config<br/>needs GEMINI_API_KEY"]
  KEY -->|missing| FAILKEY["failed / llm_error"]
  KEY -->|ok| POL["guardrails.load_policy"]
  POL --> MOCK["mock_server.ensure_mock"]
  MOCK --> SESS["session.WebSession.start<br/>goto /"]
  SESS --> OBS["observe.observe_ax_text<br/>refs e1, e2, …"]
  OBS --> LOOP

  subgraph loop ["Observe → decide → act, max 20 steps"]
    LOOP["llm.complete_turn<br/>system prompt + AX text"]
    LOOP --> TOOLS{"tool calls?"}
    TOOLS -->|none| NUDGE["ask model to call a tool"]
    NUDGE --> LOOP
    TOOLS -->|fill / click / extract| APPLY["_apply_tool"]
    APPLY --> GATE["guardrails.before_act"]
    GATE --> ACT["surface.fill / click / extract<br/>target_from_node: role first, never CSS"]
    ACT -->|ok| RECORD["append to recorded[]"]
    ACT -->|LocatorMiss| RETRY["return error to model; keep looping"]
    RECORD --> REOBS["observe again; new refs"]
    REOBS --> LOOP
    RETRY --> REOBS
    TOOLS -->|done| COMP
    TOOLS -->|escalate| HAND["hitl handoff; discovery does not auto-resume"]
  end

  HAND --> ESC["escalated / hitl_required"]
  LOOP -->|max steps| MAX["failed / max_steps"]

  COMP["compiler.compile_capability"]
  COMP --> LEAK{"artifact contains this run's member_id?"}
  LEAK -->|yes| LEAKFAIL["failed / compiler_leaked_param"]
  LEAK -->|no| WRITE["write capabilities/lookup-savings-balance.discovered.json<br/>+ evidence/discovery/"]
  WRITE --> OK["success / ok"]
```

What the compiler **drops**: chat transcript, chain-of-thought, raw AX dumps, the concrete member id.

What it **keeps**: ordered `fill` / `click` / `extract` steps, role+name locators, `input_binding` for `member_id`, checkpoint, `known_outcomes`.

---

## 4. Replay path (no LLM)

```mermaid
flowchart TD
  R["replay.run_replay"] --> NAV["guardrails.assert_navigation_allowed<br/>+ capability origin_allowlist"]
  NAV -->|deny| FAILPOL["failed / origin_not_allowlisted<br/>or path_not_allowlisted"]
  NAV -->|ok| PRE["preflight before_act on every step"]
  PRE -->|PolicyDenied| FAILPRE["failed before browser launch"]
  PRE -->|HumanRequired deferred| BROWSER

  BROWSER["session.WebSession.start<br/>goto entry"]
  BROWSER --> STEPS["_attempt_steps"]

  subgraph steps ["For each capability step"]
    STEPS --> BEFORE["guardrails.before_act"]
    BEFORE -->|HumanRequired| BLOCK["_Blocker irreversible_blocked<br/>automation_may_retry=false"]
    BEFORE -->|ok| ACT{"action"}
    ACT -->|fill| FILL["surface.fill + bind input_schema param"]
    ACT -->|click| CLICK["surface.click<br/>wait_for_outcome_or_checkpoint"]
    ACT -->|extract| EXT{"classify.classify_known_outcome?"}
    EXT -->|yes| BIZ
    EXT -->|no| READ["surface.extract → outputs[key]"]
    FILL --> AFTER
    CLICK --> AFTER
    READ --> AFTER
    AFTER["classify_known_outcome again"]
    AFTER -->|banner| BIZ["business_outcome<br/>e.g. member_not_found"]
    AFTER -->|none| NEXT[next step]
  end

  NEXT --> STEPS
  STEPS -->|all steps done| CK{"classify.checkpoint_met?"}
  CK -->|yes| WIN["success / ok"]
  CK -->|no| MISS["failed / checkpoint_miss"]

  STEPS -->|LocatorMiss / timeout| CLASSB["hitl.classify_blocker<br/>session_expired vs locator_miss"]
  CLASSB --> BLOCK2["_Blocker"]
  BLOCK --> HITL
  BLOCK2 --> HITL{"escalate?"}
```

Replay **does not invent locators**. First strategy hit wins; CSS cannot be primary (`CssPrimaryForbidden`).

---

## 5. HITL on the same session (`src/hitl.py` + `src/session.py`)

Handoff is a flag flip on the **same** Playwright page. No second window.

```mermaid
flowchart TD
  B["_Blocker"] --> MUST{"outcome_code == irreversible_blocked<br/>OR recoverable + operator attached?"}
  MUST -->|no| FAIL["failed<br/>plain replay stays failed"]
  MUST -->|yes, fresh| TOHUMAN["session.hand_to_human<br/>controller = human, paused = true"]

  TOHUMAN --> SNAP["evidence snapshots<br/>ax_before_handoff.json"]
  SNAP --> REQ["hitl.write_intervention_request"]
  REQ --> OP["operator.review<br/>none → abort<br/>cli → stdin resume/abort<br/>auto → scripted resume"]

  OP -->|abort| ESC["escalated"]
  OP -->|resume| TOAUTO["session.hand_to_automation"]
  TOAUTO --> AFTER["ax_after_handoff.json"]
  AFTER --> REV["replay._reverify — never resume on trust"]

  REV --> V{"verdict"}
  V -->|known_outcome_visible| BIZ["business_outcome"]
  V -->|checkpoint_met| WIN["success — human finished the goal"]
  V -->|retry_step| RETRY["_attempt_steps from the failed step"]
  V -->|human_owns_step<br/>still_session_expired<br/>step_target_still_missing| BACK["hand_to_human again → escalated"]
```

`surface.fill` / `click` call `assert_controller_allows`. While `controller=human`, automation raises `ControllerLocked`.

Irreversible steps (`policies/allowlist-hitl.yaml`, `irreversible.mode: require_human`) never retry after resume: `_reverify` returns `human_owns_step`.

---

## 6. Locator resolution (`src/surface.py`)

Same adapter for discovery and replay. Desktop would swap this module, not the capability JSON.

```mermaid
flowchart TD
  T["target: frame + strategies[]"] --> FRAME["frame_locator iframe name=content"]
  FRAME --> S0{"strategies[0] is css?"}
  S0 -->|yes| FORBID["CssPrimaryForbidden"]
  S0 -->|no| TRY["try each strategy in order"]
  TRY --> KIND{"strategy"}
  KIND -->|role| ROLE["get_by_role name exact"]
  KIND -->|nearby_text| NEAR["label cell → following sibling"]
  KIND -->|accessible_name| AN["get_by_label"]
  KIND -->|css| CSS["locator — never primary"]
  ROLE --> HIT{"visible?"}
  NEAR --> HIT
  AN --> HIT
  CSS --> HIT
  HIT -->|yes, first win| USE["fill / click / extract"]
  HIT -->|no| NEXT[next strategy]
  NEXT --> TRY
  TRY -->|all miss| MISS["LocatorMiss"]
```

---

## 7. Guardrails before every act (`src/guardrails.py`)

```mermaid
flowchart TD
  A["before_act"] --> ACT["assert_action_allowed<br/>click / fill / extract only"]
  ACT -->|denied| D1["PolicyDenied action_not_allowlisted"]
  ACT -->|ok| IRR{"is_irreversible_step?<br/>capability.risk or Confirm open"}
  IRR -->|no| URL
  IRR -->|yes, mode=block| D2["PolicyDenied irreversible_blocked"]
  IRR -->|yes, mode=require_human| HR{"preflight?"}
  HR -->|yes| SKIP["defer — need a live session"]
  HR -->|no| D3["HumanRequired → escalated"]
  SKIP --> URL["assert_navigation_allowed on current URLs"]
  URL -->|off origin| D4["origin_not_allowlisted"]
  URL -->|off path| D5["path_not_allowlisted"]
  URL -->|ok| GO[act]
```

`session.WebSession` also aborts off-allowlist network requests at the Playwright route layer.

---

## 8. Mock app (`apps/legacy_core/app.py`)

What replay and discovery actually drive:

```mermaid
flowchart TD
  SHELL["GET / → shell.html<br/>iframe name=content"] --> INQ["GET /inquiry → lookup.html"]
  INQ -->|expired=1| EXP["expired.html<br/>Login expired + Restore session"]
  INQ -->|POST Inquiry| LOOK{"member_id"}
  LOOK -->|12345| PROF["profile.html<br/>heading Member Profile<br/>Savings balance 1,240.55"]
  LOOK -->|99999| NF["not_found.html<br/>Record not found"]
  LOOK -->|empty| VAL[validation error on lookup]
  PROF --> OPEN["open-sub-account.json<br/>Confirm open → irreversible"]
```

---

## 9. Result envelope (`src/result.py`)

Every path ends in one of four statuses. Evidence is written by `src/evidence.py` (trace + AX + DOM, **no screenshots**), with PII masked by `src/redact.py`.

| `status` | Typical `outcome_code` | Who decided it |
| --- | --- | --- |
| `success` | `ok` | `classify.checkpoint_met` after all steps |
| `business_outcome` | `member_not_found` | `classify.classify_known_outcome` — automation worked |
| `failed` | `locator_miss`, `action_not_allowlisted`, `origin_not_allowlisted`, `irreversible_blocked` (block mode), `checkpoint_miss` | Guardrail or locator; no operator |
| `escalated` | `hitl_required`, `session_expired`, `irreversible_blocked` (require_human) | `hitl` handoff on the same session |
