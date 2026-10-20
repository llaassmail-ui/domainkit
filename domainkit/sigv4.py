"""AWS Signature Version 4 signing, stdlib only."""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Mapping, Optional
from urllib.parse import parse_qsl, quote, urlsplit

from .errors import ConfigError

ALGORITHM = "AWS4-HMAC-SHA256"
EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str
    session_token: Optional[str] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Credentials":
        env = os.environ if env is None else env
        access_key = (env.get("AWS_ACCESS_KEY_ID") or "").strip()
        secret_key = (env.get("AWS_SECRET_ACCESS_KEY") or "").strip()
        if not access_key or not secret_key:
            raise ConfigError(
                "missing AWS credentials: set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                "(AWS_SESSION_TOKEN too when using temporary credentials)"
            )
        token = (env.get("AWS_SESSION_TOKEN") or "").strip() or None
        return cls(access_key, secret_key, token)


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, datestamp: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), datestamp)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    encoded = sorted(
        (quote(key, safe="-_.~"), quote(value, safe="-_.~")) for key, value in pairs
    )
    return "&".join("{0}={1}".format(key, value) for key, value in encoded)


def _canonical_path(path: str) -> str:
    if not path:
        return "/"
    return quote(path, safe="/~")


def _canonical_headers(headers: Mapping[str, str]) -> "tuple[str, str]":
    normalized = {}
    for key, value in headers.items():
        normalized[key.strip().lower()] = " ".join(str(value).split())
    names = sorted(normalized)
    canonical = "".join("{0}:{1}\n".format(name, normalized[name]) for name in names)
    return canonical, ";".join(names)


def sign(
    method: str,
    url: str,
    *,
    service: str,
    region: str,
    credentials: Credentials,
    headers: Optional[Mapping[str, str]] = None,
    body: bytes = b"",
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Return the request headers to send, including `Authorization`.

    The caller keeps ownership of the URL and body; every header passed in is
    signed, which is what AWS expects for headers like `X-Amz-Target`.
    """
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError("cannot sign a request without a host: {0}".format(url))

    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    datestamp = moment.strftime("%Y%m%d")

    outgoing: Dict[str, str] = {key: str(value) for key, value in (headers or {}).items()}
    lower = {key.lower() for key in outgoing}
    if "host" not in lower:
        outgoing["Host"] = parts.netloc
    if "x-amz-date" not in lower:
        outgoing["X-Amz-Date"] = amz_date
    if credentials.session_token and "x-amz-security-token" not in lower:
        outgoing["X-Amz-Security-Token"] = credentials.session_token

    payload_hash = hashlib.sha256(body or b"").hexdigest() if body else EMPTY_PAYLOAD_HASH
    canonical_headers, signed_headers = _canonical_headers(outgoing)
    canonical_request = "\n".join(
        [
            method.upper(),
            _canonical_path(parts.path),
            _canonical_query(parts.query),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    scope = "{0}/{1}/{2}/aws4_request".format(datestamp, region, service)
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(credentials.secret_key, datestamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    outgoing["Authorization"] = (
        "{algorithm} Credential={access_key}/{scope}, "
        "SignedHeaders={signed_headers}, Signature={signature}"
    ).format(
        algorithm=ALGORITHM,
        access_key=credentials.access_key,
        scope=scope,
        signed_headers=signed_headers,
        signature=signature,
    )
    return outgoing
