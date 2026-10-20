"""GoDaddy DNS client.

Used to publish the ACM validation CNAME and to point the site at CloudFront.
GoDaddy has no ALIAS record at the apex, so the site pattern is a CNAME on
``www`` plus a GoDaddy domain forward from the apex to ``https://www.<domain>``.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .errors import ConfigError, HttpError
from .http import Response, Transport, UrllibTransport
from .naming import normalize_domain, registrable_domain, relative_record_name

PRODUCTION_ENDPOINT = "https://api.godaddy.com"
OTE_ENDPOINT = "https://api.ote-godaddy.com"

# GoDaddy's forwarding API vocabulary. 301 permanent, address bar shows the
# real target rather than framing the site.
REDIRECT_PERMANENT = "REDIRECT_PERMANENT"
REDIRECT_TEMPORARY = "REDIRECT_TEMPORARY"
REDIRECT_MASKED = "MASKED"

# Forwarding lives on a different route than DNS records and is the least
# stable part of GoDaddy's public API. Kept overridable so a route change can
# be fixed by configuration instead of a code edit.
FORWARD_PATH_TEMPLATE = "/v1/domains/forwards/{fqdn}"


@dataclass(frozen=True)
class DnsRecord:
    name: str  # relative to the zone, "@" for apex
    type: str
    data: str
    ttl: int = 600

    def to_payload(self) -> Dict[str, Any]:
        return {"name": self.name, "type": self.type, "data": self.data, "ttl": self.ttl}


class GoDaddyClient:
    """Thin wrapper over the GoDaddy v1 domains API."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        endpoint: str = PRODUCTION_ENDPOINT,
        transport: Optional[Transport] = None,
    ) -> None:
        self._key = api_key
        self._secret = api_secret
        self._endpoint = endpoint.rstrip("/")
        self._transport = transport or UrllibTransport()

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any) -> "GoDaddyClient":
        env = os.environ if env is None else env
        key = (env.get("GODADDY_API_KEY") or "").strip()
        secret = (env.get("GODADDY_API_SECRET") or "").strip()
        if not key or not secret:
            raise ConfigError(
                "missing GoDaddy credentials: set GODADDY_API_KEY and GODADDY_API_SECRET "
                "(create them at https://developer.godaddy.com/keys)"
            )
        endpoint = (env.get("GODADDY_API_ENDPOINT") or "").strip() or PRODUCTION_ENDPOINT
        return cls(key, secret, endpoint=endpoint, **kwargs)

    def _request(self, method: str, path: str, *, json_body: Any = None) -> Response:
        url = "{0}{1}".format(self._endpoint, path)
        headers = {
            "Authorization": "sso-key {0}:{1}".format(self._key, self._secret),
            "Accept": "application/json",
        }
        body: Optional[bytes] = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        response = self._transport.request(method, url, headers=headers, body=body)
        if not response.ok:
            raise HttpError(response.status, url, response.text)
        return response

    def list_domains(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/v1/domains?statuses=ACTIVE&limit=1000").json() or []

    def owns_domain(self, domain: str) -> bool:
        wanted = normalize_domain(domain)
        return any(
            normalize_domain(entry.get("domain") or "zzz.invalid") == wanted
            for entry in self.list_domains()
            if entry.get("domain")
        )

    def get_records(
        self,
        domain: str,
        record_type: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        zone = normalize_domain(domain)
        path = "/v1/domains/{0}/records".format(urllib.parse.quote(zone, safe=""))
        if record_type:
            path += "/{0}".format(urllib.parse.quote(record_type.upper(), safe=""))
            if name:
                path += "/{0}".format(urllib.parse.quote(name, safe=""))
        try:
            return self._request("GET", path).json() or []
        except HttpError as exc:
            if exc.status == 404:
                return []
            raise

    def upsert_record(self, domain: str, record: DnsRecord) -> bool:
        """Publish one record. Returns True when a change was sent.

        Uses the type+name scoped PUT so sibling record types stay untouched.
        """
        zone = normalize_domain(domain)
        existing = self.get_records(zone, record.type, record.name)
        already_correct = (
            len(existing) == 1
            and str(existing[0].get("data", "")).rstrip(".") == record.data.rstrip(".")
            and int(existing[0].get("ttl", 0)) == record.ttl
        )
        if already_correct:
            return False

        path = "/v1/domains/{0}/records/{1}/{2}".format(
            urllib.parse.quote(zone, safe=""),
            urllib.parse.quote(record.type.upper(), safe=""),
            urllib.parse.quote(record.name, safe=""),
        )
        self._request("PUT", path, json_body=[{"data": record.data, "ttl": record.ttl}])
        return True

    def upsert_records(self, domain: str, records: Sequence[DnsRecord]) -> List[DnsRecord]:
        """Publish several records, returning the ones that actually changed."""
        changed: List[DnsRecord] = []
        for record in records:
            if self.upsert_record(domain, record):
                changed.append(record)
        return changed

    def get_forwarding(self, domain: str) -> Optional[Dict[str, Any]]:
        """Read the apex domain forward, or None when none is configured."""
        zone = normalize_domain(domain)
        path = FORWARD_PATH_TEMPLATE.format(fqdn=urllib.parse.quote(zone, safe=""))
        try:
            payload = self._request("GET", path).json()
        except HttpError as exc:
            if exc.status in (404, 422):
                return None
            raise
        return payload if isinstance(payload, dict) else None

    def set_forwarding(
        self,
        domain: str,
        target_url: str,
        *,
        redirect_type: str = REDIRECT_PERMANENT,
        masking: bool = False,
        subdomains: bool = False,
    ) -> bool:
        """Point the apex at `target_url`. Returns True when a change was sent."""
        zone = normalize_domain(domain)
        wanted_type = REDIRECT_MASKED if masking else redirect_type
        current = self.get_forwarding(zone)
        if (
            current
            and str(current.get("url", "")).rstrip("/") == target_url.rstrip("/")
            and str(current.get("type", "")).upper() == wanted_type.upper()
        ):
            return False

        body: Dict[str, Any] = {"type": wanted_type, "url": target_url}
        path = FORWARD_PATH_TEMPLATE.format(fqdn=urllib.parse.quote(zone, safe=""))
        if subdomains:
            path += "?includeSubs=true"
        try:
            self._request("PUT", path, json_body=body)
        except HttpError as exc:
            if exc.status == 404:
                # No forward exists yet on some accounts; POST creates the first one.
                self._request("POST", path, json_body=body)
            else:
                raise
        return True


def site_records(domain: str, cloudfront_domain: str, ttl: int = 600) -> List[DnsRecord]:
    """The records that actually carry traffic once the distribution is live."""
    apex = registrable_domain(domain)
    host = normalize_domain(domain)
    target = cloudfront_domain.rstrip(".")
    if host == apex:
        # Apex itself cannot be a CNAME on GoDaddy, so only www is a record;
        # the apex is handled by domain forwarding.
        return [DnsRecord(name="www", type="CNAME", data=target, ttl=ttl)]
    return [DnsRecord(name=relative_record_name(host, apex), type="CNAME", data=target, ttl=ttl)]


def apex_redirect_url(domain: str) -> str:
    return "https://www.{0}".format(registrable_domain(domain))


def validation_record_for_zone(fqdn: str, value: str, zone: str, ttl: int = 600) -> DnsRecord:
    """Turn an ACM validation record into a zone-relative GoDaddy record."""
    return DnsRecord(
        name=relative_record_name(fqdn, zone),
        type="CNAME",
        data=value.rstrip("."),
        ttl=ttl,
    )


def apex_guidance(domain: str, cloudfront_domain: str) -> str:
    """Explain how to serve the apex, since GoDaddy DNS has no ALIAS record."""
    apex = registrable_domain(domain)
    return (
        "GoDaddy DNS cannot CNAME the apex ({apex}). Options:\n"
        "  1. Point www.{apex} at {target} (CNAME) and use GoDaddy domain forwarding "
        "for {apex} -> https://www.{apex}\n"
        "  2. Move the zone to Route 53 and use an ALIAS A record at the apex\n"
        "  3. Serve the apex from CloudFront via a separate redirect distribution"
    ).format(apex=apex, target=cloudfront_domain)
