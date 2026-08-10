# Devin @ apache/superset

An event-driven Devin automation for the Superset fork, plus the reporting layer
that makes its effect auditable.

Something happens on GitHub — an issue is filed, CI fails, a code-scanning alert
opens. A scope rule decides whether it is in scope and which work stream it
belongs to. In-scope work becomes a Devin session against the fork. Everything
that happens afterwards — pull requests, reviews, merges — is read back out of
GitHub and reconciled into a funnel and a Sankey flow.

```
GitHub event ──▶ normalise ──▶ scope rules ──▶ Devin session ──▶ PR on the fork
                                    │                                  │
                                 filtered                       collector reads
                                 deduped                        GitHub back out
                                    └──────────▶ SQLite facts ◀────────┘
                                                      │
                                              funnel + Sankey
```

**The load-bearing design constraint:** every headline number is computable from
GitHub alone. Devin data is an attribution and cost overlay that begins existing
on deployment day. Get this backwards and the "before" column of a before/after
report is empty, and the whole thing becomes an assertion rather than a
measurement. See [`docs/PRD.md`](docs/PRD.md) for the full rationale.

## Where to look first

If you are reviewing this rather than operating it, the three things worth
checking are the trigger, the Devin call, and the loop that closes behind it.

| Claim | Where it lives | How to see it |
| --- | --- | --- |
| Triggered by an event | `POST /webhook/github` in [`api.py`](src/scoreboard/api.py) (HMAC-verified) and `scoreboard poll` in [`cli.py`](src/scoreboard/cli.py) (scheduled) | `scoreboard replay --event issues payload.json` |
| Routing is data, not code | [`scope.yaml`](scope.yaml) → [`flow.py`](src/scoreboard/flow.py) | edit a rule, replay the same payload |
| Sessions started programmatically | `HttpDevinClient.create_session` in [`devin.py`](src/scoreboard/devin.py), called from `Orchestrator.handle` | `POST /sessions` against `api.devin.ai` |
| Sessions *managed*, not fired and forgotten | `Orchestrator.sync` in [`orchestrator.py`](src/scoreboard/orchestrator.py) | `scoreboard sync` |
| Observable output | pull requests on the fork, plus `GET /dashboard` | `GET /funnel`, `GET /compliance` |
| Nothing is lost between intake and outcome | `reconciles` in [`store.py`](src/scoreboard/store.py) | asserted in CI; the CLI exits non-zero if it fails |

The interesting design decisions, in rough order of how much argument they took:
upstream is unwritable by construction rather than by policy (`assert_writable`);
the authorship gate blocks on the *paragraph a human must write*, not on an
approval click, so the queue collects the artifact the rule actually asks for;
and the debt series treats a ruleset change as an instrument change and draws a
break rather than a slope. Each is argued where it is implemented.

## Two repositories, and why they are not the same one

There are two repository names in the configuration and they do different jobs.

`scope.yaml`'s `defaults.target_repo` — `jethac/superset` — is the **write
target**: where the fleet's pull requests land, and the repository the funnel,
the Sankey and the fleet roster describe. `MEASURE_REPO`, defaulting to
`apache/superset` in [`config.py`](src/scoreboard/config.py), is the
**measurement target**: the codebase the debt series and the CI cost series are
computed against. `dashboard_payload` takes both and keeps them apart —
`throughput` and `flow` come from the write target, `debt` and `ci_cost` from
the measurement target — and the document it returns names each of them.

The split is the point, not a convenience. The problem being measured is
Superset's — thousands of lint violations under its own configured rules, and
tens of billed CI minutes for every change — and it exists whether or not this
deployment ever runs. Measuring the fork's own CI instead would describe the
deployment's activity rather than the debt it exists to shrink, and would make
the "before" column a property of when we started. So upstream is read in both
directions and written in neither:
`assert_writable` in [`github.py`](src/scoreboard/github.py) raises on any
upstream target unless `ALLOW_UPSTREAM_WRITE` is set, which it is not by
default, and the GitHub token you are told to create below is not granted
upstream write either.

## Run it live

Four commands from a clean checkout to a Devin session the automation started
by itself:

