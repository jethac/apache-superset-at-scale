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
| `POST /webhook/github` | Signed event intake. |

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
  wheel. CI recompiles the lock and diffs it.
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
- The Superset dashboard layer is a follow-up; this repository produces the
  facts it would read.
- Throughput without a paired quality metric is trivially gamed by filing
  trivial pull requests. The PRD makes the pairing binding; do not report one
  without the other.
- A before/after comparison alone cannot separate the deployment's effect from
  everything else that changed in the window. The agent-versus-contemporaneous-human
  split is the honest version of the claim.
