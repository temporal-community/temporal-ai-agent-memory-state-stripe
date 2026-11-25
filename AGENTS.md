# Repository guidance for coding agents

## Purpose

This repository is a teaching demo for a 10- to 12-minute talk about agent
memory, state, and durable execution. Preserve the story as carefully as the
code. It uses the README's four kinds of information, in the README's order:

- **Context** (working memory) is what the model has in front of it for the
  current decision; here it is the `working_memory` list, which lives in the
  agent process in Demo 1 and in the Workflow in Demo 2.
- **Long-term memory** is what an agent carries from one run to the next, and
  it lives in a store you own. Here that is a light version: `remember_case`
  saves a case note to the `case_notes` memory table in the SQLite demo store
  (`src/refund_agent/store.py`) after the refund, and the agent's
  `search_memory` tool reads this customer's notes back. Memory is kept apart
  from the customer records: never return it through a record lookup such as
  `lookup_customer_history`. Each stage run starts from fresh seed data, so
  the agent's search shows `Past cases (memory): none yet`. Demo 2's final
  frame shows the note the new Worker saved (read by Workflow ID with
  `store.get_case_note`); Demo 1's follow-up shows none, because its process
  died before the refund. Keep that contrast: memory is written only when the
  run finishes, so it supports the Temporal point instead of competing with it.
- **Execution state** is where the work stands, and Temporal records it in
  Event History, which lives in the Temporal Service rather than in the
  Worker.
- **The refund itself**: Stripe, or the offline Stripe-like ledger, is the
  source of truth for whether it happened. The `issue_refund` Activity result
  is the Workflow's record of it, and the idempotency key reconciles the two
  after a crash. Don't call this "effect state" in docs; the published video
  labels the Stripe panel "Effect State", and the README notes that once.

Execution state and Stripe's refund record are authoritative: each is an
owner's record that grounds what the application may safely do, and it wins
when copies disagree. Memory is not authoritative, because it says what the agent
believed, not what happened. These are roles, not storage types: an order row
in the application database becomes context once the agent looks it up, and
the order row still wins.

## Narrative invariants

- Nyghtowl starts with a paid python-plushy order. In Stripe test mode, create
  the PaymentIntent before the first refund prompt so `PAID` is a real
  Stripe record, not stage decoration.
- In Stripe test mode, Demo 1's new agent process must retrieve the
  PaymentIntent and refund list from Stripe. That read must never create a
  refund. In the stage run, only Demo 2 submits a refund.
- In Demo 1, the agent visibly loops: ask whether the package was opened,
  observe the answer, ask what was damaged, observe the answer, look up the
  order and refund history, search past cases in long-term memory, then
  choose `issue refund` as its next action. The
  agent process disappears before Stripe is called. The fixed policy always
  asks both questions in that order. A live model chooses its own questions
  (it may ask fewer, or in another order), so the stage must work with zero,
  one, or two questions in any order and forward whatever Demo 2's agent asks.
- When Demo 1's agent process dies, its process-local working memory goes with
  it, including Nyghtowl's answers and the loop's next action. Stripe correctly
  keeps `PAID` with no refund, but it does not own the customer intake or the
  loop's progress. Never imply Stripe received or lost a refund request at
  this point.
- Demo 2 runs the same loop as a Temporal Workflow. Customer answers are
  Signals, lookups are Activities, and the next action is Workflow state.
  After the kill, a new Worker picks up the Workflow, and the agent resumes at
  `issue refund` without repeating questions or restarting the loop, then
  says, “Your refund is complete,” without the customer asking, and shows the
  case note it saved to long-term memory.
- Do not claim Temporal prevents duplicate refunds by itself. Stripe's
  idempotency support makes the repeated refund call safe. Temporal records
  that the refund step hasn't finished and drives it to completion with
  retries; Stripe, not Temporal, owns whether the refund happened.
- Do not describe Demo 2's run as two refund requests. It is one logical
  refund operation that may need more than one Activity attempt or Stripe
  call.
- Do not claim that each step runs only once. An Activity that was running
  at the crash has no recorded result, so Temporal runs it again. The intended
  result is “one customer request, possibly two calls, one refund.”
- A restarted application must keep or derive the same Workflow ID and
  surface its status or result. Temporal does not automatically update an
  arbitrary chat session.
- Say “refund complete” only when Stripe returned `succeeded`. Render `pending`
  or any other status literally; an accepted API call is not proof of a
  completed refund.
