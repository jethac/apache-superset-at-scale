# PRD — Devin @ apache/superset

**Status:** draft · **Owner:** Jetha · **Target repo under measurement:** `jethac/superset`

---

## 1. Problem

An agentic deployment produces a great deal of visible activity — sessions, PRs, comments, CI runs —
and almost no legible evidence of *value*. The people who authorise the spend cannot answer three
questions from the artifacts the deployment leaves behind:

1. What is in flight right now, and what is stuck?
2. Is the work actually good, or merely voluminous?
3. Is anything measurably better than before the deployment?

Existing observability in the target repo does not close this gap. Application telemetry is a no-op by
default (`STATS_LOGGER = DummyStatsLogger()`; events go to a `Log` table in the metadata DB; no
tracing, no `/metrics`). Repo and CI observability is genuinely rich — CodeQL, Dependabot, Codecov,
49 workflows, an existing tech-debt series — but it is scattered across surfaces built for maintainers
doing maintenance, not for someone assessing whether a deployment worked.

**This product turns that existing, independently-maintained signal into a before/after account of the
deployment, with every number clickable through to its evidence.**

## 2. Goals

- **G1.** Report status of active and completed work, including work that is stuck on a human.
- **G2.** Report success and failure signals, with every throughput metric paired to a quality metric.
- **G3.** Report throughput and progress against a stated baseline window.
- **G4.** Make every headline number auditable: one click from the number to the PR, review, CI run, or
  session that produced it.
- **G5.** Be legible in about four seconds to someone who has never seen it, and survive an hour of
  hostile questioning from someone who has.

### Non-goals

- **NG1.** Not a replacement for application/runtime observability in Superset. Out of scope entirely.
- **NG2.** No synthetic ROI. No "engineer-hours saved", no dollar figures derived from a multiplier.
  Unless review time is measured directly, an invented number is the fastest way to lose the audience.
- **NG3.** Not real-time. Minutes-fresh is fine; nothing here is operational.
- **NG4.** Not a control panel. Read-only. It never starts, stops, or messages a session.
- **NG5.** No causal claim. The baseline is a historical comparison, not a control group.

## 3. Users

| User | Question they arrive with | What they need |
| --- | --- | --- |
| **Sponsor / CTO** | "What did this do for me since we turned it on?" | One screen, four-second read, no legend, no jargon |
| **Eng lead** | "What is it costing my reviewers?" | Review rounds, human-edit ratio, escalation aging |
| **Skeptic** | "Prove that number" | Drill-through to raw evidence; visible methodology |
| **Operator (you)** | "What is stuck and why?" | Active board sorted by age, failure taxonomy |

The skeptic is the design-driving user. Anything that cannot be drilled into does not belong on the
page.

## 4. The constraint that shapes the design

The Devin control plane only produces data from deployment day (`T0`) forward. GitHub's data plane is
retroactively queryable. Therefore:

> **R0 (binding).** Every headline metric MUST be computable from the repo/CI plane alone. The Devin
> plane is an *attribution and cost overlay*, never the sole source of a headline number.

Consequences: the entire "before" panel can be generated before the deployment has done anything, and
it is independently verifiable by anyone with a GitHub token. Violating R0 leaves the "before" column
empty and downgrades the whole product from a measurement to an assertion.

Baseline convention: state `T0` on the page; compare a trailing 90-day pre-`T0` window against an
equal-length rolling post-`T0` window. Never compare unequal windows.

## 5. Functional requirements

### 5.1 Ingest

- **F1.** Collect from the repo/CI plane: pull requests, reviews and review threads, issues and labels,
  workflow runs, check runs, code-scanning alerts, Dependabot alerts, Codecov coverage, and the
  existing tech-debt series.
- **F2.** Collect from the Devin plane: sessions (status, `status_detail`, tags, playbook,
  `acus_consumed`, `origin`, `parent_session_id`, `child_session_ids`, `structured_output`,
  `pull_requests[].url`) and session insights.
