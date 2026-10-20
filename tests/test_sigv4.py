"""SigV4 checked against the AWS-published test vector."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from domainkit.errors import ConfigError
from domainkit.sigv4 import Credentials, _canonical_query, sign

# From the AWS "Signature Version 4 test suite" (get-vanilla).
VECTOR_KEY = "AKIDEXAMPLE"
VECTOR_SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
VECTOR_MOMENT = datetime(2015, 8, 30, 12, 36, 0, tzinfo=timezone.utc)


class SigV4VectorTests(unittest.TestCase):
    def test_get_vanilla_signature(self) -> None:
        headers = sign(
            "GET",
            "https://example.amazonaws.com/",
            service="service",
            region="us-east-1",
            credentials=Credentials(VECTOR_KEY, VECTOR_SECRET),
            headers={"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"},
            now=VECTOR_MOMENT,
        )
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=host;x-amz-date, "
            "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
        )


class CanonicalQueryTests(unittest.TestCase):
    """Canonical query rules from the SigV4 spec, checked structurally."""

    def test_repeated_keys_sort_by_value(self) -> None:
        self.assertEqual(_canonical_query("Param1=value2&Param1=value1"), "Param1=value1&Param1=value2")

    def test_keys_sort_by_code_point_so_upper_case_wins(self) -> None:
        self.assertEqual(_canonical_query("b=1&A=2"), "A=2&b=1")

    def test_blank_values_are_kept(self) -> None:
        self.assertEqual(_canonical_query("Marker="), "Marker=")

    def test_reserved_characters_are_percent_encoded(self) -> None:
        self.assertEqual(_canonical_query("Marker=a/b c"), "Marker=a%2Fb%20c")

    def test_unreserved_characters_are_left_alone(self) -> None:
        self.assertEqual(_canonical_query("k=-_.~"), "k=-_.~")


class SigningBehaviourTests(unittest.TestCase):
    def _sign(self, **kwargs):
        defaults = dict(
            service="acm",
            region="us-east-1",
            credentials=Credentials("AK", "SK"),
            now=VECTOR_MOMENT,
        )
        defaults.update(kwargs)
        return sign("POST", "https://acm.us-east-1.amazonaws.com/", **defaults)

    def test_adds_host_and_date_when_missing(self) -> None:
        headers = self._sign()
        self.assertEqual(headers["Host"], "acm.us-east-1.amazonaws.com")
        self.assertEqual(headers["X-Amz-Date"], "20150830T123600Z")

    def test_session_token_is_signed(self) -> None:
        headers = self._sign(credentials=Credentials("AK", "SK", "TOKEN"))
        self.assertEqual(headers["X-Amz-Security-Token"], "TOKEN")
        self.assertIn("x-amz-security-token", headers["Authorization"])

    def test_custom_headers_are_signed(self) -> None:
        headers = self._sign(headers={"X-Amz-Target": "CertificateManager.ListCertificates"})
        self.assertIn("x-amz-target", headers["Authorization"])

    def test_body_changes_the_signature(self) -> None:
        without = self._sign()["Authorization"]
        with_body = self._sign(body=b'{"a":1}')["Authorization"]
        self.assertNotEqual(without, with_body)


class CredentialTests(unittest.TestCase):
    def test_from_env_requires_both_values(self) -> None:
        with self.assertRaises(ConfigError):
            Credentials.from_env({"AWS_ACCESS_KEY_ID": "only-key"})

    def test_from_env_reads_optional_token(self) -> None:
        credentials = Credentials.from_env(
            {
                "AWS_ACCESS_KEY_ID": "AK",
                "AWS_SECRET_ACCESS_KEY": "SK",
                "AWS_SESSION_TOKEN": "ST",
            }
        )
        self.assertEqual((credentials.access_key, credentials.secret_key, credentials.session_token), ("AK", "SK", "ST"))


if __name__ == "__main__":
    unittest.main()
