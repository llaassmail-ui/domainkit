"""Profile loading: JSON on disk merged with environment variables.

Secrets stay in the environment. The profile only carries non-secret defaults
such as the GitLab project, base branch, and variable prefix.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ConfigError

DEFAULT_PROFILE_NAME = "domainkit.json"


@dataclass
class Settings:
    """Non-secret configuration for a run."""

    gitlab_url: str = "https://gitlab.com"
    gitlab_project: str = ""
    base_branch: str = "dev"
    variable_prefix: str = "CLOUDFRONT"
    variable_protected: bool = False
    variable_masked: bool = False
    godaddy_endpoint: str = "https://api.godaddy.com"
    spaceship_endpoint: str = "https://spaceship.dev/api"
    dns_provider: str = "godaddy"
    dns_ttl: int = 600
    wildcard_certificate: bool = True
    www_alias_for_apex: bool = True
    minimum_tls: str = "TLSv1.2_2021"
    tags: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        path: Optional[Path] = None,
        env: Optional[Mapping[str, str]] = None,
    ) -> "Settings":
        env = os.environ if env is None else env
        data: Dict[str, Any] = {}

        candidate = path
        if candidate is None:
            from_env = (env.get("DOMAINKIT_PROFILE") or "").strip()
            if from_env:
                candidate = Path(from_env)
            elif Path(DEFAULT_PROFILE_NAME).is_file():
                candidate = Path(DEFAULT_PROFILE_NAME)

        if candidate is not None:
            if not candidate.is_file():
                raise ConfigError("profile not found: {0}".format(candidate))
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ConfigError("profile {0} is not valid JSON: {1}".format(candidate, exc)) from exc
            if not isinstance(loaded, dict):
                raise ConfigError("profile {0} must contain a JSON object".format(candidate))
            data.update(loaded)

        settings = cls(
            gitlab_url=str(data.get("gitlab_url") or env.get("GITLAB_URL") or "https://gitlab.com").rstrip("/"),
            gitlab_project=str(data.get("gitlab_project") or env.get("GITLAB_PROJECT_ID") or ""),
            base_branch=str(data.get("base_branch") or env.get("DOMAINKIT_BASE_BRANCH") or "dev"),
            variable_prefix=str(data.get("variable_prefix") or env.get("DOMAINKIT_VAR_PREFIX") or "CLOUDFRONT"),
            variable_protected=_as_bool(data.get("variable_protected"), False),
            variable_masked=_as_bool(data.get("variable_masked"), False),
            godaddy_endpoint=str(
                data.get("godaddy_endpoint") or env.get("GODADDY_API_ENDPOINT") or "https://api.godaddy.com"
            ).rstrip("/"),
            spaceship_endpoint=str(
                data.get("spaceship_endpoint") or env.get("SPACESHIP_API_ENDPOINT") or "https://spaceship.dev/api"
            ).rstrip("/"),
            dns_provider=str(data.get("dns_provider") or env.get("DOMAINKIT_DNS_PROVIDER") or "godaddy").strip().lower(),
            dns_ttl=int(data.get("dns_ttl") or 600),
            wildcard_certificate=_as_bool(data.get("wildcard_certificate"), True),
            www_alias_for_apex=_as_bool(data.get("www_alias_for_apex"), True),
            minimum_tls=str(data.get("minimum_tls") or "TLSv1.2_2021"),
            tags={str(k): str(v) for k, v in (data.get("tags") or {}).items()},
        )
        if settings.dns_ttl < 600:
            raise ConfigError("dns_ttl must be at least 600 seconds (GoDaddy minimum)")
        if settings.dns_provider not in {"godaddy", "spaceship"}:
            raise ConfigError("dns_provider must be 'godaddy' or 'spaceship'")
        return settings


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default