- **F3.** Join the planes on **PR URL**, with a session-tag convention as the fallback path for work
  that never produced a PR (`fde:<initiative>`, `fde:trigger=<...>`, `fde:wave=<n>`).
- **F4.** Ingest MUST be idempotent and windowed: `collect --since --until` upserts by natural key, so
  backfill and incremental collection are the same code path with different arguments. Re-running over
  any window MUST produce identical output. *This is what makes the baseline trustworthy — the "before"
  panel is produced by the exact same code as the "after" panel.*
- **F5.** Work that cannot be attributed MUST land in an explicit `unattributed` bucket. Silently
  dropping it is a correctness bug, not a presentation choice.

### 5.2 Metrics

- **F6 — Task status.** A *task* is one triggering event, not one session; it may span retries and child
  sessions. Report the funnel as absolute counts:
  `triggered → session started → work delivered → CI green → approved → merged`.
  Report an active board aged by time-in-state, and a terminal-state breakdown
  (`merged / closed-unmerged / abandoned / errored / suspended`).
  **Aging on `waiting_for_user` is a required metric** — it is the deployment asking a human for
  something, and it is the failure mode that quietly kills adoption.
- **F7 — Success & failure.** Merge rate; **first-push CI pass rate**; review rounds to merge;
  human-edit ratio (human commits/lines on an agent PR before merge); revert rate within 30 days;
  escalation rate; and a failure taxonomy bucketed from session analysis plus terminal states.
- **F8 — Throughput.** PRs merged per week; lead time open→merge (p50/p90); time to first review; CI
  wall-clock. Plus backlog burn-down over the *pre-existing* counters: open CodeQL alerts (nightly
  cron gives a clean daily series), Dependabot alert count and median age, the tech-debt series, and
  coverage. These carry disproportionate weight precisely because the deployment did not invent them.
- **F9 — Cost.** ACUs per merged PR and per closed alert. Trend over level: a falling cost-per-outcome
  is the most persuasive curve available.
- **F10 — Cohorts.** Every metric in F7–F9 MUST be sliceable by cohort:
  `agent · human (contemporaneous) · dependabot · human (baseline window)`.
  **The comparison that survives scrutiny is agent vs. contemporaneous human**, not agent vs. the past,
  because it controls for everything else that changed in the window.
- **F11 — Pairing rule (binding).** No throughput metric may be displayed without its paired quality
  metric adjacent to it. Merge rate is trivially gamed by filing trivial PRs and a technical audience
  will probe exactly there; the pairing is the pre-emptive answer.

### 5.3 Presentation

- **F12 — Hero visual: work-flow Sankey.** Strictly left-to-right, one unit of flow = one task, ribbon
  width = task count. Work fans out from trigger sources into parallel agent work and fans back in to
  shared outcome nodes. Because a Sankey conserves flow, tasks that went nowhere cannot be quietly
  dropped — the chart forces an honest `abandoned` and `unattributed` node. That property is the reason
  to prefer it over a funnel, which hides its losses.
- **F13 — Stream colour, not zones.** Work streams progress independently; a bugfix and a greenfield
  task are not at the same stage at the same time. The diagram MUST NOT imply synchronisation by
  drawing stage zones as vertical regions. Stream identity is encoded by **colour** (e.g. greenfield
  vs. bugfix vs. security), and the agent→organisation handoff is a property of a **node** — exposed in
  the tooltip and the drill-through table — not a line drawn across the canvas.
- **F14 — Handoff semantics.** `Work delivered` is the limit of agent accountability: PR created **or**
  a correctly reasoned "no action needed" investigation posted. Counting only PRs creates an incentive
  to open junk ones. Review and merge stages MUST remain visible downstream of the handoff: stopping
  the chart at delivery measures activity and invites a fair "so what?", while omitting merge outcomes
  reads as concealment. Keeping both is what permits the argument that unmerged agent PRs are a
  review-capacity finding rather than an agent-quality one — an argument that is not credible if made
  only under challenge.
