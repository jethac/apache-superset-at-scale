"""Interactive setup wizard.

The wizard exists because three separate things must line up before the automation can do
anything, and each fails in a different place at a different time: the GitHub token must be able
to read the repositories in scope, the Devin API key must be valid, and — the one people miss —
*Devin's own git integration* must have access to the repository sessions will be asked to clone.
That last one is not something the API key grants and not something this tool can fix, so the
wizard checks it explicitly and points at the settings page rather than letting a session fail
at clone time.

Secrets are written to `.env` (git-ignored, mode 0600) and never to `scope.yaml`.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

import httpx
import yaml

DEVIN_SETTINGS_URL = "https://app.devin.ai/settings/integrations"
ENV_PATH = Path(".env")


@dataclass
class CheckResult:
    ok: bool
    detail: str


def _prompt_secret(label: str, existing: str | None) -> str:
    if existing:
        keep = input(f"{label} found in environment. Reuse it? [Y/n] ").strip().lower()
        if keep in {"", "y", "yes"}:
            return existing
    return getpass(f"{label}: ").strip()


def check_github_token(token: str, api_url: str = "https://api.github.com") -> CheckResult:
    try:
        response = httpx.get(
            f"{api_url}/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15.0,
        )
    except httpx.HTTPError as error:
        return CheckResult(False, f"could not reach GitHub: {error}")
    if response.status_code != 200:
        return CheckResult(False, f"GitHub rejected the token (HTTP {response.status_code})")
    return CheckResult(True, f"authenticated as {response.json().get('login')}")


def check_repo_readable(
    token: str, repo: str, api_url: str = "https://api.github.com"
) -> CheckResult:
    try:
        response = httpx.get(
            f"{api_url}/repos/{repo}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15.0,
        )
    except httpx.HTTPError as error:
        return CheckResult(False, f"could not reach GitHub: {error}")
    if response.status_code == 404:
        return CheckResult(False, f"{repo} not visible to this token")
    if response.status_code != 200:
        return CheckResult(False, f"HTTP {response.status_code} reading {repo}")
    return CheckResult(True, f"{repo} readable")


def check_devin_repo_access(
    api_key: str, org_id: str, repo: str, base_url: str = "https://api.devin.ai"
) -> CheckResult:
    """Verify Devin's git integration can see the repository sessions will work in."""
    from .devin import HttpDevinClient

    client = HttpDevinClient(api_key, base_url, org_id=org_id)
    try:
        repositories = client.list_repositories()
    except (httpx.HTTPError, ValueError) as error:
        return CheckResult(False, f"could not list Devin repositories: {error}")
    finally:
        client.close()

    paths = {repository.repo_path for repository in repositories}
    if repo not in paths:
        return CheckResult(
            False,
            f"Devin cannot see {repo}. Grant its git integration access at {DEVIN_SETTINGS_URL}, "
            f"then re-run. Visible to Devin: {', '.join(sorted(paths)) or '(none)'}",
        )
    return CheckResult(True, f"Devin can reach {repo}")


def write_env(values: dict[str, str], path: Path = ENV_PATH) -> None:
    """Write secrets to a user-only-readable dotenv file."""
    lines = [f"{key}={value}" for key, value in values.items() if value]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def write_scope(target_repo: str, intake_repos: list[str], path: Path) -> None:
    """Rewrite only the repo-selection parts of an existing scope file, preserving the rules."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["defaults"]["target_repo"] = target_repo
    for rule in document.get("rules", []):
        repos = rule.get("when", {}).get("repo")
        if repos:
            rule["when"]["repo"] = [repo for repo in repos if repo in intake_repos] or repos
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def run_wizard(scope_path: Path = Path("scope.yaml")) -> int:
    print("Deployment Scoreboard setup\n")

    github_token = _prompt_secret("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN"))
    result = check_github_token(github_token)
    print(f"  github: {result.detail}")
    if not result.ok:
        return 1

    devin_key = _prompt_secret("DEVIN_API_KEY", os.environ.get("DEVIN_API_KEY"))
    devin_org = input(
        f"DEVIN_ORG_ID [{os.environ.get('DEVIN_ORG_ID', '')}]: "
    ).strip() or os.environ.get("DEVIN_ORG_ID", "")

    target_repo = input("Repository Devin should open PRs against [jethac/superset]: ").strip()
    target_repo = target_repo or "jethac/superset"
    raw_intake = input(
        f"Repositories to read issues from, comma separated [{target_repo},apache/superset]: "
    ).strip()
    intake_repos = [
        repo.strip()
        for repo in (raw_intake or f"{target_repo},apache/superset").split(",")
        if repo.strip()
    ]

    failures = 0
    for repo in intake_repos:
        check = check_repo_readable(github_token, repo)
        print(f"  github: {check.detail}")
        failures += 0 if check.ok else 1

    if devin_key and devin_org:
        access = check_devin_repo_access(devin_key, devin_org, target_repo)
        print(f"  devin: {access.detail}")
        failures += 0 if access.ok else 1
    else:
        print("  devin: skipped repository access check (needs both API key and org id)")

    write_env(
        {
            "GITHUB_TOKEN": github_token,
            "DEVIN_API_KEY": devin_key,
            "DEVIN_ORG_ID": devin_org,
            "WEBHOOK_SECRET": os.environ.get("WEBHOOK_SECRET", ""),
            "DRY_RUN": "true",
        }
    )
    write_scope(target_repo, intake_repos, scope_path)
    print(f"\nWrote .env (0600) and {scope_path}. DRY_RUN=true until you flip it deliberately.")

    if failures:
        print(f"{failures} check(s) failed — fix those before running with DRY_RUN=false.")
    return 1 if failures else 0