- General-audience stage copy uses one simple name per role. Demo 1's side is
  the “agent process”: `AGENT PROCESS`, `PROCESS GONE`, `NEW AGENT PROCESS`,
  never “Worker,” because it is not a Temporal Worker. Demo 2's side uses
  `TEMPORAL WORKER`, `WORKER GONE`, `NEW TEMPORAL WORKER`, and “new Worker.”
  Say “new” rather than mixing in “replacement” or “reloaded.” Keep SDK and
  event-history terms in the detailed `refund-demo watch` and inspection
  paths.
- Label scripted parts on screen. Each demo header carries one mode line
  (scripted steps; fixed policy or live model; sample lookups; Stripe test mode
  or offline ledger). An offline run's line starts with
  `OFFLINE: fixed policy, no model calls` in bold yellow, never dim. The reuse
  of Demo 1's answers and the demo-only hold before Stripe, where the stage's
  Worker hook holds the refund Activity, are labeled where they happen. Never label the offline ledger as just “Stripe”:
  its heading is `OFFLINE LEDGER (Stripe stand-in)`, and `STRIPE (test mode)`
  appears only with `--real`.
- Give each stage frame one cue, carried by the input prompt. It says
  literally what Enter does next (“start”) and matches the frame that follows.
  Stage-mode frames have no footer cue panel.
- The crash cue is the one exception. It reads “Press Enter to submit the
  refund” in both demos so the presenter never says “kill,” and the frame that
  follows must say, in dim text, that the demo stopped the process or Worker
  before the refund reached Stripe.
- A label that says a value was read from Temporal after the kill must be
  backed by an Event History read, as `loop_from_history` does, never by a
  cached Query result. Keep the cached-fallback label distinct.
- Treat `NO REPEATED QUESTIONS`, `NO LOOP RESTART`, and the customer-facing
  result as the primary payoff: contrast what a memory store could bring back
  with what Temporal records, which is the completed observations and the
  loop's next action. Do not make a duplicate refund or an uncertain Stripe refund the
  main payoff. `2 CALLS → 1 REFUND` is supporting evidence about refund safety
  in the manual technical path, and the post-refund idempotency case belongs
  in the manual walkthrough
  (`docs/GUIDE.md#later-loss-after-stripe-accepts-the-refund`). Show call
  counts only when there is more than one call (the retry paths); never show a
  one-call count on the default path.

## Implementation boundaries

- Keep Workflow code deterministic. Network calls, model calls, filesystem
  access, and other nondeterministic work belong in Activities.
- Both demos run one agent loop, `run_agent_loop` in `agent_loop.py`. It does
  no I/O and takes injected callables (decide, use_tool, ask_customer): the
  Workflow passes Activities and a Signal wait, and Demo 1 passes direct calls
  and stdin. Change the loop once, there. Working memory holds one
  `agent_step` entry per model turn, then the customer answer or tool result
  that turn produced.
- The Workflow, `RefundRequest`, and the Activities are what a real app would
  ship: no prints, no files for the UI, and no demo switches. Activities log
  with `activity.logger`. The stage follows Demo 2 through the `progress`
  Query and Event History. Demo machinery stays at the edges:
  - `demo_hooks.py` is an Activity interceptor that wraps `issue_refund` only.
    The stage switches it on per Worker with `STAGE_HOLD_REFUND` (hold the
    refund before Stripe until the Worker is killed) and
    `STAGE_SIMULATE_STRIPE_TIMEOUT`, and the retry drill and manual
    walkthrough use `REFUND_RESTART_WINDOW_SECONDS`. A Worker with none set
    registers no interceptor.
  - The hook writes `refund-held.json` in the state directory when it holds
    the refund; the stage waits for it before it kills the Worker.
  - The fixed offline policy is chosen explicitly with
    `model_provider="offline"` (`OFFLINE_POLICY`) in the request. The
    Activity and Demo 1 pick it; `model_next_step` only calls a model and
    never falls back to it.
- Preserve a stable refund identity. `issue_refund` derives the Stripe
  idempotency key from the Workflow ID, the refund's business identity (one
  refund request = one Workflow ID), so every attempt, a reset, and a new run
  of the same Workflow ID send the same key. Stripe keeps keys for about 24
  hours, so a later reset still needs an Stripe check; say so wherever the
  key is explained. Never derive the key from the Run ID.
- Keep the stage runner isolated with its private task queue and state
  directory. It must not stop or delete unrelated Workers or user data.
- The live run (a model and Stripe test mode) is the default for
  `refund-demo stage` and `refund-demo start`. Offline happens only on
  purpose: `--offline` (`start` also takes `--dry-run`), or Enter at the
  stage's key prompt. Missing keys with no terminal to ask, or a bad key at
  the prompt, are errors that say to pass `--offline`. `--real` is Stripe test
  mode only, and live Stripe keys must remain rejected.