- **F15 — Rework is unrolled, not looped.** Retries appear as distinct stage nodes
  (`CI green attempt 1 / 2 / 3+ / never green`) so flow stays acyclic and rightward while remaining
  visible. A thick "attempt 2" ribbon is informative; hiding it costs more credibility than showing it.
- **F16 — Loss ordering.** Loss ribbons are ordered to the vertical extremes so the surviving trunk
  reads as a continuous mid-line river rather than a braid.
- **F17 — Time series companion (required).** A Sankey has no time axis: "today" and "since deployment"
  are the same chart with different filters, and neither shows a trend. The Sankey MUST be paired with
  at least one time series carrying a `T0` marker. The Sankey answers *where does work go*; the time
  series answers *is it getting better*.
- **F18 — Side-by-side baseline.** The identical Sankey MUST be renderable for the baseline window from
  GitHub data alone — human PRs flow through the same trigger, validation, review, and terminal stages.
  Same node layout, same scale, two charts. This satisfies R0 and is the single strongest artifact.
- **F19 — Drill-through.** Every number is clickable to its evidence. A cross-filtered table sits
  beneath the Sankey with HTML-rendered anchor columns to the PR, the review thread, the CI run, and the
  session. Drill-to-detail on the Sankey itself is a bonus, not a dependency — the table is more legible
  and its behaviour is known.
- **F20 — Methodology is on the page.** Metric definitions and the caveat list (§9) are visible in the
  product, not just in a deck.

### 5.4 Explicit anti-requirements

- **F21.** The widest ribbon must not be an intake node. If "triggered" dominates the visual, the chart
  is telling a story about activity rather than value; scale by merged-PR-equivalents if intake volume
  swamps the picture.
- **F22.** No trend line through fewer than ~10 points. Show counts beside every rate.

## 6. Data model

```
fact_pr(pr_url PK, repo, number, author, cohort, opened_at, merged_at, closed_at,
        additions, deletions, changed_files, review_rounds, human_commits, reverted_by)
fact_check_run(pr_url, sha, name, conclusion, started_at, completed_at, is_first_push)
fact_review(pr_url, reviewer, state, submitted_at, thread_count)
fact_alert_transition(alert_id, source, severity, opened_at, fixed_at, fixed_by_pr_url)
fact_session(session_id PK, tags, playbook_id, status, status_detail, origin,
             parent_session_id, acus_consumed, pr_url NULL, terminal_reason)
fact_flow_edge(window_start, window_end, cohort, stream, source_node, target_node, task_count)
snapshot_daily(day, counter_name, value)
```

Notes:

- `fact_flow_edge` is the Sankey's entire input. `sankey_v2` wants a flat edge list — `source`, `target`,
  `metric` — and `stream` is the column that drives colour (see §8).
- `snapshot_daily` is derived from facts and never from a live "current count"; historical charts must
  not depend on the present state of an API.
- All rates and ratios are SQL views computed at read time, so a metric definition lives in exactly one
  auditable place and can be corrected retroactively.

## 7. Architecture

Deliberately boring. The substance is the metric model; an over-engineered stack reads as poor judgment.

```
   GitHub API ──┐
   Codecov    ──┼─▶ collector (Python, idempotent, windowed) ──▶ facts.db ──▶ dashboard (read-only)
   Devin API  ──┘                    ▲
                          scheduled Devin automation
                        (cron trigger + webhook refresh)
```

The collector is one service with one command. The dashboard is read-only.

Running the collector *from a Devin automation* — schedule trigger for the periodic refresh, GitHub
trigger for merge-event refresh — is a deliberate choice: the reporting system for the deployment is
itself operated by the deployment.

## 8. Dashboard surface

Superset is the presentation layer. This is dogfooding the target product, and it is not a compromise:
click-through-to-evidence works there via four mechanisms — HTML-rendered anchor columns in table
charts, drill-to-detail and drill-by (both on by default), `url_param()` templating in virtual datasets,
and the Handlebars chart for bespoke evidence lists.

