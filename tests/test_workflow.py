"""End-to-end pipeline tests: plan, apply, idempotent rerun, and guards."""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Optional

from domainkit.aws import AcmClient, CloudFrontClient, OriginSpec
from domainkit.config import Settings
from domainkit.gitlab import GitLabClient
from domainkit.godaddy import GoDaddyClient
from domainkit.http import Response
from domainkit.sigv4 import Credentials
from domainkit.workflow import Onboarder, Outcome, Plan

from .support import FakeTransport, distribution_config_xml, distribution_list_xml, distribution_xml

CREDENTIALS = Credentials("AKIDEXAMPLE", "secret")
DISTRIBUTION_ID = "E1234567890ABC"
CERT_ARN = "arn:aws:acm:us-east-1:1:certificate/abc"
VALIDATION_NAME = "_9c8f1a.www.livingtechnology.com"
VALIDATION_VALUE = "_2b7d0e.acm-validations.aws"

EMPTY_LIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DistributionList xmlns="http://cloudfront.amazonaws.com/doc/2020-05-31/">
  <IsTruncated>false</IsTruncated>
  <Quantity>0</Quantity>
</DistributionList>
"""


def _certificate(status: str) -> Dict[str, Any]:
    return {
        "Certificate": {
            "CertificateArn": CERT_ARN,
            "DomainName": "www.livingtechnology.com",
            "Status": status,
            "SubjectAlternativeNames": ["www.livingtechnology.com"],
            "DomainValidationOptions": [
                {
                    "DomainName": "www.livingtechnology.com",
                    "ResourceRecord": {
                        "Name": VALIDATION_NAME + ".",
                        "Type": "CNAME",
                        "Value": VALIDATION_VALUE + ".",
                    },
                }
            ],
        }
    }

def _step(report, name):
    for step in report.steps:
        if step.name == name:
            return step
    raise AssertionError("no step named {0} in {1}".format(name, [item.name for item in report.steps]))


class WorkflowHarness:
    """Wires an Onboarder to fake AWS, GoDaddy, and GitLab endpoints."""

    def __init__(
        self,
        *,
        certificate_status: str = "ISSUED",
        certificate_exists: bool = True,
        distribution_aliases=("old.example.net",),
        distribution_certificate: Optional[str] = None,
        distribution_found: bool = True,
        branch_exists: bool = False,
        variable_value: Optional[str] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.aws = FakeTransport()
        self.dns = FakeTransport()
        self.git = FakeTransport()
        self.settings = settings or Settings(gitlab_project="group/repo", base_branch="dev")

        summaries = [{"CertificateArn": CERT_ARN, "DomainName": "www.livingtechnology.com"}] if certificate_exists else []
        self.aws.aws_action("ListCertificates", {"CertificateSummaryList": summaries})
        self.aws.aws_action("DescribeCertificate", _certificate(certificate_status))
        self.aws.aws_action("RequestCertificate", {"CertificateArn": CERT_ARN})

        listing = (
            distribution_list_xml(DISTRIBUTION_ID, distribution_aliases, distribution_certificate)
            if distribution_found
            else EMPTY_LIST_XML
        )
        self.aws.xml_route("GET", r"/distribution\?", listing)
        self.aws.xml_route(
            "GET",
            r"/distribution/{0}$".format(DISTRIBUTION_ID),
            distribution_xml(DISTRIBUTION_ID, distribution_aliases, distribution_certificate),
        )
        self.aws.xml_route(
            "GET",
            r"/distribution/{0}/config".format(DISTRIBUTION_ID),
            distribution_config_xml(distribution_aliases, distribution_certificate),
            etag="ETAG1",
        )
        self.aws.xml_route(
            "PUT",
            r"/distribution/{0}/config".format(DISTRIBUTION_ID),
            distribution_xml(
                DISTRIBUTION_ID,
                tuple(distribution_aliases) + ("www.livingtechnology.com",),
                CERT_ARN,
            ),
            etag="ETAG2",
        )

        self.dns.json_route("GET", r"/records/CNAME/", [])
        self.dns.json_route("PUT", r"/records/CNAME/", None)

        self.dns.json_route("GET", r"/v1/domains/forwards/", {"message": "404"}, status=404)
        self.dns.json_route("PUT", r"/v1/domains/forwards/", {"url": "https://www.livingtechnology.com", "type": "REDIRECT_PERMANENT"})

        if branch_exists:
            self.git.json_route("GET", r"/repository/branches/", {"name": "livingtechnology.com"})
        else:
            self.git.json_route("GET", r"/repository/branches/", {"message": "404 Branch Not Found"}, status=404)
        self.git.json_route("POST", r"/repository/branches", {"name": "livingtechnology.com"})

        key = "CLOUDFRONT_LIVINGTECHNOLOGY_COM"
        if variable_value is None:
            self.git.json_route("GET", r"/variables/", {"message": "404"}, status=404)
        else:
            self.git.json_route("GET", r"/variables/", {"key": key, "value": variable_value})
        self.git.json_route("POST", r"/variables", {"key": key, "value": DISTRIBUTION_ID})
        self.git.json_route("PUT", r"/variables/", {"key": key, "value": DISTRIBUTION_ID})

    def onboarder(self, *, with_dns: bool = True, with_gitlab: bool = True) -> Onboarder:
        return Onboarder(
            self.settings,
            acm=AcmClient(CREDENTIALS, transport=self.aws, sleep=lambda _s: None),
            cloudfront=CloudFrontClient(CREDENTIALS, transport=self.aws, sleep=lambda _s: None),
            godaddy=GoDaddyClient("key", "secret", transport=self.dns) if with_dns else None,
            gitlab=(
                GitLabClient("https://gitlab.example.com", "group/repo", "token", transport=self.git)
                if with_gitlab
                else None
            ),
        )

    def run(self, **kwargs: Any):
        """Run the pipeline for the test domain.

        A brand-new domain is not an alias on any distribution yet, so a first
        run names its target explicitly, exactly as an operator would.
        """
        kwargs.setdefault("distribution_id", DISTRIBUTION_ID)
        with_dns = kwargs.pop("with_dns", True)
        with_gitlab = kwargs.pop("with_gitlab", True)
        onboarder = self.onboarder(with_dns=with_dns, with_gitlab=with_gitlab)
        return onboarder.run("livingtechnology.com", **kwargs)

    def writes(self) -> List[str]:
        """Every mutating request sent to any of the three fakes.

        AWS query-protocol reads are POSTs, so read-only ACM actions are filtered
        out by target name.
        """
        found: List[str] = []
        for transport in (self.aws, self.dns, self.git):
            for request in transport.sent:
                if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
                    continue
                if request.target().startswith(("List", "Describe", "Get")):
                    continue
                found.append("{0} {1}".format(request.method, request.url))
        return found


class PlanDerivationTests(unittest.TestCase):
    def test_plan_matches_the_documented_convention(self) -> None:
        from domainkit.workflow import DEFAULT_CERTIFICATE_TIMEOUT
        self.assertEqual(DEFAULT_CERTIFICATE_TIMEOUT, 600.0)
        plan = Plan.build("livingtechnology.com", Settings())
        self.assertEqual(plan.branch, "livingtechnology.com")
        self.assertEqual(plan.variable_key, "CLOUDFRONT_LIVINGTECHNOLOGY_COM")
        self.assertEqual(plan.service_domain, "www.livingtechnology.com")
        self.assertEqual(plan.aliases, ["www.livingtechnology.com"])
        self.assertEqual(plan.certificate_primary, "www.livingtechnology.com")
        self.assertEqual(plan.certificate_alternatives, [])

    def test_subdomain_keeps_the_registrable_zone_for_dns(self) -> None:
        plan = Plan.build("cdn.livingtechnology.com", Settings())
        self.assertEqual(plan.zone, "livingtechnology.com")
        self.assertEqual(plan.branch, "cdn.livingtechnology.com")
        self.assertEqual(plan.variable_key, "CLOUDFRONT_CDN_LIVINGTECHNOLOGY_COM")


class DryRunTests(unittest.TestCase):
    def test_certificate_timeout_is_retryable_and_stops_downstream(self) -> None:
        harness = WorkflowHarness(certificate_status="PENDING_VALIDATION")
        report = harness.run(dry_run=False, wait_for_certificate=True, certificate_timeout=0)
        self.assertTrue(report.pending_validation)
        self.assertFalse(report.to_dict()["ok"])
        self.assertIn("可稍后重试", _step(report, "aborted").detail)
        self.assertFalse(any(step.name.startswith(("cloudfront", "gitlab")) for step in report.steps))
        self.assertEqual(harness.git.sent, [])

    def test_plan_performs_no_writes(self) -> None:
        harness = WorkflowHarness(certificate_status="PENDING_VALIDATION")
        report = harness.run(dry_run=True)
        self.assertEqual(harness.writes(), [])
        self.assertFalse(report.failed)

    def test_plan_reports_the_branch_and_variable_it_would_set(self) -> None:
        harness = WorkflowHarness()
        report = harness.run(dry_run=True)
        step = _step(report, "gitlab")
        self.assertIs(step.outcome, Outcome.PLANNED)
        self.assertEqual(step.data["branch"], "livingtechnology.com")
        self.assertEqual(step.data["variable_key"], "CLOUDFRONT_LIVINGTECHNOLOGY_COM")
        self.assertEqual(step.data["variable_value"], DISTRIBUTION_ID)
        self.assertEqual(step.data["ref"], "dev")

    def test_plan_without_a_certificate_describes_the_request(self) -> None:
        harness = WorkflowHarness(certificate_exists=False)
        report = harness.run(dry_run=True)
        step = _step(report, "certificate")
        self.assertIs(step.outcome, Outcome.PLANNED)
        self.assertEqual(step.data["names"], ["www.livingtechnology.com"])
        self.assertEqual(harness.writes(), [])

    def test_plan_discovers_a_distribution_by_existing_alias(self) -> None:
        harness = WorkflowHarness(distribution_aliases=("www.livingtechnology.com",))
        report = harness.onboarder().run("livingtechnology.com", dry_run=True)
        step = _step(report, "cloudfront")
        self.assertEqual(step.data["distribution_id"], DISTRIBUTION_ID)
        self.assertEqual(harness.writes(), [])


class ApplyTests(unittest.TestCase):
    def test_happy_path_sets_branch_and_variable(self) -> None:
        harness = WorkflowHarness()
        report = harness.run(dry_run=False)
        self.assertFalse(report.failed)

        self.assertIs(_step(report, "cloudfront").outcome, Outcome.UPDATED)
        self.assertIs(_step(report, "gitlab-branch").outcome, Outcome.CREATED)
        variable = _step(report, "gitlab-variable")
        self.assertIs(variable.outcome, Outcome.CREATED)
        self.assertEqual(variable.data["variable_key"], "CLOUDFRONT_LIVINGTECHNOLOGY_COM")
        self.assertEqual(variable.data["variable_value"], DISTRIBUTION_ID)

        created = harness.git.requests_matching("POST", "/repository/branches")[0]
        self.assertIn("branch=livingtechnology.com", created.url)
        self.assertIn("ref=dev", created.url)
        posted = harness.git.requests_matching("POST", "/variables")[0].json()
        self.assertEqual(posted["key"], "CLOUDFRONT_LIVINGTECHNOLOGY_COM")
        self.assertEqual(posted["value"], DISTRIBUTION_ID)

    def test_aliases_and_certificate_reach_cloudfront(self) -> None:
        harness = WorkflowHarness()
        harness.run(dry_run=False)
        put = harness.aws.requests_matching("PUT", "/config")
        self.assertEqual(len(put), 1)
        self.assertIn("<CNAME>www.livingtechnology.com</CNAME>", put[0].text)
        self.assertIn(CERT_ARN, put[0].text)
        self.assertEqual(put[0].headers["If-Match"], "ETAG1")
        # The certificate covers *.livingtechnology.com as a SAN, but CloudFront
        # aliases are explicit; the wildcard is not an alias.
        self.assertNotIn("<CNAME>*.livingtechnology.com</CNAME>", put[0].text)
        self.assertNotIn("<CNAME>livingtechnology.com</CNAME>", put[0].text)

    def test_legacy_apex_alias_is_replaced_with_www_alias(self) -> None:
        harness = WorkflowHarness(distribution_aliases=("example.com",), branch_exists=True)
        harness.aws.xml_route(
            "GET",
            r"/distribution/{0}/config".format(DISTRIBUTION_ID),
            distribution_config_xml(("example.com",), None),
            etag="ETAGLEGACY",
        )
        harness.aws.xml_route(
            "PUT",
            r"/distribution/{0}/config".format(DISTRIBUTION_ID),
            distribution_xml(DISTRIBUTION_ID, ("www.example.com",), CERT_ARN),
            etag="ETAGMIGRATED",
        )
        report = harness.onboarder().run("example.com", distribution_id=DISTRIBUTION_ID, dry_run=False)
        step = _step(report, "cloudfront")
        self.assertIs(step.outcome, Outcome.UPDATED)
        self.assertEqual(step.data["removed_aliases"], ["example.com"])
        body = harness.aws.requests_matching("PUT", "/config")[0].text
        self.assertIn("<CNAME>www.example.com</CNAME>", body)
        self.assertNotIn("<CNAME>example.com</CNAME>", body)
    def test_existing_aliases_are_preserved_on_the_distribution(self) -> None:
        harness = WorkflowHarness()
        harness.run(dry_run=False)
        body = harness.aws.requests_matching("PUT", "/config")[0].text
        self.assertIn("<CNAME>old.example.net</CNAME>", body)
        self.assertIn("<DefaultRootObject>index.html</DefaultRootObject>", body)

    def test_validation_record_is_published_relative_to_the_zone(self) -> None:
        harness = WorkflowHarness(certificate_status="PENDING_VALIDATION")
        report = harness.run(dry_run=False)
        self.assertIs(_step(report, "dns-validation").outcome, Outcome.UPDATED)
        written = harness.dns.requests_matching("PUT", "/records/CNAME/")[0]
        self.assertIn("/records/CNAME/_9c8f1a.www", written.url)
        self.assertEqual(written.json(), [{"data": VALIDATION_VALUE, "ttl": 600}])

    def test_rerun_is_idempotent(self) -> None:
        harness = WorkflowHarness(
            distribution_aliases=("www.livingtechnology.com",),
            distribution_certificate=CERT_ARN,
            branch_exists=True,
            variable_value=DISTRIBUTION_ID,
        )
        harness.dns.json_route(
            "GET",
            r"/records/CNAME/",
            [{"name": "_9c8f1a.www", "type": "CNAME", "data": VALIDATION_VALUE, "ttl": 600}],
        )
        harness.dns.json_route(
            "GET",
            r"/records/CNAME/www$",
            [{"name": "www", "type": "CNAME", "data": "d111111abcdef8.cloudfront.net", "ttl": 600}],
        )
        harness.dns.json_route(
            "GET",
            r"/v1/domains/forwards/",
            {"url": "https://www.livingtechnology.com", "type": "REDIRECT_PERMANENT"},
        )
        report = harness.onboarder().run("livingtechnology.com", dry_run=False)

        self.assertFalse(report.changed, [step.to_dict() for step in report.steps])
        self.assertEqual(harness.writes(), [])
        for name in ("certificate", "cloudfront", "site-dns", "apex-forward", "gitlab-branch", "gitlab-variable"):
            self.assertIs(_step(report, name).outcome, Outcome.EXISTS, name)

    def test_unavailable_godaddy_forwarding_api_is_guidance_not_failure(self) -> None:
        harness = WorkflowHarness(branch_exists=True, variable_value=DISTRIBUTION_ID)
        harness.dns.json_route(
            "GET",
            r"/v1/domains/forwards/",
            {"code": "NOT_FOUND", "message": "There is no method to handle request"},
            status=404,
        )

        def missing_route(_request):
            return Response(404, {}, b'{"message":"There is no method to handle request"}')

        harness.dns.add(lambda r: r.method in {"PUT", "POST"} and "/forwards/" in r.url, missing_route)
        report = harness.onboarder().run("livingtechnology.com", dry_run=False)

        self.assertFalse(report.failed)
        step = _step(report, "apex-forward")
        self.assertIs(step.outcome, Outcome.SKIPPED)
        self.assertIn("set 301 forward", step.detail)
        self.assertIs(_step(report, "gitlab-variable").outcome, Outcome.EXISTS)
    def test_variable_drift_is_corrected(self) -> None:
        harness = WorkflowHarness(
            distribution_aliases=("www.livingtechnology.com",),
            distribution_certificate=CERT_ARN,
            branch_exists=True,
            variable_value="ESTALE0000000",
        )
        report = harness.onboarder().run("livingtechnology.com", dry_run=False)
        step = _step(report, "gitlab-variable")
        self.assertIs(step.outcome, Outcome.UPDATED)
        self.assertIn("ESTALE0000000", step.detail)
        self.assertEqual(harness.git.requests_matching("PUT", "/variables")[0].json()["value"], DISTRIBUTION_ID)

    def test_certificate_drift_is_reattached(self) -> None:
        harness = WorkflowHarness(
            distribution_aliases=("www.livingtechnology.com",),
            distribution_certificate="arn:aws:acm:us-east-1:1:certificate/stale",
            branch_exists=True,
            variable_value=DISTRIBUTION_ID,
        )
        report = harness.onboarder().run("livingtechnology.com", dry_run=False)
        self.assertIs(_step(report, "cloudfront").outcome, Outcome.UPDATED)
        self.assertIn(CERT_ARN, harness.aws.requests_matching("PUT", "/config")[0].text)


class GuardTests(unittest.TestCase):
    def test_pending_certificate_is_not_attached_to_cloudfront(self) -> None:
        harness = WorkflowHarness(certificate_status="PENDING_VALIDATION")
        report = harness.run(dry_run=False)
        step = _step(report, "cloudfront")
        self.assertIs(step.outcome, Outcome.SKIPPED)
        self.assertIn("PENDING_VALIDATION", step.detail)
        self.assertEqual(harness.aws.requests_matching("PUT", "/config"), [])

    def test_pending_certificate_still_lets_gitlab_proceed(self) -> None:
        harness = WorkflowHarness(certificate_status="PENDING_VALIDATION")
        report = harness.run(dry_run=False)
        self.assertIs(_step(report, "gitlab-variable").outcome, Outcome.CREATED)

    def test_missing_distribution_skips_gitlab(self) -> None:
        harness = WorkflowHarness(distribution_found=False)
        report = harness.run(distribution_id=None, dry_run=False)
        self.assertIs(_step(report, "cloudfront").outcome, Outcome.SKIPPED)
        gitlab = _step(report, "gitlab")
        self.assertIs(gitlab.outcome, Outcome.SKIPPED)
        self.assertIn("CLOUDFRONT_LIVINGTECHNOLOGY_COM", gitlab.detail)
        # Discovery may read the recorded variable as a last-resort signal,
        # but nothing may be written when there is no distribution to record.
        self.assertEqual([sent for sent in harness.git.sent if sent.method != "GET"], [])

    def test_explicit_distribution_id_skips_discovery(self) -> None:
        harness = WorkflowHarness(distribution_found=False)
        report = harness.run(dry_run=False)
        self.assertIs(_step(report, "cloudfront").outcome, Outcome.UPDATED)
        self.assertEqual(harness.aws.requests_matching("GET", "/distribution?"), [])

    def test_skip_flags_are_respected(self) -> None:
        harness = WorkflowHarness(certificate_status="PENDING_VALIDATION")
        report = harness.run(dry_run=False, skip_dns=True, skip_gitlab=True)
        self.assertIs(_step(report, "dns-validation").outcome, Outcome.SKIPPED)
        self.assertIs(_step(report, "gitlab").outcome, Outcome.SKIPPED)
        self.assertEqual(harness.dns.sent, [])
        self.assertEqual(harness.git.sent, [])

    def test_missing_dns_client_reports_the_records_to_publish(self) -> None:
        harness = WorkflowHarness(certificate_status="PENDING_VALIDATION")
        report = harness.run(with_dns=False, dry_run=False)
        step = _step(report, "dns-validation")
        self.assertIs(step.outcome, Outcome.SKIPPED)
        self.assertEqual(
            step.data["records"],
            [{"name": "_9c8f1a.www", "type": "CNAME", "data": VALIDATION_VALUE, "ttl": 600}],
        )

    def test_api_failure_is_reported_not_raised(self) -> None:
        harness = WorkflowHarness()
        harness.aws.aws_action("ListCertificates", {"__type": "AccessDeniedException", "message": "nope"}, status=403)
        report = harness.run(dry_run=False)
        self.assertTrue(report.failed)
        self.assertIn("AccessDenied", _step(report, "aborted").detail)

    def test_report_serializes_to_json(self) -> None:
        harness = WorkflowHarness()
        report = harness.run(dry_run=True)
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertTrue(payload["dry_run"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["plan"]["variable_key"], "CLOUDFRONT_LIVINGTECHNOLOGY_COM")
        names = [step["step"] for step in payload["steps"]]
        self.assertIn("site-dns", names)
        self.assertIn("apex-forward", names)


class DiscoveryOrderTests(unittest.TestCase):
    """分配发现的四级顺序：显式 ID → alias → marker → GitLab 变量"""

    def test_explicit_id_wins_over_every_other_signal(self) -> None:
        harness = WorkflowHarness(distribution_aliases=("www.livingtechnology.com",))
        report = harness.run(dry_run=True)
        self.assertEqual(_step(report, "cloudfront").data["distribution_id"], DISTRIBUTION_ID)
        # 显式 ID 时不做列举查询
        self.assertEqual(harness.aws.requests_matching("GET", "/distribution?"), [])

    def test_alias_lookup_is_used_when_no_id_is_given(self) -> None:
        harness = WorkflowHarness(distribution_aliases=("www.livingtechnology.com",))
        report = harness.onboarder().run("livingtechnology.com", dry_run=True)
        self.assertEqual(_step(report, "cloudfront").data["distribution_id"], DISTRIBUTION_ID)
        self.assertTrue(harness.aws.requests_matching("GET", "/distribution?"))

    def test_marker_lookup_finds_a_half_finished_run(self) -> None:
        """别名还没挂上，但 comment 里有 marker，应当被认领而不是再建一个"""
        harness = WorkflowHarness(distribution_found=False)
        harness.aws.xml_route(
            "GET",
            r"/distribution\?",
            distribution_list_xml(
                DISTRIBUTION_ID,
                (),
                None,
                comment="domainkit managed [domainkit:livingtechnology.com]",
            ),
        )
        report = harness.onboarder().run("livingtechnology.com", dry_run=True)
        self.assertEqual(_step(report, "cloudfront").data["distribution_id"], DISTRIBUTION_ID)

    def test_gitlab_variable_is_the_last_resort_signal(self) -> None:
        """没有别名也没有 marker 时，回落到上一轮写进 GitLab 的 ID"""
        harness = WorkflowHarness(distribution_found=False, variable_value=DISTRIBUTION_ID)
        harness.aws.xml_route("GET", r"/distribution\?", EMPTY_LIST_XML)
        report = harness.onboarder().run("livingtechnology.com", dry_run=True)
        self.assertEqual(_step(report, "cloudfront").data["distribution_id"], DISTRIBUTION_ID)
        self.assertTrue(harness.git.requests_matching("GET", "/variables/"))

    def test_stale_gitlab_id_does_not_abort_the_run(self) -> None:
        harness = WorkflowHarness(distribution_found=False, variable_value="EGONE00000000")
        harness.aws.xml_route("GET", r"/distribution\?", EMPTY_LIST_XML)
        harness.aws.xml_route(
            "GET",
            r"/distribution/EGONE00000000$",
            "<ErrorResponse><Error><Code>NoSuchDistribution</Code>"
            "<Message>gone</Message></Error></ErrorResponse>",
            status=404,
        )
        report = harness.onboarder().run("livingtechnology.com", dry_run=True)
        self.assertIs(_step(report, "cloudfront").outcome, Outcome.SKIPPED)
        self.assertFalse(report.failed)


class CreateDistributionTests(unittest.TestCase):
    """--create-distribution 的规划、创建与重跑行为"""

    ORIGIN = "site.s3-website-us-east-1.amazonaws.com"

    def _harness(self) -> WorkflowHarness:
        harness = WorkflowHarness(distribution_found=False)
        harness.aws.xml_route("GET", r"/distribution\?", EMPTY_LIST_XML)
        return harness

    def test_dry_run_plans_the_creation_without_writing(self) -> None:
        harness = self._harness()
        report = harness.onboarder().run(
            "livingtechnology.com",
            dry_run=True,
            create_distribution=True,
            origin=OriginSpec(self.ORIGIN),
        )
        step = _step(report, "cloudfront-create")
        self.assertIs(step.outcome, Outcome.PLANNED)
        self.assertEqual(step.data["origin"], self.ORIGIN)
        self.assertEqual(step.data["origin_type"], "s3-website")
        self.assertEqual(harness.writes(), [])

    def test_create_without_origin_is_skipped_with_guidance(self) -> None:
        harness = self._harness()
        report = harness.onboarder().run(
            "livingtechnology.com", dry_run=False, create_distribution=True
        )
        step = _step(report, "cloudfront")
        self.assertIs(step.outcome, Outcome.SKIPPED)
        self.assertIn("--origin", step.detail)

    def test_missing_distribution_without_the_flag_explains_both_options(self) -> None:
        harness = self._harness()
        report = harness.onboarder().run("livingtechnology.com", dry_run=False)
        step = _step(report, "cloudfront")
        self.assertIs(step.outcome, Outcome.SKIPPED)
        self.assertIn("--distribution-id", step.detail)
        self.assertIn("--create-distribution", step.detail)

    def test_creation_posts_the_config_and_records_the_new_id(self) -> None:
        harness = self._harness()
        harness.aws.xml_route(
            "GET",
            r"/distribution/ENEWLY0000001/config",
            distribution_config_xml((), None, comment="domainkit managed [domainkit:livingtechnology.com]"),
            etag="ETAGNEW",
        )
        harness.aws.xml_route(
            "PUT",
            r"/distribution/ENEWLY0000001/config",
            distribution_xml("ENEWLY0000001", ("www.livingtechnology.com",), CERT_ARN),
            etag="ETAGNEW2",
        )
        harness.aws.add(
            lambda request: request.method == "POST" and "/distribution" in request.url,
            lambda request: Response(
                201,
                {"ETag": "ETAGNEW"},
                distribution_xml("ENEWLY0000001").encode("utf-8"),
            ),
        )
        report = harness.onboarder().run(
            "livingtechnology.com",
            dry_run=False,
            create_distribution=True,
            origin=OriginSpec(self.ORIGIN),
        )
        step = _step(report, "cloudfront-create")
        self.assertIs(step.outcome, Outcome.CREATED)
        self.assertEqual(step.data["distribution_id"], "ENEWLY0000001")

        body = harness.aws.requests_matching("POST", "/distribution")[0].text
        self.assertIn(self.ORIGIN, body)
        self.assertIn("[domainkit:livingtechnology.com]", body)
        # 别名要等证书签发后在更新步骤里挂，创建请求里不带
        self.assertNotIn("<CNAME>www.livingtechnology.com</CNAME>", body)

    def test_rerun_claims_the_existing_distribution_instead_of_creating_a_second(self) -> None:
        harness = WorkflowHarness(
            distribution_aliases=("www.livingtechnology.com",),
            distribution_certificate=CERT_ARN,
            branch_exists=True,
            variable_value=DISTRIBUTION_ID,
        )
        report = harness.onboarder().run(
            "livingtechnology.com",
            dry_run=False,
            create_distribution=True,
            origin=OriginSpec(self.ORIGIN),
        )
        self.assertIs(_step(report, "cloudfront").outcome, Outcome.EXISTS)
        self.assertEqual(harness.aws.requests_matching("POST", "/distribution"), [])
        with self.assertRaises(AssertionError):
            _step(report, "cloudfront-create")


if __name__ == "__main__":
    unittest.main()
