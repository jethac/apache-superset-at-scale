"""Technical-debt series that cannot lie about a change of instrument.

Superset's debt dashboard is fed by `superset-frontend/scripts/oxlint-metrics-uploader.js`, which
runs `npx oxlint --format json` with no `--config`. Superset keeps its configuration in
`oxlint.json`, which is not a filename oxlint auto-discovers, so the uploaded numbers describe
oxlint's default rules rather than Superset's. Measured on apache/superset @ 3b164e42 with oxlint
1.76.0 the bare command reports 92 diagnostics, 85 of them `eslint(no-unused-vars)` — a rule
`oxlint.json` switches off — while `--config oxlint.json` reports 1470.

The existing tracker therefore shows debt falling from 677 to 92 without a single violation being
fixed: on 2026-05-13, fourteen rules that had been measured across 2,075 consecutive runs stopped
being measured at counts totalling 561, and `react-hooks(exhaustive-deps)` left the tracker at 238
while standing at 381 today. A total plotted over a changing rule set is not a measurement of debt,
it is a measurement of the measurer.

So the rule set measured by a run is stored as part of the run and hashed into a `ruleset_id`, and
every point carries whether it is comparable to the point before it. A consumer draws a broken line
rather than a slope when the instrument changed. `series_on_fixed_ruleset` gives the other honest
answer: hold the rule set fixed, and drop the runs that never measured it. An unmeasured rule is
never counted as zero, because zero is a claim about the code and absence is a claim about the run.

Writes reuse `FactStore`'s connection through its transaction helper instead of opening a second
connection to the same file, so debt writes serialise against the rest of the store rather than
competing with it for the database lock.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .store import FactStore

# The linter is pinned by version and by digest, and fetched from the project's own release
# artefacts rather than resolved from a package registry.
#
# Two reasons, and the second is the one that matters more here. `npx oxlint` resolves whatever is
# newest at the moment it runs, downloads it, and executes it — on a host that holds a GitHub token
# with write access to the fork and a live Devin key. Every other dependency in this repository is
# pinned: the base image by digest, Python packages by hash under `--require-hashes`, Actions by
# commit SHA, with a Dependabot cooldown so a malicious release has time to be yanked. One
# unpinned fetch-and-execute undoes all of it.
#
# The second reason is that this module exists to argue a count is uninterpretable without the
# ruleset that produced it. A linter that silently upgrades between the baseline run and today's
# is the same defect the module was written to expose, arriving through the back door: the
# instrument changes and the series reads as a slope. Pinning is what makes two points comparable.
#
# To move to a new oxlint: bump the version, replace the digests, and expect the ruleset to change.
OXLINT_VERSION = "1.77.0"
OXLINT_RELEASE = "https://github.com/oxc-project/oxc/releases/download/apps_v{version}/{asset}"
# Digest of the release tarball, and of the binary inside it. Both are pinned because they are
# checked at different moments: the archive on download, the binary before every run. A cached
# executable that is never re-checked is a trusted file in a directory the linter's own output
# gives an attacker a reason to write to.
OXLINT_ARCHIVE_DIGESTS = {
    "x86_64-unknown-linux-gnu": "09994ebf16e9cb3537d36847cb07ffc6f096557d3137a95db08fe2d67186c58c",
    "aarch64-unknown-linux-gnu": "182acc8df9ae90b5b34188ca2334b81e76352a1bba87b7c728087bd7f7cc395e",
    "x86_64-apple-darwin": "c0fe77e58f54d76afd23800ffb798506c369a48d05bb0068ccbd7f9fe3bd392f",
    "aarch64-apple-darwin": "ef6e6bd5fcf3c20eb9f8120e559408a252c4ffa0baa4af9bff1780c45b8e2bf6",
    "x86_64-pc-windows-msvc": "75b927530a3689fb6c319e37666eb1755312869d38e7e254865116021e9f25c8",
    "aarch64-pc-windows-msvc": "dd4eee251310feaf61945268ab4f5dc0b142baad7264657aad2a20177662a866",
}
OXLINT_BINARY_DIGESTS = {
    "x86_64-unknown-linux-gnu": "db7cdf09d9abe722f7cddc8dbb48dc4152b8c9e9fb3ce54a224622b8701cad86",
    "aarch64-unknown-linux-gnu": "b48546ba1268ef0381b5bfb07ba80edb51cdf15a837b334ded90e5dbd6150af2",
    "x86_64-apple-darwin": "0aef541a0ca8f76f532b8353f15c8ce44bba497c46033ba3feeb6e5c41ea85ec",
    "aarch64-apple-darwin": "d418b2e824b25a05cc426c516258df9c900ad43f7da9d91383c8c4b8fc9283a6",
    "x86_64-pc-windows-msvc": "89e55ef7336180cf5df6613eec813f6923478d41d7fe364217d08e75ccbd8411",
    "aarch64-pc-windows-msvc": "7ff03fc28c9660f401ff9d70cf1683e009d8eeb72b79951d77dbbf663a222add",
}


class LinterUnavailableError(RuntimeError):
    """The pinned linter could not be obtained, or did not match its digest."""


def oxlint_target() -> str:
    """The release artefact for this machine, as a Rust target triple."""
    machine = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(
        machine
    )
    if arch is None:
        raise LinterUnavailableError(f"no pinned oxlint build for architecture {machine!r}")
    if sys.platform.startswith("linux"):
        return f"{arch}-unknown-linux-gnu"
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if sys.platform == "win32":
        return f"{arch}-pc-windows-msvc"
    raise LinterUnavailableError(f"no pinned oxlint build for platform {sys.platform!r}")


def _verify(data: bytes, expected: str, source: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise LinterUnavailableError(
            f"{source} does not match its pinned digest "
            f"(expected {expected}, got {actual}); refusing to run it"
        )


def _sole_member(archive: Path, asset: str) -> bytes:
    """The single file the release archive is expected to hold, read without unpacking to disk.

    Insisting on exactly one member is not fussiness. Extracting a whole archive by name is how
    path traversal and unexpected extra payloads get in; reading one member into memory and never
    honouring a name from the archive avoids the class outright.
    """
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            names = [name for name in bundle.namelist() if not name.endswith("/")]
            if len(names) != 1:
                raise LinterUnavailableError(
                    f"{asset} held {len(names)} files; expected exactly the oxlint binary"
                )
            return bundle.read(names[0])
    with tarfile.open(archive) as tar:
        members = [member for member in tar.getmembers() if member.isfile()]
        if len(members) != 1:
            raise LinterUnavailableError(
                f"{asset} held {len(members)} files; expected exactly the oxlint binary"
            )
        extracted = tar.extractfile(members[0])
        if extracted is None:
            raise LinterUnavailableError(f"{asset} held no readable binary")
        return extracted.read()


def oxlint_binary(cache_dir: Path | None = None) -> Path:
    """Path to the pinned oxlint, fetching it once into `cache_dir` if it is not already there.

    The digest is checked on download and again on a cached copy, so a cache poisoned after the
    fact is caught rather than trusted. A mismatch raises: running an unverified linter to avoid
    an error would produce a number nobody can stand behind, which is worse than no number.
    """
    target = oxlint_target()
    root = cache_dir or Path(os.environ.get("OXLINT_CACHE_DIR") or tempfile.gettempdir())
    binary = root / f"oxlint-{OXLINT_VERSION}-{target}"

    if binary.exists():
        _verify(binary.read_bytes(), OXLINT_BINARY_DIGESTS[target], f"cached {binary.name}")
        return binary

    # Windows releases ship a zip; every other target ships a gzipped tar.
    asset = f"oxlint-{target}.zip" if target.endswith("windows-msvc") else f"oxlint-{target}.tar.gz"
    url = OXLINT_RELEASE.format(version=OXLINT_VERSION, asset=asset)
    try:
        with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - fixed https URL
            payload = response.read()
    except OSError as error:
        raise LinterUnavailableError(
            f"could not fetch the pinned oxlint from {url}: {error}"
        ) from error

    _verify(payload, OXLINT_ARCHIVE_DIGESTS[target], asset)

    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as staging:
        archive = Path(staging) / asset
        archive.write_bytes(payload)
        payload_binary = _sole_member(archive, asset)
        _verify(payload_binary, OXLINT_BINARY_DIGESTS[target], f"oxlint binary in {asset}")
        staged = Path(staging) / "oxlint"
        staged.write_bytes(payload_binary)
        staged.chmod(0o755)
        # Rename last: a reader either sees no binary or sees a fully written, verified one.
        staged.replace(binary)
    return binary


SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_debt (
    repo         TEXT NOT NULL,
    measured_at  TEXT NOT NULL,
    rule         TEXT NOT NULL,
    count        INTEGER NOT NULL,
    PRIMARY KEY (repo, measured_at, rule)
);

-- One row per run. The ruleset_id and config_path are what make the count interpretable, so they
-- live beside the total rather than being recoverable only by inspecting the rule rows.
CREATE TABLE IF NOT EXISTS fact_debt_run (
    repo         TEXT NOT NULL,
    measured_at  TEXT NOT NULL,
    commit_sha   TEXT NOT NULL,
    config_path  TEXT NOT NULL,
    ruleset_id   TEXT NOT NULL,
    total        INTEGER NOT NULL,
    PRIMARY KEY (repo, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_debt_run_repo ON fact_debt_run (repo, measured_at);
"""

