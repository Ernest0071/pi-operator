# Design notes

This document argues the decisions. The README says what the system does; this
says why it is built this way and where it breaks.

---

## 0. The brief did not exist

No instructions were issued and dashboard access was never granted. That is not
a complaint — it is the actual starting condition, and it decided the shape of
the work.

The choice was between waiting and defining the problem. I defined it: pick a
real dealership domain, pick a target the operator does not control, set success
criteria that can be checked mechanically, and make the seam to a different
system explicit so the work transfers.

Everything below follows from that.

---

## 1. Where deterministic software beats AI reasoning

This is the question the role context calls out directly, so it gets the longest
answer.

**The observation.** Almost none of a dealership workflow is ambiguous. "Open
the new-vehicle form, put the VIN in the VIN field, set condition, save" does not
require reasoning. It required reasoning *once*. Paying a model to rediscover it
on every run costs money, costs seconds, and — the part that actually matters —
costs determinism: the same goal can take a different path each time, so
failures are not reproducible.

**The split.**

| Layer | Mechanism | Why |
|---|---|---|
| Known, stable paths | Recorded skills (parameterised Playwright routines) | Free, fast, deterministic, replayable |
| Novel pages, recovery | LLM navigator over the tool registry | Only where reasoning is genuinely needed |
| Structured data out | LLM extractor against a caller-supplied schema | Schema-constrained and validated |
| "Did it work?" | Deterministic assertion first, model read-back second | Never trust the actor's own report |
| Control flow between them | LangGraph conditional edges | Explicit and inspectable, not prompt-enforced |

**Promotion.** When the navigator completes a workflow that had no skill, that
trajectory is evidence a deterministic path exists. It is compiled into a
recorded skill — literals generalised to `{{parameters}}`, refs stripped,
elements stored as role + accessible name + context — and promoted **only if it
replays cleanly from a fresh fixture twice**. A path that worked once may have
depended on incidental state; a path that replays twice from a clean start is a
routine.

The consequence is the interesting part: **the system becomes more
deterministic the more it runs**, which is the opposite of how prompt-only
agents age. `determinism_rate` in the eval report is the share of actions served
without a model call, and it should rise over a project's life.

**Where the line actually sits.** Use deterministic code when the path is known
*and* stable. Use the model when the page is novel, when something failed, or
when a judgement is required (which of these three customers is "Marcus"?).
The mistake in both directions is common: scripting the judgement makes a
brittle macro, and modelling the routine makes an expensive, non-reproducible one.

---

## 2. Perception: why not just send the HTML

Sending raw HTML fails on three axes at once.

*Cost.* The intake form is 4,768 characters of markup and 19 meaningful
elements. Real ERP pages are 200KB+. At 60 steps a run, prefix cost dominates
everything else.

*Accuracy.* Markup contains many things that look actionable and are not —
hidden elements, decorative divs with click handlers, offscreen menu trees. A
model reading raw HTML picks them.

*Addressing.* Having reasoned about an element, the model has to say which one it
means. CSS selectors are the obvious answer and the wrong one: generated ids
churn, and a selector written from a snapshot may match a different node by the
time it executes.

**What is built instead.** A distiller runs in the page, walks the DOM once, and
emits role, accessible name (resolved through `aria-labelledby` → `aria-label` →
`<label for>` → placeholder → text, roughly following accname), current value,
state, available options for selects, and a context anchor. It then writes
`data-pi-ref` onto each element it returned.

That tag is the contract between perception and action, and it buys a property
that matters more than it sounds: **if the page re-rendered between perceiving
and acting, the ref is gone and the action fails loudly** instead of clicking
whatever now occupies that position. Stale-ref mis-clicks are one of the most
common silent failure modes in browser agents; here they are a caught exception
with a message telling the model to re-observe.

**Context anchors.** `in:Identification` versus `in:Pricing` is what lets an
operator tell five "Save" buttons apart. Sourced from the nearest *preceding*
heading, legend or landmark — an early version took the first heading in the
ancestor subtree and confidently mislabelled a button that sat after two
fieldsets.

**Diffs.** After an action the model gets what changed, not the whole page.
`NO VISIBLE CHANGE` is an explicit output because "I clicked and nothing
happened" is otherwise indistinguishable from success.

**Native validation.** HTML5 `required` blocks submission client-side with no
server round-trip and no visible alert. To an agent this looks exactly like a
no-op, and it is the most confusing failure on the list. The distiller reads
`checkValidity()` / `validationMessage` and promotes it to a page-level alert.
This was found by an integration test, not by reasoning.

---

## 3. Guardrails: derived, not enumerated

The naive design is a list: "clicking Submit requires approval". It breaks the
moment someone adds a tool, and it cannot see that the button now says "Void
Invoice".

Risk here comes from three sources:

1. **What the tool declares** — `mutates_state`, `reversible`, `risk`, as class
   attributes on the tool itself
2. **What the element says it does** — `Delete|Void|Remove|Cancel` reads as
   destructive; `Submit|Confirm|Approve|Pay|Post` reads as committing
3. **What the action carries** — a currency-shaped number at or above a
   threshold escalates

Measured behaviour:

```
allow    click  "Save Draft"
approve  click  "Submit for Finance Approval"   commits a business action
approve  click  "Delete Deal"                   appears destructive
allow    type   1200   into Asking Price
approve  type   45000  into Asking Price        at/above approval threshold
approve  handle_dialog                          declared irreversible
deny     navigate https://evil.example.com      outside the target application
```

