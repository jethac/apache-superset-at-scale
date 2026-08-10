# Demo script — five minutes, recorded

Audience: a VP of Engineering and three or four senior individual contributors.
The VP wants to know what changed and what it cost. The ICs will look for the
place where the numbers are made up. Order is What, How, Why, When.

Total 5:00. Ten beats, budgeted to the second. Each beat gives the exact screen
and the spoken line. Read the lines as written — they are written to be said out
loud, not read off a slide. No lists are read aloud. Two things must be said
aloud rather than left on screen: why a fall in the headline debt line is not
evidence of anything on its own, and why fork-originated CI runs have to be
attributed by commit. If a beat runs long, cut from Beat 7, not from Beat 5.

---

## Beat 1 — What: the problem (0:00–0:30, 30s)

**Screen.** Superset's own tech-debt dashboard, the lint panel, showing 92
violations. Then a terminal beside it running `scoreboard measure --checkout
../superset`, which runs the project's configured rules and records 1,470.

**Spoken.**
> This is Superset's tech-debt dashboard. It says there are 92 lint violations.
> Here are the project's own configured rules, run against the same code. One
> thousand four hundred and seventy. The uploader runs oxlint without pointing it
> at the config file, so the dashboard has been reporting the default rule set
> for months. While that was happening, exhaustive-deps went from 238 violations
> to 381, and nobody saw it. That is the problem: work gets delegated against
> instruments nobody checked.

---

## Beat 2 — How: an event arrives, a session opens (0:30–1:05, 35s)

**Screen.** Split. Left: the GitHub issue being labelled `bug` on the fork.
Right: the service log tailing, showing `POST /webhook/github`, the signature
verified, the matched `scope.yaml` rule id, then the session id returned by
`api.devin.ai`. Cut to that session on app.devin.ai, mid-run, with the id
matching the log line.

**Spoken.**
> Someone labels an issue. GitHub posts it to the webhook, the signature is
> checked, and the event is matched against an ordered list of scope rules. That
> match is the whole routing decision — work stream, the repository the pull
> request will land in, the compute budget. First rule that matches wins; if
> nothing matches the event is filtered and recorded as filtered. The match
> creates a Devin session over the API, and the id in the log is the id
> api.devin.ai handed back. Nothing here is typed in by hand. Where GitHub does
> not push to us — upstream, or while the receiver was down — a timer runs the
> same intake, so the trigger is not conditional on a public URL.

---

## Beat 3 — How: the output is on GitHub (1:05–1:35, 30s)

**Screen.** The resulting pull request on the fork. Scroll slowly: the AI
disclosure section, the `Generated-by:` trailer, the local test evidence, the
adversarial self-review note. Then the CI checks list.

**Spoken.**
> Here is what came back. One issue, one session, one pull request on the fork.
> The disclosure, the trailer, the test output and the self-review are in the
> body because the target project's contribution rules were put into the prompt
> before the work started, then checked again on the way out. Two repositories,
> two jobs: we write to the fork, and we read and measure apache/superset,
> because that is where the debt and the CI bill are. Nothing here can open a
> pull request upstream unless someone deliberately turns that off. The reviewer
> gets a diff they can hold in their head and a check that says whether it works.

---

## Beat 4 — What: the thesis, in three claims (1:35–1:55, 20s)

**Screen.** The dashboard at `GET /dashboard`, top of the page, the three thesis
cards visible before any chart.

**Spoken.**
> The page opens on the three claims the deployment is making, so you can
> disagree with them before you look at a chart. Debt down. CI compute-minutes
> per pull request down — that one is the P0. More issues shipped over time. A
> claim with nothing collected behind it says no data rather than showing you a
> zero: a zero is a claim about the repository, absence is a claim about our
> collection, and the page does not confuse the two.

---

## Beat 5 — How: Trends, and where those two numbers come from (1:55–2:45, 50s)

**Screen.** The **Trends** tab: the debt chart with the solid headline line, its
break annotated, and the dashed like-for-like line under it — hover the dashed
line so the tooltip naming the shared rule count is visible. Then the CI
compute-minutes chart with the workflow breakdown. Point at the Cypress
contribution.

**Spoken.**
> The solid line is oxlint run under the project's own config, and every point
> stores the rules it measured. Read it alone and it will fool you: the total
> falls when violations are fixed, and it falls when rules stop being measured.
> That is what took the published series from 677 to 92. So the line breaks where
> the rule set changed, and the dashed line under it is the same runs counting
> only the rules every run measured. That one you can read end to end. It sits
> lower because it counts fewer rules, not because there is less debt.
>
> The cost line sums billed job-minutes per pull request: about 170 minutes a
> change, twenty of it the last two Cypress specs. The Actions API only names the
> pull request when the branch is in the repository, and nearly every Superset
> contribution comes from a fork, so we ask the head commit instead. Without that
> this median would describe committers, not contributors.

---

