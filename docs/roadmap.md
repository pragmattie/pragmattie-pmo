# PragMattie PMO roadmap from P3

Informed by generally accepted project management and AI governance practice. Every name below is
our own. Approved by Geoff on 2026-11-02. Each milestone's items become specs, which Geoff approves
before any code is written. P1 Foundation and P2 Login and roles are complete.

## The project lifecycle

A project moves through four stages we already use in the code: **intake, planning, delivery and
closure**, then closed. A **gate** sits between each pair, and only a person passes a gate (R1).

| Gate | Between | The question it answers |
| --- | --- | --- |
| Approval | intake and planning | Is this worth doing, and is it ready to plan? |
| Go-ahead | planning and delivery | Is the plan, price and risk acceptable to start work? |
| Acceptance | delivery and closure | Did we deliver what was agreed? |
| Close-out | closure and closed | Are the lessons and the final record complete? |

## Our names

| We call it | What it holds |
| --- | --- |
| Project request | What someone asks for, before anyone has agreed to it |
| Value case | Why it is worth doing: expected value, how it is measured, what it costs |
| Readiness check | R11 for AI work: problem worth solving, AI the right tool, data good enough; green, yellow or red |
| Project brief | What the approval gate creates: purpose, objectives, sponsor, project manager, scope, rough dates and budget, and who decides what |
| Stakeholder list | Who has an interest, and what they need to hear |
| Assumptions list | What we are taking as true, with an owner to confirm each |
| Plan | Scope, milestones, estimate, who does what, and the reporting plan |
| Change request | A proposed change to scope, dates or cost, with a decision |
| Proposal | The client-facing offer, drafted from the plan |
| Status report | Each period's picture of a project, every number traced to its source |
| Closure report | What was delivered against the value case, and the lessons |
| Risk register | Threats and opportunities, AI and ordinary, scored by `policies/risk.yaml` |

## Milestones

**P3 Risk register.** First, because intake, proposals, status reports and gates all read risks.
3.1 the risk model, scored by the existing `risk.yaml` scales, limits and bands. 3.2 a starter list
of AI risks (bias, drift, made-up answers, over-reliance, personal data, runaway cost, misuse) and
ordinary project risks. 3.3 the register screen with a heat map and totals. 3.4 escalation: a risk
in a band goes to that band's owner, and only a person accepts a risk (R9 keeps ethical and
reputational risks away from agent scoring). 3.5 the risk agent: proposes risks from a project's
data, and a person accepts or rejects each.

**P4 Intake and the approval gate.** 4.1 project requests and their queue. 4.2 the value case.
4.3 the readiness check (R11). 4.4 the gate framework in `gates.yaml`, with the approval gate as its
first gate. 4.5 the intake agent: drafts the value case and readiness check under a strict schema;
a person decides. 4.6 passing the approval gate creates the project brief, stakeholder list,
assumptions list and first risks.

**P5 Planning, proposals and the go-ahead gate.** 5.1 the plan. 5.2 change requests. 5.3 the
proposal: a drafter agent writes it from the plan, a checker ties every figure to the estimate, the
project manager edits, the sponsor approves (never below T2), and a person sends it; no agent sends
anything. 5.4 the go-ahead gate.

**P6 Delivery and closure.** 6.1 status reports: the agent drafts with a source for every number,
the project manager publishes, sponsors see only published ones. 6.2 executive and detailed views.
6.3 overdue and unread flags. 6.4 the acceptance and close-out gates, the closure report and lessons,
which feed the next intake's risk suggestions.

**P7 Portfolio and oversight.** 7.1 the portfolio dashboard: health, budget, risk and gate status
across projects. 7.2 PragMattie Sync as the live project, imported from its exported record. 7.3 the
agent registry: each agent's purpose, limits, owner, mode, run history and an off switch (R2, R4).
7.4 one decision log across intake, risks, gates and reports. 7.5 the simulated ethics reviewer seat
on gates for high-impact AI work, always labelled simulated.

**P8 Demo.** 8.1 the standards lens (each screen shows the rules it follows). 8.2 reset and
pre-demo check. 8.3 the demo script, rehearsal and recorded backup. 8.4 the consulting-offer slide.

**Later, not planned:** an MCP server, billing and invoices, forecasting dates with simulations,
and work-item intake into running projects.
