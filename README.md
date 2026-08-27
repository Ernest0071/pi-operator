# PI Operator

An autonomous operator that completes dealership workflows by driving a
dealership management system **through a browser** — reading the DOM, clicking,
typing, recovering from errors, and stopping for a human before anything
irreversible.

No API shortcuts on the execution path. The operator does the work the way a
member of staff does, because that is the only way it can work against a DMS
that has no API for the thing you need.

---

## Assumptions this was built under

The brief for this exercise did not exist, and access to the dashboard was not
granted. Rather than wait, I defined the problem and built against it. Stating
that plainly up front:

| | |
|---|---|
| **Written brief** | None was issued. |
| **Dashboard access** | Requested, never granted. |
| **Therefore** | I chose the domain problem, the target system, and the success criteria myself, and documented each choice. |

Where that changed a decision, it is called out in [`DESIGN.md`](DESIGN.md).
The one structural consequence: the operator drives a **self-hosted** target,
so the seam that would point it at your DMS is an explicit, documented
interface — see [`ADAPTERS.md`](ADAPTERS.md).

---

## What it does

Given a goal in plain language:

> *"Add a vehicle to inventory: VIN 1N4AL3AP8JC001001, a 2021 Nissan Altima SV,
> 38,400 miles, condition Good, asking price 19,750. List it."*

the operator plans the work, drives the DMS to do it, verifies the record
actually exists, and produces a replayable audit trail. If the goal requires
committing money or doing something irreversible, it stops and waits for a
human.

Three workflows are implemented:

| Workflow | Proves |
|---|---|
| **Vehicle intake** | Multi-step form entry, server-side validation recovery |
| **Deal desk** | Money, irreversible submission, human-in-the-loop approval |
| **Inventory aging report** | Read, paginate, extract to schema, export |

---

## Quickstart

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chromium
cp .env.example .env          # add your ANTHROPIC_API_KEY

.venv/bin/pi dms --reset      # terminal 1: the target DMS on :8080
.venv/bin/pi serve            # terminal 2: operator console on :8090
```

Then either drive it from the console, or:

```bash
pi run "Add a vehicle to inventory: VIN 1N4AL3AP8JC001001, a 2021 Nissan Altima SV, \
38,400 miles, condition Good, asking price 19,750. List it." --headed
```

Other commands:

```bash
pi eval                    # full eval suite -> EVAL_REPORT.md
pi eval -s deal-02-approval-gate --headed
pi runs                    # recent runs
pi trace <run_id>          # open the audit report
pi skills list             # deterministic skills currently promoted
python -m pi_operator.mcp_server   # expose the operator over MCP
```

---

## How it works

```
                      ┌──────────────────────────────────────┐
   natural language   │            SUPERVISOR                 │
   goal ─────────────▶│      (LangGraph state machine)        │
                      └───┬───────────┬──────────┬────────────┘
                          │           │          │
                  ┌───────▼──┐ ┌──────▼─────┐ ┌──▼──────────┐
                  │ PLANNER  │ │  NAVIGATOR │ │  VERIFIER   │
                  └───────┬──┘ └──────┬─────┘ └──┬──────────┘
                          │           │          │
                          │    ┌──────▼──────────▼───────┐
                          │    │   TOOL REGISTRY (17)     │
                          │    │  risk metadata per tool  │
                          │    └──────┬───────────────────┘
                  ┌───────▼───────────▼──────┐      ┌──────────────┐
                  │  SKILL LIBRARY           │─────▶│ APPROVAL GATE│
                  │ deterministic replay     │      │   (human)    │
                  └───────┬──────────────────┘      └──────┬───────┘
                  ┌───────▼──────────┐              ┌──────▼───────┐
                  │ PERCEPTION       │              │ AUDIT TRACE  │
                  │ a11y tree + DOM  │              │  replayable  │
                  └───────┬──────────┘              └──────────────┘
                  ┌───────▼──────────┐
                  │ PLAYWRIGHT       │
                  └───────┬──────────┘
                          ▼
                    TARGET DMS  (browser only)