```bash
uv venv .venv -p 3.12 && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/scoreboard init                 # credentials, validated against live endpoints
sed -i 's/^DRY_RUN=true/DRY_RUN=false/' .env
.venv/bin/scoreboard intake --repo apache/superset --since-days 30
```

What that last command does, per issue: normalise it, evaluate the scope rules,
deduplicate against work already tracked, and for anything admitted, `POST
/v1/sessions` to `api.devin.ai` with a prompt carrying the target project's
contribution policy. A representative pass over `apache/superset`, 95 open
issues read, one admitted:

```
INFO root apache/superset#42340 -> session_started (matched rule upstream-viz-plugin-work)
INFO root apache/superset#42926 -> filtered (no matching rule)
...
```

`scoreboard sync` then polls that session to its outcome. Neither step accepts
a hand-written session id: the id in `fact_task.session_id` is whatever
`api.devin.ai` returned, and `GET /v1/sessions/{id}` with your own key will show
you the same session.

The scheduled form of both is `scoreboard poll --repo jethac/superset --repo
apache/superset --interval 60`, which is the `poller` service under
`docker compose --profile live`. The webhook form is below.

### The offline path is for tests, not for demonstrations

`scoreboard simulate` runs the same scope rules, orchestrator, fact store and
funnel arithmetic against generated events and a fake Devin client — no
credentials, no network. It exists so CI can assert the invariant

```
filtered + deduped + queued + in_flight
  + work_delivered + awaiting_authorship + escalated + errored == triggered
```

on every commit, and the command exits non-zero if it ever fails. It proves the
arithmetic, not the integration; nothing it produces is evidence that the live
path works.

## Configure it for real

```bash
scoreboard init
```

The wizard prompts for a GitHub token and a Devin API key, validates each
against a live endpoint *separately*, asks which repository is the write target
and which repositories are read for intake, and then does the check that is easy
to forget: it asks Devin which repositories *Devin* can reach. GitHub granting
you access and your Devin org's Git integration granting Devin access are two
different things, and the second one usually fails later, when a session cannot
clone.

Separately is the operative word. Two secrets go into two masked prompts in a
row, and the failure that actually happens is not an invalid key but the wrong
one: paste the GitHub token into both and every Devin call returns 403, which
reads exactly like a bad org id and sends you to the wrong settings page. So the
key is checked on its own against `GET /v1/sessions` before anything that also
depends on the org id, and an obvious `github_pat_…` in the Devin field is
rejected without a request at all.

The repository listing is reported but not blocking: `GET
/v3beta1/organizations/{org}/repositories` wants an organisation-scoped key, and
a user key that creates sessions perfectly well cannot enumerate the org. A 403
there says nothing about whether Devin can clone, so the wizard says so and
prints <https://app.devin.ai/settings/integrations> rather than failing setup on
a question it is not entitled to answer.

It writes `.env` with mode `0600` and updates the repository fields in
`scope.yaml`. It never writes a secret into `scope.yaml`, the database, or an
image layer.

### Credentials

| Variable | What it is |
| --- | --- |
| `DEVIN_API_KEY` | Devin service-user or personal API key (`apk_user_…`), sent as `Authorization: Bearer`. SSO governs webapp and org login; it is not the API credential. |
| `DEVIN_ORG_ID` | Needed only for the org repository-listing check, which is advisory. |
| `GITHUB_TOKEN` | Read on every intake repository; write only on the fork. |
| `MEASURE_REPO` | Default `apache/superset`: the repository the debt and CI-cost series describe, which is not the repository the fleet writes to. |
| `WEBHOOK_SECRET` | HMAC secret for GitHub deliveries. Unset means *every* webhook is rejected. |
| `DRY_RUN` | Default `true`: route and record, create no sessions. |
| `ALLOW_UPSTREAM_WRITE` | Default `false`: refuse to target an upstream repository. |
| `DB_PATH` | Default `data/facts.db`: the SQLite fact store every command reads and writes. |

