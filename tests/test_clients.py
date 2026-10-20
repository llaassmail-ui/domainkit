"""ACM, CloudFront, GitLab, and GoDaddy clients against a fake transport."""

from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from domainkit.aws import AcmClient, CloudFrontClient, DistributionOptions, OriginSpec
from domainkit.errors import AwsError, ConfigError, GitLabError
from domainkit.gitlab import GitLabClient, normalize_project_id
from domainkit.godaddy import (
    REDIRECT_PERMANENT,
    REDIRECT_TEMPORARY,
    DnsRecord,
    GoDaddyClient,
    apex_guidance,
    apex_redirect_url,
    site_records,
    validation_record_for_zone,
)
from domainkit.http import Response, UrllibTransport
from domainkit.sigv4 import Credentials

from .support import (
    DISTRIBUTION_CONFIG_XML,
    FakeTransport,
    distribution_list_xml,
    distribution_xml,
)

CREDENTIALS = Credentials("AKIDEXAMPLE", "secret")
NAMESPACE = "http://cloudfront.amazonaws.com/doc/2020-05-31/"


def _acm(transport: FakeTransport) -> AcmClient:
    return AcmClient(CREDENTIALS, transport=transport, sleep=lambda _seconds: None)


def _cloudfront(transport: FakeTransport) -> CloudFrontClient:
    return CloudFrontClient(CREDENTIALS, transport=transport, sleep=lambda _seconds: None)


class AcmTests(unittest.TestCase):
    def test_request_certificate_sends_apex_and_wildcard(self) -> None:
        transport = FakeTransport().aws_action(
            "RequestCertificate", {"CertificateArn": "arn:aws:acm:us-east-1:1:certificate/abc"}
        )
        arn = _acm(transport).request_certificate("Example.com")
        self.assertEqual(arn, "arn:aws:acm:us-east-1:1:certificate/abc")

        payload = transport.sent[-1].json()
        self.assertEqual(payload["DomainName"], "example.com")
        self.assertEqual(payload["SubjectAlternativeNames"], ["*.example.com"])
        self.assertEqual(payload["ValidationMethod"], "DNS")
        self.assertTrue(payload["IdempotencyToken"])
        self.assertLessEqual(len(payload["IdempotencyToken"]), 32)

    def test_request_is_signed_against_us_east_1(self) -> None:
        transport = FakeTransport().aws_action("RequestCertificate", {"CertificateArn": "arn:1"})
        _acm(transport).request_certificate("example.com")
        authorization = transport.sent[-1].headers["Authorization"]
        self.assertIn("/us-east-1/acm/aws4_request", authorization)

    def test_describe_extracts_validation_records(self) -> None:
        transport = FakeTransport().aws_action(
            "DescribeCertificate",
            {
                "Certificate": {
                    "CertificateArn": "arn:1",
                    "DomainName": "example.com",
                    "Status": "PENDING_VALIDATION",
                    "SubjectAlternativeNames": ["example.com", "*.example.com"],
                    "DomainValidationOptions": [
                        {
                            "DomainName": "example.com",
                            "ResourceRecord": {
                                "Name": "_abc.example.com.",
                                "Type": "CNAME",
                                "Value": "_xyz.acm-validations.aws.",
                            },
                        },
                        {"DomainName": "*.example.com"},
                    ],
                }
            },
        )
        certificate = _acm(transport).describe("arn:1")
        self.assertFalse(certificate.issued)
        self.assertEqual(len(certificate.validation), 1)
        record = certificate.validation[0]
        self.assertEqual(record.name, "_abc.example.com")
        self.assertEqual(record.value, "_xyz.acm-validations.aws")

    def test_find_reusable_prefers_issued_certificate(self) -> None:
        transport = FakeTransport()
        transport.aws_action(
            "ListCertificates",
            {
                "CertificateSummaryList": [
                    {"CertificateArn": "arn:pending", "DomainName": "example.com"},
                    {"CertificateArn": "arn:issued", "DomainName": "example.com"},
                    {"CertificateArn": "arn:other", "DomainName": "other.com"},
                ]
            },
        )

        def describe(request):
            arn = request.json()["CertificateArn"]
            status = "ISSUED" if arn == "arn:issued" else "PENDING_VALIDATION"
            body = {
                "Certificate": {
                    "CertificateArn": arn,
                    "DomainName": "example.com",
                    "Status": status,
                    "SubjectAlternativeNames": ["example.com", "*.example.com"],
                }
            }
            import json as _json

            return Response(200, {}, _json.dumps(body).encode("utf-8"), request.url)

        transport.add(lambda request: request.target() == "DescribeCertificate", describe)
        found = _acm(transport).find_reusable("example.com", ["example.com", "*.example.com"])
        self.assertIsNotNone(found)
        self.assertEqual(found.arn, "arn:issued")

    def test_find_reusable_ignores_certificates_that_do_not_cover(self) -> None:
        transport = FakeTransport()
        transport.aws_action(
            "ListCertificates", {"CertificateSummaryList": [{"CertificateArn": "arn:1", "DomainName": "example.com"}]}
        )
        transport.aws_action(
            "DescribeCertificate",
            {
                "Certificate": {
                    "CertificateArn": "arn:1",
                    "DomainName": "example.com",
                    "Status": "ISSUED",
                    "SubjectAlternativeNames": ["example.com"],
                }
            },
        )
        found = _acm(transport).find_reusable("example.com", ["example.com", "*.example.com"])
        self.assertIsNone(found)

    def test_modeled_error_is_surfaced(self) -> None:
        transport = FakeTransport().aws_action(
            "RequestCertificate",
            {"__type": "com.amazonaws.acm#LimitExceededException", "message": "too many certificates"},
            status=400,
        )
        with self.assertRaises(AwsError) as caught:
            _acm(transport).request_certificate("example.com")
        self.assertEqual(caught.exception.code, "LimitExceededException")
        self.assertIn("too many certificates", str(caught.exception))


