# Agents

Each product agent proposes; a person decides, and the record shows who. Each agent's limits are
written here and enforced in its code. The agent registry (milestone P8) will hold this later.

## Risk agent

Code: `apps/api/app/agents/risk_agent.py`. Run by `POST /api/v1/projects/{id}/risks/suggest`
(`project:write`), from the **Suggest risks** button on the Risks page.

**What it reads.** One project's name, stage, status and client (name and industry); the
project's current risks (title, kind, category and status); the starter risk library; and the
probability and impact scales from the risk policy. It never sees people's names or email
addresses. Project text is passed to the model inside tagged data blocks that the prompt marks as
data, not instructions, and `<`, `>` and `&` in it are escaped so it can't close a block.

**What it may write.** Only in `AGENT_MODE=enforce`: new risks with status `proposed`,
`source = agent`, no probability, impact, score or band, and a response plan that carries the
agent's reason and suggested scores for the person who decides. Each saved risk gets an audit row
(agent `risk_register`, action `create`, `by: risk_agent`). In `shadow` it saves no risk.

**What it never does.** Open, reject, accept, score or close a risk; change an existing risk; send
anything outside the system; or run on a schedule. A person opens or rejects each proposal.

**When it stops and asks.** A risk it marks ethical or reputational comes back unscored with
`needs_judgement`, whatever probability and impact the model suggested: a person scores it.

**Modes.** `off` refuses (409) and calls nothing. `shadow` calls Claude and returns the proposals
without saving them. `enforce` saves them as proposed risks. `?dry_run=true` returns the exact
request and a cost ceiling and calls nothing, in `shadow` or `enforce`.

**Model.** `RISK_AGENT_MODEL`, default `claude-sonnet-5-5`. One call per run, with the JSON
returned through structured outputs (a strict JSON schema); no sampling parameters are sent. The
key comes only from `ANTHROPIC_API_KEY`.

**Caps, enforced in code.**

- At most 5 proposals; the rest are dropped.
- `RISK_AGENT_MAX_TOKENS` output tokens (default 4096).
- Probability and impact clamped to 1 to 5.
- Dropped: anything outside the schema; a kind, category, objective or response outside the risk
  vocabulary, or a response that doesn't fit the kind; a title already on the project (any
  status) or repeated within the run; text over its length limit.
- Cost ceiling for a dry run: one input token per byte of the serialized request, plus
  `max_tokens` of output, at `RISK_AGENT_INPUT_USD_PER_MTOK` and `RISK_AGENT_OUTPUT_USD_PER_MTOK`
  (defaults $2 and $10 per million tokens), rounded up to the cent.

**Record.** Every run that calls Claude writes one `pmo_audit` row, agent `risk_agent`, subject the
project, success or failure: model, prompt version, tokens, cost, the proposals, what code dropped
and why, what it adjusted (clamped scores, judgement risks left unscored) and the ids of risks it
saved. A failed run (an API error, output that isn't valid JSON or stopped early) is a 502, saves
no risk and records the error. A dry run or a refused run calls nothing and records nothing.

## Intake agent

Code: `apps/api/app/agents/intake_agent.py`. Run by `POST /api/v1/requests/{id}/draft`
(`project:write`, a `draft` request only), from the **Draft with the agent** button on a request.

**What it reads.** One draft request's text (title, what was asked for, the problem, the outcome
and known risks), its client's name and whether it uses AI. It never sees people's names or email
addresses. All of it goes to the model inside one `<request>` data block that the prompt marks as
data, not instructions; it is JSON-encoded and `<`, `>` and `&` in it are escaped so it can't
close the block.

**What it may write.** Only in `AGENT_MODE=enforce`: the request's value case (benefits with
measures and targets, a cost range and how it was estimated, strategic fit) and, for a request
that uses AI, its readiness check (a rating and note for problem, approach and data, and actions
with no owner or date). Each is saved with `source = agent` and no confirmation, and replaces only
a section the request doesn't have yet or one no person has confirmed; a confirmed section is
left alone and named as skipped. In `shadow` it saves nothing.

**What it never does.** Confirm a section; rate an area green; give an action an owner or a date;
submit, withdraw or decide on a request; send anything outside the system; or run on a schedule.
An agent's draft counts only once a person saves it, which confirms it; a person's save needs an
owner and a due date on every action, and a request can't be submitted until then.

**When it stops and asks.** A green rating is saved as yellow with a note that a person must rate
it. An ethical, legal or reputational concern is named in that area's note (or the value case's
strategic fit) with a note that a person must judge it; the agent is told never to turn one into
a number or a cost.

