# Demo script — five minutes, recorded

Audience: a VP of Engineering and three or four senior individual contributors.
The VP wants to know what changed and what it cost. The ICs will look for the
place where the numbers are made up. Order is What, How, Why, When.

Total 5:00. Nine beats, budgeted to the second. Each beat gives the exact screen
and the spoken line. Read the lines as written — they are written to be said out
loud, not read off a slide. No lists are read aloud. If a beat runs long, cut
from the trend beat, not from the evidence beats.

---

## Beat 1 — What: the problem (0:00–0:35, 35s)

**Screen.** Superset's own tech-debt dashboard, the lint panel, showing 92
violations. Then a terminal beside it running the project's configured rules,
showing 1,470.

**Spoken.**
> This is Superset's tech-debt dashboard. It says there are 92 lint violations.
> Here are the project's own configured rules, run against the same code. One
> thousand four hundred and seventy. The uploader runs oxlint without pointing it
> at the config file, so the dashboard has been reporting the default rule set
> for months. While that was happening, one rule — exhaustive-deps — went from
> 238 violations to 381. Nobody saw it. Not CI, not the dashboard. This is the
> problem I care about: work gets delegated against instruments nobody checked.

---

## Beat 2 — How: an issue arrives, a session opens (0:35–1:15, 40s)

**Screen.** Split. Left: the GitHub issue being labelled `bug` on the fork.
Right: the service log tailing, showing the webhook signature verified, the
matched `scope.yaml` rule id, then the Devin session id. Cut to the Devin session
page mid-run.

**Spoken.**
> Someone labels an issue on the fork. The webhook arrives, the signature is
> checked, and the event is matched against an ordered list of scope rules. That
> match is the whole routing decision — it picks the work stream, the repository
> the pull request will land in, and the compute budget. First rule that matches
> wins. If nothing matches, the event is filtered and recorded as filtered.
> Widening what the agent touches is an edit to a file in version control, not
> something that drifts. The match here starts one Devin session for this one
> issue.

---

## Beat 3 — How: the pull request (1:15–1:50, 35s)

**Screen.** The resulting pull request on the fork. Scroll slowly: the AI
disclosure section, the `Generated-by:` trailer, the local test evidence, the
adversarial self-review note. Then the CI checks list.

**Spoken.**
> Here is what came back. One issue, one session, one pull request. The
> disclosure, the trailer, the test output and the self-review are in the body
> because the target project's contribution rules were put into the prompt before
> the work started, then checked again on the way out. The reviewer gets a diff
> they can hold in their head and a check run that says whether it works. That is
> the unit. Everything else in this system exists to count these honestly.

---

## Beat 4 — How: where work actually goes (1:50–2:30, 40s)

**Screen.** The Superset dashboard: the Sankey, with the funnel counts beside
it. Hover a loss ribbon so the tooltip shows. Then click a ribbon through to the
drill-through table with links to the PR.

**Spoken.**
> This is every task since we turned it on. Intake on the left, outcomes on the
> right, one ribbon-width is one task. The reason it is a Sankey and not a funnel
> is that a Sankey has to conserve flow, so the tasks that went nowhere get their
> own named ribbon instead of quietly disappearing. Filtered, deduplicated,
> errored, escalated, still waiting on a person. That arithmetic is asserted in
> CI — the build fails if the outcomes stop adding up to the intake. Every ribbon
> clicks through to the pull requests behind it.

---

## Beat 5 — How: the trend lines (2:30–3:10, 40s)

**Screen.** Two time series with a `T0` marker: the lint-violation series against
the corrected count, and CI compute-minutes per pull request. Point at the
Cypress contribution on the second chart.

**Spoken.**
> Two trends, both on counters that existed before this deployment did. The first
> is the lint series against the corrected number, so the 1,470 is now the
> baseline and the line means something. The second is CI cost: about 170
> compute-minutes per pull request, and the last two Cypress specs are about
> twenty of them. Those are the two curves I would be judged on — is the debt
> going down, and is the cost per pull request going down. Both are computed from
> GitHub and Superset's own CI, so you can recompute them without me.

---

## Beat 6 — How: the authorship gate (3:10–3:30, 20s)

**Screen.** `GET /outbox` in the browser showing one draft in
`draft_awaiting_authorship` with its age, then the pull request flipping from
draft to ready after the paragraph is posted.

**Spoken.**
> Superset closes pull requests that read as entirely machine-written. So the
> agent opens a draft and stops, and it sits in this queue until a human writes
> the summary paragraph themselves. There is no button that writes it for them —
> a test asserts that. The queue age measures how slow we are, not how slow the
> agent is.

---

## Beat 7 — Why an agent rather than a script (3:30–4:05, 35s)

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

## Beat 8 — When: what a real engagement does next (4:05–4:45, 40s)

**Screen.** `PRD.md` milestones table, then `scope.yaml` with the rule set
visible.

**Spoken.**
> If this were your repository, the order would be the same as it was here. Fix
> the measurement, backfill ninety days of baseline from GitHub so the before
> column exists before the agent does anything, then run with dry-run on for a
> day and read what it would have picked up. Then widen one scope rule at a time.
> The parts that are not built yet are the reviewer-cost metrics and the
> cost-per-outcome curve; they are specified and they are next. I would rather
> tell you that than show you a chart with no data behind it.

---

## Beat 9 — Close (4:45–5:00, 15s)

**Screen.** The dashboard, whole, still.

**Spoken.**
> Every number on this screen comes from GitHub or from Superset's own CI, and
> every one of them clicks through to the thing it came from. The point is not
> that the agent is fast. It is that you can check.

---

## Recording notes

- Do not say "great question", do not open with "so", do not say "seamlessly",
  "robust", or "comprehensive". If a sentence can lose its first four words, lose
  them.
- Beats 2 and 3 must be a real session and a real pull request, not a
  reconstruction. If the live session is slow, record it separately and cut in —
  but do not narrate a session that did not run.
- Beat 6 is capped at twenty seconds. It is a differentiator, not the subject.
- **Before recording, confirm what actually renders.** Today the funnel, the
  Sankey edge list, the scope routing, the policy checks and the outbox are
  implemented and tested; `scoreboard simulate` drives them end to end with no
  credentials. The collector reads issues and pull requests only, so the Beat 5
  trend charts require the lint and CI-minute series to be collected into
  `snapshot_daily` first. If they are not in place at record time, cut Beat 5 to
  the terminal evidence for the 92-versus-1,470 discrepancy and the
  `exhaustive-deps` counts, say plainly that the series is not collected yet, and
  give the reclaimed seconds to Beat 4.
