"""Naming rules: the conventions this whole workflow leans on."""

from __future__ import annotations

import unittest

from domainkit.naming import (
    DomainNameError,
    branch_name,
    certificate_names,
    covers,
    normalize_domain,
    registrable_domain,
    relative_record_name,
    variable_key,
)


class NormalizeTests(unittest.TestCase):
    def test_accepts_the_shapes_people_paste(self) -> None:
        for raw in [
            "livingtechnology.com",
            "  LivingTechnology.COM  ",
            "https://livingtechnology.com/",
            "http://livingtechnology.com/path?x=1",
            "livingtechnology.com.",
            "livingtechnology.com:443",
        ]:
            self.assertEqual(normalize_domain(raw), "livingtechnology.com", raw)

    def test_punycode_for_unicode_labels(self) -> None:
        self.assertEqual(normalize_domain("bücher.de"), "xn--bcher-kva.de")

    def test_rejects_bad_input(self) -> None:
        for raw in ["", "   ", "localhost", "-bad.com", "bad-.com", "192.168.0.1", "a..b.com"]:
            with self.assertRaises(DomainNameError, msg=raw):
                normalize_domain(raw)

    def test_wildcard_requires_opt_in(self) -> None:
        with self.assertRaises(DomainNameError):
            normalize_domain("*.example.com")
        self.assertEqual(normalize_domain("*.Example.com", allow_wildcard=True), "*.example.com")


class RegistrableDomainTests(unittest.TestCase):
    def test_plain_and_multi_label_suffixes(self) -> None:
        cases = {
            "livingtechnology.com": "livingtechnology.com",
            "cdn.livingtechnology.com": "livingtechnology.com",
            "a.b.c.example.com": "example.com",
            "shop.example.co.uk": "example.co.uk",
            "example.com.cn": "example.com.cn",
            "www.example.com.cn": "example.com.cn",
        }
        for domain, expected in cases.items():
            self.assertEqual(registrable_domain(domain), expected, domain)


class VariableKeyTests(unittest.TestCase):
    def test_matches_the_documented_convention(self) -> None:
        self.assertEqual(variable_key("livingtechnology.com"), "CLOUDFRONT_LIVINGTECHNOLOGY_COM")
        self.assertEqual(variable_key("LivingTechnology.COM"), "CLOUDFRONT_LIVINGTECHNOLOGY_COM")

    def test_keeps_every_label_and_sanitizes_hyphens(self) -> None:
        self.assertEqual(variable_key("cdn.example.com"), "CLOUDFRONT_CDN_EXAMPLE_COM")
        self.assertEqual(variable_key("my-site.co.uk"), "CLOUDFRONT_MY_SITE_CO_UK")

    def test_custom_prefix_and_no_prefix(self) -> None:
        self.assertEqual(variable_key("example.com", "CF"), "CF_EXAMPLE_COM")
        self.assertEqual(variable_key("example.com", ""), "EXAMPLE_COM")

    def test_key_never_starts_with_a_digit(self) -> None:
        # A bare numeric domain label would produce an invalid GitLab key
        # unless the prefix protects it.
        self.assertEqual(variable_key("123.com", "CLOUDFRONT"), "CLOUDFRONT_123_COM")
        with self.assertRaises(DomainNameError):
            variable_key("123.com", "")


class BranchNameTests(unittest.TestCase):
    def test_branch_is_the_domain(self) -> None:
        self.assertEqual(branch_name("LivingTechnology.com"), "livingtechnology.com")
        self.assertEqual(branch_name("https://cdn.example.com/"), "cdn.example.com")


class RelativeRecordTests(unittest.TestCase):
    def test_apex_becomes_at_sign(self) -> None:
        self.assertEqual(relative_record_name("example.com", "example.com"), "@")

    def test_subdomain_is_relative(self) -> None:
        self.assertEqual(
            relative_record_name("_abc123.cdn.example.com", "example.com"),
            "_abc123.cdn",
        )

    def test_outside_zone_is_an_error(self) -> None:
        with self.assertRaises(DomainNameError):
            relative_record_name("_abc.other.com", "example.com")


class CertificateNameTests(unittest.TestCase):
    def test_apex_plus_wildcard(self) -> None:
        primary, alternatives = certificate_names("example.com")
        self.assertEqual(primary, "example.com")
        self.assertEqual(alternatives, ["*.example.com"])

    def test_extra_names_are_deduplicated(self) -> None:
        primary, alternatives = certificate_names(
            "example.com", extra=["example.com", "cdn.example.com", "cdn.example.com"]
        )
        self.assertEqual(primary, "example.com")
        self.assertEqual(alternatives, ["*.example.com", "cdn.example.com"])


class CoversTests(unittest.TestCase):
    def test_wildcard_covers_one_level_only(self) -> None:
        names = ["example.com", "*.example.com"]
        self.assertTrue(covers(names, ["example.com"]))
        self.assertTrue(covers(names, ["cdn.example.com"]))
        self.assertFalse(covers(names, ["deep.cdn.example.com"]))
        self.assertFalse(covers(names, ["other.com"]))

    def test_exact_match_without_wildcard(self) -> None:
        self.assertTrue(covers(["cdn.example.com"], ["cdn.example.com"]))
        self.assertFalse(covers(["cdn.example.com"], ["example.com"]))


if __name__ == "__main__":
    unittest.main()
