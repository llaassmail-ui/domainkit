"""Spaceship DNS API client.

This client deliberately only implements DNS record management. Domain
purchase, renewal, deletion, transfer, billing and contacts are outside the
scope of DomainKit.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ConfigError, DnsError, HttpError
from .godaddy import DnsRecord
from .http import Response, Transport, UrllibTransport
from .naming import normalize_domain, relative_record_name

# This is the example endpoint used by the Spaceship API documentation. It is
# intentionally configurable because API gateways can differ by account/region.
DEFAULT_ENDPOINT = "https://spaceship.dev/api"


class SpaceshipClient:
    """Thin, testable wrapper for Spaceship's DNS records API."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        transport: Optional[Transport] = None,
    ) -> None:
        self._key = api_key.strip()
        self._secret = api_secret.strip()
        self._endpoint = endpoint.rstrip("/")
        self._transport = transport or UrllibTransport()
        if not self._key:
            raise ConfigError("Spaceship API key is required")
        if not self._secret:
            raise ConfigError("Spaceship API secret is required")
        if not self._endpoint:
            raise ConfigError("Spaceship API endpoint is required")
        parsed = urllib.parse.urlsplit(self._endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigError("Spaceship endpoint must be an HTTPS URL without credentials, query or fragment")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, **kwargs: Any) -> "SpaceshipClient":
        env = os.environ if env is None else env
        key = (env.get("SPACESHIP_API_KEY") or "").strip()
        secret = (env.get("SPACESHIP_API_SECRET") or "").strip()
        if not key or not secret:
            raise ConfigError(
                "missing Spaceship credentials: set SPACESHIP_API_KEY and SPACESHIP_API_SECRET"
            )
        endpoint = (env.get("SPACESHIP_API_ENDPOINT") or "").strip() or DEFAULT_ENDPOINT
        kwargs.setdefault("endpoint", endpoint)
        return cls(key, secret, **kwargs)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def _request(self, method: str, path: str, *, json_body: Any = None) -> Response:
        url = "{0}{1}".format(self._endpoint, path)
        headers = {
            "X-API-Key": self._key,
            "X-API-Secret": self._secret,
            "Accept": "application/json",
        }
        body = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        response = self._transport.request(method, url, headers=headers, body=body)
        if not response.ok:
            message = response.text.strip() or "no response body"
            if response.status == 401:
                message = "Spaceship credentials are invalid (HTTP 401): {0}".format(message)
            elif response.status == 403:
                message = "Spaceship token lacks DNS permission (HTTP 403; need dnsrecords:read/write): {0}".format(message)
            elif response.status == 404:
                message = "Spaceship endpoint, DNS path or domain was not found (HTTP 404): {0}".format(message)
            message = "{0}: {1}".format(method, message)
            for secret in (self._key, self._secret):
                message = message.replace(secret, "[REDACTED]")
            raise HttpError(response.status, url, message)
        return response

    def get_records(
        self,
        domain: str,
        record_type: Optional[str] = None,
        name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        zone = normalize_domain(domain)
        path = "/v1/dns/records/{0}".format(urllib.parse.quote(zone, safe=""))
        records: List[Dict[str, Any]] = []
        skip = 0
        while True:
            payload = self._request("GET", path + "?take=100&skip={0}".format(skip)).json()
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise DnsError("unexpected Spaceship DNS response")
            page = payload["items"]
            if any(not isinstance(item, dict) for item in page):
                raise DnsError("unexpected Spaceship DNS record")
            records.extend(page)
            total = payload.get("total")
            if not isinstance(total, int) or total < 0 or total > 10000:
                raise DnsError("invalid Spaceship DNS record count")
            skip += len(page)
            if skip >= total:
                break
            if not page:
                raise DnsError("Spaceship DNS pagination ended before total")
        if record_type:
            wanted_type = record_type.upper()
            records = [item for item in records if str(item.get("type", "")).upper() == wanted_type]
        if name is not None:
            wanted_name = _relative_name(name, zone)
            records = [item for item in records if _relative_name(str(item.get("name", "@")), zone) == wanted_name]
        return records

    def upsert_records(self, domain: str, records: Sequence[DnsRecord]) -> List[DnsRecord]:
        zone = normalize_domain(domain)
        wanted = [DnsRecord(_relative_name(r.name, zone), r.type.upper(), r.data, r.ttl) for r in records]
        existing = self.get_records(zone)
        normalized_existing = [_from_api(item, zone) for item in existing]
        changed: List[DnsRecord] = []

        for record in wanted:
            for other in normalized_existing:
                if other.name.lower() == record.name.lower() and other.type != record.type and "CNAME" in (other.type, record.type):
                    raise DnsError("Spaceship DNS conflict: CNAME cannot coexist with another type at {0}".format(record.name))
            matches = [item for item in normalized_existing if _same_key(item, record)]
            exact = [item for item in matches if _same_value(item, record)]
            if matches and not exact:
                raise DnsError(
                    "Spaceship DNS conflict for {0} {1}: existing target differs from {2}".format(
                        record.name, record.type, record.data
                    )
                )
            if exact:
                continue
            normalized_existing.append(record)
            changed.append(record)

        if not changed:
            return []
        # PUT adds records/updates TTL; it does not replace the zone. Never
        # replay unrelated records or provider-managed fields from GET.
        body = {"force": False, "items": [_to_api(record) for record in changed]}
        self._request("PUT", "/v1/dns/records/{0}".format(urllib.parse.quote(zone, safe="")), json_body=body)
        return changed

    def delete_record(self, domain: str, record: DnsRecord) -> bool:
        zone = normalize_domain(domain)
        record = DnsRecord(_relative_name(record.name, zone), record.type.upper(), record.data, record.ttl)
        existing = [_from_api(item, zone) for item in self.get_records(zone)]
        matches = [item for item in existing if _same_key(item, record) and _same_value(item, record)]
        if not matches:
            return False
        item = _to_api(record)
        item.pop("ttl")
        body = [item]
        self._request("DELETE", "/v1/dns/records/{0}".format(urllib.parse.quote(zone, safe="")), json_body=body)
        return True


def _relative_name(name: str, zone: str) -> str:
    value = str(name).strip().rstrip(".")
    if value in {"", "@"}:
        return "@"
    if value.lower() == zone.lower() or value.lower().endswith("." + zone.lower()):
        return relative_record_name(value, zone)
    return value.lower()


def _value(item: Mapping[str, Any]) -> str:
    field = {"CNAME": "cname", "TXT": "value", "MX": "exchange", "NS": "nameserver", "PTR": "pointer", "ALIAS": "aliasName"}.get(str(item.get("type", "")).upper(), "address")
    raw = item.get(field, "")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return str(raw or "")


def _from_api(item: Mapping[str, Any], zone: str) -> DnsRecord:
    return DnsRecord(
        name=_relative_name(str(item.get("name", "@")), zone),
        type=str(item.get("type", "")).upper(),
        data=_value(item),
        ttl=int(item.get("ttl") or 600),
    )


def _to_api(record: DnsRecord) -> Dict[str, Any]:
    kind = record.type.upper()
    field = {"A": "address", "AAAA": "address", "CNAME": "cname", "TXT": "value"}.get(kind)
    if field is None:
        raise DnsError("unsupported Spaceship write record type: {0}".format(kind))
    return {"name": record.name, "type": kind, field: record.data, "ttl": record.ttl}


def _same_key(left: DnsRecord, right: DnsRecord) -> bool:
    return left.name.lower() == right.name.lower() and left.type.upper() == right.type.upper()


def _same_value(left: DnsRecord, right: DnsRecord) -> bool:
    if left.type.upper() == "TXT" or right.type.upper() == "TXT":
        same_value = left.data == right.data
    else:
        same_value = left.data.rstrip(".").lower() == right.data.rstrip(".").lower()
    return same_value