- Live model mode supports Anthropic and OpenAI. Record an explicit provider in
  Workflow input so new Workers cannot switch providers based on key
  availability. The live model gets no scripted questions; its instructions
  ask it to collect `item_opened` and `damage` with `ask_customer` when
  useful. Keep the `question_id` enum.
- Build the Anthropic and OpenAI clients with `max_retries=0` and a timeout
  shorter than the `agent_decide_next_step` start-to-close timeout, and keep
  `stripe.max_network_retries = 0`. The Temporal Activity retry policy is the
  only retry layer, so every retry is visible in Temporal.
- Keep the Stripe refund call's client timeout (`STRIPE_TIMEOUT_SECONDS`: 3 s
  to connect, 10 s to read, per socket operation) shorter than the
  `issue_refund` start-to-close timeout, and keep `issue_refund` heartbeating
  every second while it waits (`_call_with_heartbeats`). A slow call is then
  not taken for a lost Worker, and a hung one fails and retries with the same
  idempotency key. The timeouts are constants in `workflow.py`: heartbeat 5 s
  (the Worker-loss window, so a new Worker picks up the next attempt within
  seconds), start-to-close 5 minutes, schedule-to-close 30 minutes.
- Pass `temporal_identity()` to every Temporal client, so each process reports
  `<pid>@refund-demo` (or `TEMPORAL_IDENTITY` verbatim) and the machine's
  hostname never shows in the Temporal Web UI.
- Never print, commit, or expose values from `.env` or API-key environment
  variables.
- Do not run a real-model or Stripe test-mode rehearsal unless the task calls
  for it. Stripe test mode still creates external test objects.

## Changing the Workflow safely

- This is a demo. Before upgrading the code, finish or terminate any running
  demo Workflows (`uv run refund-demo stop <workflow-id>`, which cancels; add `--terminate`
  when no Worker is running, or use Terminate in the Temporal Web UI). The repository doesn't carry compatibility code for runs
  started by older code: no deprecated request fields, Signals, or Activity
  signatures kept for old histories, and no `workflow.patched()` guards. Don't
  add them; change the code and re-record the replay history instead.
- Workflow code must stay deterministic. A new Worker rebuilds a run by
  replaying its Event History, which means it runs the Workflow code again from
  the top and checks each step against what the history recorded.
- A change to the order or kind of Commands breaks replay of runs that are
  already in flight. Commands come from steps such as starting an Activity,
  starting a timer, or starting a child Workflow. Moving where the Workflow
  waits for a Signal also changes which Commands come next. In a production
  app you would guard such a change with `workflow.patched("<change-id>")` or
  use Worker Versioning; here, finish or terminate demo runs first, as above.
- Run `make test` before merging. It includes `tests/test_replay.py`, which
  replays `tests/histories/stage_offline_worker_hooks.json`, an offline stage
  run recorded from the current code, against the current Workflow.
- When a change alters the Workflow's Commands on purpose, re-record that
  history as the docstring of `tests/test_replay.py` describes and replace the
  file. If the replay test fails after a change you didn't mean to alter the
  Commands, fix the change, not the history.

## Important paths

- `src/refund_agent/stage.py`: guided single-terminal talk path
- `src/refund_agent/store.py`: the SQLite demo store. Its record tables
  (orders, customers) stand in for the business systems the lookups read; its
  `case_notes` table is the agent's long-term memory, which `remember_case`
  writes and `search_memory` reads, kept apart from the records
- `src/refund_agent/tui.py`: technical and general-audience panels
- `src/refund_agent/agent_loop.py`: the agent loop both demos run, with its
  turn budget and the allowed questions and tools
- `src/refund_agent/workflow.py`: Demo 2 (the `RefundApprovalAgent`
  Workflow): the loop's Activities and Signal wait, approval, the refund, the
  `progress` Query, and the `approve` and `customer_answer` Signals
- `src/refund_agent/activities/`: the Activities, split by role. `model.py`
  holds the model turn (`model_next_step`), the fixed offline policy
  (`fixed_policy_step`), and usage logging, `tools.py` the three lookups,
  the `search_memory` memory read, and the `remember_case` memory write, and
  `refund.py` the refund side effect (`refund_payment`, shared with Demo 1)
  and its idempotency key. Activity names are what Event History and
  `tests/test_replay.py` refer to; renaming one means re-recording the replay
  history.
- `src/refund_agent/demo_hooks.py`: the demo-only Activity interceptor for the
  stage's hold before Stripe and its failure drills
- `src/refund_agent/naive_refund.py`: Demo 1's agent process, the comparison
  without Temporal. It runs the shared loop with direct calls and stdin and
  refunds through `refund_payment` into the same offline ledger
