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

from .devin import HttpDevinClient

DEVIN_SETTINGS_URL = "https://app.devin.ai/settings/integrations"
ENV_PATH = Path(".env")


@dataclass
class CheckResult:
    ok: bool
    detail: str
    blocking: bool = True
    """Whether a failure should stop a live run, as opposed to being unverifiable here."""


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


def check_devin_token(api_key: str, base_url: str = "https://api.devin.ai") -> CheckResult:
    """Check the Devin key on its own, before anything that also depends on the org id.

    Two credentials are pasted into masked prompts one after the other, so the
    interesting failure is not an invalid key but the *wrong* key: paste the GitHub
    token twice and every Devin call returns 403, which reads like an org-id problem
    and sends you looking in the wrong settings page.
    """
    if api_key.startswith(("github_pat_", "ghp_", "gho_", "ghs_")):
        return CheckResult(False, "that is a GitHub token, not a Devin API key")
    try:
        response = httpx.get(
            f"{base_url}/v1/sessions",
            params={"limit": 1},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
        )
    except httpx.HTTPError as error:
        return CheckResult(False, f"could not reach the Devin API: {error}")
    if response.status_code in {401, 403}:
        return CheckResult(False, f"Devin rejected the key (HTTP {response.status_code})")
    if response.status_code >= 400:
        return CheckResult(False, f"unexpected response from Devin (HTTP {response.status_code})")
    return CheckResult(True, "API key accepted")


def check_devin_repo_access(
    api_key: str, org_id: str, repo: str, base_url: str = "https://api.devin.ai"
) -> CheckResult:
    """Verify Devin's git integration can see the repository sessions will work in."""
    client = HttpDevinClient(api_key, base_url, org_id=org_id)
    try:
        repositories = client.list_repositories()
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 403:
            # The organisation endpoint wants an organisation-scoped key. A user key can
            # create sessions perfectly well but cannot enumerate the org's repositories,
            # so the absence of an answer here says nothing about whether Devin can clone.
            return CheckResult(
                False,
                "repository listing needs an organisation-scoped key; "
                f"confirm access manually at {DEVIN_SETTINGS_URL}",
                blocking=False,
            )
        return CheckResult(False, f"could not list Devin repositories: {error}")
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
    """Write secrets to a dotenv file that is user-only from the moment it exists.

    Credentials live here and nowhere else: not in the fact store, not in an image
    layer, not in the process table. The descriptor is opened with the restrictive
    mode rather than chmod-ed afterwards, so there is no window in which another
    local user can read the file.
    """
    lines = [f"{key}={value}" for key, value in values.items() if value]
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def write_scope(target_repo: str, intake_repos: list[str], path: Path) -> None:
    """Rewrite only the repo-selection parts of an existing scope file, preserving the rules.

    A round trip through the YAML loader drops the comments, and the comments are
    where the reasoning behind each rule lives. Accepting the defaults is the common
    path, so the file is left untouched unless the answers actually change it.
    """
    original = path.read_text(encoding="utf-8")
    document = yaml.safe_load(original)
    document["defaults"]["target_repo"] = target_repo
    for rule in document.get("rules", []):
        repos = rule.get("when", {}).get("repo")
        if repos:
            rule["when"]["repo"] = [repo for repo in repos if repo in intake_repos] or repos
    if document == yaml.safe_load(original):
        return
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def run_wizard(scope_path: Path = Path("scope.yaml")) -> int:
    print("Deployment Scoreboard setup\n")

    github_token = _prompt_secret("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN"))
    result = check_github_token(github_token)
    print(f"  github: {result.detail}")
    if not result.ok:
        return 1

    devin_key = _prompt_secret("DEVIN_API_KEY", os.environ.get("DEVIN_API_KEY"))
    devin_token_check = check_devin_token(devin_key) if devin_key else CheckResult(False, "not set")
    print(f"  devin: {devin_token_check.detail}")
    if not devin_token_check.ok:
        return 1
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

    if devin_org:
        access = check_devin_repo_access(devin_key, devin_org, target_repo)
        print(f"  devin: {access.detail}")
        failures += 0 if access.ok or not access.blocking else 1
    else:
        print("  devin: skipped repository access check (needs an org id)")

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
