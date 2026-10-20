import unittest

from domainkit.errors import DnsError, HttpError
from domainkit.godaddy import DnsRecord
from domainkit.spaceship import SpaceshipClient
from .support import FakeTransport


class SpaceshipClientTests(unittest.TestCase):
    def client(self, items, **kwargs):
        transport = FakeTransport().json_route(
            "GET", r"/example\.com\?take=100&skip=0$", {"items": items, "total": len(items)}
        )
        transport.json_route("PUT", r"/example\.com$", None)
        transport.json_route("DELETE", r"/example\.com$", None)
        return SpaceshipClient("key", "secret", transport=transport, **kwargs), transport

    def test_endpoint_and_headers(self):
        client, transport = self.client([], endpoint="https://custom.example/api/")
        self.assertEqual(client.get_records("example.com"), [])
        request = transport.sent[0]
        self.assertEqual(request.url, "https://custom.example/api/v1/dns/records/example.com?take=100&skip=0")
        self.assertEqual(request.headers["X-API-Key"], "key")
        self.assertEqual(request.headers["X-API-Secret"], "secret")

    def test_pagination(self):
        transport = FakeTransport()
        transport.json_route("GET", r"skip=0$", {"items": [{"name": "@"}], "total": 2})
        transport.json_route("GET", r"skip=1$", {"items": [{"name": "www"}], "total": 2})
        self.assertEqual(len(SpaceshipClient("k", "s", transport=transport).get_records("example.com")), 2)

    def test_incomplete_pagination_fails(self):
        transport = FakeTransport().json_route("GET", r"skip=0$", {"items": [], "total": 1})
        with self.assertRaises(DnsError):
            SpaceshipClient("k", "s", transport=transport).get_records("example.com")

    def test_merge_preserves_mx_and_txt(self):
        records = [{"type": "MX", "name": "@", "exchange": "mail.example.com", "preference": 10, "ttl": 600},
                   {"type": "TXT", "name": "@", "value": "MixedCase.", "ttl": 600}]
        client, transport = self.client(records)
        client.upsert_records("example.com", [DnsRecord("www.example.com.", "CNAME", "cdn.example.net")])
        body = transport.sent[-1].json()
        self.assertFalse(body["force"])
        self.assertEqual(body["items"], [{"type": "CNAME", "name": "www", "cname": "cdn.example.net", "ttl": 600}])

    def test_provider_metadata_is_not_replayed(self):
        client, transport = self.client([
            {"type": "NS", "name": "@", "nameserver": "ns.example.net", "ttl": 600, "readOnly": True},
            {"type": "CNAME", "name": "www", "cname": "cdn.example.net", "ttl": 600},
        ])
        client.upsert_records("example.com", [
            DnsRecord("www", "CNAME", "cdn.example.net"),
            DnsRecord("_validation.www", "CNAME", "_target.acm-validations.aws"),
        ])
        self.assertEqual(transport.sent[-1].json(), {"force": False, "items": [
            {"type": "CNAME", "name": "_validation.www", "cname": "_target.acm-validations.aws", "ttl": 600},
        ]})

    def test_write_error_identifies_method_and_redacts_credentials(self):
        transport = FakeTransport()
        transport.json_route("GET", r"skip=0$", {"items": [], "total": 0})
        transport.json_route("PUT", r"/example\.com$", {"detail": "secret-value"}, status=502)
        client = SpaceshipClient("key-value", "secret-value", transport=transport)
        with self.assertRaises(HttpError) as raised:
            client.upsert_records("example.com", [DnsRecord("www", "CNAME", "cdn.example.net")])
        self.assertIn("PUT:", str(raised.exception))
        self.assertNotIn("secret-value", str(raised.exception))

    def test_relative_names(self):
        for name, expected in [("www.example.com.", "www"), ("example.com", "@"), ("_abc.example.com.", "_abc")]:
            client, transport = self.client([])
            client.upsert_records("example.com", [DnsRecord(name, "CNAME", "target.example.net")])
            self.assertEqual(transport.sent[-1].json()["items"][0]["name"], expected)

    def test_idempotent_even_with_different_ttl(self):
        client, transport = self.client([{"type": "CNAME", "name": "www", "cname": "CDN.example.net.", "ttl": 300}])
        self.assertEqual(client.upsert_records("example.com", [DnsRecord("www", "CNAME", "cdn.example.net")]), [])
        self.assertEqual(len(transport.sent), 1)

    def test_conflicts(self):
        for record in [{"type": "CNAME", "name": "www", "cname": "other.example.net"},
                       {"type": "A", "name": "www", "address": "192.0.2.1"}]:
            client, transport = self.client([record])
            with self.assertRaises(DnsError):
                client.upsert_records("example.com", [DnsRecord("www", "CNAME", "cdn.example.net")])
            self.assertEqual(len(transport.sent), 1)

    def test_input_conflict_has_no_write(self):
        client, transport = self.client([])
        with self.assertRaises(DnsError):
            client.upsert_records("example.com", [DnsRecord("www", "CNAME", "one.example.net"), DnsRecord("www", "CNAME", "two.example.net")])
        self.assertEqual(len(transport.sent), 1)

    def test_txt_case_and_trailing_dot_preserved(self):
        for target in ["MixedCase", "mixedcase."]:
            client, transport = self.client([{"type": "TXT", "name": "@", "value": "MixedCase."}])
            with self.assertRaises(DnsError):
                client.upsert_records("example.com", [DnsRecord("@", "TXT", target)])
        client, transport = self.client([])
        client.upsert_records("example.com", [DnsRecord("@", "TXT", "MixedCase.")])
        self.assertEqual(transport.sent[-1].json()["items"][0]["value"], "MixedCase.")

    def test_exact_delete(self):
        client, transport = self.client([{"type": "CNAME", "name": "www", "cname": "cdn.example.net", "ttl": 300}])
        self.assertFalse(client.delete_record("example.com", DnsRecord("www", "CNAME", "other.example.net")))
        self.assertTrue(client.delete_record("example.com", DnsRecord("www.example.com.", "CNAME", "cdn.example.net")))
        self.assertEqual(transport.sent[-1].json(), [{"type": "CNAME", "name": "www", "cname": "cdn.example.net"}])

    def test_errors(self):
        for status, message in [(401, "credentials are invalid"), (403, "dnsrecords:read/write"), (404, "404")]:
            transport = FakeTransport().json_route("GET", r"skip=0$", {}, status=status)
            with self.assertRaises(HttpError) as raised:
                SpaceshipClient("test-api-key", "test-api-secret", transport=transport).get_records("example.com")
            self.assertIn(message, str(raised.exception))

    def test_endpoint_override(self):
        client = SpaceshipClient.from_env({"SPACESHIP_API_KEY": "k", "SPACESHIP_API_SECRET": "s"}, endpoint="https://custom.example/api")
        self.assertEqual(client.endpoint, "https://custom.example/api")

    def test_workflow_requires_credentials_before_aws(self):
        from domainkit.config import Settings
        from domainkit.workflow import Onboarder
        report = Onboarder(Settings(dns_provider="spaceship")).run("example.com", dry_run=False)
        self.assertTrue(report.failed)
        self.assertIn("Spaceship credentials", report.steps[-1].detail)

    def test_spaceship_workflow_and_manual_forward(self):
        from .test_workflow import WorkflowHarness, DISTRIBUTION_ID
        harness = WorkflowHarness()
        onboarder = harness.onboarder()
        transport = FakeTransport()
        transport.json_route("GET", r"/livingtechnology\.com\?", {"items": [], "total": 0})
        transport.json_route("PUT", r"/livingtechnology\.com$", None)
        onboarder._spaceship = SpaceshipClient("k", "s", transport=transport)
        for dry_run in (True, False):
            report = onboarder.run("livingtechnology.com", dry_run=dry_run,
                                   distribution_id=DISTRIBUTION_ID, dns_provider="spaceship")
            self.assertFalse(report.failed)
            step = next(s for s in report.steps if s.name == "apex-forward")
            self.assertTrue("manually" in step.detail or "手工" in step.detail)
        self.assertFalse(harness.dns.sent)
        writes = [r for r in transport.sent if r.method == "PUT"]
        self.assertEqual(len(writes), 2)