What Superset gives up is art direction: a narrative before/after layout with a fixed reading order is a
presentation artifact and Superset dashboards resist being composed that way. Hence the split:

- **Superset dashboard** — the analyst and evidence surface. All slicing, all drill-through, all raw
  data. This is where the skeptic goes.
- **Thin static summary page (optional, second)** — three headline charts, `T0` marker, fixed reading
  order, deep links into the Superset dashboard for anything a skeptic wants to verify. Reads the same
  database.

**Dependency:** F13 (stream colour) requires the Sankey plugin to key node colour on a chosen dimension
rather than on the full node name, which it does not do today. Tracked as
[jethac/superset#1](https://github.com/jethac/superset/issues/1); two candidate implementations are in
flight. Interim fallback if neither lands: pin node colours via `label_colors` in dashboard metadata —
zero code, but requires enumerating node names by hand and does not survive the node vocabulary
changing. **This is a hard blocker for F13 only; every other requirement ships without it.**

## 9. Caveats (shipped as part of the product)

Volunteering these reads as rigour; being caught by them reads as spin. They belong on the page.

- Attribution is imperfect: a session that opens no PR, or a human who cherry-picks agent work, breaks
  the join. Hence the explicit unattributed bucket (F5).
- Selection bias: the agent gets the tasks someone chose to give it. Agent-vs-human throughput is not
  like-for-like; first-push CI pass rate and revert rate are the less biased signals.
- Merge rate is gameable, which is why F11 exists.
- Small-n: early windows are noisy (F22).
- Novelty effects: reviewer enthusiasm early, fatigue later. A rolling window plus the contemporaneous
  cohort only partly controls for this.
- The baseline is a historical comparison, not a control group. The honest framing is "here is what
  changed, and here is what else was changing" — never "we caused this".

## 10. Milestones

Each is independently demoable.

| # | Milestone | Proves |
| --- | --- | --- |
| M1 | Collector + repo/CI plane + baseline backfill | The entire "before" panel, with zero agent activity. De-risks everything after it. |
| M2 | `snapshot_daily` + burn-down curves (CodeQL, Dependabot, Codecov, tech debt) | The money chart, on counters nobody can accuse you of inventing |
| M3 | Devin plane + PR-URL join + tag convention | Attribution turns on |
| M4 | Superset dashboard: three panels, `T0`, cohort selector, drill-through | The skeptic surface |
| M5 | Sankey (F12–F18), pending or working around #1 | The hero visual |
| M6 | Failure taxonomy + cost-per-outcome | Reads as someone who has run a deployment, not demoed one |

M1–M3 are the substance; M4–M5 are presentation; M6 is credibility.

## 11. Acceptance criteria

- **A1.** Re-running the collector over an arbitrary past window produces byte-identical facts.
- **A2.** Every number on the dashboard reaches its underlying PR, review, CI run, or session in one
  click.
- **A3.** The "before" panel renders correctly with the Devin plane entirely absent (R0 verification —
  test it by pointing at an empty session table).
- **A4.** The Sankey conserves flow: for every stage, inflow equals outflow, and losses are named nodes
  rather than missing ribbons.
- **A5.** No throughput metric renders without its paired quality metric (F11).
- **A6.** A reader who has never seen the dashboard can state what came in, what landed, and where the
  rest went, within ten seconds, without a legend.
- **A7.** Total tasks reconcile: `delivered + escalated + errored + filtered + unattributed = triggered`.

## 12. Open questions

- **Q1.** What is `T0` exactly, and is the pre-window 90 days or one release cycle?
- **Q2.** Which streams are worth colouring — work type (greenfield/bugfix/security/chore), or trigger
  source? These give quite different pictures and only one should be the default.
- **Q3.** Does "work delivered" include a posted investigation from the first release, or is that a
  later refinement? It changes the shape of the funnel.
- **Q4.** SQLite or Postgres? SQLite is simpler and sufficient; Postgres is a less distracting answer if
  someone asks about scale.
- **Q5.** Is the thin static summary page in scope at all, or does the Superset dashboard carry the
  narrative on its own?