**Modes.** `off` refuses (409) and calls nothing. `shadow` calls Claude and returns the proposal
without saving it. `enforce` saves it as above. `?dry_run=true` returns the exact request and a
cost ceiling and calls nothing, in `shadow` or `enforce`.

**Model.** `INTAKE_AGENT_MODEL`, default `claude-sonnet-5-5`. One call per run, with the JSON
returned through structured outputs (a strict JSON schema); no sampling parameters are sent. The
key comes only from `ANTHROPIC_API_KEY`.

**Caps, enforced in code.**

- At most 10 benefits and 10 actions per area; the rest are dropped.
- `INTAKE_AGENT_MAX_TOKENS` output tokens (default 4096).
- Money: a decimal string of at least 0 with at most 2 places that fits the column, saved with 2
  places; the low end not above the high end, and both or neither. Otherwise the cost range is
  dropped and a person fills it in.
- Dropped: anything outside the schema (an unknown key, a benefit without a description, an area
  with a rating, note or concern outside the vocabulary, text over its length limit), and a
  readiness check for a request that doesn't use AI.
- Cost ceiling for a dry run: one input token per byte of the serialized request, plus
  `max_tokens` of output, at `INTAKE_AGENT_INPUT_USD_PER_MTOK` and
  `INTAKE_AGENT_OUTPUT_USD_PER_MTOK` (defaults $2 and $10 per million tokens), rounded up to the
  cent.

**Record.** Every run that calls Claude writes one `pmo_audit` row, agent `intake_agent`, subject
the request, success or failure: the data it read, model, prompt version, tokens, cost, the
proposal, what code dropped and why, what it changed (green saved as yellow, concerns left to a
person) and which sections it saved or skipped. The row shows in the request's history. A failed
run (an API error, output that isn't valid JSON or stopped early) is a 502, saves nothing and
records the error. A dry run or a refused run calls nothing and records nothing.

## Proposal agent

Code: `apps/api/app/agents/proposal_agent.py`. Run by `POST /api/v1/projects/{id}/proposal/draft`
(`project:write`, a `draft` proposal only), from the **Draft with agent** button on a proposal.

**What it reads.** One proposal's project: its name, its client's name, its brief (purpose,
objectives, scope in and out), its assumptions that aren't known to be wrong, its plan (scope in
and out, estimate line labels, milestones and deliverables) and the risks the proposal shows
(title and response only). The plan's structured values (the total, estimate line amounts and
milestone dates) reach it only as tokens, never as values. Free text from the brief and the plan
(purpose, objectives, scope, assumptions, descriptions) goes to it as written, so it may contain a
figure or a date; anything of that kind the agent copies into its draft is removed by the rule in
"Figures through tokens" below. It never sees people's names or email addresses, a risk's score
or band, or the brief's budget and dates.
All of it goes to the model inside one `<project>` data block that the prompt marks as data, not
instructions; it is JSON-encoded and `<`, `>` and `&` anywhere in it (the brief, the client's
name, risk titles, estimate line labels) are escaped so it can't close the block.

**What it may write.** Only in `AGENT_MODE=enforce`: the proposal's text sections (summary, scope
in, scope out, assumptions and terms). Each is saved as `source = agent`, unreviewed, and shows
"Agent draft, not reviewed" until a person edits it (it becomes a person's) or confirms it as it
is (`POST .../proposal/sections/{section}/confirm`, `draft` only; who and when are recorded and
audited). A draft replaces only a section that is empty or still an unreviewed agent draft; a
section a person edited or confirmed, or one prefilled from the plan, is kept, and the answer
names what was replaced and what was kept. Refreshing from the plan never changes who wrote a
section. In `shadow` it saves nothing.

**Figures through tokens.** The agent writes `{total}`, `{line:N:amount}`, `{milestone:N:name}`,
`{milestone:N:date}` or `{deliverable:N}`, and code puts in the plan's value (`$1,650.00`,
`1 March 2027`); a token in doubled braces, such as `{{total}}`, is filled in the same way with no
braces left over. Any other token, and every date or amount in the agent's own text, is removed and
recorded, even one that matches the plan. That is wider than the proposal checker reads: as well
as its forms, a month and year ("March 2027"), a day and month ("30 June", "June 30"), a numeric
date ("15/03/2027", "03-15-2027"), a quarter ("Q3 2027", "Q3"), a year on its own ("in 2027"), and
a number next to a currency word, code or sign either way round ("50000 dollars", "USD 50000",
"$40k", "40k"). Counts such as "3 workshops" or "2 phases" stay. The checker itself is unchanged;
it then runs on the result and its flags come back with the draft.