class CloudFrontTests(unittest.TestCase):
    def _transport(self) -> FakeTransport:
        transport = FakeTransport()
        transport.xml_route("GET", r"/distribution\?", distribution_list_xml())
        transport.xml_route("GET", r"/distribution/E1234567890ABC$", distribution_xml())
        transport.xml_route("GET", r"/distribution/E1234567890ABC/config", DISTRIBUTION_CONFIG_XML, etag="ETAG1")
        transport.xml_route(
            "PUT",
            r"/distribution/E1234567890ABC/config",
            distribution_xml(aliases=("old.example.net", "example.com", "*.example.com")),
            etag="ETAG2",
        )
        return transport

    def test_find_by_alias(self) -> None:
        client = _cloudfront(self._transport())
        self.assertIsNotNone(client.find_by_alias("old.example.net"))
        self.assertIsNone(client.find_by_alias("nope.example.net"))

    def test_add_aliases_preserves_existing_config(self) -> None:
        transport = self._transport()
        _cloudfront(transport).add_aliases(
            "E1234567890ABC", ["example.com", "*.example.com"], certificate_arn="arn:acm:1"
        )
        put = transport.requests_matching("PUT", "/config")
        self.assertEqual(len(put), 1)
        root = ET.fromstring(put[0].text)

        names = [node.text for node in root.iter("{%s}CNAME" % NAMESPACE)]
        self.assertEqual(names, ["old.example.net", "example.com", "*.example.com"])
        quantity = root.find("{%s}Aliases/{%s}Quantity" % (NAMESPACE, NAMESPACE))
        self.assertEqual(quantity.text, "3")

        # Untouched settings survive the edit.
        self.assertEqual(root.find("{%s}CallerReference" % NAMESPACE).text, "ref-1")
        self.assertEqual(root.find("{%s}DefaultRootObject" % NAMESPACE).text, "index.html")
        self.assertIsNotNone(root.find("{%s}Origins" % NAMESPACE))

    def test_add_aliases_swaps_default_certificate_for_acm(self) -> None:
        transport = self._transport()
        _cloudfront(transport).add_aliases("E1234567890ABC", ["example.com"], certificate_arn="arn:acm:1")
        root = ET.fromstring(transport.requests_matching("PUT", "/config")[0].text)
        viewer = root.find("{%s}ViewerCertificate" % NAMESPACE)
        self.assertEqual(viewer.find("{%s}ACMCertificateArn" % NAMESPACE).text, "arn:acm:1")
        self.assertEqual(viewer.find("{%s}SSLSupportMethod" % NAMESPACE).text, "sni-only")
        self.assertEqual(viewer.find("{%s}MinimumProtocolVersion" % NAMESPACE).text, "TLSv1.2_2021")
        self.assertIsNone(viewer.find("{%s}CloudFrontDefaultCertificate" % NAMESPACE))

    def test_if_match_uses_the_config_etag(self) -> None:
        transport = self._transport()
        _cloudfront(transport).add_aliases("E1234567890ABC", ["example.com"])
        self.assertEqual(transport.requests_matching("PUT", "/config")[0].headers["If-Match"], "ETAG1")

    def test_no_change_means_no_put(self) -> None:
        transport = self._transport()
        result = _cloudfront(transport).add_aliases("E1234567890ABC", ["old.example.net"])
        self.assertEqual(transport.requests_matching("PUT", "/config"), [])
        self.assertEqual(result.id, "E1234567890ABC")

    def test_xml_error_is_parsed(self) -> None:
        transport = FakeTransport().xml_route(
            "GET",
            r"/distribution\?",
            "<ErrorResponse><Error><Code>AccessDenied</Code><Message>nope</Message></Error></ErrorResponse>",
            status=403,
        )
        with self.assertRaises(AwsError) as caught:
            _cloudfront(transport).list_distributions()
        self.assertEqual(caught.exception.code, "AccessDenied")

    def test_create_distribution_sends_correct_xml(self) -> None:
        transport = FakeTransport()
        origin = OriginSpec("site.s3-website-us-east-1.amazonaws.com", type="s3-website")
        options = DistributionOptions(comment="test distribution")
        
        # Capture the POST request
        captured = []
        def capture(req):
            captured.append(req)
            xml = distribution_xml("ENEW123", aliases=("example.com",))
            return Response(201, {"ETag": "ETAG123"}, xml.encode("utf-8"))
        transport.add(lambda r: r.method == "POST", capture)
        
        result = _cloudfront(transport).create_distribution(
            "example.com",
            origin=origin,
            options=options,
            aliases=("example.com",),
            certificate_arn="arn:aws:acm:us-east-1:1:certificate/test"
        )
        
        self.assertEqual(1, len(captured))
        req = captured[0]
        self.assertEqual("POST", req.method)
        self.assertIn("/distribution", req.url)
        
        # Verify XML contains key elements
        body = req.text
        self.assertIn("<CallerReference>", body)
        self.assertIn("<Aliases>", body)
        self.assertIn("<CNAME>example.com</CNAME>", body)
        self.assertIn("site.s3-website-us-east-1.amazonaws.com", body)
        self.assertIn("[domainkit:example.com]", body)
        
        # Verify result parsing
        self.assertEqual("ENEW123", result.id)
        self.assertEqual(("example.com",), result.aliases)
    
    def test_create_distribution_parses_response(self) -> None:
        transport = FakeTransport()
        origin = OriginSpec("bucket.s3.amazonaws.com", type="custom")
        
        xml = distribution_xml(
            "ECREATED456",
            aliases=("new.example.com",),
            certificate_arn="arn:aws:acm:us-east-1:1:certificate/xyz",
            comment="managed [domainkit:new.example.com]"
        )
        transport.xml_route("POST", r"/distribution", xml, status=201, etag="ETAG456")
        
        result = _cloudfront(transport).create_distribution(
            "new.example.com",
            origin=origin,
            aliases=("new.example.com",),
            certificate_arn="arn:aws:acm:us-east-1:1:certificate/xyz"
        )
        
        self.assertEqual("ECREATED456", result.id)
        self.assertEqual("d111111abcdef8.cloudfront.net", result.domain_name)
        self.assertEqual(("new.example.com",), result.aliases)
        self.assertEqual("arn:aws:acm:us-east-1:1:certificate/xyz", result.certificate_arn)
        self.assertEqual("managed [domainkit:new.example.com]", result.comment)
        self.assertEqual("ETAG456", result.etag)
    
    def test_find_by_marker(self) -> None:
        transport = FakeTransport()
        
        # Distribution with marker in comment
        xml = distribution_list_xml(
            "EMARKED789",
            aliases=("marked.example.com",),
            comment="custom description [domainkit:marked.example.com]"
        )
        transport.xml_route("GET", r"/distribution\?", xml)
        
        result = _cloudfront(transport).find_by_marker("marked.example.com")
        
        self.assertIsNotNone(result)
        self.assertEqual("EMARKED789", result.id)
        self.assertEqual(("marked.example.com",), result.aliases)
        self.assertIn("[domainkit:marked.example.com]", result.comment)
    
    def test_find_by_marker_returns_none_when_not_found(self) -> None:
        transport = FakeTransport()
        
        # Distribution without matching marker
        xml = distribution_list_xml(
            "EOTHER999",
            aliases=("other.example.com",),
            comment="manually created"
        )
        transport.xml_route("GET", r"/distribution\?", xml)
        
        result = _cloudfront(transport).find_by_marker("notfound.example.com")
        
        self.assertIsNone(result)


