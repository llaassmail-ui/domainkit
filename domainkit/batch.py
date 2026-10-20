"""Batch processing for multiple domains.

A batch file is a JSON object with an optional ``defaults`` mapping and a
required ``domains`` list. Every entry in ``domains`` inherits ``defaults``
and may override any field, which is how one file can send several domains
to one GitLab project or spread them over several projects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .aws import DistributionOptions, OriginSpec
from .config import Settings
from .errors import DomainKitError
from .workflow import Onboarder, RunReport

SETTINGS_FIELDS = ("gitlab_url", "gitlab_project", "base_branch", "variable_prefix", "dns_ttl", "www_alias_for_apex", "dns_provider")


class BatchError(DomainKitError):
    """Raised when batch file validation fails."""


class BatchResult:
    """Result of running a batch."""

    def __init__(self) -> None:
        self.reports: List[RunReport] = []
        self.failures: List[Tuple[str, str]] = []

    @property
    def ok(self) -> bool:
        return len(self.failures) == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "total": len(self.reports) + len(self.failures),
            "succeeded": len(self.reports),
            "failed": len(self.failures),
            "reports": [r.to_dict() for r in self.reports],
            "failures": [{"domain": d, "error": e} for d, e in self.failures],
        }


def load_batch(path: Path) -> Dict[str, Any]:
    """Load and validate a batch JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise BatchError("cannot read batch file: {0}".format(exc)) from exc

    if not isinstance(data, dict):
        raise BatchError("batch file must be a JSON object")

    domains = data.get("domains", [])
    if not isinstance(domains, list):
        raise BatchError("'domains' must be a list")
    if len(domains) == 0:
        raise BatchError("batch file contains no domains")

    return data


def validate_item(item: Dict[str, Any], defaults: Dict[str, Any]) -> None:
    """Validate one batch item against the schema."""
    if not isinstance(item, dict):
        raise BatchError("each domain entry must be a JSON object")

    merged = {**defaults, **item}

    if not merged.get("domain"):
        raise BatchError("item missing required field 'domain'")

    create = merged.get("create_distribution", False)
    has_origin = bool(merged.get("origin"))
    has_dist_id = bool(merged.get("distribution_id"))

    if create and not has_origin and not has_dist_id:
        raise BatchError("item '{0}' requests creation but has no 'origin'".format(merged["domain"]))


def _settings_for(merged: Dict[str, Any], base: Optional[Settings] = None) -> Settings:
    """Build per-domain settings so each entry can target its own project."""
    kwargs: Dict[str, Any] = {}
    if base is not None:
        for field in SETTINGS_FIELDS:
            kwargs[field] = getattr(base, field)
        kwargs["godaddy_endpoint"] = base.godaddy_endpoint
        kwargs["spaceship_endpoint"] = base.spaceship_endpoint
        kwargs["variable_protected"] = base.variable_protected
        kwargs["variable_masked"] = base.variable_masked
        kwargs["wildcard_certificate"] = base.wildcard_certificate
        kwargs["www_alias_for_apex"] = base.www_alias_for_apex
        kwargs["minimum_tls"] = base.minimum_tls
        kwargs["tags"] = dict(base.tags)
    for field in SETTINGS_FIELDS:
        if field in merged:
            kwargs[field] = merged[field]
    return Settings(**kwargs)


def _run_kwargs_for(merged: Dict[str, Any]) -> Dict[str, Any]:
    """Translate one merged batch entry into ``Onboarder.run`` keywords."""
    origin = None
    if merged.get("origin"):
        raw_origin = merged["origin"]
        if isinstance(raw_origin, dict):
            origin_domain = raw_origin.get("domain")
            if not origin_domain:
                raise BatchError("origin object requires 'domain'")
            origin_path = str(raw_origin.get("path") or merged.get("origin_path", ""))
            if origin_path and not origin_path.startswith("/"):
                origin_path = "/" + origin_path
            origin = OriginSpec(
                domain=str(origin_domain),
                type=str(raw_origin.get("type") or merged.get("origin_type", "s3-website")),
                path=origin_path,
            )
        else:
            origin = OriginSpec(
                domain=str(raw_origin),
                type=merged.get("origin_type", "s3-website"),
                path=merged.get("origin_path", ""),
            )

    kwargs: Dict[str, Any] = {
        "wait_for_certificate": merged.get("wait_certificate", False),
        "create_distribution": merged.get("create_distribution", False),
        "skip_dns": merged.get("skip_dns", False),
        "skip_site_dns": merged.get("skip_site_dns", False),
        "skip_gitlab": merged.get("skip_gitlab", False),
        "dns_provider": merged.get("dns_provider"),
        "options": DistributionOptions(
            spa=merged.get("spa", False),
            default_root_object=merged.get("default_root_object", "index.html"),
            price_class=merged.get("price_class", "PriceClass_All"),
        ),
    }

    if origin is not None:
        kwargs["origin"] = origin
    if merged.get("distribution_id"):
        kwargs["distribution_id"] = merged["distribution_id"]
    if merged.get("aliases"):
        kwargs["aliases"] = tuple(merged["aliases"])
    if merged.get("certificate_timeout"):
        kwargs["certificate_timeout"] = float(merged["certificate_timeout"])
    return kwargs


def run_batch(
    batch_path: Path,
    *,
    onboarder_factory: Callable[[Settings], Onboarder],
    dry_run: bool = True,
    log: Optional[Callable[[str], None]] = None,
    base_settings: Optional[Settings] = None,
) -> BatchResult:
    """Run every domain in a batch file, one after another.

    File-level problems raise ``BatchError`` because nothing can run. Anything
    wrong with a single entry, including a schema problem, is recorded as a
    failure so the remaining domains still get their turn.
    """
    data = load_batch(batch_path)
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise BatchError("'defaults' must be a JSON object")
    domains = data["domains"]

    result = BatchResult()

    for index, item in enumerate(domains, 1):
        label = "entry #{0}".format(index)
        try:
            validate_item(item, defaults)
        except BatchError as exc:
            if isinstance(item, dict) and item.get("domain"):
                label = str(item["domain"])
            result.failures.append((label, str(exc)))
            if log:
                log("{0}: {1}".format(label, exc))
            continue

        merged = {**defaults, **item}
        domain = str(merged["domain"])

        if log:
            log("processing {0}".format(domain))

        try:
            onboarder = onboarder_factory(_settings_for(merged, base_settings))
            report = onboarder.run(domain, dry_run=dry_run, **_run_kwargs_for(merged))
            result.reports.append(report)

            if report.failed:
                aborted = next((s for s in report.steps if s.name == "aborted"), None)
                result.failures.append((domain, aborted.detail if aborted else "workflow failed"))
        except Exception as exc:  # noqa: BLE001 - one bad domain must not stop the batch
            result.failures.append((domain, str(exc)))
            if log:
                log("  error: {0}".format(exc))

    return result