DEFAULT_CONFIG = "oxlint.json"
HISTORY_REPO = "apache/superset"
_MISSING = ""


@dataclass(frozen=True)
class DebtObservation:
    """One rule's count within one run. The natural grain of the fact table."""

    measured_at: datetime
    repo: str
    commit_sha: str
    config_path: str
    ruleset_id: str
    rule: str
    count: int


@dataclass(frozen=True)
class DebtPoint:
    measured_at: datetime
    repo: str
    ruleset_id: str
    total: int
    by_rule: dict[str, int]
    comparable_to_previous: bool
    ruleset_change: str


def parse_oxlint_json(payload: str) -> dict[str, int]:
    """Count diagnostics per rule label from oxlint's JSON output.

    oxlint has shipped both a bare array of diagnostics and an object with a `diagnostics` key, and
    names the rule in `code`, `rule` or a plugin/rule pair depending on version. Accepting all of
    them keeps a routine oxlint upgrade from silently emptying the dashboard.
    """
    document: object = json.loads(payload)
    if isinstance(document, dict):
        diagnostics: object = document.get("diagnostics", [])
    else:
        diagnostics = document
    if not isinstance(diagnostics, list):
        raise ValueError("oxlint output contained no diagnostics list")

    counts: dict[str, int] = {}
    for entry in diagnostics:
        if not isinstance(entry, dict):
            raise ValueError("oxlint diagnostic was not an object")
        label = _rule_label(entry)
        counts[label] = counts.get(label, 0) + 1
    return counts


