# Agents

Each product agent proposes; a person decides, and the record shows who. Each agent's limits are
written here and enforced in its code. The agent registry (milestone P7) will hold this later.

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