**What it never does.** Change the price, the estimate lines, the milestones, the deliverables or
the risks shown; write about risks (the proposal shows them from the register, so it can't restate
one or its score); submit, approve, confirm or mark anything sent; choose a price; or run on a
schedule. Submit is refused (409, naming the sections, audited) while any section is still an
unreviewed agent draft.

**Modes.** `off` refuses (409, audited) and calls nothing. `shadow` calls Claude and returns the
draft, with the checker's flags on the proposal as it would be, without saving it. `enforce` saves
it as above. `?dry_run=true` returns the exact request and a cost ceiling and calls nothing. A
proposal that isn't a draft is refused (409, audited).

**Model.** `PROPOSAL_AGENT_MODEL`, default `claude-sonnet-5-5`. One call per run, with the JSON
returned through structured outputs (a strict JSON schema); no sampling parameters are sent. The
key comes only from `ANTHROPIC_API_KEY`.

**Caps, enforced in code.**

- `PROPOSAL_AGENT_MAX_TOKENS` output tokens (default 4096).
- A section longer than 2000 characters once its tokens are filled in, or left empty once figures
  are removed, is dropped.
- Dropped: anything outside the schema (an unknown key, a section that isn't text or null).
- Cost ceiling for a dry run: one input token per byte of the serialized request, plus
  `max_tokens` of output, at `PROPOSAL_AGENT_INPUT_USD_PER_MTOK` and
  `PROPOSAL_AGENT_OUTPUT_USD_PER_MTOK` (defaults $2 and $10 per million tokens), rounded up to the
  cent.

**Record.** Every run that calls Claude writes one `pmo_audit` row, agent `proposal_agent`,
subject the proposal, success or failure: the data it read, model, prompt version, tokens, cost,
the draft, what code removed (figures, dates, unknown tokens), filled in (each token and its value)
or dropped, and which sections it replaced or kept. A failed run (an API error, output that isn't
valid JSON, misses a section or stopped early) is a 502, saves nothing and records the error. A dry
run or a refused run calls nothing; a refused run is recorded as a person's refused attempt (agent
`proposal`).

## Orchestrator

Code: `apps/api/app/orchestrator.py`. Run by `python -m app.orchestrator once` (one pass) or
`python -m app.orchestrator run` (a pass every `ORCHESTRATOR_INTERVAL_SECONDS`, default 30, until
Ctrl+C; a pass that fails, say because the database is unreachable, is logged and the loop tries
again at the next interval), or by `POST /api/v1/orchestrator/run` (admin only, `orchestrator:run`), which runs one
pass. It runs locally only. It is not an agent itself: it calls no model, and moves work between
the agents and people as the autonomy policy (`policies/autonomy.yaml`) says.

**What it reads.** Every open request (`draft` or `submitted`), every project in planning or
delivery that isn't cancelled, and their risks, proposals and change requests, as they are now;
the autonomy policy, the risk policy (for a risk's band) and the go-ahead gate's checks. From
these the next-step engine (`next_steps`, rules only) gives each subject's next steps: a step id,
a plain reason ("the plan is incomplete: ...", "the proposal is approved and waiting to be sent"),
how it runs now (`agent`, `window` or `person`) and who or what it waits on. The inbox and the
board use the same function.

**What it may do alone.** Only in `ORCHESTRATOR_MODE=enforce` and `AGENT_MODE=enforce`:

- run an `agent` step through the same code the **Draft with agent** buttons use (the intake,
  risk and proposal agents), with no person as the actor and `trigger = orchestrator`;
- open a `window` step as a pending action with its due time, and run it once the window has
  passed with no hold;
- open one inbox item for a `person` step, for the role the policy names (or, for a risk's
  acceptance, the role it is escalated to), and close it once the step is no longer needed.

A step whose agent isn't built, has no action yet ("no agent does this yet"), is switched off in
the policy, or can't run because `AGENT_MODE` isn't `enforce`, goes to a person instead. So does a
`score_risk` step for a risk marked as needing judgement, and an agent step that already ran once
but is still open. A step already done, pending or in the inbox is not repeated.

An inbox item opened only because the agent couldn't act (switched off, not built, or `AGENT_MODE`
not `enforce`) gives way once it can: the next pass that finds the step's agent available again
cancels the item ("agent available again", with when) and handles the step as normal. Items opened
for any other reason (a person step, a hold, a judgement call, retries used up) stay open.

**What it never does.** Decide a gate, approve, return or mark sent a proposal, record a client's
answer, accept a risk or decide a change request, whatever the policy says: those steps, and any
step no agent may do, always go to a person. It never sends anything outside the product. It
never deletes an inbox item: items close as `done` or `cancelled` with when and why.

