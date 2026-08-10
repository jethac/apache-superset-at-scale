# FDE deployment scoreboard

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

## Try it in one command, with no credentials

The simulator runs the real code paths — real scope rules, real orchestrator,
real fact store, real funnel arithmetic — against generated events and a fake
Devin client. It needs no API keys and makes no network calls.

```bash
docker compose --profile demo run --rm simulate
```

Or without Docker:

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/scoreboard simulate --events 48
```

You get the funnel, the Sankey edge list, and a `reconciles` flag. That flag is
the point: it asserts

```
filtered + deduped + work_delivered + escalated + errored + in_flight == triggered
```

so no task can be quietly lost between intake and outcome. It is checked in CI
and the command exits non-zero if it ever fails.

## Configure it for real

```bash
scoreboard init
```

The wizard prompts for a GitHub token and a Devin API key, validates both
against live endpoints, asks which repository is the write target and which
repositories are read for intake, and then does the check that is easy to
forget: it asks Devin which repositories *Devin* can reach. GitHub granting you
access and your Devin org's Git integration granting Devin access are two
different things, and the second one usually fails later, when a session cannot
clone. If the target is not in Devin's list the wizard says so and prints
<https://app.devin.ai/settings/integrations> rather than letting you find out
the hard way.

It writes `.env` with mode `0600` and updates the repository fields in
`scope.yaml`. It never writes a secret into `scope.yaml`, the database, or an
image layer.

### Credentials

| Variable | What it is |
| --- | --- |
| `DEVIN_API_KEY` | Devin service-user or personal API key (`cog_…`), sent as `Authorization: Bearer`. SSO governs webapp and org login; it is not the API credential. |
| `DEVIN_ORG_ID` | Needed only for the org repository-listing check. |
| `GITHUB_TOKEN` | Read on every intake repository; write only on the fork. |
| `WEBHOOK_SECRET` | HMAC secret for GitHub deliveries. Unset means *every* webhook is rejected. |
| `DRY_RUN` | Default `true`: route and record, create no sessions. |
| `ALLOW_UPSTREAM_WRITE` | Default `false`: refuse to target an upstream repository. |

Create the Devin key from a dedicated service user with a minimal role
(<https://docs.devin.ai/api-reference/authentication>) rather than from your own
account, so its actions are attributable and it can be revoked without
disrupting a human.

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
| `GET /dashboard` | Operator page: debt and CI-cost trends, flow, funnel, outbox. |
| `GET /dashboard/data` | The page's single JSON document, if you would rather read it raw. |
| `GET /dashboard/lozenge.min.css` | The vendored design system the page is styled with. |
| `GET /compliance` | Per-pull-request policy evidence: which checks ran, which passed. |
| `POST /webhook/github` | Signed event intake. |

The page is styled with [Lozenge](https://github.com/jethac/lozenge), vendored as a built
stylesheet and served by this container: it charts with the design system's tokens, so the trends
follow the scheme and contrast dial rather than a hard-coded palette. Nothing is fetched at page
load — a CDN reference would be unreviewed code arriving from outside the image, and the container
runs without egress anyway. `src/scoreboard/static/VENDOR.md` records the commit it was built from.

While `DRY_RUN=true` the service routes, deduplicates and records every event
but starts no sessions — which is exactly what you want for the first day
against a live repository, because you can inspect what *would* have been
picked up before anything acts.

### Without webhooks

`scoreboard replay --event issues payload.json` routes a saved delivery, and
`scoreboard intake --repo jethac/superset --repo apache/superset` polls issues
directly. Both are useful when you cannot expose a public URL.

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

**Upstream is read-only.** We read `apache/superset` issues; the resulting pull
request is opened on the fork. `assert_writable` refuses any upstream target
unless `ALLOW_UPSTREAM_WRITE` is explicitly set, so no rule edit alone can cause
a PR against someone else's repository. The `age_days_min` floor on upstream
issues is deliberate too: racing human contributors to fresh issues is the
fastest way to make an agent deployment unwelcome in a project you do not own.

## Contribution policy and the authorship outbox

[`policy.yaml`](policy.yaml) holds the target project's rules for AI-assisted
contributions, selected per repository. Apache Superset's contributor
expectations are not advisory: a pull request that reads as entirely
machine-written is tagged `lacks-human-authorship` and closed, so the
deployment has to know the rules before it writes rather than after.

The profile is applied twice. At intake, `prompt_section` renders it into the
session prompt — `Generated-by:` trailer, AI disclosure section, local test
evidence, adversarial self-review, open as draft. At submit, the same profile
is evaluated against what came back, and every check is stored per pull request
in `fact_policy_check`, so compliance is queryable evidence rather than a claim.

What the agent cannot supply is the paragraph in a human's own voice. Rather
than parking the session until someone is available, it opens a draft and moves
on; the draft lands in the outbox in state `draft_awaiting_authorship`, which is
its own node in the funnel and the Sankey. The age of that queue measures the
operator's latency, not the deployment's.

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
`tests/test_policy.py` asserts its absence. A button that writes the paragraph
would satisfy the check while defeating the rule it implements.

## Reporting

`scoreboard collect --repo jethac/superset --since-days 90` reads pull requests
back out of GitHub and labels each one `agent` or `human` by matching its URL
against attributed tasks. Because the cohort split is derived from GitHub, the
historical baseline can be reconstructed for a period long before the
deployment existed — the comparison that survives scrutiny is agent versus
*contemporaneous* human, not agent versus the past.

`scoreboard report` emits the funnel and the Sankey edge list. Edges carry
`stream`, so a downstream chart can colour ribbons by work stream while still
converging into shared outcome nodes. Facts live in SQLite (`fact_event`,
`fact_task`, `fact_pr`, `snapshot_daily`) with natural-key upserts, so
re-running a collection is idempotent.

## The two trend lines

Above the flow diagram the page carries two series, because "we shipped a lot"
is not an outcome and neither is "CI is red less often".

**Technical debt.** Superset already publishes a lint-debt dashboard, and it
says 92 violations. Running the project's own configured rules says 1,470: the
metrics uploader invokes `npx oxlint --format json` with no `--config`, and the
project's config is `oxlint.json`, which is not oxlint's auto-discovered
filename. 85 of the reported 92 are `no-unused-vars`, a rule that config sets to
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

**CI cost per pull request.** [`cicost.py`](src/scoreboard/cicost.py) records
every job of every pull-request workflow run and reports the median
compute-minutes a change had to buy, broken down by workflow. Measured on
`apache/superset` that is roughly 170 minutes, of which `cypress-matrix` — two
shards kept alive by the last two Cypress specs against 25 Playwright ones — is
about 20. `savings_if_removed` computes that retirement rather than asserting
it, which is what turns [issue #6](https://github.com/jethac/superset/issues/6)
into a number a reader can check.

Median, not mean: a couple of retried runs would otherwise decide the answer.

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
    --python-version 3.12 --exclude-newer 2026-08-01T00:00:00Z -o requirements.txt
  uv pip compile requirements-build.in --generate-hashes --no-header \
    --python-version 3.12 --exclude-newer 2026-08-01T00:00:00Z -o requirements-build.txt
  ```

- Every GitHub Action and pre-commit hook pinned to a **full commit SHA**; a CI
  job fails the build if any `uses:` is on a mutable ref.
- Workflows are `permissions: contents: read` by default, opting in per job.
- Container runs as a non-root user, read-only root filesystem, all capabilities
  dropped, `no-new-privileges`; the demo profile runs with `network_mode: none`.
- CodeQL, dependency review, gitleaks, and Dependabot with a 7-day cooldown so a
  malicious release has time to be yanked before we adopt it.
- `.dockerignore` and `.gitignore` both exclude `.env`, keys and local state.

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
  their movement over the simulated window is not a claim about the fork.
- The CI-cost collector reads workflow jobs from the GitHub API and has been
  exercised against fixtures, not against a live token.
- Throughput without a paired quality metric is trivially gamed by filing
  trivial pull requests. The PRD makes the pairing binding; do not report one
  without the other.
- A before/after comparison alone cannot separate the deployment's effect from
  everything else that changed in the window. The agent-versus-contemporaneous-human
  split is the honest version of the claim.