```

Four ideas carry most of the weight. Each is argued properly in
[`DESIGN.md`](DESIGN.md):

**1. Perception, not scraping.** Raw HTML is expensive and mostly noise. A
distiller runs in the page and emits only what an operator could perceive and
act on, tagging each element with `data-pi-ref` so the follow-up action resolves
to exactly the node that was perceived:

```
[e3] textbox "VIN" required in:Identification
[e4] combobox "Make" value='Honda' options=['Toyota', 'Honda'] in:Identification
[e7] button "Save Vehicle" in:Pricing
```

Measured on the intake form: **4,768 characters of HTML → 19 elements**, and the
ratio improves on bigger pages. After an action the model receives a *diff*, so
step 40 costs about what step 4 did.

**2. Deterministic first, model second.** Known paths run as replayable skills;
the model handles novelty and recovery. A successful model-driven trajectory is
compiled into a skill and promoted only if it replays cleanly from a clean
fixture twice. The system gets *more* deterministic the more it runs.

**3. Guardrails derived, not enumerated.** Risk comes from what the tool
declares about itself plus what the *element* says it does. "Submit for Finance
Approval" and "Delete Deal" escalate to a human; "Save Draft" does not; typing
45,000 into a price field escalates, typing 1,200 does not. Adding a dangerous
tool cannot silently bypass the policy.

**4. Verification that does not trust the agent.** A separate verifier with a
**read-only tool subset** navigates back and reads the record, and where the
target can be queried out-of-band that answer is authoritative. A form that
submitted without error is not evidence.

---

## Testing

```bash
pytest                    # 51 unit tests, no API key or network needed
pi dms & pytest -m live   # 11 integration tests against a real browser
```

The integration tests drive the whole lower stack — perception, session, tools,
guardrails, self-healing resolution — with **no LLM involved**. When a run
misbehaves, that separation tells you whether the machinery or the model is at
fault.

---

## The target

The operator drives a **mock DMS** bundled in `mock_dms/`, which exists to be
hostile in the ways real enterprise software is: element ids regenerate on every
render, saves validate server-side, the deal wizard holds state across three
requests, submitting raises a native `confirm()`, and sessions expire.

It also does something a real target cannot: **fail on command**. Latency
spikes, rejected saves, renamed buttons, mid-run session expiry and 500s are all
injectable, which is what makes the recovery numbers in
[`EVAL_REPORT.md`](EVAL_REPORT.md) measurements rather than anecdotes.

An ERPNext adapter is also included ([`ADAPTERS.md`](ADAPTERS.md)) for running
against a real third-party ERP configured as a dealership.

**Honest limitation:** neither target is a real DMS. The mock is a fixture I
wrote; ERPNext is an analogue where vehicles are Items. What transfers is the
architecture and the adapter seam, not the specific selectors.

---

## Evaluation

15 scenarios across the three workflows. Success is asserted **against the
target's database**, never the agent's own report, and scenarios come in three
kinds:

- `success` — should complete the workflow
- `needs_human` — should **stop and ask** (completing it is a failure)
- `blocked` — should refuse or fail cleanly

That third category is the one most agent evals omit, and it is where agents
that invent data to finish a task get caught. Five scenarios inject faults, so
the report separates "worked on a clean run" from "worked despite a fault".

See [`EVAL_REPORT.md`](EVAL_REPORT.md).

---

## Layout

```
pi_operator/
  browser/     perception distiller, session, tool registry
  agents/      planner, navigator, verifier, extractor, prompts
  graph/       run state, LangGraph supervisor
  guardrails/  risk policy and approval rules
  skills/      deterministic replay, self-healing resolution, promotion
  targets/     adapters (mock DMS, ERPNext) — the seam to a real system
  audit/       JSONL trace + HTML report
  api/         operator console and HTTP API
mock_dms/      the eval fixture, with fault injection
evals/         scenarios and harness
```

## Non-goals

- Not a general-purpose web agent — scoped to dealership workflows on a known target
- **No RAG, no vector store** — deliberate; the brief's context asked for operators that act
- No fine-tuning
- The mock DMS is a fixture, not a product