def _rule_label(entry: dict[str, object]) -> str:
    """Normalise a diagnostic to `plugin(rule)`, the form the tracker and oxlint's own text use."""
    for key in ("code", "rule", "ruleId"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            scope = _scope_of(entry)
            if scope and not value.endswith(")"):
                return f"{scope}({value})"
            return value
    raise ValueError("oxlint diagnostic named no rule")


def _scope_of(entry: dict[str, object]) -> str:
    for key in ("plugin", "scope", "source"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def ruleset_id(rules: Iterable[str]) -> str:
    """Identify the instrument: a stable short hash over the sorted unique rule names.

    Two runs share an id exactly when they measured the same rules, which is the only condition
    under which their totals may be subtracted from one another.
    """
    joined = "\n".join(sorted(set(rules)))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def ensure_schema(store: FactStore) -> None:
    with store._tx() as connection:  # noqa: SLF001 - store.py is owned elsewhere; see module docs
        connection.executescript(SCHEMA)


def record_run(store: FactStore, observations: Sequence[DebtObservation]) -> None:
    """Upsert whole runs. Re-recording a run replaces its rows rather than adding to them."""
    if not observations:
        return
    ensure_schema(store)

    runs: dict[tuple[str, str], list[DebtObservation]] = {}
    for observation in observations:
        key = (observation.repo, observation.measured_at.isoformat())
        runs.setdefault(key, []).append(observation)

    with store._tx() as connection:  # noqa: SLF001 - see ensure_schema
        for (repo, measured_at), run in runs.items():
            connection.executemany(
                """
                INSERT INTO fact_debt (repo, measured_at, rule, count) VALUES (?, ?, ?, ?)
                ON CONFLICT(repo, measured_at, rule) DO UPDATE SET count=excluded.count
                """,
                [(repo, measured_at, item.rule, item.count) for item in run],
            )
            first = run[0]
            connection.execute(
                """
                INSERT INTO fact_debt_run (repo, measured_at, commit_sha, config_path, ruleset_id,
                                           total)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, measured_at) DO UPDATE SET
                    commit_sha=excluded.commit_sha,
                    config_path=excluded.config_path,
                    ruleset_id=excluded.ruleset_id,
                    total=excluded.total
                """,
                (
                    repo,
                    measured_at,
                    first.commit_sha,
                    first.config_path,
                    first.ruleset_id,
                    sum(item.count for item in run),
                ),
            )


def ingest_csv(store: FactStore, path: str | Path, default_repo: str = HISTORY_REPO) -> int:
    """Replay the spreadsheet's history, returning the number of rule rows imported.

    The rules present in a row's run define that run's rule set, which is how the historical
    instrument changes become visible without anyone having recorded them. Commit and config are
    left empty because the spreadsheet never captured them, and inventing a value there would
    manufacture exactly the false comparability this module exists to prevent.
    """
    rows: dict[tuple[str, str], dict[str, int]] = {}
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            repo = (row.get("repo") or default_repo).strip()
            measured_at = datetime.fromisoformat(row["measured_at"].strip())
            rule = row["rule"].strip()
            rows.setdefault((repo, measured_at.isoformat()), {})[rule] = int(row["count"])

    observations: list[DebtObservation] = []
    for (repo, stamp), by_rule in rows.items():
        identity = ruleset_id(by_rule)
        observations.extend(
            DebtObservation(
                measured_at=datetime.fromisoformat(stamp),
                repo=repo,
                commit_sha=_MISSING,
                config_path=_MISSING,
                ruleset_id=identity,
                rule=rule,
                count=count,
            )
            for rule, count in by_rule.items()
        )

    record_run(store, observations)
    return len(observations)


def series(store: FactStore, repo: str) -> list[DebtPoint]:
    """Chronological points, each stating whether it may be compared with the one before it.

    The first point is not comparable to anything, so it is marked as a discontinuity too: a
    consumer that starts a line at a point it was told is comparable would be drawing a segment
    that does not exist.
    """
    ensure_schema(store)
    points: list[DebtPoint] = []
    previous_rules: set[str] | None = None
    previous_id: str | None = None

    for measured_at, identity, by_rule in _runs(store, repo):
        rules = set(by_rule)
        if previous_id is None:
            comparable = False
            change = f"first measured ruleset ({len(rules)} rules)"
        elif identity == previous_id:
            comparable = True
            change = ""
        else:
            comparable = False
            change = _describe_change(previous_rules or set(), rules)
        points.append(
            DebtPoint(
                measured_at=measured_at,
                repo=repo,
                ruleset_id=identity,
                total=sum(by_rule.values()),
                by_rule=by_rule,
                comparable_to_previous=comparable,
                ruleset_change=change,
            )
        )
        previous_rules = rules
        previous_id = identity

    return points


def series_on_fixed_ruleset(store: FactStore, repo: str, rules: Sequence[str]) -> list[DebtPoint]:
    """Restrict every point to a caller-chosen rule set, dropping runs that did not measure it.

    This is the only line that may be read as a trend across an instrument change. Runs missing any
    required rule are omitted rather than zero-filled: a rule that was not measured has no count,
    and substituting zero would reproduce the fall from 677 to 92 that never happened.
    """
    ensure_schema(store)
    required = list(dict.fromkeys(rules))
    identity = ruleset_id(required)
    points: list[DebtPoint] = []

    for measured_at, _, by_rule in _runs(store, repo):
        if not set(required).issubset(by_rule):
            continue
        restricted = {rule: by_rule[rule] for rule in required}
        points.append(
            DebtPoint(
                measured_at=measured_at,
                repo=repo,
                ruleset_id=identity,
                total=sum(restricted.values()),
                by_rule=restricted,
                comparable_to_previous=bool(points),
                ruleset_change="",
            )
        )
    return points


def _runs(store: FactStore, repo: str) -> list[tuple[datetime, str, dict[str, int]]]:
    rows = store.query(
        """
        SELECT run.measured_at AS measured_at, run.ruleset_id AS ruleset_id,
               debt.rule AS rule, debt.count AS count
        FROM fact_debt_run AS run
        JOIN fact_debt AS debt
          ON debt.repo = run.repo AND debt.measured_at = run.measured_at
        WHERE run.repo = ?
        ORDER BY run.measured_at, debt.rule
        """,
        (repo,),
    )
    grouped: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        key = (str(row["measured_at"]), str(row["ruleset_id"]))
        grouped.setdefault(key, {})[str(row["rule"])] = int(row["count"])
    return [
        (datetime.fromisoformat(measured_at), identity, by_rule)
        for (measured_at, identity), by_rule in grouped.items()
    ]


def _describe_change(before: set[str], after: set[str]) -> str:
    """Name what entered and what left, because a hash alone tells a reader nothing."""
    entered = sorted(after - before)
    left = sorted(before - after)
    parts: list[str] = []
    if entered:
        parts.append("added " + ", ".join(entered))
    if left:
        parts.append("removed " + ", ".join(left))
    return "; ".join(parts)


def scan(
    repo_path: Path,
    config: str = DEFAULT_CONFIG,
    repo: str | None = None,
    measured_at: datetime | None = None,
    cache_dir: Path | None = None,
) -> list[DebtObservation]:
    """Measure the checkout at `repo_path` with the configuration actually named.

    oxlint exits non-zero whenever it reports a diagnostic, so a non-zero status is expected and
    only an empty document is treated as failure. Output is spooled to a temporary file rather than
    held in a pipe buffer, which is this module's answer to the uploader's `maxBuffer` problem: the
    full-configuration run emits well over a thousand diagnostics.

    `measured_at` defaults to now, which is right for a live run and wrong for a historical one: a
    caller measuring an old commit is describing that commit's date, and stamping every backfilled
    point with the afternoon of the backfill would collapse the series into a single instant.
    """
    frontend = repo_path / "superset-frontend"
    oxlint = oxlint_binary(cache_dir)

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as sink:
        subprocess.run(  # noqa: S603 - fixed argument list, no shell, caller-supplied config only
            [str(oxlint), "--config", config, "--format", "json"],
            cwd=frontend,
            stdout=sink,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        sink.seek(0)
        payload = sink.read()

    counts = parse_oxlint_json(payload)
    stamp = measured_at if measured_at is not None else datetime.now(UTC)
    commit_sha = _head_sha(repo_path)
    identity = ruleset_id(counts)
    return [
        DebtObservation(
            measured_at=stamp,
            repo=repo if repo is not None else repo_path.name,
            commit_sha=commit_sha,
            config_path=config,
            ruleset_id=identity,
            rule=rule,
            count=count,
        )
        for rule, count in sorted(counts.items())
    ]


def _head_sha(repo_path: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is not on PATH, so the measured commit cannot be recorded")
    completed = subprocess.run(  # noqa: S603 - fixed argument list, no shell
        [git, "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()
