"""Domain parsing and the naming conventions this workflow depends on.

The rules encoded here are the ones that are easy to get wrong by hand:

* GitLab branch name  -> the main domain, verbatim (``livingtechnology.com``)
* GitLab variable key -> ``CLOUDFRONT_LIVINGTECHNOLOGY_COM``
* ACM subject names   -> ``example.com`` plus ``*.example.com``
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

from .errors import DomainKitError

LABEL_PATTERN = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
# DNS validation records (ACM, DKIM, DMARC) use a leading underscore, which is
# legal in DNS even though it is not a legal hostname character.
UNDERSCORE_LABEL_PATTERN = re.compile(r"^_?(?!-)[a-z0-9_-]{1,63}(?<!-)$")
VARIABLE_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Enough of the public suffix list to cover the registrars people actually use.
# Only consulted to find the registrable domain, never to validate a name.
MULTI_LABEL_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk", "sch.uk", "ac.uk", "gov.uk",
        "com.au", "net.au", "org.au", "edu.au", "id.au",
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
        "com.hk", "org.hk", "net.hk", "idv.hk",
        "com.tw", "net.tw", "org.tw",
        "com.sg", "net.sg", "org.sg", "edu.sg",
        "com.br", "net.br", "org.br",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
        "co.kr", "or.kr", "ne.kr",
        "co.nz", "net.nz", "org.nz",
        "co.in", "net.in", "org.in", "firm.in", "gen.in", "ind.in",
        "com.mx", "com.ar", "com.co", "com.tr", "com.ua", "com.vn", "com.my", "com.ph",
        "co.za", "org.za", "net.za",
        "com.es", "com.pl", "com.pt", "com.ru", "com.sa", "com.eg", "com.ng", "com.pk",
    }
)


class DomainNameError(DomainKitError):
    """A domain name is malformed."""


def to_ascii(value: str) -> str:
    """Punycode a possibly unicode domain, label by label."""
    labels: List[str] = []
    for label in value.split("."):
        if not label:
            labels.append(label)
            continue
        if label.isascii():
            labels.append(label)
            continue
        try:
            labels.append(label.encode("idna").decode("ascii"))
        except UnicodeError as exc:
            raise DomainNameError("cannot encode label {0!r} as punycode: {1}".format(label, exc)) from exc
    return ".".join(labels)


def normalize_domain(value: str, *, allow_wildcard: bool = False, allow_underscore: bool = False) -> str:
    """Return a lowercase, punycoded, validated domain name.

    Tolerates the shapes people paste in practice: ``HTTPS://Example.com/``,
    a trailing dot, or stray whitespace. Set ``allow_underscore`` when the name
    is a DNS record rather than a hostname.
    """
    if value is None:
        raise DomainNameError("domain is required")

    candidate = str(value).strip().lower()
    if not candidate:
        raise DomainNameError("domain is required")

    candidate = re.sub(r"^[a-z][a-z0-9+.-]*://", "", candidate)
    candidate = candidate.split("/", 1)[0]
    candidate = candidate.split("?", 1)[0]
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[1]
    candidate = candidate.rstrip(".")
    if candidate.startswith("[") or re.search(r":\d+$", candidate):
        candidate = re.sub(r":\d+$", "", candidate).strip("[]")
    candidate = to_ascii(candidate)

    wildcard = candidate.startswith("*.")
    if wildcard:
        if not allow_wildcard:
            raise DomainNameError("wildcard names are not allowed here: {0!r}".format(value))
        candidate = candidate[2:]

    if not candidate:
        raise DomainNameError("domain is empty after normalization: {0!r}".format(value))
    if len(candidate) > 253:
        raise DomainNameError("domain is longer than 253 characters: {0!r}".format(value))

    labels = candidate.split(".")
    if len(labels) < 2:
        raise DomainNameError("expected at least two labels, got {0!r}".format(value))
    pattern = UNDERSCORE_LABEL_PATTERN if allow_underscore else LABEL_PATTERN
    for label in labels:
        if not pattern.match(label):
            raise DomainNameError("invalid label {0!r} in domain {1!r}".format(label, value))
    if re.fullmatch(r"[\d.]+", candidate):
        raise DomainNameError("expected a domain name, got an IP address: {0!r}".format(value))

    return ("*." + candidate) if wildcard else candidate


def registrable_domain(domain: str) -> str:
    """Best-effort apex for a name, e.g. ``a.b.example.co.uk`` -> ``example.co.uk``."""
    normalized = normalize_domain(domain, allow_wildcard=True).lstrip("*.")
    labels = normalized.split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_apex(domain: str, zone: Optional[str] = None) -> bool:
    normalized = normalize_domain(domain, allow_wildcard=True).lstrip("*.")
    return normalized == (normalize_domain(zone) if zone else registrable_domain(normalized))


def service_domain(domain: str, *, www_for_apex: bool = True) -> str:
    """Domain CloudFront should serve directly.

    Operators enter the registrable/apex domain as the business key, branch
    name, and GitLab variable key. GoDaddy cannot CNAME the apex, so the actual
    HTTPS hostname is normally ``www.<apex>`` while the apex is a redirect.
    Subdomains are already CNAME-capable and are left unchanged.
    """
    normalized = normalize_domain(domain)
    if www_for_apex and is_apex(normalized):
        return "www.{0}".format(normalized)
    return normalized


def relative_record_name(fqdn: str, zone: str) -> str:
    """Record name relative to a zone, using ``@`` for the zone apex.

    GoDaddy and most registrar UIs expect this shape.
    """
    normalized = normalize_domain(fqdn, allow_wildcard=True, allow_underscore=True)
    zone_name = normalize_domain(zone)
    if normalized == zone_name:
        return "@"
    suffix = "." + zone_name
    if not normalized.endswith(suffix):
        raise DomainNameError("{0!r} does not sit inside zone {1!r}".format(fqdn, zone))
    return normalized[: -len(suffix)]


def variable_key(domain: str, prefix: str = "CLOUDFRONT") -> str:
    """``livingtechnology.com`` -> ``CLOUDFRONT_LIVINGTECHNOLOGY_COM``.

    Every label is kept, so ``shop.example.co.uk`` becomes
    ``CLOUDFRONT_SHOP_EXAMPLE_CO_UK``. Hyphens become underscores because
    GitLab only accepts ``[A-Za-z0-9_]`` in a variable key.
    """
    normalized = normalize_domain(domain, allow_wildcard=True).lstrip("*.")
    parts = [re.sub(r"[^A-Z0-9]", "_", label.upper()) for label in normalized.split(".")]
    cleaned_prefix = re.sub(r"[^A-Z0-9]", "_", str(prefix).strip().upper()).strip("_")
    key = "_".join(([cleaned_prefix] if cleaned_prefix else []) + parts)
    key = re.sub(r"_{2,}", "_", key)
    if not VARIABLE_KEY_PATTERN.match(key):
        raise DomainNameError("derived variable key is not usable in GitLab: {0!r}".format(key))
    return key


def branch_name(domain: str) -> str:
    """Branch name for a domain: the domain itself, normalized."""
    return normalize_domain(domain, allow_wildcard=True).lstrip("*.")


def certificate_names(domain: str, *, wildcard: bool = True, extra: Iterable[str] = ()) -> Tuple[str, List[str]]:
    """Return ``(primary_name, subject_alternative_names)`` for an ACM request."""
    primary = normalize_domain(domain)
    alternatives: List[str] = []
    if wildcard:
        alternatives.append("*." + primary)
    for name in extra:
        candidate = normalize_domain(name, allow_wildcard=True)
        if candidate != primary and candidate not in alternatives:
            alternatives.append(candidate)
    return primary, alternatives


def covers(certificate_names_present: Sequence[str], required: Iterable[str]) -> bool:
    """True when a certificate's names cover every required alias.

    Understands one level of wildcard, the way TLS does.
    """
    present = {normalize_domain(name, allow_wildcard=True) for name in certificate_names_present}
    wildcards = {name[2:] for name in present if name.startswith("*.")}
    for name in required:
        candidate = normalize_domain(name, allow_wildcard=True)
        if candidate in present:
            continue
        if candidate.startswith("*."):
            return False
        parent = candidate.split(".", 1)[1] if "." in candidate else ""
        if parent and parent in wildcards:
            continue
        return False
    return True
