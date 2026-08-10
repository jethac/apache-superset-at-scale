# What Nubank actually did, and what this repository does

Cognition's flagship customer story is Nubank. It is also the origin of nearly
every number about Devin in circulation, which means most of those numbers have
one source. This document states what the sources say, who is saying it, where
the deployment in this repository has the same structure, and where it does not.
The purpose is to be able to talk about the resemblance without borrowing
Nubank's numbers, because the numbers are not ours and the scale is not
comparable.

## 1. What the sources say

**The work.** Nubank had a monolithic ETL repository of roughly six million
lines and over 100,000 data sets to migrate. Cognition's case study describes
each unit of that migration as "far too complex to be scriptable, but
high-volume enough to be a significant manual effort"
([devin.ai/customers/nubank](https://devin.ai/customers/nubank), published
approximately 2024-12-10). That sentence is the whole reason an agent was the
right instrument: scripting fails on the per-unit judgment, and humans fail on
the volume.

**The claimed efficiency figure.** "8-12x efficiency gains" is defined in that
same case study as engineering hours to complete a data-class migration manually
versus engineering hours spent *prompting and reviewing* Devin on the same task.
It is engineer-time per task. It is not calendar time, and it is not money.
Nubank's CTO Vitor Olivier states "8 to 12X speed in our ability to deliver" on
video in Cognition's LinkedIn post of 2024-12-10. Nubank's own engineering blog
repeats a "12-fold increase in efficiency"
([building.nubank.com](https://building.nubank.com/enhancing-engineering-workflows-with-ai-a-real-world-experience/),
2025-02-17, written up from a Clojure Conj talk by Carin Meier and Marlon
Silva). So the efficiency figure has customer corroboration, in the customer's
own voice, on the customer's own channel.

**The claimed cost figure.** "Over 20x cost savings" is Cognition-voiced only.
It is defined as Devin compute cost versus an engineer's hourly cost on the same
task, and it applies only to the scope that was delegated to Devin. No Nubank
source repeats it. It is a single-source vendor figure and should be described
that way whenever it is mentioned.

**The shape of the work.** Cognition's framing is "an army of Devins to tackle
subtasks in parallel", with "a human kept in the loop just to manage the project
and approve Devin's changes". The most concrete description of the human's job
comes from Nubank Senior PM Jose Carlos Castro, who says engineers "could just
review Devin's changes, make minor adjustments, then merge their PR"
([devin.ai/customers/nubank](https://devin.ai/customers/nubank)).

**What is not documented anywhere.** The number of concurrent sessions. Whether
the Devin API, playbooks, or wave orchestration were used. Whether any CI gate
stood between a Devin PR and a merge. Whether QA was sampled or exhaustive.
What the escalation process was when a unit failed. None of this appears in the
case study, the LinkedIn video, or the Nubank blog post. Anyone who tells you
the session count or the orchestration mechanism is extrapolating. This document
does not extrapolate; it says unverified and leaves it there.

**Comparable Cognition case studies**, for calibration:

| Customer | Work | Reported outcome |
| --- | --- | --- |
| AngelList | Redshift to Snowflake, 14,000 Metabase cards; five composable migration tools built first, then 20 Devin agents in parallel, each owning one collection | three weeks; claimed 5.2x |
| Ramp | feature-flag cleanup | ~50 flags removed in a month |
| Gumroad | general development | "1,500+ merged PRs" |
| FE fundinfo | fleet-wide work | 1,800 repositories |

AngelList is the most useful of these, because it is the one that documents the
sequence: tooling first, fan-out second.

## 2. Where this repository has the same structure

**Decomposition into small, individually reviewable, mechanically verifiable
units.** Nubank's unit was one data class. Here the unit is one triggering
GitHub event — an issue, a failing check run, a code-scanning alert — routed by
`scope.yaml` into a work stream. The property that matters is not smallness for
its own sake; it is that a reviewer can hold one unit in their head and that
something other than a human opinion can tell you whether it worked. For a lint
regression or a failing spec, the verification is the check run.

**One pull request per unit, human reviews and merges.** Castro's sentence —
review, minor adjustments, merge — describes the loop this repository
implements. `orchestrator.py` starts one Devin session per admitted task via the
Devin API, and the collector reads the resulting pull request back out of GitHub
and joins it to the task on PR URL. `Work delivered` is the limit of agent
accountability; review and merge stay visible downstream of it, which is what
allows unmerged agent PRs to be read as a review-capacity finding rather than an
agent-quality one.

**Tooling and measurement before fan-out.** AngelList built five composable
migration tools before putting 20 agents to work. The equivalent bet here is
that the measurement instrument gets fixed first. Superset's own tech-debt
dashboard reports 92 lint violations; the project's configured rules produce
1,470, because the metrics uploader runs oxlint without `--config oxlint.json`.
Under that broken instrument, `react-hooks(exhaustive-deps)` grew from 238 to
381 while remaining invisible to both CI and the dashboard. Fanning agents out
against a counter that is wrong by a factor of sixteen produces motion that
cannot be evaluated, so the counter comes first.

## 3. Where it does not

The scale is not comparable, and nothing in this document should be read as
implying otherwise: a fork with a handful of open issues is not a six-million-line
ETL migration across 100,000 data sets, and no 8-12x or 20x figure may be
repeated as if it were a result of this deployment.

Two further differences are worth naming. Nubank had a migration with a known
end state, which is the friendliest possible shape for an agent — the correct
answer for each unit is defined before work starts. The work here is
heterogeneous: bug fixes, security alerts, and plugin features, arriving from
two repositories, with no fixed target. And Nubank's units were homogeneous
enough to make per-unit cost predictable; here the ACU cost per outcome varies
by stream, which is why `scope.yaml` sets `max_acu_limit` per rule rather than
globally.

## 4. Where this repository is arguably stricter

Apache Superset's contributor expectations are enforced socially and
destructively: a pull request that reads as entirely machine-written is tagged
`lacks-human-authorship` and closed. That constraint is encoded in `policy.yaml`
and applied twice — rendered into the session prompt at intake, and evaluated
against the returned pull request at submit, with each check stored in
`fact_policy_check`.

The consequence is a gate the published Nubank sources do not describe. The
agent opens a draft, records it in the outbox in state
`draft_awaiting_authorship`, and moves on. A human then writes the authorship
paragraph in their own voice, and the draft cannot flip to ready until they
have. There is no generate, improve, or rewrite affordance anywhere on that
path, and `tests/test_policy.py` asserts its absence, because a button that
writes the paragraph for you would satisfy the check and defeat the rule. In
practice this means somebody has to read the diff well enough to describe it
honestly before the pull request can be reviewed at all. The Nubank sources
describe review-and-merge with no documented gate of any kind — which is not a
criticism of Nubank, since a private monorepo migration has no external
authorship policy to satisfy. It does mean the comparison runs in our favour on
this axis, and that the queue in front of the gate is a real cost: the age of
the outbox measures the operator's latency, and it is reported as its own node
in the funnel and the Sankey rather than hidden inside "in progress".

## 5. Ingredient mapping

| Nubank ingredient (as documented) | Implementation here | Evidence available today |
| --- | --- | --- |
| Units "too complex to script, high-volume enough to matter" | `scope.yaml` ordered rules route each event to a stream and a target repository; first match wins, no match means filtered | `scope.yaml` in the repository; `tests/test_scope.py`; routing decisions recorded per event in `fact_event` / `fact_task` |
| "An army of Devins" working subtasks in parallel | One Devin API session per admitted task, `max_acu_limit` per rule, session tagged `fde:initiative=…` | `orchestrator.py`; simulated end to end with a fake Devin client via `scoreboard simulate`; live sessions gated behind `DRY_RUN=false` |
| Human "manages the project and approves changes" | Draft pull request plus authorship outbox; `policy.yaml` checks stored per PR | `GET /outbox`, `GET /compliance`, `fact_policy_check`; `tests/test_outbox.py`, `tests/test_policy.py` |
| Efficiency claimed as engineer-hours per task | Not claimed. Funnel and Sankey report task counts and where work went; review rounds and human-edit ratio are the intended reviewer-cost metrics | Funnel and Sankey from `scoreboard report`, with the reconciliation assertion checked in CI; review rounds and human-edit ratio are in the data model but **not yet collected** |
| Cost claimed as compute versus salary | Not claimed. ACUs per merged PR and per closed alert, trended | `acus_consumed` is in the data model and collected per session; cost-per-outcome trend is **not yet built** |
| Tooling built before fan-out (AngelList) | Fix the lint measurement first: `--config oxlint.json` omission, 92 versus 1,470 | Reproducible by running the project's configured rules against the uploader's command; the `exhaustive-deps` series 238 to 381 |
| Backlog burn-down on pre-existing counters | `snapshot_daily` over CodeQL, Dependabot, coverage, tech-debt series; ~170 CI compute-minutes per pull request, of which the last two Cypress specs cost ~20 | `scoreboard measure` records oxlint counts from a checkout and `scoreboard cicost` records Actions job-minutes for pull-request runs, both from the repository under measurement; the CI figure is a median over a bounded sample of the window. Alert and coverage series are **not yet collected** |

The third column is deliberately unflattering. The funnel, the flow, the scope
routing, the policy checks and the outbox exist and are tested. The quality
panel, the cost curve and the burn-down series are specified in
[`PRD.md`](PRD.md) and are not built. Presenting them as shipped would be the
same category of error as repeating the 20x figure without saying who said it.

## 6. Numbers we may not claim

- **8-12x, or 12x, efficiency.** Nubank's figure, measured on Nubank's
  migration, defined as engineer-hours per data-class migration. Not measured
  here, and the work is not the same work.
- **Over 20x cost savings.** Cognition-voiced only, no customer corroboration,
  scoped to the delegated portion of the work. Even Nubank does not repeat it.
- **5.2x** (AngelList), **~50 flags in a month** (Ramp), **1,500+ merged PRs**
  (Gumroad), **1,800 repositories** (FE fundinfo). Different companies,
  different work, cited above only for calibration.
- **Any engineer-hours-saved or dollar figure derived from a multiplier.** The
  PRD makes this a non-goal (NG2). Review time here is not yet measured
  directly, so any such number would be arithmetic on an assumption.
- **Any claim about concurrency, playbooks, wave orchestration, CI gating, QA
  sampling or escalation at Nubank.** Not documented in any source. If the
  parallel structure of this deployment is described as resembling Nubank's, the
  resemblance is to the phrase "an army of Devins", not to a documented
  architecture.
- **Any causal claim from a before/after window.** The baseline is a historical
  comparison, not a control group (PRD §9). The defensible comparison is agent
  versus contemporaneous human.

What is left after those deletions is still the interesting part: the shape of
the work is the same, the review loop is the same, the ordering — instrument
first, fan-out second — is the same, and everything asserted about this
deployment can be recomputed from GitHub by anyone holding a token.

## Sources

- Cognition, Nubank case study: <https://devin.ai/customers/nubank> (~2024-12-10).
  Source of the 8-12x definition, the 20x cost claim, the ~6M-line and 100,000
  data set figures, the "too complex to be scriptable" and "army of Devins"
  framing, and the Jose Carlos Castro quotation.
- Vitor Olivier (CTO, Nubank), on video in Cognition's LinkedIn post, 2024-12-10:
  "8 to 12X speed in our ability to deliver".
- Carin Meier and Marlon Silva (Nubank), Clojure Conj talk written up at
  <https://building.nubank.com/enhancing-engineering-workflows-with-ai-a-real-world-experience/>,
  2025-02-17: "12-fold increase in efficiency".
- Cognition customer stories for AngelList, Ramp, Gumroad and FE fundinfo.
- This repository: [`scope.yaml`](../scope.yaml), [`policy.yaml`](../policy.yaml),
  [`PRD.md`](PRD.md), `src/scoreboard/`, `tests/`.