class GitLabTests(unittest.TestCase):
    def _client(self, transport: FakeTransport) -> GitLabClient:
        return GitLabClient("https://gitlab.example.com", "group/repo", "token", transport=transport)

    def test_project_path_is_url_encoded(self) -> None:
        transport = FakeTransport().json_route("GET", r"/branches/dev$", {"name": "dev"})
        self._client(transport).get_branch("dev")
        self.assertIn("/projects/group%2Frepo/", transport.sent[-1].url)
        self.assertEqual(transport.sent[-1].headers["PRIVATE-TOKEN"], "token")

    def test_create_branch_is_idempotent(self) -> None:
        transport = FakeTransport().json_route("GET", r"/branches/", {"name": "example.com"})
        self._client(transport).create_branch("example.com", "dev")
        self.assertEqual(transport.requests_matching("POST", "/branches"), [])

    def test_create_branch_when_missing(self) -> None:
        transport = FakeTransport()
        transport.json_route("GET", r"/branches/", {"message": "404 Branch Not Found"}, status=404)
        transport.json_route("POST", r"/branches", {"name": "example.com"})
        self._client(transport).create_branch("example.com", "dev")
        created = transport.requests_matching("POST", "/branches")[0]
        self.assertIn("branch=example.com", created.url)
        self.assertIn("ref=dev", created.url)

    def test_set_variable_creates_when_absent(self) -> None:
        transport = FakeTransport()
        transport.json_route("GET", r"/variables/", {"message": "404"}, status=404)
        transport.json_route("POST", r"/variables", {"key": "CLOUDFRONT_EXAMPLE_COM", "value": "E123"})
        self._client(transport).set_variable("CLOUDFRONT_EXAMPLE_COM", "E123")
        payload = transport.requests_matching("POST", "/variables")[0].json()
        self.assertEqual(payload["key"], "CLOUDFRONT_EXAMPLE_COM")
        self.assertEqual(payload["value"], "E123")
        self.assertEqual(payload["variable_type"], "env_var")

    def test_set_variable_updates_on_drift(self) -> None:
        transport = FakeTransport()
        transport.json_route("GET", r"/variables/", {"key": "CLOUDFRONT_EXAMPLE_COM", "value": "OLD"})
        transport.json_route("PUT", r"/variables/", {"key": "CLOUDFRONT_EXAMPLE_COM", "value": "E123"})
        self._client(transport).set_variable("CLOUDFRONT_EXAMPLE_COM", "E123")
        self.assertEqual(transport.requests_matching("PUT", "/variables")[0].json()["value"], "E123")

    def test_set_variable_skips_when_already_correct(self) -> None:
        transport = FakeTransport()
        transport.json_route("GET", r"/variables/", {"key": "CLOUDFRONT_EXAMPLE_COM", "value": "E123"})
        self._client(transport).set_variable("CLOUDFRONT_EXAMPLE_COM", "E123")
        self.assertEqual(transport.requests_matching("PUT", "/variables"), [])

    def test_non_404_errors_propagate(self) -> None:
        transport = FakeTransport().json_route("GET", r"/variables/", {"message": "401"}, status=401)
        with self.assertRaises(GitLabError):
            self._client(transport).get_variable("ANY")

    def test_normalize_project_id_strips_breadcrumb_spaces(self) -> None:
        self.assertEqual(normalize_project_id("Ruby / Ruby-WebSite"), "Ruby/Ruby-WebSite")
        self.assertEqual(normalize_project_id("  group / sub group / repo  "), "group/sub group/repo")
        self.assertEqual(normalize_project_id("/group/repo/"), "group/repo")
        self.assertEqual(normalize_project_id("12345"), "12345")
        self.assertEqual(normalize_project_id("https://gitlab.example.com/group/repo.git"), "group/repo")
        self.assertEqual(normalize_project_id(""), "")

    def test_breadcrumb_project_id_builds_clean_path(self) -> None:
        transport = FakeTransport().json_route("GET", r"/branches/dev$", {"name": "dev"})
        client = GitLabClient("https://gitlab.example.com", "Ruby / Ruby-WebSite", "token", transport=transport)
        client.get_branch("dev")
        self.assertIn("/projects/Ruby%2FRuby-WebSite/", transport.sent[-1].url)
        self.assertNotIn("%20", transport.sent[-1].url)

    def test_get_project_returns_none_on_404(self) -> None:
        transport = FakeTransport().json_route("GET", r"/projects/[^/]+$", {"message": "404"}, status=404)
        self.assertIsNone(self._client(transport).get_project())

    def test_instance_client_gets_current_user(self) -> None:
        transport = FakeTransport().json_route("GET", r"/api/v4/user$", {"username": "ops-bot"})
        client = GitLabClient.for_instance("https://gitlab.example.com", "token", transport=transport)
        user = client.get_current_user()
        self.assertEqual(user["username"], "ops-bot")
        self.assertEqual(transport.sent[-1].headers["PRIVATE-TOKEN"], "token")

    def test_list_accessible_projects_follows_gitlab_pagination(self) -> None:
        transport = FakeTransport()
        transport.json_route(
            "GET",
            r"/api/v4/projects\?.*page=1",
            [{"id": 1, "path_with_namespace": "Ruby/Ruby-WebSite"}],
            headers={"X-Next-Page": "2"},
        )
        transport.json_route(
            "GET",
            r"/api/v4/projects\?.*page=2",
            [{"id": 2, "path_with_namespace": "Ops/Deploy"}],
        )
        client = GitLabClient.for_instance("https://gitlab.example.com", "token", transport=transport)
        projects = client.list_accessible_projects(search="Ruby")

        self.assertEqual([project["id"] for project in projects], [1, 2])
        first = transport.requests_matching("GET", "/api/v4/projects")[0].url
        self.assertIn("membership=true", first)
        self.assertIn("search=Ruby", first)
        self.assertIn("per_page=100", first)

    def test_instance_client_requires_project_for_project_operations(self) -> None:
        client = GitLabClient.for_instance("https://gitlab.example.com", "token", transport=FakeTransport())
        with self.assertRaisesRegex(ConfigError, "GITLAB_PROJECT_ID is required"):
            client.get_branch("dev")


