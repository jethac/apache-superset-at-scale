"""Runtime configuration, read from the environment only.

Secrets are never read from files inside the image and never written to the fact store. The
defaults are the safe ones: dry-run on, upstream writes off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    devin_api_key: str | None
    devin_base_url: str
    devin_org_id: str | None
    github_token: str | None
    github_api_url: str
    webhook_secret: str | None
    scope_path: Path
    policy_path: Path
    db_path: Path
    measure_repo: str
    dry_run: bool
    allow_upstream_write: bool

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            devin_api_key=os.environ.get("DEVIN_API_KEY"),
            devin_base_url=os.environ.get("DEVIN_BASE_URL", "https://api.devin.ai"),
            devin_org_id=os.environ.get("DEVIN_ORG_ID"),
            github_token=os.environ.get("GITHUB_TOKEN"),
            github_api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
            webhook_secret=os.environ.get("WEBHOOK_SECRET"),
            scope_path=Path(os.environ.get("SCOPE_PATH", "scope.yaml")),
            policy_path=Path(os.environ.get("POLICY_PATH", "policy.yaml")),
            db_path=Path(os.environ.get("DB_PATH", "data/facts.db")),
            measure_repo=os.environ.get("MEASURE_REPO", "apache/superset"),
            dry_run=_flag("DRY_RUN", True),
            allow_upstream_write=_flag("ALLOW_UPSTREAM_WRITE", False),
        )
