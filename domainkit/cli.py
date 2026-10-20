"""Command line interface for domainkit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import __version__
from .aws import AcmClient, CloudFrontClient, DistributionOptions, OriginSpec, ORIGIN_TYPES
from .batch import run_batch
from .config import Settings
from .errors import ConfigError, DomainKitError
from .gitlab import GitLabClient
from .godaddy import GoDaddyClient
from .naming import normalize_domain
from .sigv4 import Credentials
from .spaceship import SpaceshipClient
from .workflow import Onboarder, Outcome, Plan, RunReport, DEFAULT_CERTIFICATE_TIMEOUT

SYMBOLS = {
    Outcome.CREATED: "+",
    Outcome.UPDATED: "~",
    Outcome.EXISTS: "=",
    Outcome.PLANNED: "?",
    Outcome.SKIPPED: "-",
    Outcome.FAILED: "!",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domainkit",
        description="Onboard a domain: ACM certificate, DNS validation, CloudFront alias, GitLab branch and CI variable.",
    )
    parser.add_argument("--version", action="version", version="domainkit {0}".format(__version__))
    parser.add_argument("--profile", type=Path, help="path to a JSON profile with non-secret defaults")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    names = sub.add_parser("names", help="show derived branch, variable key, and certificate names (no API calls)")
    names.add_argument("domains", nargs="+")
    names.add_argument("--prefix", default=None, help="variable key prefix (default CLOUDFRONT)")

    plan = sub.add_parser("plan", help="read-only preview of what a run would change")
    apply_cmd = sub.add_parser("apply", help="perform the onboarding")
    for command in (plan, apply_cmd):
        command.add_argument("domains", nargs="+", help="one or more domains, e.g. livingtechnology.com")
        command.add_argument("--alias", action="append", default=[], help="extra alias to attach (repeatable)")
        command.add_argument("--distribution-id", help="target CloudFront distribution instead of discovering one")
        command.add_argument("--project", help="GitLab project ID or namespace/path")
        command.add_argument("--base-branch", help="branch to fork from (default dev)")
        command.add_argument("--skip-dns", action="store_true", help="do not touch GoDaddy DNS")
        command.add_argument("--dns-provider", choices=("godaddy", "spaceship"), help="DNS provider override")
        command.add_argument("--skip-gitlab", action="store_true", help="do not touch GitLab")
        command.add_argument(
            "--skip-site-dns", action="store_true", help="do not write the www record or apex forward"
        )
        command.add_argument(
            "--create-distribution",
            action="store_true",
            help="create a CloudFront distribution when none is found (needs --origin)",
        )
        command.add_argument("--origin", help="origin domain, e.g. site.s3-website-us-east-1.amazonaws.com")
        command.add_argument(
            "--origin-type",
            choices=ORIGIN_TYPES,
            default="s3-website",
            help="origin type (default s3-website)",
        )
        command.add_argument("--origin-path", default="", help="origin path prefix, must start with /")
        command.add_argument(
            "--spa", action="store_true", help="map 403 and 404 to /index.html for single-page apps"
        )
    apply_cmd.add_argument(
        "--yes", action="store_true", help="required to make changes without an interactive prompt"
    )
    apply_cmd.add_argument(
        "--wait", action="store_true", help="block until the certificate is issued, then attach it"
    )
    apply_cmd.add_argument(
        "--wait-timeout",
        type=float,
        default=DEFAULT_CERTIFICATE_TIMEOUT,
        help="seconds to wait for issuance (default {0:.0f})".format(DEFAULT_CERTIFICATE_TIMEOUT),
    )

    batch = sub.add_parser("batch", help="run a JSON batch of domains, possibly across several projects")
    batch.add_argument("file", type=Path, help="path to the batch JSON file")
    batch.add_argument("--apply", action="store_true", help="make changes; without it the batch is a preview")
    batch.add_argument("--yes", action="store_true", help="required together with --apply")

    variables = sub.add_parser("variables", help="list CloudFront CI variables already in the project")
    variables.add_argument("--project", help="GitLab project ID or namespace/path")
    variables.add_argument("--prefix", default=None, help="only show keys with this prefix")

    doctor = sub.add_parser("doctor", help="check credentials and connectivity")
    doctor.add_argument("--project", help="GitLab project ID or namespace/path")
    doctor.add_argument("--domain", help="DNS zone to check")
    doctor.add_argument("--dns-provider", choices=("godaddy", "spaceship"))
    return parser


def _settings(args: argparse.Namespace, env: Mapping[str, str]) -> Settings:
    settings = Settings.load(getattr(args, "profile", None), env)
    if getattr(args, "project", None):
        settings.gitlab_project = args.project
    if getattr(args, "base_branch", None):
        settings.base_branch = args.base_branch
    if getattr(args, "prefix", None):
        settings.variable_prefix = args.prefix
    if getattr(args, "dns_provider", None):
        settings.dns_provider = args.dns_provider
    return settings


def cmd_names(args: argparse.Namespace, settings: Settings, out) -> int:
    rows: List[Dict[str, Any]] = []
    for domain in args.domains:
        plan = Plan.build(domain, settings)
        rows.append(
            {
                "domain": plan.domain,
                "service_domain": plan.service_domain,
                "zone": plan.zone,
                "branch": plan.branch,
                "variable_key": plan.variable_key,
                "aliases": plan.aliases,
                "certificate_names": [plan.certificate_primary] + plan.certificate_alternatives,
            }
        )
    if args.json:
        json.dump(rows, out, indent=2)
        out.write("\n")
        return 0
    for row in rows:
        out.write("{0}\n".format(row["domain"]))
        out.write("  service host  {0}\n".format(row["service_domain"]))
        out.write("  branch        {0}\n".format(row["branch"]))
        out.write("  variable key  {0}\n".format(row["variable_key"]))
        out.write("  dns zone      {0}\n".format(row["zone"]))
        out.write("  aliases       {0}\n".format(", ".join(row["aliases"])))
        out.write("  certificate   {0}\n".format(", ".join(row["certificate_names"])))
    return 0


def _make_onboarder(
    settings: Settings,
    env: Mapping[str, str],
    *,
    need_write: bool,
    log,
    gitlab_override: Optional[GitLabClient] = None,
) -> Onboarder:
    credentials = Credentials.from_env(env)
    acm = AcmClient(credentials)
    cloudfront = CloudFrontClient(credentials)

    godaddy: Optional[GoDaddyClient] = None
    try:
        godaddy = GoDaddyClient.from_env(env)
    except ConfigError as exc:
        log("godaddy disabled: {0}".format(exc))

    if gitlab_override is not None:
        gitlab = gitlab_override
    else:
        gitlab = None
        try:
            gitlab_env = dict(env)
            if settings.gitlab_project:
                gitlab_env["GITLAB_PROJECT_ID"] = settings.gitlab_project
            gitlab_env.setdefault("GITLAB_URL", settings.gitlab_url)
            gitlab = GitLabClient.from_env(gitlab_env)
        except ConfigError as exc:
            log("gitlab disabled: {0}".format(exc))

    spaceship = None
    try:
        spaceship = SpaceshipClient.from_env(env, endpoint=settings.spaceship_endpoint)
    except ConfigError as exc:
        if settings.dns_provider == "spaceship":
            log("spaceship disabled: {0}".format(exc))

    if need_write and gitlab is None and godaddy is None and spaceship is None:
        raise ConfigError("apply needs at least one of GoDaddy or GitLab credentials to do useful work")

    return Onboarder(settings, acm=acm, cloudfront=cloudfront, godaddy=godaddy, spaceship=spaceship, gitlab=gitlab, log=log)


def _print_report(report: RunReport, out) -> None:
    plan = report.plan
    header = "plan" if report.dry_run else "apply"
    out.write("{0}: {1}\n".format(header, plan.domain))
    out.write("  branch {0}, variable {1}\n".format(plan.branch, plan.variable_key))
    for step in report.steps:
        out.write("  {0} {1:<18} {2}\n".format(SYMBOLS.get(step.outcome, " "), step.name, step.detail))
    for note in report.notes:
        for line in note.splitlines():
            out.write("  note: {0}\n".format(line))


def _origin_from_args(args: argparse.Namespace) -> Optional[OriginSpec]:
    """Build an OriginSpec from CLI flags, or None when no origin was given."""
    origin = getattr(args, "origin", None)
    if not origin:
        return None
    return OriginSpec(
        domain=origin,
        type=getattr(args, "origin_type", "s3-website"),
        path=getattr(args, "origin_path", "") or "",
    )


def cmd_run(args: argparse.Namespace, settings: Settings, out, env: Mapping[str, str], *, dry_run: bool) -> int:
    if not dry_run and not args.yes:
        out.write("apply makes real changes in AWS, GoDaddy, and GitLab. Re-run with --yes to proceed.\n")
        return 2

    log = (lambda message: None) if args.json else (lambda message: out.write("  .. {0}\n".format(message)))
    onboarder = _make_onboarder(settings, env, need_write=not dry_run, log=log)

    origin = _origin_from_args(args)
    options = DistributionOptions(spa=getattr(args, "spa", False))

    reports: List[RunReport] = []
    for domain in args.domains:
        report = onboarder.run(
            domain,
            aliases=args.alias,
            distribution_id=args.distribution_id,
            dry_run=dry_run,
            wait_for_certificate=getattr(args, "wait", False),
            certificate_timeout=getattr(args, "wait_timeout", DEFAULT_CERTIFICATE_TIMEOUT),
            origin=origin,
            options=options,
            create_distribution=getattr(args, "create_distribution", False),
            skip_dns=args.skip_dns,
            skip_site_dns=getattr(args, "skip_site_dns", False),
            skip_gitlab=args.skip_gitlab,
            dns_provider=getattr(args, "dns_provider", None),
        )
        reports.append(report)
        if not args.json:
            _print_report(report, out)

    if args.json:
        json.dump([report.to_dict() for report in reports], out, indent=2)
        out.write("\n")
    return 1 if any(report.failed for report in reports) else 0


def cmd_variables(args: argparse.Namespace, settings: Settings, out, env: Mapping[str, str]) -> int:
    gitlab_env = dict(env)
    if settings.gitlab_project:
        gitlab_env["GITLAB_PROJECT_ID"] = settings.gitlab_project
    gitlab_env.setdefault("GITLAB_URL", settings.gitlab_url)
    client = GitLabClient.from_env(gitlab_env)
    prefix = (args.prefix or settings.variable_prefix).upper()
    rows = [
        {"key": item.get("key"), "value": item.get("value"), "protected": item.get("protected")}
        for item in client.list_variables()
        if str(item.get("key", "")).upper().startswith(prefix)
    ]
    rows.sort(key=lambda row: str(row["key"]))
    if args.json:
        json.dump(rows, out, indent=2)
        out.write("\n")
        return 0
    if not rows:
        out.write("no variables with prefix {0}\n".format(prefix))
        return 0
    width = max(len(str(row["key"])) for row in rows)
    for row in rows:
        out.write("{0:<{1}}  {2}\n".format(row["key"], width, row["value"]))
    return 0


def cmd_doctor(args: argparse.Namespace, settings: Settings, out, env: Mapping[str, str]) -> int:
    checks: List[Dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    try:
        credentials = Credentials.from_env(env)
        record("aws-credentials", True, "access key {0}...".format(credentials.access_key[:4]))
        try:
            count = len(AcmClient(credentials).list_certificates())
            record("acm", True, "{0} certificate(s) visible in us-east-1".format(count))
        except DomainKitError as exc:
            record("acm", False, str(exc))
        try:
            count = len(CloudFrontClient(credentials).list_distributions())
            record("cloudfront", True, "{0} distribution(s) visible".format(count))
        except DomainKitError as exc:
            record("cloudfront", False, str(exc))
    except ConfigError as exc:
        record("aws-credentials", False, str(exc))

    try:
        if settings.dns_provider == "spaceship":
            client = SpaceshipClient.from_env(env, endpoint=settings.spaceship_endpoint)
            domain = getattr(args, "domain", None)
            if not domain:
                raise ConfigError("Spaceship connection check requires --domain (DNS zone)")
            count = len(client.get_records(domain))
            record("spaceship", True, "{0} DNS record(s); read access verified; dnsrecords:write must be granted separately (not tested by writing)".format(count))
        else:
            count = len(GoDaddyClient.from_env(env).list_domains())
            record("godaddy", True, "{0} active domain(s)".format(count))
    except DomainKitError as exc:
        record(settings.dns_provider, False, str(exc))

    try:
        gitlab_env = dict(env)
        if settings.gitlab_project:
            gitlab_env["GITLAB_PROJECT_ID"] = settings.gitlab_project
        gitlab_env.setdefault("GITLAB_URL", settings.gitlab_url)
        client = GitLabClient.from_env(gitlab_env)
        record("gitlab-target", True, "{0} -> project {1}".format(client.base_url, client.project_id))
        project = client.get_project()
        if project is None:
            record(
                "gitlab",
                False,
                "project {0} not found on {1}, or the token lacks access (check GITLAB_URL / project path)".format(
                    client.project_id, client.base_url
                ),
            )
        else:
            label = project.get("path_with_namespace") or client.project_id
            base = client.get_branch(settings.base_branch)
            if base is None:
                record(
                    "gitlab",
                    False,
                    "project {0} reachable but base branch {1} not found".format(label, settings.base_branch),
                )
            else:
                record("gitlab", True, "project {0}, base branch {1} is present".format(label, settings.base_branch))
    except DomainKitError as exc:
        detail = str(exc)
        if "401" in detail:
            detail += (
                " | HTTP 401 means the token was rejected by this server. "
                "Confirm GITLAB_URL points at the instance that issued the token, "
                "and that the token is active with the api scope."
            )
        elif "403" in detail:
            detail += " | HTTP 403 means the token is valid but lacks permission; CI/CD variables need Maintainer."
        record("gitlab", False, detail)

    if args.json:
        json.dump(checks, out, indent=2)
        out.write("\n")
    else:
        for check in checks:
            out.write("{0} {1:<16} {2}\n".format("ok " if check["ok"] else "err", check["check"], check["detail"]))
    return 0 if all(check["ok"] for check in checks) else 1


def cmd_batch(args: argparse.Namespace, settings: Settings, out, env: Mapping[str, str]) -> int:
    """Run a batch of domains from a JSON file."""
    dry_run = not args.apply
    if not dry_run and not args.yes:
        out.write("batch --apply makes real changes. Re-run with --yes to proceed.\n")
        return 2

    log = lambda message: out.write("{0}\n".format(message))

    def onboarder_factory(item_settings: Settings):
        return _make_onboarder(item_settings, env, need_write=not dry_run, log=log)

    try:
        result = run_batch(
            args.file,
            onboarder_factory=onboarder_factory,
            dry_run=dry_run,
            log=log,
            base_settings=settings,
        )
    except DomainKitError as exc:
        out.write("batch error: {0}\n".format(exc))
        return 1

    if args.json:
        json.dump(result.to_dict(), out, indent=2)
        out.write("\n")
    else:
        out.write("batch complete: {0} succeeded, {1} failed\n".format(
            len(result.reports), len(result.failures)
        ))
        if result.failures:
            out.write("\nfailed domains:\n")
            for domain, error in result.failures:
                out.write("  {0}: {1}\n".format(domain, error))

    return 0 if result.ok else 1


def main(argv: Optional[Sequence[str]] = None, out=None, env: Optional[Mapping[str, str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stream = out or sys.stdout
    environment = os.environ if env is None else env

    try:
        settings = _settings(args, environment)
        if args.command == "names":
            return cmd_names(args, settings, stream)
        if args.command == "plan":
            return cmd_run(args, settings, stream, environment, dry_run=True)
        if args.command == "apply":
            return cmd_run(args, settings, stream, environment, dry_run=False)
        if args.command == "variables":
            return cmd_variables(args, settings, stream, environment)
        if args.command == "doctor":
            return cmd_doctor(args, settings, stream, environment)
        if args.command == "batch":
            return cmd_batch(args, settings, stream, environment)
        parser.error("unknown command {0}".format(args.command))
        return 2
    except DomainKitError as exc:
        stream.write("error: {0}\n".format(exc))
        return 1
    except KeyboardInterrupt:
        stream.write("interrupted\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