class GoDaddyTests(unittest.TestCase):
    def _client(self, transport: FakeTransport) -> GoDaddyClient:
        return GoDaddyClient("key", "secret", transport=transport)

    def test_authorization_header_shape(self) -> None:
        transport = FakeTransport().json_route("GET", r"/v1/domains", [])
        self._client(transport).list_domains()
        self.assertEqual(transport.sent[-1].headers["Authorization"], "sso-key key:secret")

    def test_upsert_writes_when_absent(self) -> None:
        transport = FakeTransport()
        transport.json_route("GET", r"/records/CNAME/_abc$", [])
        transport.json_route("PUT", r"/records/CNAME/_abc$", None)
        changed = self._client(transport).upsert_record(
            "example.com", DnsRecord("_abc", "CNAME", "_xyz.acm-validations.aws")
        )
        self.assertTrue(changed)
        payload = transport.requests_matching("PUT", "/records/CNAME/_abc")[0].json()
        self.assertEqual(payload, [{"data": "_xyz.acm-validations.aws", "ttl": 600}])

    def test_upsert_is_a_noop_when_already_correct(self) -> None:
        transport = FakeTransport()
        transport.json_route(
            "GET", r"/records/CNAME/_abc$", [{"name": "_abc", "type": "CNAME", "data": "_xyz.acm-validations.aws", "ttl": 600}]
        )
        changed = self._client(transport).upsert_record(
            "example.com", DnsRecord("_abc", "CNAME", "_xyz.acm-validations.aws")
        )
        self.assertFalse(changed)
        self.assertEqual(transport.requests_matching("PUT", "/records"), [])

    def test_upsert_rewrites_on_value_drift(self) -> None:
        transport = FakeTransport()
        transport.json_route(
            "GET", r"/records/CNAME/_abc$", [{"name": "_abc", "type": "CNAME", "data": "stale", "ttl": 600}]
        )
        transport.json_route("PUT", r"/records/CNAME/_abc$", None)
        self.assertTrue(
            self._client(transport).upsert_record("example.com", DnsRecord("_abc", "CNAME", "_xyz.acm-validations.aws"))
        )

    def test_validation_record_is_made_relative(self) -> None:
        record = validation_record_for_zone("_abc.example.com", "_xyz.acm-validations.aws.", "example.com")
        self.assertEqual(record.name, "_abc")
        self.assertEqual(record.data, "_xyz.acm-validations.aws")

    def test_apex_guidance_names_the_zone_and_target(self) -> None:
        text = apex_guidance("cdn.example.com", "d111.cloudfront.net")
        self.assertIn("example.com", text)
        self.assertIn("d111.cloudfront.net", text)

    def test_set_forwarding_idempotent_when_unchanged(self) -> None:
        """转发已存在且 URL/type 匹配时返回 False，不发 PUT"""
        transport = FakeTransport()
        transport.json_route(
            "GET",
            r"/v1/domains/forwards/",
            {"url": "https://www.example.com", "type": "REDIRECT_PERMANENT"}
        )
        changed = self._client(transport).set_forwarding(
            "example.com",
            "https://www.example.com",
            redirect_type=REDIRECT_PERMANENT
        )
        self.assertFalse(changed)
        self.assertEqual(transport.requests_matching("PUT", "/forwards"), [])
        self.assertEqual(transport.requests_matching("POST", "/forwards"), [])

    def test_set_forwarding_writes_on_url_drift(self) -> None:
        """URL 变化触发 PUT"""
        transport = FakeTransport()
        transport.json_route(
            "GET",
            r"/v1/domains/forwards/",
            {"url": "https://old.example.com", "type": "REDIRECT_PERMANENT"}
        )
        transport.json_route("PUT", r"/v1/domains/forwards/", None)
        changed = self._client(transport).set_forwarding(
            "example.com",
            "https://www.example.com",
            redirect_type=REDIRECT_PERMANENT
        )
        self.assertTrue(changed)
        req = transport.requests_matching("PUT", "/forwards")[0]
        payload = req.json()
        self.assertEqual(payload["url"], "https://www.example.com")
        self.assertEqual(payload["type"], "REDIRECT_PERMANENT")

    def test_set_forwarding_writes_on_type_drift(self) -> None:
        """type 变化触发 PUT"""
        transport = FakeTransport()
        transport.json_route(
            "GET",
            r"/v1/domains/forwards/",
            {"url": "https://www.example.com", "type": "REDIRECT_TEMPORARY"}
        )
        transport.json_route("PUT", r"/v1/domains/forwards/", None)
        changed = self._client(transport).set_forwarding(
            "example.com",
            "https://www.example.com",
            redirect_type=REDIRECT_PERMANENT
        )
        self.assertTrue(changed)
        payload = transport.requests_matching("PUT", "/forwards")[0].json()
        self.assertEqual(payload["type"], "REDIRECT_PERMANENT")

    def test_set_forwarding_falls_back_to_post_on_404(self) -> None:
        """PUT 404 时退回 POST"""
        transport = FakeTransport()
        transport.json_route("GET", r"/v1/domains/forwards/", None, status=404)
        
        def put_handler(req):
            return Response(404, {}, b'{"message":"Not Found"}')
        transport.add(lambda r: r.method == "PUT" and "/forwards/" in r.url, put_handler)
        
        transport.json_route("POST", r"/v1/domains/forwards/", None)
        
        changed = self._client(transport).set_forwarding(
            "example.com",
            "https://www.example.com"
        )
        self.assertTrue(changed)
        self.assertEqual(len(transport.requests_matching("POST", "/forwards")), 1)

    def test_set_forwarding_includes_subs_param(self) -> None:
        """subdomains=True 时追加 includeSubs 参数"""
        transport = FakeTransport()
        transport.json_route("GET", r"/v1/domains/forwards/", None, status=404)
        
        captured = []
        def capture_put(req):
            captured.append(req)
            return Response(200, {}, b'{}')
        transport.add(lambda r: r.method == "PUT" and "/forwards/" in r.url, capture_put)
        
        self._client(transport).set_forwarding(
            "example.com",
            "https://www.example.com",
            subdomains=True
        )
        self.assertEqual(len(captured), 1)
        self.assertIn("includeSubs=true", captured[0].url)

    def test_site_records_apex_only_produces_www(self) -> None:
        """apex 域名只产生 www CNAME 记录"""
        records = site_records("example.com", "d111.cloudfront.net", ttl=300)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "www")
        self.assertEqual(records[0].type, "CNAME")
        self.assertEqual(records[0].data, "d111.cloudfront.net")
        self.assertEqual(records[0].ttl, 300)

    def test_site_records_subdomain_is_relative(self) -> None:
        """子域名记录名相对化"""
        records = site_records("cdn.example.com", "d222.cloudfront.net")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].name, "cdn")
        self.assertEqual(records[0].data, "d222.cloudfront.net")

    def test_apex_redirect_url_always_uses_www(self) -> None:
        """apex_redirect_url 总是生成 www 子域名"""
        self.assertEqual(apex_redirect_url("example.com"), "https://www.example.com")
        self.assertEqual(apex_redirect_url("sub.example.com"), "https://www.example.com")


class TransportRetryTests(unittest.TestCase):
    def test_backoff_doubles_per_attempt(self) -> None:
        transport = UrllibTransport(backoff_base=0.5, jitter=lambda: 0.0)
        self.assertAlmostEqual(transport._delay(1), 0.5)
        self.assertAlmostEqual(transport._delay(2), 1.0)
        self.assertAlmostEqual(transport._delay(3), 2.0)

    def test_jitter_is_added_on_top_of_the_backoff(self) -> None:
        transport = UrllibTransport(backoff_base=0.5, jitter=lambda: 1.0)
        self.assertAlmostEqual(transport._delay(1), 1.0)

    def test_retry_after_header_wins(self) -> None:
        transport = UrllibTransport()
        response = Response(429, {"Retry-After": "7"}, b"", "https://example.com")
        self.assertEqual(transport._retry_after(response), 7.0)
        self.assertIsNone(transport._retry_after(Response(429, {}, b"", "https://example.com")))

    def test_malformed_retry_after_is_ignored(self) -> None:
        transport = UrllibTransport()
        response = Response(503, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, b"", "https://example.com")
        self.assertIsNone(transport._retry_after(response))


if __name__ == "__main__":
    unittest.main()