**Holding.** `POST /api/v1/orchestrator/actions/{id}/hold` stops a pending action. Only the step's
`who` role may hold it, with a reason of at least 10 characters; the action becomes `held` and the
step an inbox item. The hold is a person's decision, so its audit row has `trigger = human` and
that person as `actor_user_id`. A refusal (wrong role, a short or missing reason, not pending) is
a `rejected` audit row.

A pass doesn't hold the action's row lock while its agent runs: it commits after the cap check,
calls the agent, then re-reads the action and finishes only if it is still `pending`. So a hold
made during the call returns at once. If a person held the action meanwhile, the hold stands: the
agent's changes are rolled back, its run row is written again as `skipped` with
`applied: false` (keeping its tokens and cost, which still count against the daily cap), and the
pass records the run as not applied.

**Modes.** `ORCHESTRATOR_MODE=off` (the default) does nothing. `shadow` records what it would do,
as `skipped` audit rows, and changes nothing: a row is written when a step and subject first
appear, and again only when the reason or route changes. Only shadow rows count here, so
switching from `enforce` to `shadow` records each step once. `enforce` acts as above.

**Caps and retries, enforced in code.**

- At most `ORCHESTRATOR_MAX_AGENT_CALLS` agent runs per pass (default 5, and never more than 20).
- `ORCHESTRATOR_DAILY_CAP_USD` (default 5.00): once the cost of today's (UTC) agent runs with
  `trigger = orchestrator`, summed from the audit rows, reaches it, agent steps wait for the next
  day. The pass records once per day that it stopped for the cap.
- A step waiting for a cap keeps a pending action whose wait reason says which cap. The first
  time its wait reason becomes a cap, the pass writes one `skipped` row with the step and the cap;
  later passes that leave it waiting for the same cap write nothing.
- An agent step that fails is retried at most 3 times, 5 minutes apart; then its action is
  cancelled and the step goes to a person.
- Only one pass runs at a time: a pass takes a lease (15 minutes) first and gives up when another
  pass holds it. It renews the lease before each agent call, so a long pass keeps it. If another
  pass has taken the lease over, the pass stops: before a call, it records the step as `skipped`
  and leaves it to the other pass; after a call, the result is not applied (as for a hold) and the
  action stays pending for the other pass.

**Record.** A pass that does nothing writes nothing. Otherwise each action taken, opened, held,
closed, skipped (in shadow, or left waiting for a cap) or failed is one `pmo_audit` row, agent `orchestrator`,
`trigger = orchestrator`, no `actor_user_id`, subject the step's subject, with the step, the
reason and the policy digest in its signals. An agent run it starts also writes that agent's own
row, with `trigger = orchestrator`. An error on one subject is recorded and the pass carries on.

## Decision inbox

Code: `apps/api/app/inbox.py`, routes `GET /api/v1/inbox` and `GET /api/v1/inbox/count`
(`project:read`), page **Needs you** in the web. It is not an agent and calls no model: it shows
the decisions the orchestrator has left waiting on a person, and decides nothing itself.

**What it lists.** The signed-in person's open inbox items (a `person` step, a held window action,
an agent step whose retries are used up) and pending window actions, oldest first, paged. An item
waits on a role; where its project names someone for that role (its sponsor or project manager),
only that person sees it. A draft request goes to the project manager who raised it, or to any
project manager when someone in another role raised it. The ethics reviewer and admin are never
named on a project, so they see their role's items. An admin or auditor decides nothing, so their
inbox is always empty and the page says so.

**A decided item leaves at once.** Before listing, each item's subject is read again through the
orchestrator's own rules (`subject_steps`, the same rules as `next_steps`); an item whose step is
no longer needed is left out. The next pass closes it, as before.

**The case.** Each item shows its step's plain reason and, when it went to a person instead of an
agent, why ("no agent does this yet", "the risk_agent is switched off", "held by <name>: <reason>",
"the intake_agent failed 4 times"), in the words its audit row uses. Then the facts the decision
rests on, read from the subject now (a gate's checks, a risk's score and band, a proposal's
checker flags, a change's effect, a draft's gaps), and a one-line recommendation written by code
from those facts: one function per step, in `RECOMMENDERS`.

**Deciding.** Each item opens the existing form for its step inside the inbox (the gate decision,
the risk's accept, open and reject, the proposal section, the change requests), which calls the
existing route with its own checks; a 403 or 409 shows as it does elsewhere. A window action shows
"happens at HH:MM unless you hold it", and only the step's role gets the Hold button, which calls
the orchestrator's hold route. The menu's count refreshes every 15 seconds while the tab is
visible, and at once when it becomes visible again.
