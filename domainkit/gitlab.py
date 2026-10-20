"""GitLab API client for branch and CI variable management."""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional

from .errors import ConfigError, GitLabError
from .http import Response, Transport, UrllibTransport


def normalize_project_id(value: str) -> str:
    """Clean a GitLab project reference into an API-safe ``namespace/project`` form.

    GitLab breadcrumbs are often copied as ``Group / Sub Group / Project``, which
    carries stray spaces the API will not accept. Numeric IDs pass through as-is.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit():
        return raw
    for prefix in ("https://", "http://"):
        if raw.lower().startswith(prefix):
            raw = urllib.parse.urlsplit(raw).path
            break
    raw = raw.strip().strip("/")
    if raw.lower().endswith(".git"):
        raw = raw[:-4]
    segments = [segment.strip() for segment in raw.split("/")]
    segments = [segment for segment in segments if segment]
    return "/".join(segments)


class GitLabClient:
    """Branch creation and CI/CD variable management for a single project."""

    def __init__(
        self,
        base_url: str,
        project_id: str,
        token: str,
        *,
        transport: Optional[Transport] = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._project_id = normalize_project_id(project_id)
        self._token = token
        self._transport = transport or UrllibTransport()
        self._project_path = self._make_project_path(self._project_id)

    @property
    def project_id(self) -> str:
        """Normalized project reference used for API calls."""
        return self._project_id

    @property
    def base_url(self) -> str:
        """GitLab instance this client talks to."""
        return self._base

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any) -> "GitLabClient":
        """Load from ``GITLAB_URL``, ``GITLAB_PROJECT_ID``, ``GITLAB_TOKEN``."""
        env = os.environ if env is None else env
        base = (env.get("GITLAB_URL") or "").strip() or "https://gitlab.com"
        project_id = normalize_project_id(env.get("GITLAB_PROJECT_ID") or "")
        token = (env.get("GITLAB_TOKEN") or "").strip()
        if not project_id:
            raise ConfigError(
                "GITLAB_PROJECT_ID is required (numeric ID or namespace/project-path)"
            )
        if not token:
            raise ConfigError("GITLAB_TOKEN is required (personal or project access token)")
        return cls(base, project_id, token, **kwargs)

    @classmethod
    def for_instance(cls, base_url: str, token: str, **kwargs: Any) -> "GitLabClient":
        """Create an instance-level client for user and project discovery."""
        base = (base_url or "").strip() or "https://gitlab.com"
        token = (token or "").strip()
        if not token:
            raise ConfigError("GITLAB_TOKEN is required (personal or project access token)")
        return cls(base, "", token, **kwargs)

    @staticmethod
    def _make_project_path(project_id: str) -> str:
        if not project_id:
            return ""
        return "/api/v4/projects/{0}".format(urllib.parse.quote(project_id, safe=""))

    def _require_project_path(self) -> str:
        if not self._project_path:
            raise ConfigError("GITLAB_PROJECT_ID is required (numeric ID or namespace/project-path)")
        return self._project_path

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Any = None,
    ) -> Response:
        url = "{0}{1}".format(self._base, path)
        if params:
            query = urllib.parse.urlencode([(k, v) for k, v in params.items() if v is not None])
            url = "{0}?{1}".format(url, query) if query else url
        headers = {"PRIVATE-TOKEN": self._token}
        body: Optional[bytes] = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        response = self._transport.request(method, url, headers=headers, body=body)
        if not response.ok:
            raise GitLabError(response.status, url, response.text)
        return response

    def get_project(self) -> Optional[Dict[str, Any]]:
        """Return project metadata, or None when it is missing or not visible."""
        try:
            response = self._request("GET", self._require_project_path())
            return response.json() or None
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def get_current_user(self) -> Dict[str, Any]:
        """Return metadata for the token owner."""
        response = self._request("GET", "/api/v4/user")
        return response.json() or {}

    def list_accessible_projects(
        self,
        *,
        search: str = "",
        membership: bool = True,
    ) -> List[Dict[str, Any]]:
        """List projects visible to this token, following GitLab pagination."""
        found: List[Dict[str, Any]] = []
        page = 1
        while True:
            params: Dict[str, Any] = {"per_page": 100, "page": page}
            if membership:
                params["membership"] = "true"
            if search.strip():
                params["search"] = search.strip()
            response = self._request("GET", "/api/v4/projects", params=params)
            chunk = response.json()
            if isinstance(chunk, list):
                found.extend(chunk)
            next_page = response.header("X-Next-Page", "") or ""
            if next_page:
                page = int(next_page)
                continue
            if isinstance(chunk, list) and len(chunk) == 100:
                page += 1
                continue
            return found

    def get_branch(self, branch: str) -> Optional[Dict[str, Any]]:
        """Return branch metadata or None if it does not exist."""
        encoded = urllib.parse.quote(branch, safe="")
        try:
            response = self._request("GET", "{0}/repository/branches/{1}".format(self._require_project_path(), encoded))
            return response.json() or None
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def create_branch(self, branch: str, ref: str = "dev") -> Dict[str, Any]:
        """Create a branch from ``ref``; idempotent if the branch already exists."""
        existing = self.get_branch(branch)
        if existing:
            return existing
        response = self._request(
            "POST",
            "{0}/repository/branches".format(self._require_project_path()),
            params={"branch": branch, "ref": ref},
        )
        return response.json() or {}

    def list_variables(self) -> List[Dict[str, Any]]:
        """List all CI/CD variables for the project."""
        found: List[Dict[str, Any]] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                "{0}/variables".format(self._require_project_path()),
                params={"per_page": 100, "page": page},
            )
            chunk = response.json()
            if not chunk or not isinstance(chunk, list):
                return found
            found.extend(chunk)
            if len(chunk) < 100:
                return found
            page += 1

    def get_variable(self, key: str) -> Optional[Dict[str, Any]]:
        """Return a CI/CD variable or None if it does not exist."""
        encoded = urllib.parse.quote(key, safe="")
        try:
            response = self._request("GET", "{0}/variables/{1}".format(self._require_project_path(), encoded))
            return response.json() or None
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def set_variable(
        self,
        key: str,
        value: str,
        *,
        protected: bool = False,
        masked: bool = False,
        variable_type: str = "env_var",
        description: str = "",
    ) -> Dict[str, Any]:
        """Create or update a CI/CD variable; idempotent."""
        existing = self.get_variable(key)
        payload: Dict[str, Any] = {
            "value": value,
            "protected": protected,
            "masked": masked,
            "variable_type": variable_type,
        }
        if description:
            payload["description"] = description
        if existing:
            if existing.get("value") == value:
                return existing
            encoded = urllib.parse.quote(key, safe="")
            response = self._request("PUT", "{0}/variables/{1}".format(self._require_project_path(), encoded), json_body=payload)
        else:
            payload["key"] = key
            response = self._request("POST", "{0}/variables".format(self._require_project_path()), json_body=payload)
        return response.json() or {}