Create the Devin key from a dedicated service user with a minimal role
(<https://docs.devin.ai/api-reference/authentication>) rather than from your own
account, so its actions are attributable and it can be revoked without
disrupting a human.

Use a **fine-grained** GitHub token scoped to the fork and this repository only,
with Contents and Pull requests read/write and Issues read. Deliberately do not
grant it the upstream repository: intake reads public issues, which needs no
credential at all, so "upstream is read-only" becomes a property of the token
rather than only of `assert_writable`. Add Actions: read on whichever repository
you point `scoreboard cicost` at.

Devin's sessions clone through your Devin organisation's git integration, not
through this token. They are separate grants and the wizard checks both, because
the second one otherwise fails later, at clone time.

## Run the service

```bash
docker compose up --build
```

Then point a GitHub webhook at `POST /webhook/github` with content type
`application/json` and the same secret, subscribed to *Issues*, *Check runs*,
*Code scanning alerts* and *Dependabot alerts*.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Liveness; also the container health check. |
| `GET /funnel` | Current funnel counts and reconciliation status. |
| `GET /outbox` | Draft pull requests waiting on a human authorship paragraph. |
| `POST /outbox/{task_id}/authorship` | Submit that paragraph and mark the draft ready. |
| `GET /dashboard` | Operator page: Trends, Where the work is going, Fleet, Funnel. |
| `GET /dashboard/data` | The page's single JSON document, if you would rather read it raw. |
| `GET /dashboard/lozenge.min.css` | The vendored design system the page is styled with. |
| `GET /compliance` | Per-pull-request policy evidence: which checks ran, which passed. |
| `POST /webhook/github` | Signed event intake. |

The page is styled with [Lozenge](https://github.com/jethac/lozenge), vendored as a built
stylesheet and served by this container: it charts with the design system's tokens, so the trends
follow the scheme and contrast dial rather than a hard-coded palette. Nothing is fetched at page
load — a CDN reference would be unreviewed code arriving from outside the image, and the container
runs without egress anyway. `src/scoreboard/static/VENDOR.md` records the commit it was built from.

### Dry run is the default, and it is visible rather than silent

`DRY_RUN=true` is the shipped default, and it is the single switch between
observing a repository and spending money on it. Everything upstream of dispatch
still happens: events are normalised, scope rules are evaluated, work is
deduplicated, verdicts and reasons are written to the fact store, and `sync`
still polls and adopts the sessions that already exist. What does not happen is
`POST /v1/sessions`.

That matters for reading the page, because a deployment in dry run does not look
broken — it looks like a fleet that has stopped growing. Three places say which
mode you are in:

| Where | Dry run | Live |
| --- | --- | --- |
| Intake log | `-> triggered (matched rule fork-backlog (dry run: no session created))` | `-> session_started (matched rule …)` |
| Funnel | admitted work accumulates in `queued`, and `in_flight` does not rise | `queued` drains into `in_flight` as capacity frees |
| Fleet tab | roster grows only by adoption of externally started sessions | rows appear with session ids this deployment created |

So an admitted issue sitting in `queued` means one of two different things —
the concurrency cap is full, or dispatch is switched off entirely — and the log
line is what distinguishes them. Flip it deliberately:

```bash
sed -i 's/^DRY_RUN=true/DRY_RUN=false/' .env   # then restart serve and poll
```

Both the `serve` and `poller` processes read `.env` at startup, so an edit takes
effect on restart rather than on the next poll.

### Without webhooks

`scoreboard replay --event issues payload.json` routes a saved delivery, and
`scoreboard intake --repo jethac/superset --repo apache/superset` polls issues
directly. Both are useful when you cannot expose a public URL.

### Managing the sessions, not just starting them

Starting a session is the cheap half. `scoreboard sync` polls every session in
`session_started` and moves it to the outcome it actually reached — pull request,
no-action-needed, escalation, or failure — recording ACUs and grading the result
against the contribution policy on the way. Without it the funnel would report
what was true at launch and `in_flight` could only grow.

`scoreboard poll --repo jethac/superset --repo apache/superset --interval 60`
is the two together on a timer, and is the `poller` service in
`docker-compose.yml` (`--profile live`). That is the scheduled trigger: webhooks
cover what GitHub pushes to us, the poller covers what it does not — upstream
repositories we hold no webhook on, and deliveries missed while the receiver was
down.

## Scope rules

[`scope.yaml`](scope.yaml) is ordered data, not hidden routing code. Conditions
within a rule are ANDed, the first enabled matching rule wins, and an event
matching nothing is filtered. Widening scope is therefore always a deliberate
edit rather than an emergent accident.

```yaml
  - id: upstream-well-specified-bug
    when:
      repo: [apache/superset]
      event_type: [issue]
      labels_any: ["#bug"]
      labels_none: ["#WIP", "help wanted", "duplicate", "wontfix"]
      age_days_min: 14
      exclude_bots: true
    then:
      stream: bugfix
      target_repo: jethac/superset   # never upstream
```

Conditions: `repo`, `event_type`, `labels_any`, `labels_none`, `title_regex`,
`severity_min`, `age_days_min`, `age_days_max`, `exclude_bots`. Routing:
`stream`, `target_repo`, `playbook_id`, `max_acu_limit`, `tags`.

**Admitted is not the same as dispatched.** `defaults.max_concurrent_sessions`
caps how many sessions run at once; admitted work over the cap keeps its verdict
and waits, and a later intake pass starts it when the fleet has room. A backlog
filed in one afternoon therefore costs its ACUs at a rate a human can still
intervene in. The funnel counts that waiting work as `queued for capacity`
rather than `in flight`, so the fleet number matches the sessions the Devin app
shows running. Routing is also re-evaluated on every sighting of work that has
not started yet, so widening a rule picks up issues that were already seen and
filtered — a rule edit does not need a database edit to take effect.

**The fleet is larger than what this process dispatched.** A human, or another
Devin working the same backlog, starts sessions that spend the same ACUs against
the same repository, and a roster built only from this deployment's own dispatches
reports a smaller fleet than the Devin app does. Each `sync` lists sessions from
the Devin API and adopts any carrying a tag in `defaults.adopt_session_tags`, so
ownership is a configuration decision rather than a guess: sessions belonging to
anything else in the organisation are left alone. Adopted rows say so in their
reason, so the funnel never presents them as work this deployment routed.

**Upstream is read-only.** We read `apache/superset` issues; the resulting pull
request is opened on the fork. `assert_writable` refuses any upstream target
unless `ALLOW_UPSTREAM_WRITE` is explicitly set, so no rule edit alone can cause
a PR against someone else's repository. The `age_days_min` floor on upstream
issues is deliberate too: racing human contributors to fresh issues is the
fastest way to make an agent deployment unwelcome in a project you do not own.

## Contribution policy and the authorship outbox

[`policy.yaml`](policy.yaml) holds the target project's rules for AI-assisted
contributions, selected per repository. The `asf-superset` profile encodes two
published sources — the ASF's generative tooling policy
(<https://www.apache.org/legal/generative-tooling.html>) and Superset's new
contributor expectations — and neither is advisory in practice: a pull request
that reads as entirely machine-written is tagged `lacks-human-authorship` and
closed. The deployment therefore has to know the rules before it writes rather
than after.

The profile is applied twice. At intake, `prompt_section` renders it into the
session prompt — `Generated-by:` trailer, AI disclosure section, local test
evidence, adversarial self-review, open as draft. At submit, the same profile
is evaluated against what came back, and every check is stored per pull request
in `fact_policy_check`, so compliance is queryable evidence rather than a claim.

What the agent cannot supply is the paragraph in a human's own voice. Rather
than parking the session until someone is available, it opens a draft and moves
on; the draft lands in the outbox in state `draft_awaiting_authorship`, which is
its own node in the funnel and the Sankey and counts as delivered work the
moment the draft opens. The age of that queue is reported separately, per item,
as `waiting_days`: it measures how long the operator took, and it is not the
agent's delivery time. The exception is a draft that has since been cleared,
whose `updated_at` moves to the moment the paragraph was posted, so its
contribution to `median_hours_to_delivery` does include the wait — noted again
under Limitations.

```bash
scoreboard outbox                       # what is waiting, and for how long
curl -X POST localhost:8000/outbox/$TASK/authorship \
  -d '{"text": "...", "author": "jethac", "input_method": "dictated"}'
```

The text is stored verbatim alongside the author and whether it was typed or
dictated — dictation arrives as one unpunctuated block, and how it came to exist
is part of the evidence that a human wrote it. Submission fails on empty text,
splices the paragraph into the pull request body unedited, and flips draft →
ready only once every blocking check passes. Tone findings are recorded but do
not block.

There is no generate, improve, or rewrite affordance anywhere in this path, and
`tests/test_policy.py` asserts its absence. The submitted text is spliced into
the pull request body exactly as received, never reflowed or edited. A button
that writes the paragraph would satisfy the check while defeating the rule it
implements, and the app has no such button and no model call on this path at
all.

## Reporting

`scoreboard collect --repo jethac/superset --since-days 90` reads pull requests
back out of GitHub and labels each one `agent` or `human` by matching its URL
against attributed tasks. Because the cohort split is derived from GitHub, the
historical baseline can be reconstructed for a period long before the
deployment existed — the comparison that survives scrutiny is agent versus
*contemporaneous* human, not agent versus the past.

### Measuring the two trends for real

Both trend series are recorded by commands you run against the repository under
measurement, not by fixtures.

```bash
scoreboard measure --checkout ../superset
scoreboard backfill --checkout ../superset --months 12
scoreboard cicost --repo apache/superset --since-days 30
scoreboard cicost --repo apache/superset --since-days 60 --until-days 30
```

`scoreboard measure --checkout <path to a superset clone>` shells out to `npx
oxlint --config oxlint.json --format json` inside that clone's
`superset-frontend`, counts the diagnostics per rule, and records them with the
set of rules it saw and the commit it measured at. `--repo` names the repository
the checkout is of and `--config` points at a different oxlint configuration.
oxlint exits non-zero whenever it reports anything, so only an empty document is
treated as a failure, and output is spooled to a temporary file rather than a
pipe — the full-configuration run emits well over a thousand diagnostics, which
is the `maxBuffer` problem Superset's own uploader hits.

One measurement is a point, not a series. `scoreboard backfill --checkout
<clone> --months 12` produces the history by measuring commits as they stood:
one commit per month boundary, each checked out into a throwaway `git worktree`
so your working tree is untouched, each stamped with its commit's author date
rather than the afternoon of the backfill. Commits whose tree has no oxlint
configuration are skipped rather than measured bare, which leaves a gap — that
is what a missing measurement is supposed to look like. It refuses to run
against a shallow clone instead of reporting an empty series.

`scoreboard cicost --repo apache/superset --since-days 30 [--until-days N]
[--max-runs N]` reads GitHub Actions runs on pull requests in that window and
records each job's billed minutes — elapsed job time, so queueing is excluded,
which is what GitHub bills. `--until-days` ends the window N days ago, which is
how you back-sample earlier periods to get a second point to compare against.
Both need a token with Actions: read on the repository you point it at.

**Fork-originated runs have to be attributed by commit.** The Actions API only
populates `workflow_runs[].pull_requests` when the head branch lives in the same
repository, and on Superset almost every contribution arrives from a fork, so
for most runs that array is empty. Where it is, `collect` in
[`cicost.py`](src/scoreboard/cicost.py) asks `GET
/repos/{repo}/commits/{sha}/pulls` which pull request the head commit belongs
to, and caches the answer per commit. Skipping that second read would not lose
a few rows at the margin: the surviving rows would be exactly the runs pushed by
people with commit access, so the median would describe committers rather than
contributors, and would be reported as though it described the project. Runs
that neither read can attribute are still stored with a null pull-request
number, and `cost_per_pr` excludes them from the median rather than guessing.

`--max-runs` (default 40; `0` reads the whole window) bounds what is otherwise
an expensive walk: one API call per run, against a repository that runs
thousands of jobs a day. The consequence is worth stating plainly — the figure
that comes out is a median over a sample of the window, not a census of it.
Raise the cap when you want a tighter estimate and are willing to pay the API
calls for it.

### Reading the funnel

Every task sits in exactly one of these at any moment, and the eight terminal
buckets sum to `triggered` — `GET /funnel` returns `reconciles: true/false` for
that identity and the CLI exits non-zero when it is false, so a page that adds
up is a checked property rather than a hope.

| Stage | What it means |
| --- | --- |
| `triggered` | Events seen. Every sighting of an issue counts, so this is much larger than the number of issues. |
| `filtered` | No scope rule matched. Re-evaluated on every later sighting, so widening a rule rescues these. |
| `deduped` | Already tracked under the same natural key. Not a duplicate issue — a duplicate *sighting*. |
| `admitted` | Matched a rule. Not itself terminal: admitted work is either queued or in flight. |
| `queued` | Admitted, no session. Either the concurrency cap is full or `DRY_RUN` is on. |
| `in_flight` | A real Devin session is running. This is the number that should match the Devin app. |
| `work_delivered` | A pull request that needs nothing further from a human. |
| `awaiting_authorship` | Draft open, waiting on the human paragraph the contribution policy requires. |
| `escalated` | The session handed the work back rather than guessing. |
| `errored` | The session failed. |

**ACUs are reported as unknown, not as zero.** The Devin API returns
`acus_consumed: null` for a session it has not costed yet, and writing that as
`0.0` would state a cost we do not have; `acus_total` and
`acus_per_delivered_pr` are `null` — rendered *no data* — until at least one
session reports a figure, and the average is taken over the sessions that did.

`scoreboard report` emits the funnel and the Sankey edge list. Edges carry
`stream`, so a downstream chart can colour ribbons by work stream while still
converging into shared outcome nodes. Facts live in SQLite (`fact_event`,
`fact_task`, `fact_pr`, `snapshot_daily`) with natural-key upserts, so
re-running a collection is idempotent.

`scoreboard brief --repo jethac/superset` writes the same picture as markdown —
thesis verdicts, fleet roster, outbox queue — for pasting into an issue, a
stand-up note or an email, so the reporting does not depend on someone having
the dashboard open.

## The dashboard, and the two trend lines

The page leads with a thesis in three claims — technical debt down, CI
compute-minutes per pull request down (the P0), more issues shipped over time —
and then four tabs that are the evidence for them: **Trends**, **Where the work
is going**, **Fleet** and **Funnel**. Each claim carries its own status, and a
claim with nothing collected behind it reads *no data* rather than a zero: a
zero is a claim about the repository, an empty table is a claim about the
collection.

**Where the work is going** is the Sankey: intake on the left, every task
leaving through exactly one edge at each stage, losses drawn as named nodes
rather than as missing ribbons. It is present tense on purpose — most of what it
shows has not finished, and a diagram captioned *where the work went* would be
describing a completed programme rather than a running one.

**Fleet** is the roster: every session this deployment knows about, its state,
its pull request, its ACUs, and whether it is running now. Sessions started
outside the deployment appear here too if they carry an adopted tag, marked as
adopted so the funnel never presents them as work it routed. Above it, the
dispatch DAG draws each fan-out as a node with an edge per session terminating
in that session's outcome, so a burst of parallel Devins reads as branching and
you can see how much of a burst actually landed.

**Funnel** is the stage counts and the reconciliation check, stage by stage as
set out under *Reading the funnel* above.

Two of those claims are series, because "we shipped a lot" is not an outcome and
neither is "CI is red less often".

**Technical debt.** Superset already publishes a lint-debt dashboard, and it
says 92 violations. Running the project's own configured rules says thousands —
4,216 at the commit this deployment last measured: the metrics uploader invokes
`npx oxlint --format json` with no `--config`, and the project's config is
`oxlint.json`, which is not oxlint's auto-discovered filename. 85 of the reported 92 are `no-unused-vars`, a rule that config sets to
`off`. That is [issue #3](https://github.com/jethac/superset/issues/3) on the
fork, and [#7](https://github.com/jethac/superset/issues/7) covers the `--quiet`
flag that hides the rest.

The published series also *appears* to fall, from 677 to 92. It does not. Every
drop lines up with rules leaving the tracker: on 2026-05-13, fourteen rules
tracked across 2,075 consecutive runs vanished at non-zero counts totalling 561.
`react-hooks(exhaustive-deps)` disappeared at 238 and measures 381 today, having
grown 60% while invisible to CI and to the dashboard alike.

So [`debt.py`](src/scoreboard/debt.py) treats the measured rule set as the
identity of the instrument. A point whose rule set differs from its predecessor
is marked not comparable, the page draws a break with the entering and departing
rules named, and `series_on_fixed_ruleset` omits — never zero-fills — a rule
that was not measured. A line that slopes smoothly through an instrument change
is the defect this replaces, not the product.

That honesty leaves a reviewer with a series that often refuses to answer "is
debt falling", so `dashboard_payload` emits a second one. `debt` is the headline
total per run under whatever rules that run measured. `debt_comparable` is the
same runs restricted to the intersection — only the rules *every* run measured —
summed per run, with the rule count attached. The page draws the second dashed
under the first, and the legend says which is which. Two things follow, and both
need saying out loud rather than being left to a tooltip. The dashed line sits
lower than the solid one because it counts fewer rules, not because debt is
lower than reported. And a fall in the solid line is not evidence of anything on
its own: the number can drop because violations were fixed or because rules
stopped being measured, and only the dashed line distinguishes those. That is
not a hypothetical failure mode — it is precisely what happened to the published
series between 677 and 92. The `Technical debt falls` thesis card reads the
comparable series for its verdict and says the headline count is not comparable
when it is not, rather than reporting a direction it cannot support.

**CI cost per pull request.** [`cicost.py`](src/scoreboard/cicost.py) records
every job of every pull-request workflow run and reports the median
compute-minutes a change had to buy, broken down by workflow. Read the figure
off the page rather than from here: it is a median over a bounded sample of a
window, so it moves with `--since-days` and `--max-runs`, and quoting a fixed
number in a README is how a measurement becomes a slogan.

What the breakdown is *for* is retirement arguments. `savings_if_removed` in
[`cicost.py`](src/scoreboard/cicost.py) computes what dropping a named workflow
or job would take off the per-pull-request median, which is what turns
[issue #6](https://github.com/jethac/superset/issues/6) — two Cypress shards
kept alive by the last two specs against 25 Playwright ones — into a number a
reader can check rather than a claim they have to accept.

Median, not mean: a couple of retried runs would otherwise decide the answer.
The thesis card refuses a direction until at least two periods carry three or
more pull requests each, because a period holding one pull request describes
that change rather than the project.

## Security model

Assume the events are attacker-influenced: anyone can file an issue with any
title, body and labels.

- **Webhooks fail closed.** No secret configured means every request is
  rejected, not accepted. Signatures are HMAC-SHA256 over the raw body bytes,
  compared with `hmac.compare_digest`.
- **The payload surface is bounded.** Normalisation reads a fixed set of fields
  into a typed model; there is no expression evaluation over payload paths. Only
  `opened`, `reopened` and `labeled` issue actions route, so editing an issue
  cannot re-trigger work repeatedly.
- **Issue text never becomes an instruction.** The prompt names the repository
  and issue by reference; the body is not interpolated into it.
- **Writes are bounded** by `assert_writable` and `DRY_RUN`, both defaulting to
  the safe value.
- **Deduplication is stable** on repository + event type + subject number, so a
  webhook redelivery costs nothing.

### Supply chain

- Base image pinned by **digest**, not tag, in both build stages.
- All dependencies resolved to a hash-pinned `requirements.txt` and installed
  with `--require-hashes --no-deps`, so a compromised index cannot substitute a
  wheel. CI recompiles the lock and diffs it, resolving with `--exclude-newer`
  so the diff fails on a real input change rather than on any upstream release.
  Regenerate with:

  ```bash
  uv pip compile pyproject.toml --generate-hashes --no-header \
    --python-version 3.12 --exclude-newer 2026-08-02T00:00:00Z -o requirements.txt
  uv pip compile requirements-build.in --generate-hashes --no-header \
    --python-version 3.12 --exclude-newer 2026-08-02T00:00:00Z -o requirements-build.txt
  ```

- Every GitHub Action and pre-commit hook pinned to a **full commit SHA**; a CI
  job fails the build if any `uses:` is on a mutable ref.
- Workflows are `permissions: contents: read` by default, opting in per job.
- Container runs as a non-root user, read-only root filesystem, all capabilities
  dropped, `no-new-privileges`; the demo profile runs with `network_mode: none`.
- CodeQL, dependency review, gitleaks, and Dependabot with a 7-day cooldown so a
  malicious release has time to be yanked before we adopt it.
- `.dockerignore` and `.gitignore` both exclude `.env`, keys and local state.

## Command reference

Every command reads credentials from `.env` and writes to the fact store at
`DB_PATH` (default `data/facts.db`); each is argued where it is implemented
above.

| Command | What it does |
| --- | --- |
| `init` | Interactive credential wizard; writes `.env` at 0600 and the repository fields in `scope.yaml`. |
| `serve` | Webhook receiver, report API and dashboard. |
| `intake` | Read issues, route them, dispatch sessions for what is admitted. |
| `sync` | Poll started sessions to their outcome, adopt tagged external sessions, grade against policy. |
| `poll` | `intake` + `sync` on an interval. The scheduled trigger; `--interval 60` in the live profile. |
| `replay` | Route a saved webhook payload from a file. |
| `collect` | Read pull requests back out of GitHub and split them agent vs human. |
| `cicost` | Record billed CI job-minutes per pull-request run. |
| `measure` | Run oxlint against a checkout and record violations per rule. |
| `backfill` | Measure historical commits to produce a debt series. |
| `report` | Print the funnel and the Sankey edge list. |
| `brief` | Write the markdown status report. |
| `outbox` | List drafts waiting on a human authorship paragraph. |
| `simulate` | The offline path: fake Devin client, generated events, for tests only. |

## Development

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy          # strict
.venv/bin/pytest -q
pre-commit install
```

## Limitations

Worth stating plainly rather than being caught on:

- The collector currently reads issues and pull requests. Review rounds, check
  runs and alert burn-down are in the data model and the PRD but not yet
  collected, so the reporting available today is the funnel and the flow, not
  the full quality panel.
- The trend series produced by `scoreboard simulate` are fixture data built on
  measured starting values. The measurements of `apache/superset` are real;
  their movement over the simulated window is not a claim about the fork. Run
  against a live database it charts collected facts instead.
- The wizard cannot confirm Devin's Git integration with a user-scoped key, so
  "Devin can clone the target" is asserted by the first live session rather than
  by setup.
- `scoreboard cicost` reads a bounded sample of pull-request runs, capped by
  `--max-runs`. The median it reports estimates the window; it does not
  enumerate it. Runs that neither the Actions payload nor the commit's pull
  request list can attribute are recorded with no pull-request number and left
  out of the median.
- The headline debt series is not comparable across rule-set changes, and the
  dashboard says so rather than smoothing it. The comparable series answers the
  trend question on the rules common to every run, which is fewer rules than the
  project configures today; it is a like-for-like number, not the project's
  debt.
- `median_hours_to_delivery` measures a task from creation to its last state
  change. For a draft still waiting in the outbox that is the moment the draft
  opened, so the queue is excluded; for one whose paragraph has since been
  posted it is the moment of posting, so the operator's wait is inside the
  figure. Read `waiting_days` in the outbox for the queue on its own.
- ACU figures are whatever the Devin API reports. A session it has not costed is
  recorded as unknown and excluded from the totals rather than counted as zero,
  so `acus_per_delivered_pr` describes the sessions with a figure, not the fleet.
- `apache/superset` is an intake-only repository here. Issues are read from it;
  pull requests are opened on the fork, and `assert_writable` refuses an
  upstream target unless `ALLOW_UPSTREAM_WRITE` is set.
- The wizard's Devin org repository-listing check is advisory. A user-scoped key
  gets a 403 from
  `GET /v3beta1/organizations/{org}/repositories`, which says nothing about
  whether Devin can clone, so the result is reported rather than enforced.
- Any figure with no facts behind it renders as *no data*, not as a zero. A zero
  is a claim about the repository; an empty table is a claim about the
  collection, and the page does not conflate them.
- Throughput without a paired quality metric is trivially gamed by filing
  trivial pull requests. The PRD makes the pairing binding; do not report one
  without the other.
- A before/after comparison alone cannot separate the deployment's effect from
  everything else that changed in the window. The agent-versus-contemporaneous-human
  split is the honest version of the claim.
