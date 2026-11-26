# Run and present the demo

Back to the [README](../README.md).

This guide picks up where the README's quick start ends. It walks through what
the demo does at each step, how to watch the same run in the Temporal Web UI,
how to run each piece by hand, and how to fix what goes wrong.

The demo turns on one distinction. Memory helps the agent reason, but it
doesn't record where the work stands. Temporal records that execution state in
Event History, which lives in the Temporal Service rather than in the Worker,
so a new Worker can pick the work back up. Stripe, or the offline ledger that
stands in for it, is the only record of whether the refund happened.

If you're presenting the demo, the timed notes for a 10-minute talk are in
[TALK_10_MIN.md](TALK_10_MIN.md).

**Contents:**

- [Run the stage](#run-the-stage)
- [Expected output](#expected-output)
- [Watch it in the Temporal Web UI](#watch-it-in-the-temporal-web-ui)
- [Manual walkthrough](#manual-walkthrough)
- [Troubleshooting](#troubleshooting)

## Run the stage

`refund-demo stage` runs both demos in one terminal, and this guide calls it
the stage.

```bash
uv run refund-demo stage             # or: make run
uv run refund-demo stage --offline   # or: make run-offline
```

Press Enter at each prompt to move to the next step. By default, the stage is
live: it uses your model key and Stripe test key from `.env`, and asks for any
that are missing (for that run only). Press Enter at that prompt, or pass
`--offline`, to use a deterministic policy in place of a model and an offline
Stripe-like ledger in place of Stripe. Offline runs say
`OFFLINE: fixed policy, no model calls` on every frame. Offline happens only
on purpose: without a terminal to ask, as in tests and CI, missing keys stop
the stage with `STAGE | the live run needs ..., and there is no terminal to
ask` and exit code 1, and the message tells you to pass `--offline`. If it
can't reach a Temporal server, it starts a local dev server. When it exits, it
shuts down only the processes it started.

**Cost.** Starting over pays for every model call again. Replay doesn't pay
again for calls that already finished, so only a call that was in flight at
the crash can run twice. These numbers were measured on one live run on
GPT-5.6 Luna on 2026-11-23, before the `search_memory` tool existed; the
current code adds one lookup turn, so expect about one more model call per
demo:

- A Demo 1 pass made 6 model calls: 4,235 tokens, about $0.0013. If the
  customer starts over, as Demo 1 tells them to, the redo pays for about one
  more pass.
- Demo 2 made 6 model calls in all, replay included: 4,426 tokens, about
  $0.0015. Replay after the kill returns recorded results instead of calling
  the model again.
- With `--offline`, or `--real` without `--real-model`, a stage run uses the
  fixed policy and makes no model calls (0 tokens, $0).

The README's [Cost to run](../README.md#cost-to-run) has the token breakdown,
the Claude estimates, and an estimate for 1M requests a month.

### What the runner does

1. The stage welcomes Nyghtowl back and shows the last python-plushy order as
   `PAID`.
2. **Demo 1.** You ask for a refund, and the agent process works through the
   loop in its own memory:
   - It asks the customer what it needs. The fixed policy always asks both
     intake questions (was the package opened, and what was damaged); a live
     model asks only the ones it wants, in any order. Press Enter to accept
     each suggested answer.
   - It runs the lookups its policy or model picks. The fixed policy looks up
     the order and the customer's history, then searches past cases in
     long-term memory with `search_memory` (`Past cases (memory): none yet`,
     since every stage run starts fresh).
   - It chooses `issue refund`. It runs the same agent loop and model step as
     Demo 2, but in process, with direct calls and stdin.
3. The stage kills the agent process after it chooses `issue refund` but
   before any refund call, and holds on a visible `PROCESS GONE` frame.
4. The stage starts a new agent process (`NEW AGENT PROCESS`) and sends it the
   customer's status question across that process boundary.
   - The new process checks Stripe, or the offline ledger, for the payment.
     With `--real`, it retrieves the PaymentIntent and its refund list directly
     from Stripe, without submitting a refund.
   - It has no answers and no next step, so the customer starts the return
     again (`THE CUSTOMER STARTS OVER`).
5. **Demo 2.** The stage starts the Workflow and follows it with the
   `progress` Query. Whatever Demo 2's agent asks, the stage answers with your
   Demo 1 answer to that question (or the question's suggested answer, if Demo
   1's agent never asked it), sent as a `customer_answer` Signal. The screen
   says "Reusing your Demo 1 answers so you don't type them twice..."
   - The lookups run as Activities. Each one has a plain-words summary in
     Event History, such as `Look up order 1234`.
   - Once the agent decides, the Workflow starts `issue_refund`. The stage's
     first Worker runs with a demo-only Activity interceptor
     ([`demo_hooks.py`](../src/refund_agent/demo_hooks.py)) that holds
     attempt 1 before it calls Stripe, so the crash lands at the same point
     on every run. The Workflow itself has no demo pause.
6. The stage kills the Worker during that hold, at the same next action.
   - The `WORKER GONE` frame reads Event History from Temporal, which needs no
     Worker.
   - Under `Read from Temporal just now:`, it shows the customer answers, the
     completed lookups, and `Next action: issue refund`. The fixed policy gives
     two answers and three lookups (the third is `search_memory`). A live
     model may ask fewer questions, and shows four lookups when it also checks
     the refund policy.
7. The stage starts a new, plain Worker, with no interceptor. The held attempt
   stopped heartbeating when its Worker died, so after the 5-second heartbeat
   timeout the Temporal Service schedules attempt 2, a retry of the refund
   Activity. The new Worker replays Event History to rebuild the Workflow,
   picks up attempt 2, and the refund reaches Stripe once, on attempt 2. A
   real app has no hold: its Worker can stop at any step, and Temporal
   recovers the same way. The README lists
   [what's written for the demo](../README.md#whats-written-for-the-demo).
   - The `NEW TEMPORAL WORKER` resumes at `issue refund` without repeating
     questions, issues the refund, and reports completion.

**The kill is real.** The failure the demo shows is a Worker that dies in the
middle of the agent loop. The default run sends the Worker a real SIGKILL one
step before the refund.

- Before the kill, Demo 2's agent loop shows `→ Next: issue refund`, the stage
  waits for you to press Enter, and the Worker is still alive.
- After the kill (`WORKER GONE`), the Workflow is still `Running`, even though
  no Worker is alive.

That is expected. The Temporal Service, here the dev server, keeps the
Workflow's Event History, and a Worker only polls the Task Queue for the next
step to run. So the Workflow waits until a new Worker arrives. To see this for
yourself, [watch it in the Temporal Web UI](#watch-it-in-the-temporal-web-ui).

**What the screens say.**

- Demo 1's side is called the agent process (`PROCESS GONE`,
  `NEW AGENT PROCESS`), because it is not a Temporal Worker. The word "Worker"
  is used only for Demo 2 (`WORKER GONE`, `NEW TEMPORAL WORKER`).
- Every input prompt says what Enter does next, such as
  `Press Enter to submit the refund`.
- Each demo's header has a line that names what is scripted and what is
  live:
  - Both, in the default live run: `Live model (openai) · sample lookups ·
    Stripe test mode` (or `anthropic`)
  - Both, with `--offline`, in bold yellow:
    `OFFLINE: fixed policy, no model calls · offline ledger (no Stripe)`
  - With `--real` alone: Demo 1 reads `Scripted steps · Stripe test mode`, and
    Demo 2 reads `Fixed policy (no LLM) · sample lookups · Stripe test mode`
  - Both, with `--real-model` and no `--real`: `Live model (openai) · sample
    lookups · offline ledger (no Stripe)`
- The ledger pane is headed `OFFLINE LEDGER (Stripe stand-in)` by default and
  `STRIPE (test mode)` with `--real`.

### Two owners

In Demo 2, two systems each own a different fact. Temporal records each
Activity attempt and where the work stands, which gives a new Worker its
recovery point. Stripe, or the offline ledger, records whether the refund
happened, but it holds neither the customer's answers nor where the loop
stood. The README's [Facts have owners](../README.md#memory-and-execution-state)
explains the general rule.

What this demo adds is the shared idempotency key. Every `issue_refund`
attempt in a run sends the same key, so a retry after a crash gets its answer
from Stripe, not from memory. The
[later-loss walkthrough](#later-loss-after-stripe-accepts-the-refund) shows
that case.

### Live models

```bash
uv run refund-demo stage --real-model --model-provider openai   # or anthropic
```

**Setup.**

- OpenAI mode needs `OPENAI_API_KEY` and `OPENAI_MODEL`, and Claude mode needs
  `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`. Put local values in `.env`;
  exported shell variables take precedence.
- `AGENT_MODEL_PROVIDER` can replace `--model-provider`. If only one model key
  is set, the stage infers the provider. If both are set and no provider is
  chosen, the default run uses OpenAI, and `--real-model` stops and asks you to
  choose one.
- Run a live model against the offline ledger first, before you combine it
  with `--real`.
- The order is fixed: order 1234, an $80 python plushy. Its policy record says
  explicitly that this low-value damaged item needs no physical return. Refer
  to order 1234 or the python plushy in your request, because a live model can
  correctly deny a request for a different item.

**How it behaves.**

- Both demos run the same agent loop (`agent_loop.py`) and call the live model
  through the same model step, `model_next_step`.
- The model gets no scripted questions. It asks for `item_opened` and
  `damage` through its `ask_customer` tool only if it needs them, so it may
  ask one, both, or neither. The stage forwards whatever Demo 2's agent asks,
  with the Demo 1 answer for that question or its suggested answer.
- Demo 1 has no retry layer. Its agent process calls the model step directly,
  so one failed model call, such as an API error, ends Demo 1, and the stage
  stops with the reason.
- Demo 2 calls the model step from the `agent_decide_next_step` Activity,
  so Temporal retries a failed call and Event History records each finished
  turn. After the kill, replay returns those recorded results instead of
  calling the model again.
- If the model escalates instead of approving, the stage sends Demo 2's
  approval Signal itself, with the note `approved by the guided stage runner`.
  Demo 1 has no Signal to wait on, so it treats an escalation as approved.
- If the model denies in Demo 1, the stage stops before Demo 2, because there
  is no refund to interrupt. If it denies in Demo 2, the screen shows the
  model's rationale and that no refund was issued, instead of exiting on an
  empty screen.
- If `--real` created a Stripe test payment before a denial,
  [clean it up](#stripe-test-mode-and-cleanup).
- Both providers see the order amount in dollars, as `"$80.00"`, so the
  rationale reads like `Approve the $80.00 refund for order-1234…`. The stored
  request and working memory still keep cents (`amount_cents`).

The on-screen differences and messages are in
[With a live model](#with-a-live-model).

### Stripe test mode and cleanup

```bash
uv run refund-demo stage --real   # or: make run-real
```

`--real` needs a Stripe `sk_test_` or `rk_test_` key in `STRIPE_API_KEY`, and
it rejects live keys. It creates Stripe test objects:

- Before the first refund prompt, the runner creates and confirms Nyghtowl's
  Stripe test PaymentIntent, so `PAID` is a real record.
- Demo 1's new agent process reads that PaymentIntent and its refunds from
  Stripe, and confirms that no refund reached Stripe.
- Only Demo 2 refunds the test payment.

If a run ends before the refund, reconcile it:

```bash
uv run refund-demo cleanup
```

Cleanup refunds only the outstanding test payments this demo created. It
doesn't delete Stripe test objects or make the Dashboard's row counts match,
because Stripe keeps both the payment and the refund records. If it reports
`refunded $0.00`, it found no outstanding demo payment.

### Which lines read Stripe

Not every Stripe line on screen is a live Stripe read.

- `Payment: PAID` is a fixed label in Demo 2's pane, and in Demo 1's pane
  before the kill.
- Demo 2's refund line reads a local mirror of Stripe's response.
- Only Demo 1's new agent process reads Stripe directly, in its status check,
  and only with `--real`. Offline, that check reads the offline ledger, and the
  pane is headed `OFFLINE LEDGER (Stripe stand-in)`.
- Offline runs still say "Stripe" in the agent's lines, the Worker log, and
  the `issue_refund` summary in Event History, because "Stripe" is the story's
  name for the system that owns the refund.

### Simulated failures

Two flags move the kill into the refund call itself, so you can show what a
retry does. Like the plain stage, they use the default live run unless you add
`--offline`, or `--real` for Stripe test mode with the fixed policy. You can use
only one of them per run. Both work through the stage's Worker interceptor
([`demo_hooks.py`](../src/refund_agent/demo_hooks.py)), not the Workflow.

**Stripe timeout, the recommended retry demo.** The Worker is killed while the
refund Activity is in flight instead of before it:

```bash
uv run refund-demo stage --offline --simulate-stripe-timeout   # or: make failure
```

1. Attempt 1 enters `issue_refund`, but the simulated Stripe API does not
   respond (`Stripe API is not responding (simulated); no refund accepted` in
   the Worker log).
2. The Worker is killed before any refund is accepted.
3. After the 5-second heartbeat timeout, Temporal advances the Activity to
   attempt 2 and waits until you start a new Worker. The screen says
   `Temporal is waiting at attempt 2`.
4. The new, plain Worker runs attempt 2 with the same idempotency key and
   calls Stripe normally. Only then does the refund reach Stripe.

This path has no hold before the refund. Use it when the story is "the API
was down."

**Later loss, `--simulate-stripe-retry`.**

1. As on the main path, attempt 1 is held before Stripe and its Worker is
   killed.
2. The next Worker runs attempt 2, and Stripe accepts it. That Worker's
   restart window keeps the attempt open, and the stage kills it before
   Temporal records the result.
3. A plain Worker runs attempt 3 with the same idempotency key, so two Stripe
   calls resolve to one refund: `2 CALLS → 1 REFUND`.

This is the technical walkthrough of idempotency, not the main story. See
[Later loss](#later-loss-after-stripe-accepts-the-refund).

### Reset between runs

<a id="reset-between-takes"></a>Default runs need no reset. Every stage run
gets a fresh token, so it gets a new Workflow ID (unless you pass
`--workflow-id`), a private Task Queue named `refund-stage-<token>`, and its
own state directory, `.demo-state/stage-<token>/`.

If you pass `--workflow-id`, use a new one for every run. Reusing one while an
earlier run is still Running fails with `WorkflowAlreadyStartedError`.

| After | Run |
| --- | --- |
| A `--real` run that ended before Demo 2's refund | `uv run refund-demo cleanup` ([what it does](#stripe-test-mode-and-cleanup)) |
| An aborted run that left a Running Workflow | `uv run refund-demo stop <workflow-id>` cancels it. With no Worker running, add `--terminate` |
| The stage process was killed hard | 1. `pgrep -fl refund_agent` lists stray stage processes.<br>2. Check each command line: `python -m refund_agent.worker` or `python -m refund_agent.naive_refund`.<br>3. Stop only those. |
| You want an empty Workflow list in the Web UI | Press Ctrl+C on your own in-memory `temporal server start-dev`, then start it again.<br>Never do this to a server someone else is using. |
| You want to clear local files | Stop any dev server that uses `.demo-state/temporal.db`, then run `rm -rf .demo-state`.<br>This removes stage logs, token logs (`model-usage.jsonl`), offline ledgers, and the stage-started server's database. |

## Expected output

### Stage transcript

This is an offline run of
`uv run refund-demo stage --offline --workflow-id nyghtowl-take-01`, pressing
Enter at every prompt. A live run shows the same frames with its own header
line ([With a live model](#with-a-live-model)). For a quick check, look for
these lines, in order.

**Demo 1:**

1. `→ Next: issue refund`
2. `PROCESS GONE`
3. `Past cases (memory): none`, because the process died before it saved
   a case note.
4. `No refund request reached Stripe.`
5. `Please start the return again.`

**Demo 2:**

1. `WORKER GONE`
2. `Read from Temporal just now:` with `Next action: issue refund`
3. `NO REPEATED QUESTIONS`
4. `NO LOOP RESTART`
5. `Your refund is complete.`, without the customer asking.
6. `LONG-TERM MEMORY`: the case note the new Worker saved after the refund.

The full run is below, condensed, with the box drawing removed. Pane titles
are in capitals.

```text
Agent memory and state
  An agent can know exactly what to do and still lose the work a customer submitted.
  We'll stop the agent right before it calls Stripe: first without Temporal, then with it.
An agent works with different kinds of information
  This demo covers four of them:
  CONTEXT           what the model sees this turn
  WORKING MEMORY    this run's answers and results; part of execution state
  LONG-TERM MEMORY  case notes kept across runs, in their own store
  STATE             where the work stands:
    execution state where the run is and what's next (Temporal)
    Stripe          source of truth for whether the refund happened
Press Enter to start Demo 1 (without Temporal)

Demo 1: Without Temporal, the agent loses its place
  Its answers and next step live only inside this agent process.
  OFFLINE: fixed policy, no model calls · offline ledger (no Stripe)
  AGENT PROCESS  Welcome back, Nyghtowl
  WHAT SURVIVES  LAST ORDER  Python plushy · $80.00
                 OFFLINE LEDGER (Stripe stand-in)  Payment: PAID  Refund: none
  THE QUESTION   What if this agent process disappears right before the refund?
Ask for a refund
you>                        (Enter alone sends "Please refund order 1234.")
agent> Was the package opened? [Yes]
agent> What was damaged? [Split seam]
  AGENT LOOP
    ✓ Package opened: Yes
    ✓ Damage: Split seam
    ✓ Order: python plushy
    ✓ Refund history: clean
    ✓ Past cases (memory): none yet
    → Next: issue refund
  WORK NOT SAVED
    Next step: submit the refund.
    The answers and next step exist only inside this process.
Press Enter to submit the refund

  AGENT PROCESS  PROCESS GONE
                 It held the answers and next step.
                 They are gone with it.
                 The demo stops the process here,
                 before the refund reaches Stripe.
  WHAT'S LEFT    Only Stripe's record: PAID, no refund.
                 That is correct. Stripe was never called.
Press Enter to start a new agent process

  NEW AGENT PROCESS  Welcome back, Nyghtowl
                     No answers. No next step.
  FRESH START        It can still read Stripe.
                     It cannot see the old answers.
Ask the new process: What happened to my refund?
you>                        (Enter alone sends "What happened to my refund?")
  AGENT  ✓ Past cases (memory): none
           nothing saved; the process died first
         Let me check Stripe.
         No refund request reached Stripe.
         I lost your return answers.
         Please start the return again.
  THE CUSTOMER STARTS OVER
    Stripe's record is right: paid, no refund.
    But Stripe never had the answers or the next step.
Press Enter for Demo 2: the same test with Temporal

Demo 2: With Temporal, the agent keeps its place
  Temporal keeps each answer, lookup and next step outside the Worker.
  OFFLINE: fixed policy, no model calls · offline ledger (no Stripe)
Ask for a refund (order 1234, the python plushy)
you>
Reusing your Demo 1 answers so you don't type them twice...
  TEMPORAL WORKER  AGENT LOOP
                     ✓ Package opened: Yes
                     ✓ Damage: Split seam
                     ✓ Order: python plushy
                     ✓ Refund history: clean
                     ✓ Past cases (memory): none yet
                     → Next: issue refund
  WHAT SURVIVES    TEMPORAL        Saved so far:
                                   Customer answers: 2
                                   Completed lookups: 3
                                   Next action: issue refund
                                   (demo pauses here, before Stripe)
                   OFFLINE LEDGER  Payment: PAID
                                   Refund: none
Press Enter to submit the refund

  TEMPORAL WORKER  WORKER GONE
                   Its in-memory loop is gone.
                   The demo stops the Worker here,
                   before the refund reaches Stripe.
                   Temporal still has the saved loop. →
  WHAT SURVIVES    TEMPORAL        Read from Temporal just now:
                                   Customer answers: 2
                                   Completed lookups: 3
                                   Next action: issue refund
                   OFFLINE LEDGER  Payment: PAID
                                   Refund: none
Press Enter to start a new Worker
Starting a new Worker. It picks up from Temporal's history...

  NEW TEMPORAL WORKER  NO REPEATED QUESTIONS
                       NO LOOP RESTART
                       Same loop, rebuilt from Temporal.
                       AGENT  (the customer didn't ask)
                         Your refund is complete.
  WHAT SURVIVES        TEMPORAL        Finished by the new Worker.
                       LONG-TERM MEMORY
                                       Case note saved after the refund:
                                         <date>  refund succeeded
                                         order 1234 · $80.00
                       OFFLINE LEDGER  Payment: PAID
                                       Refund: SUCCEEDED
Press Enter for the takeaway

The difference
  Without Temporal: the customer had to start over.
  With Temporal: a new Worker picked up at the saved next action.
Memory helps reasoning continue.
Temporal helps the operation continue.
Stripe knows whether money moved.
Stage logs: .demo-state/stage-<token>
```

- With `--real`, the ledger heading reads `STRIPE (test mode)`, and the header
  lines end in `Stripe test mode`.
- `(demo pauses here, before Stripe)` marks the stage's Worker hook holding
  `issue_refund` attempt 1. The Workflow has already started the refund step.
- The `Stage logs:` path is relative to the directory you ran the stage from,
  so from there it works as `refund-demo usage --state-dir`.
- Demo 2's `WORKER GONE` frame can take up to 3 seconds to appear, because it
  reads Event History while it draws. If that read fails or runs out of time,
  the pane says `Could not re-read Temporal.` and
  `Showing the earlier reading:` instead of `Read from Temporal just now:`.

### With a live model

The default `stage`, or `stage --real-model --model-provider openai` (or
`anthropic`), runs both demos on the live model, through the same agent loop
and model step. Compared with the offline transcript:

- Both demos' headers start with `Live model (openai)` or
  `Live model (anthropic)` instead of the `OFFLINE:` line. The default run
  reads `Live model (openai) · sample lookups · Stripe test mode`. With
  `--real-model` alone, it reads, for example,
  `Live model (anthropic) · sample lookups · offline ledger (no Stripe)`.
- While Demo 1 waits on the model, the screen shows
  `The agent is choosing its next step...`.
- The model picks its questions. It may skip one or both, so Demo 2's pane can
  show fewer than 2 customer answers.
- The model picks the lookups. In the measured 2026-10-02 run, it also checked
  the refund policy in both demos, so each loop added
  `✓ Refund policy: eligible`. With `search_memory` as well, Demo 2's
  pane would show `Completed lookups: 4`. A live model may also skip
  `search_memory`, though its instructions ask it to check past cases.
- The rationale speaks in dollars, because the model sees `"$80.00"`.
- The new Demo 1 process's reply is fixed text, not model output. It still
  says "No refund request reached Stripe. I lost your return answers. Please
  start the return again."
- If the model denies the Demo 1 request, the stage stops before Demo 2 with
  `STAGE | the model denied the Demo 1 refund; run the stage again`. A `--real`
  run adds a cleanup hint.
- If it denies in Demo 2, the screen shows the model's rationale and that no
  refund was issued, and the prompt is
  `The model denied this request. Press Enter to exit`.
- If Demo 1's process fails, for example on a missing key or an API error, the
  stage stops with `STAGE | the Demo 1 agent process failed: <reason>`.

### Worker log

The Worker log, `.demo-state/stage-<token>/worker.log`, shows the resume. This
excerpt is for an offline run, with the `MODEL REASONING`, `RECORD LOOKUP`, and
`LONG-TERM MEMORY` lines removed. Offline, the `MODEL REASONING` lines come
from the fixed policy, not a model.

```text
EXECUTION STATE | Worker connected to localhost:7233 on task queue refund-stage-<token>
EXECUTION STATE | phase=agent_loop_start
EXECUTION STATE | phase=waiting_for_answer id=item_opened
EXECUTION STATE | phase=observed answer=item_opened
EXECUTION STATE | phase=waiting_for_answer id=damage
EXECUTION STATE | phase=observed answer=damage
EXECUTION STATE | phase=observed tool=lookup_order
EXECUTION STATE | phase=observed tool=lookup_customer_history
EXECUTION STATE | phase=observed tool=search_memory
EXECUTION STATE | phase=decided recommendation=approve
EXECUTION STATE | phase=issuing_refund
EXECUTION STATE | refund held before Stripe (demo pause)
EXECUTION STATE | Worker connected to localhost:7233 on task queue refund-stage-<token>
EXECUTION STATE | issuing refund: attempt 2, decision approve, idempotency key ...<last 8 hex>
THE SYSTEM | refund accepted at Stripe: re_dry_<16 hex> (dry-run, attempt 2)
```

- `refund held before Stripe (demo pause)` comes from the stage's Worker hook,
  which holds attempt 1 until the stage kills this Worker. Attempt 1 never
  reaches the refund call.
- The second `Worker connected` line is the new Worker. No
  `agent_decide_next_step` or lookup output follows it, even though the new
  Worker runs the Workflow code again from the top. `workflow.logger` stays
  silent during replay, and each finished Activity returns its recorded result
  from Event History instead of running again. `issuing refund: attempt 2` is
  the first line from new work: Temporal scheduled attempt 2 after the killed
  attempt's 5-second heartbeat timeout.
- Offline, "at Stripe" means the offline ledger.

With a live model, the `MODEL REASONING` lines are the model's choices, one
per turn, in the form `MODEL REASONING | <action> <question, tool, or
recommendation>`. A run in which the model asks one question and also checks
the refund policy looks like this:

```text
MODEL REASONING | ask_customer damage
EXECUTION STATE | phase=waiting_for_answer id=damage
EXECUTION STATE | phase=observed answer=damage
MODEL REASONING | use_tool lookup_order
EXECUTION STATE | phase=observed tool=lookup_order
MODEL REASONING | use_tool lookup_customer_history
EXECUTION STATE | phase=observed tool=lookup_customer_history
MODEL REASONING | use_tool check_refund_policy
EXECUTION STATE | phase=observed tool=check_refund_policy
MODEL REASONING | use_tool search_memory
EXECUTION STATE | phase=observed tool=search_memory
MODEL REASONING | decide approve
EXECUTION STATE | phase=decided recommendation=approve
```

- Every turn is a model call, including each question. The model's rationale
  for each turn is in the `agent_step` entries of working memory, which the
  `progress` Query returns.
- With `LOG_MODEL_USAGE=1`, each model call also logs a `MODEL USAGE` line
  with its input and output tokens.

## Watch it in the Temporal Web UI

### Start your own dev server

Start the dev server yourself if you want its Web UI to stay up after the
stage exits. The stage reuses any server it can reach at `TEMPORAL_ADDRESS`
(default `localhost:7233`), and it stops only the processes it started, so
your server and its Web UI stay up. A server the stage starts for you uses
`.demo-state/temporal.db`, and the stage shuts it down when it exits, which
takes the Web UI with it.

```bash
# Terminal 1: in-memory dev server, with a fresh Workflow list each time
temporal server start-dev
# Web UI: http://localhost:8233

# Terminal 2: give the run a name you can find in the Web UI
uv run refund-demo stage --workflow-id nyghtowl-take-01
```

If the default ports are taken, run this demo's server on other ports and
point the stage at it. The stage then connects to that server instead of
starting its own:

```bash
lsof -nP -iTCP:7233 -iTCP:8233 -sTCP:LISTEN   # who holds the default ports?

# Terminal 1
temporal server start-dev --port 7243 --ui-port 8243
# Web UI: http://localhost:8243

# Terminal 2
TEMPORAL_ADDRESS=localhost:7243 TEMPORAL_NAMESPACE=default \
  uv run refund-demo stage --workflow-id nyghtowl-take-01
```

- Check `.env` too. If `TEMPORAL_ADDRESS` names a remote server that answers,
  the stage uses that server.
- Never press Ctrl+C on a server someone else is using.

### What to check at each frame

Open the `RefundApprovalAgent` Workflow. The stage screens don't print its
Workflow ID. Without `--workflow-id`, the ID is `talk-refund-<token>`, where
`<token>` matches the `stage-<token>` folder in the `Stage logs:` line printed
at the end.

1. **At Demo 2's `Press Enter to submit the refund`:** on the Queries tab, run
   `progress`. In an offline run, it returns:
   - `phase: "issuing_refund"`, because the refund step has started and the
     stage's Worker hook is holding it before Stripe
   - `working_memory`, in order: one `agent_step` entry per agent turn (its
     action, the question or tool, and its rationale), each followed by what
     that turn produced: two `customer_answer` entries (with the default
     answers, `item_opened`: `Yes` and `damage`: `Split seam`) and the
     `lookup_order`, `lookup_customer_history`, and `search_memory` results
     (`past_cases: []`, since every stage run starts fresh)
   - `decision`, with `recommendation: "approve"`, and the `request`
   - `pending_question: null`
   - The Pending Activities tab shows `issue_refund` at attempt 1.
2. **After the kill (`WORKER GONE`):** use the History tab.
   - The Workflow's status is still `Running`.
   - The events include two `WorkflowExecutionSignaled` (`customer_answer`)
     events, the completed `agent_decide_next_step` (six offline),
     `lookup_order`, `lookup_customer_history`, and `search_memory`
     Activities, and an
     `ActivityTaskScheduled` event for `issue_refund` with no completion.
   - On the Pending Activities tab, `issue_refund` stays at attempt 1 until
     its 5-second heartbeat timeout, then shows attempt 2, with a heartbeat
     timeout as its last failure, waiting for a Worker.
   - This is the same Event History that the stage's `WORKER GONE` pane reads.
   - Don't run a Query now, because Queries need a live Worker.
   - On the Workers tab, or the Task Queue page, check the Last Accessed time.
     A live Worker's time is never much more than a minute old. A killed
     Worker stays listed until 5 minutes after its last poll, and its Last
     Accessed time keeps getting older.
   - The Deployments page shows only versioned Workers, so it doesn't show
     this.
3. **After recovery:** the Workflow is `Completed`.
   - No new `agent_decide_next_step`, lookup, or `customer_answer` event
     appears. The new Worker ran the Workflow code again from the top, but
     each finished Activity returned its recorded result and each earlier
     Signal was read back from Event History, so none of them ran or arrived
     again.
   - The new application-level work is `issue_refund` and then
     `remember_case`, alongside the usual Workflow Task events. Event History
     compacts the retry: there is one `ActivityTaskStarted` event for
     `issue_refund`, with `attempt: 2` and a `lastFailure` of
     `activity Heartbeat timeout` (attempt 1, which the stage held and
     killed). One refund reached the ledger, on attempt 2.
   - The result carries the `idempotency_key`, derived from the Workflow ID
     ([how it's derived](../README.md#architecture)), and
     `activity_attempt: 2`.
   - That `ActivityTaskStarted` event's identity is the new Worker's
     `<pid>@refund-demo`, with a different PID from the first Worker's earlier
     events.
   - After the refund completes, you may see a `WorkflowTaskTimedOut` event
     (schedule-to-start). Temporal first offered the next Workflow Task to the
     killed Worker and stopped waiting after 10 seconds. The next
     `WorkflowTaskStarted` shows the new identity.
   - While the stage's last frame is up, a `progress` Query returns
     `phase: "completed"`.

Each Activity row carries a plain-words summary:

- `Agent turn N: decide the next step`, for each agent turn
- `Look up order 1234`
- `Look up the customer's refund history`
- `Check the refund policy`
- `Recall past cases from long-term memory`
- `Issue the refund in Stripe`, scheduled before the kill and completed after
  recovery (offline too, where the ledger stands in for Stripe)
- `Save a case note to long-term memory`, after the refund

A few more things show in the Web UI:

- Every client the demo starts reports `<pid>@refund-demo` instead of the SDK
  default `<pid>@<hostname>`, so the machine's hostname never appears on the
  Workers tab or in event identities.
- To replace that identity, set `TEMPORAL_IDENTITY`. The value is used
  verbatim, so a Worker restart no longer shows a new PID.
- The Workflow's summary also shows its Event History size, which
  [When Event History grows](HISTORY_GROWTH.md) uses.

![Temporal Web UI showing the running RefundApprovalAgent Workflow and the result of a stage_progress Query, recorded before that Query was renamed to progress: phase ready_to_refund, then the two customer_answer entries in working memory](../assets/temporal-workflow-state.png)

### From the CLI

These commands work even when no Worker is running. For the port variant, add
`--address localhost:7243`.

```bash
temporal workflow describe --workflow-id nyghtowl-take-01
temporal workflow show --workflow-id nyghtowl-take-01
```

- `show` prints the Event History. Besides the events above, look for
  `WorkflowExecutionSignaled` when a human approves. The `issue_refund`
  `ActivityTaskStarted` event shows attempt 2 after the main run's recovery
  and after `--simulate-stripe-timeout`, and attempt 3 after
  `--simulate-stripe-retry`.
- `describe` also prints the history length and size.
  [When Event History grows](HISTORY_GROWTH.md) says why that matters in an
  agent loop.

## Manual walkthrough

The stage is the path the talk uses, and it runs everything for you in one
terminal. This walkthrough runs each process and each recovery step
separately, so you can watch one piece at a time. Use it for development,
rehearsal, debugging, or a longer technical session. It is also where the
later-loss case lives: a crash after Stripe accepts the refund.

Both demos run the same loop (`run_agent_loop` in `agent_loop.py`): the
questions the agent needs, then the lookups it picks, then `issue refund`. In Demo 1, the loop's position exists only in its
process. In Demo 2, the answers arrive as Signals and the lookups run as
Activities, and Event History records both. The next action is Workflow
state, which replay rebuilds from those events. So a new Worker can pick up
the loop from Event History and resume without repeating questions.

### Setup and configuration

You need Python 3.11 or newer, the [Temporal CLI](https://docs.temporal.io/cli),
and [uv](https://docs.astral.sh/uv/). Then run:

```bash
uv sync --extra dev --extra tui
test -e .env || cp .env.example .env   # never overwrites an existing .env
```

Fill in only the services you use. The Worker and CLI load `.env`
automatically, and exported shell values take precedence. Configuration is
never loaded into deterministic Workflow code.

**The model and the refund are independent.** Whether a run calls a
model and whether it touches Stripe are separate choices:

- `refund-demo start` is live by default: it calls the model and Stripe test
  mode, and it needs a model key, a model name, and `STRIPE_API_KEY`. If one is
  missing, it prints `THE SYSTEM | a live run needs ...` and starts nothing.
  `--offline` (or `--dry-run`) uses the fixed policy and the offline ledger,
  with no model calls and no Stripe. `start` has no option to mix the two.
- The stage uses a model by default when the keys are set (it asks for
  missing ones), with `--real-model`, and never with `--offline`. `--real`
  alone uses Stripe test mode with the fixed policy.
- `AGENT_MODEL_PROVIDER` selects `anthropic` or `openai`, and the CLI option
  `--model-provider` records that choice in the Workflow input. Offline is
  recorded there too, so a new Worker can't switch to a model because a key
  happens to be set.
- For the refund, offline runs write to an offline Stripe-like ledger
  (`refund-ledger.json` under `DEMO_STATE_DIR`). Live runs call Stripe test
  mode and require a `sk_test_` or `rk_test_` key; live Stripe keys are
  rejected.

| Variable | Purpose |
| --- | --- |
| `AGENT_MODEL_PROVIDER` | Selects `anthropic` or `openai` when using a live model |
| `ANTHROPIC_API_KEY` | Enables Claude model reasoning |
| `ANTHROPIC_MODEL` | Claude model ID used when Anthropic is selected |
| `OPENAI_API_KEY` | Enables OpenAI model reasoning |
| `OPENAI_MODEL` | OpenAI model ID used when OpenAI is selected |
| `STRIPE_API_KEY` | Stripe test key required by `--real` |
| `REFUND_RESTART_WINDOW_SECONDS` | Demo-only Worker hook: after Stripe accepts the refund, holds the attempt open this many seconds for a Worker kill ([later loss](#later-loss-after-stripe-accepts-the-refund)) |
| `STAGE_HOLD_REFUND`, `STAGE_SIMULATE_STRIPE_TIMEOUT` | Demo-only Worker hooks the stage sets on its own Workers: hold the refund before Stripe, or simulate a Stripe API that never answers. Leave them unset |
| `LOG_MODEL_USAGE` | Set to `1` to log each live model call's tokens to `model-usage.jsonl` in `DEMO_STATE_DIR`.<br>Summarize them with `uv run refund-demo usage` |
| `TEMPORAL_ADDRESS` | Temporal endpoint, default `localhost:7233` |
| `TEMPORAL_NAMESPACE` | Temporal Namespace, default `default` |
| `TEMPORAL_TASK_QUEUE` | Worker Task Queue, default `refund-demo` |
| `TEMPORAL_IDENTITY` | Client identity shown in the Web UI, used verbatim by every process.<br>When it's unset, each process reports `<pid>@refund-demo` instead of the SDK's `<pid>@<hostname>` |
| `DEMO_STATE_DIR` | Offline ledger, logs, and local state, default `.demo-state` |

### Start Temporal and the Worker

Terminal 1:

```bash
mkdir -p .demo-state
temporal server start-dev --db-filename .demo-state/temporal.db
```

The Web UI is at <http://localhost:8233>. If another Temporal server is
already on `:7233`, use that server and skip the start command.

Terminal 2:

```bash
uv run refund-worker
```

Python does not reload a running Worker. Restart it after changing Workflow or
Activity code.

### Demo 1 by hand

Run the two-pane agent process:

```bash
uv run naive-refund
```

The left pane, `AGENT PROCESS`, shows the agent loop inside the process. The
right pane, `WHAT SURVIVES`, shows the paid order and the refund state, under
the heading `OFFLINE LEDGER (Stripe stand-in)`. This standalone view is a
fixture that keeps its ledger in the process and never refunds.

1. Press Enter or type `refund`. The standalone view condenses the agent's two
   questions, two answers, and two lookups into its completed-loop checklist,
   while the stage asks the questions one at a time.
2. Type `restart` (`deploy` and `oom` do the same). The pane becomes
   `NEW AGENT PROCESS`: "No answers. No next step." Stripe still says paid
   with no refund (`FRESH START`).
3. Ask, `What happened to my refund?`
4. The new agent process checks Stripe and answers correctly. But the customer
   must start the return over, because there is no active execution to
   resume.

Type `reset` to start over, or `quit` to exit.

In `refund-demo stage --real`, step 4 reads Stripe directly. It retrieves the
test PaymentIntent and its refund list inside a fresh agent-process
subprocess. The question crosses stdin and the result crosses structured
stdout, so this is a real process boundary, not a UI-only transition. No
refund is submitted here; the stage's only refund submission comes later, in
Demo 2.

**What this shows.** Stripe's record of the payment survives the crash, but
the agent's working memory and its unfinished work die with the process. A
long-term memory store could bring back the customer's answers. It still
wouldn't say where the loop stood or what to do next, and Stripe's record
doesn't either, because it only knows the payment and its refunds. That
missing piece is execution state. You could build it yourself with a
database-backed state machine and a recovery job. Demo 2 uses Temporal
instead.

To run the stage's agent loop, with its hold before the refund:

```bash
uv run naive-refund reset
uv run naive-refund refund --order 1234 --interactive-loop --hold-before-refund
```

This runs the same `run_agent_loop` as Demo 2, offline with the fixed policy.
For a live model, add `--real-model --model-provider openai` (or `anthropic`).

The command asks its questions on stdin, emits each `AGENT STEP`, and waits
after choosing the refund. It writes no refund to the ledger.

- If the model denies, it prints `no refund issued for order 1234` and exits
  instead of waiting.
- With `--real-model --model-provider openai` and no `OPENAI_API_KEY` (or the
  Anthropic equivalent), it stops with an `AGENT ERROR` line rather than
  falling back to the fixed policy.

The older post-refund boundary is still available for technical comparison.
Demo 1 refunds through the same `refund_payment` call as Demo 2's
`issue_refund` Activity, into the same offline ledger (`refund-ledger.json`),
keyed by its own request ID:

```bash
uv run naive-refund refund --order 1234 --exit-after-refund
uv run naive-refund ledger    # prints: offline ledger, Demo 1 refunds: N
```

`uv run naive-refund reset` clears Demo 1's refunds from the offline ledger
and its completion records, and leaves Demo 2's refunds alone.

### Demo 2 by hand

With Temporal and the Worker running, start an offline refund and wait for it:

```bash
uv run refund-demo start --offline --workflow-id demo-refund
uv run refund-demo result demo-refund
```

`start` carries both intake answers in the request, so the agent asks the
customer nothing. Without `--offline`, `start` runs live and needs the model
and Stripe keys. The Worker prints each model-reasoning turn, retrieved
memory, the decision, and execution-state transitions.

If the agent escalates, approve it:

```bash
uv run refund-demo approve demo-refund "verified defect, approving"
uv run refund-demo result demo-refund
```

An escalation waits 24 hours for that approval. If none arrives, the request
expires: no refund is issued, and `refund-demo result` shows status `expired`.

Low-value requests tend to approve automatically. To push the deterministic
policy toward escalation, use a larger amount, such as `--amount-cents 15000`.

To stop a Workflow you no longer want, cancel it:

```bash
uv run refund-demo stop demo-refund
```

Cancel lets the Workflow's code decide how to stop. Before the refund, the run
closes as Cancelled, with nothing sent to Stripe. During the refund, the
Workflow finishes the Stripe call first and records the outcome, so a cancel
never leaves a refund half done. A Worker must be running to handle the
cancel; for a run no Worker will pick up, add `--terminate`, which closes it
at once without running Workflow code.

`refund-demo start` builds the Workflow ID from `--request-id` (for example,
`--request-id refund-123` gives `refund-refund-123`). Temporal refuses a second
run for a request that is still running or already succeeded, so starting the
same request twice prints `already has a Workflow` instead of starting another.
A request whose run failed or was cancelled can be started again.

**Stripe test mode.** Put a test key in `.env`:

```text
STRIPE_API_KEY=sk_test_...
```

Then seed a succeeded test charge and refund it. This is the live default, so
it also needs a model key and model name:

```bash
uv run refund-demo start --seed --workflow-id stripe-refund-demo
uv run refund-demo approve stripe-refund-demo "approved"   # if it escalates
uv run refund-demo result stripe-refund-demo
uv run refund-demo cleanup   # refunds any leftover seeded charges
```

This runs in Stripe test mode only. It never creates a live charge, and it
rejects live keys.

### Later loss: after Stripe accepts the refund

The stage stops the agent before the refund. Here the failure lands later:
Stripe accepts the refund, then the Worker dies before the Activity reports
it. At that point only Stripe knows the refund went out, and the process
can't know whether money moved.

This matters because an Activity that was in flight at a crash can run again,
so its call must be safe to send twice.

**In one terminal,** the stage runs the whole case for you:

```bash
uv run refund-demo stage --real --simulate-stripe-retry      # Stripe test mode, fixed policy
uv run refund-demo stage --offline --simulate-stripe-retry   # rehearse offline
```

Like the main path, this first holds `issue_refund` attempt 1 at the Worker
and kills that Worker before Stripe.

1. The runner starts a Worker whose restart window
   (`REFUND_RESTART_WINDOW_SECONDS`) keeps the next attempt open after Stripe
   accepts it, and waits until Stripe accepts attempt 2.
2. It kills that Worker before the Activity reports completion.
3. It pauses with the retry unresolved.
4. Press Enter to start a new, plain Worker.
5. Attempt 3 uses the same Stripe idempotency key and returns the same refund.

Event History compacts the failures, so it shows no separate
`ActivityTaskTimedOut` events. Instead, after recovery, the
`ActivityTaskStarted` event carries `attempt: 3` and a `lastFailure` heartbeat
timeout.

**In four terminals,** you can run the complete real Stripe path:

```bash
# Terminal 1: Temporal
temporal server start-dev --db-filename .demo-state/temporal.db

# Terminal 2: a Worker whose demo-only hook holds the uncertain boundary open
REFUND_RESTART_WINDOW_SECONDS=30 uv run refund-worker

# Terminal 3: two-panel view
uv run refund-demo watch demo-restart

# Terminal 4: start a seeded Stripe test refund (live: needs the model keys too)
uv run refund-demo start --seed --workflow-id demo-restart
uv run refund-demo approve demo-restart "approved"   # if it escalates
```

Wait for these lines in Terminal 2:

```text
THE SYSTEM | refund accepted at Stripe: re_... (stripe-test, attempt 1)
EXECUTION STATE | restart window open 30s; run: refund-demo kill-worker
```

Kill the Worker and inspect the ambiguous boundary from Terminal 4:

```bash
uv run refund-demo kill-worker
uv run refund-demo inspect demo-restart
```

Stripe already has the refund, but Temporal does not yet have a completed
Activity result.

Restart the Worker in Terminal 2. Either command works: the hook opens the
restart window only once per run, because attempt 2 sees in attempt 1's last
heartbeat that Stripe already accepted the refund:

```bash
REFUND_RESTART_WINDOW_SECONDS=30 uv run refund-worker   # or: uv run refund-worker
```

Then wait for the result, inspect again, and reconcile the test charge from
Terminal 4:

```bash
uv run refund-demo result demo-restart
uv run refund-demo inspect demo-restart
uv run refund-demo cleanup
```

The final inspection shows that `issue_refund` completed on attempt 2, that
two calls used the same idempotency key, and that one unique refund exists.

**Timing and running it offline.** Keep the restart window under the
5-minute start-to-close timeout of `issue_refund`. After the kill, the next
attempt is ready within the 5-second heartbeat timeout, and the
schedule-to-close budget is 30 minutes, so the Worker can take a while to come
back without losing the run. To run this offline, replace `--seed` with
`--offline`. Without the restart window, the run completes with no later loss.

**Why there is still one refund.** Two calls reached Stripe, but only one
refund exists. Stripe's idempotency support stops a repeated call from
creating a second refund, and both attempts in one run send the same key
because it comes from the Workflow run's identity
([how](../README.md#architecture)). Temporal records that the step is
unresolved, schedules the retry after the Worker disappears, and records the
result once an attempt finishes. It doesn't merge its record and Stripe's into
one database transaction ([two owners](#two-owners)); instead, it gives the
uncertain attempt a durable recovery point and a stable identity. One
condition applies: a restarted application can read the result only if it
keeps or derives the same Workflow ID.

### Replay skips finished steps

<a id="the-clean-replay-case"></a>The later loss needs idempotency, because
the first call may have committed without a recorded Activity result. The
common case is simpler: once a step completes and is recorded, replay returns
its result without running it again. Two stage runs show both cases:

- **The main stage run** (`uv run refund-demo stage --offline`) shows replay
  skipping finished steps. After the kill, the new Worker runs the Workflow
  code again from the top, but every finished `agent_decide_next_step` and
  lookup returns its recorded result, and every earlier `customer_answer`
  Signal is read back from Event History. The Worker log shows no model or
  lookup output from the new Worker
  ([Worker log](#worker-log)), and the History tab shows no new
  `agent_decide_next_step`, lookup, or `customer_answer` events
  ([what to check](#what-to-check-at-each-frame)). The only new work is
  `issue_refund` attempt 2 and `remember_case`.
- **The retry drill** (`uv run refund-demo stage --offline
  --simulate-stripe-retry`) shows one refund across attempts. Stripe accepted
  attempt 2 before its Worker died, so attempt 3 had no recorded result to
  replay and ran again, with the same idempotency key. The ledger pane shows
  `2 CALLS → 1 REFUND`.

The idempotency key is the backstop only for the boundary where the result was
not recorded.

### Two-panel view

```bash
uv run refund-demo watch demo-restart
```

The left panel is the agent's live view, read with the Workflow's `progress`
Query. It shows the current context, the looked-up records and recalled
memory copied into working memory, and the decision. A Query needs a running Worker, so when the
Worker disappears, the left panel reads `LOST`, and when a new Worker resumes,
replay fills it in again.

The right panel keeps five things apart: the execution state Temporal records,
the refund record owned by Stripe or the offline ledger, the pending Activity
attempt, the refund ID and call count, and the unique refund count.

For a talk, make the terminal fullscreen. Press Ctrl+C to exit.

### Recovery notes

- `result` reads and waits; it does not drive the Workflow. If `result` hangs,
  see [Troubleshooting](#troubleshooting).
- Use a fresh Workflow ID for each refund request, and stop a stale run
  with `refund-demo stop <id>`. The idempotency key comes from the Workflow
  ID, so a rerun under the same ID gets the earlier refund back instead of a
  new one. It never uses the Run ID, which changes with each run, so every run
  in a Continue-As-New chain sends the same key too.
- Finish or terminate running demo Workflows before you upgrade the code. The
  repository carries no compatibility code for runs started by older code.
- To see a manual run's Event History, use [the CLI commands](#from-the-cli)
  with its Workflow ID, such as
  `temporal workflow show --workflow-id demo-restart`.

## Troubleshooting

Errors from `refund-demo stage` start with `STAGE |`. Workflow and Activity
failures show up in the Web UI and in `.demo-state/stage-<token>/worker.log`.
The first five rows are the most likely.

| You see | Cause | Fix |
| --- | --- | --- |
| ``STAGE \| Temporal is not reachable and the `temporal` CLI is not on PATH`` | Nothing answers at `TEMPORAL_ADDRESS`, and the stage can't start a server | Install the Temporal CLI, or run `temporal server start-dev` first |
| `STAGE \| Temporal dev server exited while starting:` plus a log tail | Usually port 7233 or 8233 is already in use | Run `lsof -nP -iTCP:7233 -iTCP:8233 -sTCP:LISTEN`, then use the port variant in [Start your own dev server](#start-your-own-dev-server) |
| `STAGE \| Worker exited while starting:` plus a log tail | The Worker crashed at startup.<br>A common cause is a live or malformed `STRIPE_API_KEY` in `.env`, which the Worker rejects even in offline mode | Read the tail.<br>Use a `sk_test_` or `rk_test_` key, or remove the key |
| `The stage view needs rich. Install it with: uv sync --extra tui` | Running `uv sync` without `--extra tui` removed the optional Rich extra | Run `uv sync --extra dev --extra tui` |
| `STAGE \| the live run needs ..., and there is no terminal to ask.` (exit code 1) | The default live run is missing a key, and nothing can answer the key prompt, as when you pipe input in or run in CI | Set the keys in `.env`, or pass `--offline` |
| `WorkflowAlreadyStartedError` traceback | You reused a `--workflow-id` while that run is still Running | Use a new ID, or run `uv run refund-demo stop <id>` (add `--terminate` if no Worker is running) |
| `STAGE \| could not connect to configured Temporal service <address>` | `TEMPORAL_ADDRESS` names a non-local host that isn't answering.<br>The stage won't silently fall back to localhost | Fix or unset `TEMPORAL_ADDRESS` |
| `STAGE \| Temporal dev server did not become ready` | The started server didn't answer within 20 s.<br>This also happens when `TEMPORAL_ADDRESS` uses a localhost port other than 7233, because the stage starts its server without `--port` | Start the server yourself on the port you configured, or unset `TEMPORAL_ADDRESS` |
| `STAGE \| both live-model keys are configured; choose --model-provider` | Both model keys are set and `AGENT_MODEL_PROVIDER` is blank | Add `--model-provider anthropic` or `--model-provider openai` |
| `STAGE \| --real-model requires ANTHROPIC_API_KEY or OPENAI_API_KEY`, or `STAGE \| anthropic live model requires ANTHROPIC_MODEL` | A live model was requested without its key or model name | Set both in `.env` |
| `STAGE \| --model-provider requires --real-model` | You passed a provider without `--real-model` | Add `--real-model`, or drop `--model-provider` |
| `STAGE \| STRIPE_API_KEY is required for a real refund`, `STAGE \| A live Stripe key was detected and has been rejected`, or `STAGE \| STRIPE_API_KEY must be a Stripe test mode key` | `--real` needs a Stripe test key | Put a `sk_test_` or `rk_test_` key in `.env`, or drop `--real` |
| `STAGE \| Stripe could not create the test payment: ...` or `STAGE \| Stripe could not verify the naive outcome: ...` | Stripe rejected the call or couldn't be reached | Check the key and the network, or run the offline default |
| `STAGE \| timed out waiting for the durable agent loop` | Demo 2's Workflow didn't reach its refund step, held at the Worker before Stripe, within 90 s.<br>With `--real-model`, a model turn usually failed (see the next rows) | Open the Workflow in the Web UI, or read `worker.log` in the newest `.demo-state/stage-*` folder |
| `Anthropic returned no tool call for this turn` (type `AnthropicNoToolCall`) | The model answered in text instead of calling a tool. Demo 2 retries the turn through Temporal | Usually nothing; it passes on a retry. If it repeats, try another `ANTHROPIC_MODEL` |
| `agent did not reach a decision within the turn budget` (type `AgentLoopExhausted`) | The live model used all 10 turns without calling `submit_decision` | Run it again, and check the request text and the model |
| `agent asked for an unknown tool: ...` or `agent asked an unsupported question: ...` | The live model chose a tool or question the loop doesn't allow.<br>These are non-retryable by design | Run it again |
| `LOG_MODEL_USAGE must be 1, true, yes, 0, false, or no` in `worker.log` | `LOG_MODEL_USAGE` has another value.<br>Each live model turn then fails before the paid call, so Temporal's retries make no API calls, and the stage times out | Set it to `1` or unset it |
| `MODEL USAGE \| no usage log at ...` | `LOG_MODEL_USAGE` was off during the run, or `usage` looked in the wrong folder | Pass the `Stage logs:` path as `--state-dir`, or run again with `LOG_MODEL_USAGE=1` |
| `Stripe rejected the refund: ...` (type `StripeRefundError`) | Stripe refused the refund, for example a live `refund-demo start` run against the default `pi_dry_run_demo` | Use `refund-demo start --seed`, or pass a real test PaymentIntent with `--payment-intent` |
| `Stripe refund call failed (retryable): ...` (type `StripeRetryableError`) in an `issue_refund` attempt's failure | The refund call timed out (3 s to connect, 10 s of silence while reading), lost its connection, or got a 409, 429, or 5xx that Stripe did not mark `Stripe-Should-Retry: false`.<br>Temporal retries it with the same idempotency key.<br>If Stripe sent a `Retry-After` of 60 s or less, the retry honors it first; a longer one is ignored | Usually nothing. If every attempt fails, check the network and Stripe's status |
| `Could not re-read Temporal.` and `Showing the earlier reading:` in the `WORKER GONE` pane | The Event History read failed or took longer than 3 s.<br>The counts shown are from before the Worker stopped | Check the Temporal server, then open the Workflow's History tab in the Web UI |
| A Query in the Web UI fails or hangs during `WORKER GONE` | Queries need a live Worker | Use the History tab while the Worker is gone |
| `Starting a new Worker. It picks up from Temporal's history...` stays up for several seconds | If you pressed Enter quickly, Temporal is still waiting out the killed refund attempt's 5-second heartbeat timeout before it schedules attempt 2.<br>After the refund, Temporal may also offer the next Workflow Task to the killed Worker's sticky queue and wait out its timeout (10 s by default in the Python SDK), so a `WorkflowTaskTimedOut` event may appear | Expected; nothing to fix |
| `refund-demo result <id>` hangs | No Worker is polling that Workflow's Task Queue | If you started it with `refund-demo start`, run `uv run refund-worker`.<br>A stage Workflow uses a private `refund-stage-<token>` Task Queue that only the stage's own Worker polls |
| `THE SYSTEM \| cleanup done, refunded $0.00 of demo charges` | There was no outstanding demo test payment | Not an error |