- `src/refund_agent/fake_stripe.py`: the offline ledger (the Stripe stand-in)
  and its call counter
- `README.md`: what the demo shows, how to run it, the four kinds of
  information and who owns each fact (`#memory-and-execution-state`),
  architecture, cost, and how other approaches compare
- `docs/GUIDE.md`: stage flags, Web UI checks, expected output, the manual
  walkthrough, and every error
- `docs/HISTORY_GROWTH.md`: Event History growth, claim check, and
  Continue-As-New
- `docs/TALK_10_MIN.md`: canonical talk timing, wording, and controls

## Verification

Install development and terminal dependencies with `make setup`, which runs:

```bash
uv sync --extra dev --extra tui
```

Running `uv sync` without those extras removes both, including Rich for the
stage view.

Before committing a change, run `make test`, `make lint`, and
`make typecheck`, or:

```bash
uv run --extra dev pytest -q
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev mypy
```

CI (`.github/workflows/ci.yml`) runs the same lint, type check, and tests on
every push to `main` and on every pull request.

Changes to stage copy or control flow also require a complete offline rehearsal
(`make run-offline`):

```bash
uv run refund-demo stage --offline
```

Without a terminal, `yes '' | uv run refund-demo stage --offline` presses
Enter at every prompt and plays the whole run; add `--simulate-stripe-timeout`
or `--simulate-stripe-retry` for the drills. It still starts a local Temporal
dev server if none is reachable, so point `TEMPORAL_ADDRESS` at a throwaway
server as `tests/test_replay.py` describes.

Verify the content is visible at the intended terminal size, not merely present
in the Rich render tree. The smallest stage terminal is 80 columns, and the
recording width is 100 to 120 columns (`tests/test_tui.py` checks 80 and 100).
Each pane line should fit one row at both 80 and 100 columns, so test the
longest line at those widths, not only at 128, the width
`scripts/render_demo_frames.py` renders at.

## Documentation and generated media

- Each doc owns its topics and has its own reader. Say each point once, in the
  doc that owns it, and link there from elsewhere. Add to the owner rather
  than starting a new doc.
  - `README.md` is for people trying the demo and their coding agents. It owns
    the concepts, architecture, cost, how other approaches compare, gotchas,
    and what the demo is not. It uses no presenter language.
  - `docs/GUIDE.md` is for whoever runs or presents the demo. It owns the
    stage flags, Web UI checks, expected output, the manual walkthrough, and
    troubleshooting.
  - `docs/TALK_10_MIN.md` is the presenter's run of show, which the presenter
    only glances at while speaking. It owns the talk's timing, wording, and
    controls.
  - `docs/HISTORY_GROWTH.md` is for readers who plan to run long agents on
    Temporal. It owns Event History growth, its limits, the claim check, and
    Continue-As-New.
- These wording rules apply to every doc:
  - Temporal records this run's working memory (Activity inputs and results)
    in Event History, so say Temporal “records,” never “remembers.” Long-term
    memory lives in a store you own, here the SQLite demo store.
  - Stripe, or the offline ledger, owns whether the refund happened. Never say
    Temporal tracks the refund's state.
  - Give cost in tokens and dollars together, and never make “$0” a headline.
    Lead with why the demos differ: starting over pays for every model call
    again, replay doesn't pay again for finished calls, and only a call in
    flight at a crash can run twice. Show each demo's numbers and the estimate
    at scale, not a total for a full run of both demos.
  - Don't write “proves,” and don't call the agent, its loop, or the Worker
    “plain.”
  - Don't write “exactly once” or claim a step runs only once; the reason is
    under Narrative invariants.
- Keep `README.md`, `docs/GUIDE.md`, and `docs/TALK_10_MIN.md` aligned when
  the story or stage flow changes. Stage copy appears in the README's key
  lines (`#same-crash-two-outcomes`), the guide's transcript
  (`#stage-transcript`), and the talk's cue table.
- A few facts are repeated on purpose, so change every copy: the measured cost
  (README `#cost-to-run`, the top of the guide's `#run-the-stage`, and the
  talk's cost beat) and the README's three Troubleshooting rows (the first
  rows of the guide's `#troubleshooting`).
- Each `Makefile` recipe is the raw command shown beside it in the README's
  Make targets table. Change both together, and keep `make reset` from
  deleting files or stopping servers.
- Update the README's `Last verified` line only for runs that actually
  happened.
- `scripts/render_demo_frames.py` generates deterministic HTML frames. Generated
  raster screenshots, GIFs, and videos are separate artifacts; do not claim
  they were updated unless they were regenerated and visually inspected.
- Do not commit `.env`, `.demo-state`, local Temporal databases, stage logs, or
  temporary rendering directories.
