"""The onboarding pipeline.

One domain, in order, each step idempotent and each one reporting what it did:

1. request or reuse an ACM certificate in us-east-1
2. publish the DNS validation records at GoDaddy
3. create the CloudFront distribution, or find the one that already exists
4. wait for the certificate, then attach aliases and the certificate
5. point the site DNS at the distribution (www CNAME + apex 301 forward)
6. create the GitLab branch and set ``CLOUDFRONT_<DOMAIN>_<TLD>``

``dry_run`` walks the same code path but performs reads only, so the plan you
review is the plan that runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence

from .aws import (
    AcmClient,
    Certificate,
    CloudFrontClient,
    Distribution,
    DistributionOptions,
    OriginSpec,
)
from .config import Settings
from .errors import CertificateWaitTimeout, DomainKitError, HttpError, StepError
from .godaddy import (
    DnsRecord,
    GoDaddyClient,
    apex_redirect_url,
    site_records,
    validation_record_for_zone,
)
from .provider import DnsProvider
from .spaceship import SpaceshipClient
from .gitlab import GitLabClient
from .naming import branch_name, certificate_names, is_apex, normalize_domain, registrable_domain, service_domain, variable_key

DEFAULT_CERTIFICATE_TIMEOUT = 600.0


class Outcome(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    EXISTS = "exists"
    PLANNED = "planned"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class StepResult:
    name: str
    outcome: Outcome
    detail: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return self.outcome in {Outcome.CREATED, Outcome.UPDATED}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.name,
            "outcome": self.outcome.value,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class Plan:
    """Everything derived from a domain before any API call happens."""

    domain: str
    zone: str
    branch: str
    variable_key: str
    aliases: List[str]
    service_domain: str
    certificate_primary: str
    certificate_alternatives: List[str]
    distribution_id: Optional[str] = None

    @classmethod
    def build(
        cls,
        domain: str,
        settings: Settings,
        *,
        aliases: Sequence[str] = (),
        distribution_id: Optional[str] = None,
    ) -> "Plan":
        normalized = normalize_domain(domain)
        zone = registrable_domain(normalized)
        served = service_domain(normalized, www_for_apex=settings.www_alias_for_apex)
        extra = [normalize_domain(alias, allow_wildcard=True) for alias in aliases]
        wanted = list(dict.fromkeys([served] + extra))
        primary, alternatives = certificate_names(
            served,
            wildcard=False,
            extra=[name for name in wanted if name != served],
        )
        return cls(
            domain=normalized,
            zone=zone,
            branch=branch_name(normalized),
            variable_key=variable_key(normalized, settings.variable_prefix),
            aliases=wanted,
            service_domain=served,
            certificate_primary=primary,
            certificate_alternatives=alternatives,
            distribution_id=distribution_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "zone": self.zone,
            "branch": self.branch,
            "variable_key": self.variable_key,
            "aliases": self.aliases,
            "service_domain": self.service_domain,
            "certificate": {
                "primary": self.certificate_primary,
                "alternatives": self.certificate_alternatives,
            },
            "distribution_id": self.distribution_id,
        }


@dataclass
class RunReport:
    plan: Plan
    dry_run: bool
    steps: List[StepResult] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def add(self, step: StepResult) -> StepResult:
        self.steps.append(step)
        return step

    @property
    def failed(self) -> bool:
        return any(step.outcome is Outcome.FAILED for step in self.steps)

    @property
    def pending_validation(self) -> bool:
        return any(step.data.get("retryable_reason") == "certificate-validation" for step in self.steps)

    @property
    def changed(self) -> bool:
        return any(step.changed for step in self.steps)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "dry_run": self.dry_run,
            "ok": not self.failed,
            "changed": self.changed,
            "steps": [step.to_dict() for step in self.steps],
            "notes": self.notes,
        }


class Onboarder:
    """Runs the pipeline for one domain."""

    def __init__(
        self,
        settings: Settings,
        *,
        acm: Optional[AcmClient] = None,
        cloudfront: Optional[CloudFrontClient] = None,
        godaddy: Optional[GoDaddyClient] = None,
        spaceship: Optional[SpaceshipClient] = None,
        gitlab: Optional[GitLabClient] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._settings = settings
        self._acm = acm
        self._cloudfront = cloudfront
        self._godaddy = godaddy
        self._spaceship = spaceship
        self._gitlab = gitlab
        self._log = log or (lambda message: None)

    def run(
        self,
        domain: str,
        *,
        aliases: Sequence[str] = (),
        distribution_id: Optional[str] = None,
        dry_run: bool = True,
        wait_for_certificate: bool = False,
        certificate_timeout: float = DEFAULT_CERTIFICATE_TIMEOUT,
        origin: Optional[OriginSpec] = None,
        options: Optional[DistributionOptions] = None,
        create_distribution: bool = False,
        skip_dns: bool = False,
        skip_site_dns: bool = False,
        skip_gitlab: bool = False,
        dns_provider: Optional[str] = None,
    ) -> RunReport:
        plan = Plan.build(domain, self._settings, aliases=aliases, distribution_id=distribution_id)
        report = RunReport(plan=plan, dry_run=dry_run)
        self._log("domain {0} -> branch {1}, variable {2}".format(plan.domain, plan.branch, plan.variable_key))

        certificate: Optional[Certificate] = None
        try:
            provider = self._select_dns_provider(dns_provider)
            provider_name = self._provider_name(dns_provider)
            self._log("DNS provider: {0}".format(provider_name.capitalize()))
            if provider_name == "spaceship" and provider is None and not skip_dns and not dry_run:
                raise StepError("Spaceship credentials are required before creating cloud resources")
            certificate = self._step_certificate(report, plan, dry_run=dry_run)
            if certificate and not skip_dns:
                self._step_dns_validation(report, plan, certificate, dry_run=dry_run, dns_provider=dns_provider)
            elif skip_dns:
                report.add(StepResult("dns-validation", Outcome.SKIPPED, "skipped by request"))

            if certificate and wait_for_certificate and not dry_run and not certificate.issued:
                certificate = self._step_wait_certificate(report, certificate, timeout=certificate_timeout)

            distribution = self._step_cloudfront(
                report,
                plan,
                certificate,
                dry_run=dry_run,
                origin=origin,
                options=options,
                create_distribution=create_distribution,
            )

            if skip_dns or skip_site_dns:
                report.add(StepResult("site-dns", Outcome.SKIPPED, "skipped by request"))
            elif distribution is not None:
                self._step_site_dns(report, plan, distribution, dry_run=dry_run, dns_provider=dns_provider)

            if skip_gitlab:
                report.add(StepResult("gitlab", Outcome.SKIPPED, "skipped by request"))
            else:
                self._step_gitlab(report, plan, distribution, dry_run=dry_run)
        except CertificateWaitTimeout as exc:
            report.add(StepResult("aborted", Outcome.FAILED, str(exc),
                                  {"retryable_reason": "certificate-validation"}))
        except DomainKitError as exc:
            report.add(StepResult("aborted", Outcome.FAILED, str(exc)))
        return report

    def _require_acm(self) -> AcmClient:
        if self._acm is None:
            raise StepError("ACM client is not configured")
        return self._acm

    def _discover_distribution(self, plan: Plan) -> Optional[Distribution]:
        """Find the distribution for this domain, cheapest signal first.

        Four signals, in order of confidence: an ID the operator passed, an
        alias already on a distribution, the comment marker this tool writes,
        and finally the GitLab variable from a previous run. The last two
        matter because a first run can create a distribution and then stop
        before the certificate is issued, leaving no alias to match on.
        """
        cloudfront = self._cloudfront
        if cloudfront is None:
            return None

        if plan.distribution_id:
            return cloudfront.get_distribution(plan.distribution_id)

        for alias in plan.aliases:
            found = cloudfront.find_by_alias(alias)
            if found:
                return found

        found = cloudfront.find_by_marker(plan.domain)
        if found:
            return found

        if self._gitlab is not None:
            try:
                variable = self._gitlab.get_variable(plan.variable_key)
            except DomainKitError:
                variable = None
            recorded = (variable or {}).get("value") or ""
            if recorded.strip():
                try:
                    return cloudfront.get_distribution(recorded.strip())
                except DomainKitError:
                    # A stale ID in CI is a warning, not a reason to stop.
                    return None
        return None

    def _create_distribution(
        self,
        report: RunReport,
        plan: Plan,
        *,
        dry_run: bool,
        origin: Optional[OriginSpec],
        options: Optional[DistributionOptions],
        create_distribution: bool,
    ) -> Optional[Distribution]:
        if not create_distribution:
            report.add(
                StepResult(
                    "cloudfront",
                    Outcome.SKIPPED,
                    "no distribution matched {0}; pass --distribution-id to attach one "
                    "or --create-distribution with --origin to create one".format(", ".join(plan.aliases)),
                )
            )
            return None
        if origin is None:
            report.add(
                StepResult(
                    "cloudfront",
                    Outcome.SKIPPED,
                    "--create-distribution needs an origin; pass --origin <bucket-website-endpoint>",
                )
            )
            return None

        settings = options or DistributionOptions(comment=plan.domain)
        payload = {
            "origin": origin.domain,
            "origin_type": origin.type,
            "aliases": list(plan.aliases),
            "spa": settings.spa,
        }
        if dry_run:
            report.add(
                StepResult(
                    "cloudfront-create",
                    Outcome.PLANNED,
                    "would create a distribution for {0} from origin {1}".format(plan.domain, origin.domain),
                    payload,
                )
            )
            return None

        cloudfront = self._cloudfront
        if cloudfront is None:  # pragma: no cover - guarded by the caller
            return None

        # Aliases are attached in the update pass below, once the certificate is
        # issued; CloudFront rejects an alias whose certificate is not ready.
        created = cloudfront.create_distribution(
            plan.domain,
            origin,
            options=settings,
            certificate_arn=None,
            minimum_protocol_version=self._settings.minimum_tls,
            tags=self._settings.tags or None,
        )
        self._log("created distribution {0} for {1}".format(created.id, plan.domain))
        report.add(
            StepResult(
                "cloudfront-create",
                Outcome.CREATED,
                "created {0} ({1}) from origin {2}".format(created.id, created.domain_name, origin.domain),
                dict(payload, distribution_id=created.id, domain_name=created.domain_name),
            )
        )
        return created

    def _step_certificate(self, report: RunReport, plan: Plan, *, dry_run: bool) -> Optional[Certificate]:
        required = [plan.certificate_primary] + plan.certificate_alternatives
        acm = self._require_acm()
        existing = acm.find_reusable(plan.certificate_primary, required)
        if existing:
            report.add(
                StepResult(
                    "certificate",
                    Outcome.EXISTS,
                    "reusing {0} ({1})".format(existing.arn, existing.status),
                    {"arn": existing.arn, "status": existing.status, "names": list(existing.names)},
                )
            )
            return existing

        if dry_run:
            report.add(
                StepResult(
                    "certificate",
                    Outcome.PLANNED,
                    "would request certificate for {0}".format(", ".join(required)),
                    {"names": required},
                )
            )
            return None

        arn = acm.request_certificate(
            plan.certificate_primary,
            wildcard=False,
            extra_names=plan.certificate_alternatives,
            tags=self._settings.tags or None,
        )
        certificate = acm.wait_for_validation_records(arn)
        report.add(
            StepResult(
                "certificate",
                Outcome.CREATED,
                "requested {0}".format(arn),
                {"arn": arn, "status": certificate.status, "names": list(certificate.names)},
            )
        )
        return certificate

    def _step_dns_validation(
        self,
        report: RunReport,
        plan: Plan,
        certificate: Certificate,
        *,
        dry_run: bool,
        dns_provider: Optional[str] = None,
    ) -> None:
        if certificate.issued and not certificate.validation:
            report.add(StepResult("dns-validation", Outcome.EXISTS, "certificate already issued"))
            return
        if not certificate.validation:
            report.add(
                StepResult("dns-validation", Outcome.SKIPPED, "ACM has not published validation records yet")
            )
            return

        records: List[DnsRecord] = []
        for record in certificate.validation:
            try:
                records.append(
                    validation_record_for_zone(record.name, record.value, plan.zone, self._settings.dns_ttl)
                )
            except DomainKitError as exc:
                report.notes.append("validation record outside zone {0}: {1}".format(plan.zone, exc))

        unique = list({(item.name, item.type, item.data): item for item in records}.values())
        payload = {"records": [item.to_payload() for item in unique]}
        if dry_run:
            report.add(
                StepResult(
                    "dns-validation",
                    Outcome.PLANNED,
                    "would publish {0} validation record(s) in {1}".format(len(unique), plan.zone),
                    payload,
                )
            )
            return

        provider = self._select_dns_provider(dns_provider)
        if provider is None:
            report.add(
                StepResult(
                    "dns-validation",
                    Outcome.SKIPPED,
                    "DNS provider is not configured; publish these records manually",
                    payload,
                )
            )
            return

        changed = provider.upsert_records(plan.zone, unique)
        report.add(
            StepResult(
                "dns-validation",
                Outcome.UPDATED if changed else Outcome.EXISTS,
                "{0} of {1} record(s) written".format(len(changed), len(unique)),
                payload,
            )
        )

    def _step_wait_certificate(self, report: RunReport, certificate: Certificate, *, timeout: float) -> Certificate:
        acm = self._require_acm()
        self._log("waiting for {0} to be issued".format(certificate.arn))
        issued = acm.wait_until_issued(certificate.arn, timeout=timeout)
        report.add(StepResult("certificate-issued", Outcome.EXISTS, "status {0}".format(issued.status)))
        return issued

    def _step_cloudfront(
        self,
        report: RunReport,
        plan: Plan,
        certificate: Optional[Certificate],
        *,
        dry_run: bool,
        origin: Optional[OriginSpec] = None,
        options: Optional[DistributionOptions] = None,
        create_distribution: bool = False,
    ) -> Optional[Distribution]:
        if self._cloudfront is None:
            report.add(StepResult("cloudfront", Outcome.SKIPPED, "CloudFront client is not configured"))
            return None

        distribution = self._discover_distribution(plan)

        if distribution is None:
            created = self._create_distribution(
                report,
                plan,
                dry_run=dry_run,
                origin=origin,
                options=options,
                create_distribution=create_distribution,
            )
            if created is None:
                return None
            distribution = created

        present = {normalize_domain(alias, allow_wildcard=True) for alias in distribution.aliases}
        missing = [alias for alias in plan.aliases if alias not in present]
        remove_aliases = []
        if self._settings.www_alias_for_apex and is_apex(plan.domain) and plan.domain != plan.service_domain:
            apex_alias = normalize_domain(plan.domain)
            if apex_alias in present and apex_alias not in plan.aliases:
                remove_aliases.append(apex_alias)
        # Only an issued certificate that differs from the one already attached
        # justifies an update. Comparing on "is issued" alone would make every
        # rerun PUT an identical config back to CloudFront.
        needs_certificate = bool(
            certificate and certificate.issued and distribution.certificate_arn != certificate.arn
        )

        if not missing and not remove_aliases and not needs_certificate:
            report.add(
                StepResult(
                    "cloudfront",
                    Outcome.EXISTS,
                    "{0} already serves {1}".format(distribution.id, ", ".join(plan.aliases)),
                    {
                        "distribution_id": distribution.id,
                        "domain_name": distribution.domain_name,
                        "certificate_arn": distribution.certificate_arn,
                    },
                )
            )
            return distribution

        if dry_run:
            report.add(
                StepResult(
                    "cloudfront",
                    Outcome.PLANNED,
                    "would attach {0}{1} to {2}".format(
                        ", ".join(missing) or "certificate",
                        "; remove " + ", ".join(remove_aliases) if remove_aliases else "",
                        distribution.id,
                    ),
                    {
                        "distribution_id": distribution.id,
                        "domain_name": distribution.domain_name,
                        "add_aliases": missing,
                        "remove_aliases": remove_aliases,
                        "certificate_arn": certificate.arn if certificate else None,
                    },
                )
            )
            return distribution

        if certificate and not certificate.issued:
            report.add(
                StepResult(
                    "cloudfront",
                    Outcome.SKIPPED,
                    "certificate {0} is {1}; attach aliases after it is issued".format(
                        certificate.arn, certificate.status
                    ),
                    {"distribution_id": distribution.id, "add_aliases": missing, "remove_aliases": remove_aliases},
                )
            )
            return distribution

        updated = self._cloudfront.add_aliases(
            distribution.id,
            plan.aliases,
            certificate_arn=certificate.arn if certificate else None,
            minimum_protocol_version=self._settings.minimum_tls,
            remove_aliases=remove_aliases,
        )
        report.add(
            StepResult(
                "cloudfront",
                Outcome.UPDATED,
                "attached {0}{1} to {2}".format(
                    ", ".join(missing) or "certificate",
                    "; removed " + ", ".join(remove_aliases) if remove_aliases else "",
                    updated.id,
                ),
                {
                    "distribution_id": updated.id,
                    "domain_name": updated.domain_name,
                    "aliases": list(updated.aliases),
                    "removed_aliases": remove_aliases,
                },
            )
        )
        return updated

    def _step_site_dns(
        self,
        report: RunReport,
        plan: Plan,
        distribution: Distribution,
        *,
        dry_run: bool,
        dns_provider: Optional[str] = None,
    ) -> None:
        """Point real traffic at the distribution.

        GoDaddy has no ALIAS record, so the apex cannot be a CNAME. The pattern
        is a CNAME on ``www`` plus a GoDaddy domain forward sending the apex to
        ``https://www.<domain>`` with a 301.
        """
        records = site_records(plan.service_domain, distribution.domain_name, self._settings.dns_ttl)
        payload = {
            "records": [record.to_payload() for record in records],
            "target": distribution.domain_name,
        }

        if dry_run:
            report.add(
                StepResult(
                    "site-dns",
                    Outcome.PLANNED,
                    "would point {0} at {1}".format(
                        ", ".join(record.name for record in records), distribution.domain_name
                    ),
                    payload,
                )
            )
        provider = self._select_dns_provider(dns_provider)
        provider_name = self._provider_name(dns_provider)
        if not dry_run and provider is None:
            report.add(
                StepResult(
                    "site-dns",
                    Outcome.SKIPPED,
                    "DNS provider {0} is not configured; publish these records manually".format(provider_name),
                    payload,
                )
            )
        elif not dry_run:
            changed = provider.upsert_records(plan.zone, records)
            report.add(
                StepResult(
                    "site-dns",
                    Outcome.UPDATED if changed else Outcome.EXISTS,
                    "{0} of {1} record(s) written, target {2}".format(
                        len(changed), len(records), distribution.domain_name
                    ),
                    payload,
                )
            )

        if not is_apex(plan.domain):
            return

        target = apex_redirect_url(plan.domain)
        forward = {"apex": plan.zone, "url": target, "type": "REDIRECT_PERMANENT"}
        if dry_run:
            report.add(
                StepResult(
                    "apex-forward",
                    Outcome.PLANNED,
                    ("Spaceship: configure 301 manually in the console: {0} -> {1}" if provider_name == "spaceship"
                     else "would forward {0} to {1} (301)").format(plan.zone, target),
                    forward,
                )
            )
            return
        if provider_name == "spaceship":
            report.add(
                StepResult(
                    "apex-forward",
                    Outcome.SKIPPED,
                    "Spaceship DNS 已配置。裸域名 301 转发暂不支持 API 自动化，请在 Spaceship 控制台手工配置：{0} -> {1}".format(
                        plan.zone, target
                    ),
                    forward,
                )
            )
            return
        if provider is None or not hasattr(provider, "set_forwarding"):
            report.add(
                StepResult(
                    "apex-forward",
                    Outcome.SKIPPED,
                    "DNS provider does not support forwarding; set the forward for {0} to {1} manually".format(plan.zone, target),
                    forward,
                )
            )
            return
        try:
            wrote = provider.set_forwarding(plan.zone, target)
        except HttpError as exc:
            report.add(
                StepResult(
                    "apex-forward",
                    Outcome.SKIPPED,
                    "GoDaddy forwarding API is unavailable for {0}; set 301 forward to {1} manually ({2})".format(
                        plan.zone, target, exc
                    ),
                    forward,
                )
            )
            return
        report.add(
            StepResult(
                "apex-forward",
                Outcome.UPDATED if wrote else Outcome.EXISTS,
                "{0} forwards to {1} (301)".format(plan.zone, target),
                forward,
            )
        )

    def _provider_name(self, override: Optional[str] = None) -> str:
        return (override or self._settings.dns_provider or "godaddy").strip().lower()

    def _select_dns_provider(self, override: Optional[str] = None) -> Optional[DnsProvider]:
        name = self._provider_name(override)
        if name == "spaceship":
            return self._spaceship
        if name != "godaddy":
            raise StepError("unsupported DNS provider: {0}".format(name))
        return self._godaddy

    def _step_gitlab(
        self,
        report: RunReport,
        plan: Plan,
        distribution: Optional[Distribution],
        *,
        dry_run: bool,
    ) -> None:
        if distribution is None:
            report.add(
                StepResult(
                    "gitlab",
                    Outcome.SKIPPED,
                    "no distribution ID yet, so {0} cannot be set".format(plan.variable_key),
                )
            )
            return

        if dry_run or self._gitlab is None:
            outcome = Outcome.PLANNED if dry_run else Outcome.SKIPPED
            detail = (
                "would create branch {0} from {1} and set {2}={3}".format(
                    plan.branch, self._settings.base_branch, plan.variable_key, distribution.id
                )
                if dry_run
                else "GitLab client is not configured"
            )
            report.add(
                StepResult(
                    "gitlab",
                    outcome,
                    detail,
                    {
                        "branch": plan.branch,
                        "ref": self._settings.base_branch,
                        "variable_key": plan.variable_key,
                        "variable_value": distribution.id,
                    },
                )
            )
            return

        had_branch = self._gitlab.get_branch(plan.branch) is not None
        self._gitlab.create_branch(plan.branch, self._settings.base_branch)
        report.add(
            StepResult(
                "gitlab-branch",
                Outcome.EXISTS if had_branch else Outcome.CREATED,
                "{0} {1} {2}".format(
                    plan.branch, "already existed on" if had_branch else "created from", self._settings.base_branch
                ),
                {"branch": plan.branch, "ref": self._settings.base_branch},
            )
        )

        existing = self._gitlab.get_variable(plan.variable_key)
        self._gitlab.set_variable(
            plan.variable_key,
            distribution.id,
            protected=self._settings.variable_protected,
            masked=self._settings.variable_masked,
            description="CloudFront distribution for {0}".format(plan.domain),
        )
        if existing is None:
            outcome, detail = Outcome.CREATED, "set {0}={1}".format(plan.variable_key, distribution.id)
        elif existing.get("value") == distribution.id:
            outcome, detail = Outcome.EXISTS, "{0} already equals {1}".format(plan.variable_key, distribution.id)
        else:
            outcome = Outcome.UPDATED
            detail = "{0}: {1} -> {2}".format(plan.variable_key, existing.get("value"), distribution.id)
        report.add(
            StepResult(
                "gitlab-variable",
                outcome,
                detail,
                {"variable_key": plan.variable_key, "variable_value": distribution.id},
            )
        )