## Beat 6 — How: where the work is going, and the funnel (2:45–3:15, 30s)

**Screen.** The **Where the work is going** tab: the Sankey, hovering a loss
ribbon so the tooltip shows. Then the **Funnel** tab and its counts.

**Spoken.**
> This is every task since we turned it on, on the fork — the repository we
> write to, not the one we measure. Intake on the left, outcomes on the right,
> one ribbon-width is one task. The reason it is a Sankey is that a Sankey has to
> conserve flow, so the tasks that went nowhere get their own named ribbon
> instead of quietly disappearing. Filtered, deduplicated, errored, escalated,
> still waiting on a person. The funnel is the same arithmetic, asserted in CI —
> the build fails if the outcomes stop adding up to the intake. Most intake is
> refused, and I would rather show you that than hide it.

---

## Beat 7 — How: the fleet (3:15–3:40, 25s)

**Screen.** The **Fleet** tab: dispatches and deliveries per day, then the
session table with a running session and finished ones, one row clicked through
to its transcript on app.devin.ai.

**Spoken.**
> Every session this deployment has started, running ones first. The ids came
> back from api.devin.ai at creation and each row links to its transcript, so
> the fleet is checkable rather than asserted. A session is polled to its actual
> outcome — pull request, no action needed, escalation, failure — so the state
> you are reading is not just what was true at launch. The bars are the shape of
> the day: sessions dispatched, pull requests delivered.

---

## Beat 8 — How: the authorship gate (3:40–4:00, 20s)

**Screen.** `GET /outbox` in the browser showing one draft in
`draft_awaiting_authorship` with its age, then the pull request flipping from
draft to ready after the paragraph is posted.

**Spoken.**
> The ASF's tooling policy and Superset's expectations agree, and it is enforced:
> pull requests that read as entirely machine-written get tagged and closed. So
> the agent opens a draft and moves on, and it waits here for a human paragraph,
> which goes into the body verbatim. There is no generate button on this path — a
> test asserts its absence. That age column is our latency, not the agent's.

---

## Beat 9 — Why an agent rather than a script (4:00–4:35, 35s)

**Screen.** Two diffs side by side from the same stream — one a one-line config
fix, one a hook dependency fix that required reading the component.

**Spoken.**
> The obvious objection is that this is a codemod. For the config fix, it is —
> and if every unit looked like that I would write the script. This one does not.
> Fixing the dependency array means deciding whether the value is stable, whether
> memoising it changes behaviour, and whether the test still proves anything.
> That is per-unit judgment on a few hundred units. Too complex to script,
> too many to hand out. That is the shape where an agent is the right tool, and
> it is the same shape Cognition describes in the Nubank migration.

---

## Beat 10 — When: what is next, and the close (4:35–5:00, 25s)

**Screen.** `scope.yaml` with the rule set visible, then the dashboard, whole
and still.

**Spoken.**
> If this were your repository the order would be the same. Fix the measurement,
> backfill the baseline by measuring old commits so the before column exists
> before the agent does anything, run with dry-run on for a day and read what it
> would have picked up, then widen one scope rule at a time. The reviewer-cost
> metrics and the cost-per-outcome curve are specified and not built; I would
> rather tell you that than show you a chart with no data behind it. Every number
> on this screen comes from GitHub or from Superset's own CI. The point is not
> that the agent is fast. It is that you can check.

---

## Recording notes

- Do not say "great question", do not open with "so", do not say "seamlessly",
  "robust", or "comprehensive". If a sentence can lose its first four words, lose
  them.
- Beats 2, 3 and 7 must be a real event, a real pull request and a real session,
  not a reconstruction. If the live session is slow, record it separately and cut
  in — but do not narrate a session that did not run.
- Beat 8 is capped at twenty seconds. It is a differentiator, not the subject.
- **Before recording, populate the two series.** `scoreboard backfill --checkout
  <a superset clone> --months 12` measures historical commits and is what gives
  Beat 5 a line rather than a dot; it needs a full clone, not a shallow one, and
  it takes a while. Then `scoreboard cicost --repo apache/superset --since-days
  30` plus a back-sampled window (`--since-days 60 --until-days 30`) for two cost
  periods. With fewer than two points the thesis cards read *not yet comparable*
  or *no data*, which is honest but leaves Beat 5 with nothing to point at.
- The dashed like-for-like line only draws when every debt point shares at least
  one rule and the two series are the same length. If it is missing, say so in
  Beat 5 instead of narrating a line that is not on screen.
- If the series cannot be collected at record time, cut Beat 5 to the terminal
  evidence for the 92-versus-1,470 discrepancy and the `exhaustive-deps` counts,
  say plainly that the series is not collected yet, and give the reclaimed
  seconds to Beat 6.
- Do not claim a write to `apache/superset`. It is intake-only unless
  `ALLOW_UPSTREAM_WRITE` is set, and the demo does not set it.