Because it is declaration-driven, a new dangerous tool is caught by the policy
without touching the policy. The read-only verifier surface is enforced by the
same metadata — `ToolRegistry(READ_ONLY_TOOLS)` contains zero tools with
`mutates_state`, and a test asserts it.

Budgets (steps, wall clock, spend) are separate and **deny rather than escalate**:
a run that has exhausted its budget does not get to ask permission to continue.
Denials survive unattended mode; approval gates do not.

---

## 4. Human-in-the-loop that is actually in the loop

An approval gate that prints a prompt and blocks a thread is a demo. The gate
here stops the graph *before executing the action*, checkpoints, and returns.

The mechanism worth noting: when the run pauses, the conversation deliberately
ends on a dangling `tool_use` block with no `tool_result`. On resume, the result
is supplied — either the real outcome of the approved action, or a message
saying a human rejected it and why. The model never sees a gap in the
conversation, and a rejection becomes information it can plan around rather than
an error.

With `--resume`, state is checkpointed to SQLite, so approval can arrive minutes
or hours later.

**Limitation, stated plainly:** the browser is not serialisable. Resuming in a
*fresh process* re-authenticates and re-navigates from the plan; it does not
restore a live DOM. Within a process — which covers the console, the API and the
demo — resume is exact.

---

## 5. Verification

Agents are unreliable narrators, and the failure is not usually lying — it is
that a page which submitted without error genuinely looks like success.

Two layers:

1. **Deterministic.** Where the target can be queried out-of-band, that is
   authoritative. This is a *read*, so it does not violate the browser-only rule
   for *doing*.
2. **Read-back.** A separate verifier with a read-only tool subset navigates to
   where the record should be and reads it.

Both run even when the first passes, because the API can confirm a row exists
while the operator put the mileage in the price field.

`could_not_verify` is a distinct outcome from `failed`. Collapsing them
manufactures false confidence in exactly the situation where you want none.

---

## 6. Self-healing selectors, and when not to heal

Recorded skills cannot store refs — those live for one snapshot. They store
role + accessible name + context, re-resolved through a ladder:

| Tier | Match | Healed? |
|---|---|---|
| `exact` | role + name + context | no |
| `role_name` | role + name | no |
| `name` | name, role drifted | yes |
| `normalized` | punctuation/case/whitespace differences | yes |
| `contains` | one label contains the other | yes |
| `fuzzy` | closest label above 0.82 similarity | yes |
| `none` | no match — fail | — |

`contains` exists because real renames extend or trim a label ("Save" → "Save
Vehicle"), and edit distance scores those surprisingly low: "Save Vehicle" vs
"Save Vehicle Record" is ~0.77, under the fuzzy cutoff.

**The important half is the refusal.** "Save Vehicle" → "Commit Unit To Stock"
resolves to nothing, and that is correct — a wrong click on a page with a
"Delete Vehicle" button is far worse than a clean failure that falls back to the
model. There is a test for exactly this.

Healing is reported, never silent. A skill that needed to heal is a skill whose
target has drifted, and that surfaces in the run notes and blocks promotion.

---

## 7. Evaluation design

**Assert against the database.** An agent that reports success it did not
achieve scores zero. This is the only rule that makes the headline number mean
anything.

**Score against expectation, not completion.** Three kinds of scenario:

- `success` — complete the workflow
- `needs_human` — **stop and ask**; completing it is a failure
- `blocked` — refuse or fail cleanly

The asymmetry is the point. `intake-05` gives the operator "add the blue sedan
that came in on trade this morning" — no VIN, no make, no price. An agent that
completes it has invented a vehicle record. Scoring purely on completion rewards
precisely the behaviour you least want in a system with write access to a
dealer's inventory.

**Separate clean from adversarial.** Five scenarios inject faults — rejected
saves, renamed buttons, mid-run session expiry, latency, 500s — so
`recovery_rate` is reported separately from `clean_rate`. One number hides which
one you are actually measuring.

---

## 8. What I would do next

1. **Visual grounding as a fallback tier.** Perception is text-only. Canvas
   widgets and image-only controls are invisible to it. A screenshot-plus-coordinates
   tier, tried only when DOM resolution fails, would close that.
2. **Trajectory-level replanning.** Replanning currently restarts the plan.
   Better would be to identify the step that failed and re-plan from there.
3. **Multi-tab and long-transaction workflows.** Real F&I flows span tabs,
   printed documents and third-party credit systems. Tab switching exists; the
   orchestration around it does not.
4. **Skill parameter inference.** Promotion takes the parameter set as input.
   Inferring which literals are parameters from two successful runs of the same
   workflow would make promotion automatic.
5. **A model comparison table.** The provider layer supports it; I have not spent
   the tokens.
6. **Concurrency.** One run, one browser. Multi-tenant operation needs a session
   pool and per-tenant credential isolation.

---

## 9. Known limitations

- **Neither target is a real DMS.** The mock is a fixture I wrote; ERPNext is an
  analogue. The architecture and the adapter seam transfer; the selectors do not.
- **Cross-process resume re-navigates** rather than restoring a live DOM (§4).
- **The money heuristic is crude** — a regex for currency-shaped numbers. It is
  built to catch "type 45000 into price", not to parse accounting, and it will
  have false positives on large non-monetary numbers such as mileage.
- **Verification depends on the target being queryable.** Against a system with
  no read API, only the model read-back layer applies, and it is weaker.
- **The eval numbers come from the mock**, whose faults I chose. They measure
  recovery from *anticipated* failures, which is a real but limited claim.
