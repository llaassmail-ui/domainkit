"""ACM and CloudFront clients, signed with SigV4 over the stdlib transport."""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .errors import AwsError, CertificateWaitTimeout, StepError
from .http import Response, Transport, UrllibTransport
from .naming import certificate_names, covers, normalize_domain
from .sigv4 import Credentials, sign

# ACM certificates used by CloudFront must live in us-east-1, and CloudFront
# itself is a global (us-east-1 signed) service.
ACM_REGION = "us-east-1"
CLOUDFRONT_API_VERSION = "2020-05-31"
CLOUDFRONT_HOSTED_ZONE_ID = "Z2FDTNDATAQYW2"  # fixed for all distributions
CLOUDFRONT_NAMESPACE = "http://cloudfront.amazonaws.com/doc/{0}/".format(CLOUDFRONT_API_VERSION)

# AWS managed cache/origin-request policies. IDs are global constants.
CACHE_POLICY_CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"

# Marker appended to a distribution comment so a later run can recognise its
# own work even before any alias or GitLab variable exists.
MARKER_TEMPLATE = "[domainkit:{0}]"

ORIGIN_TYPES = ("s3-website", "custom")


def marker_for(domain: str) -> str:
    return MARKER_TEMPLATE.format(normalize_domain(domain))


@dataclass(frozen=True)
class ValidationRecord:
    """A DNS record ACM wants published to prove domain control."""

    domain: str
    name: str
    value: str
    type: str = "CNAME"


@dataclass(frozen=True)
class Certificate:
    arn: str
    domain: str
    status: str
    names: Sequence[str] = field(default_factory=tuple)
    validation: Sequence[ValidationRecord] = field(default_factory=tuple)

    @property
    def issued(self) -> bool:
        return self.status == "ISSUED"


@dataclass(frozen=True)
class Distribution:
    id: str
    arn: str
    domain_name: str
    status: str
    aliases: Sequence[str] = field(default_factory=tuple)
    etag: Optional[str] = None
    enabled: bool = True
    certificate_arn: Optional[str] = None
    comment: str = ""


@dataclass(frozen=True)
class OriginSpec:
    """How a new distribution should reach the site's origin.

    Buckets here are public-read, so both origin types are plain HTTP custom
    origins. No Origin Access Control, no bucket policy edits.
    """

    domain: str
    type: str = "s3-website"
    path: str = ""
    http_port: int = 80
    https_port: int = 443
    protocol_policy: str = ""

    def __post_init__(self) -> None:
        if not self.domain.strip():
            raise StepError("origin domain is required to create a distribution")
        if self.type not in ORIGIN_TYPES:
            raise StepError(
                "unknown origin type {0!r}, expected one of {1}".format(self.type, ", ".join(ORIGIN_TYPES))
            )
        if self.path and not self.path.startswith("/"):
            raise StepError("origin path must start with '/' (got {0!r})".format(self.path))

    @property
    def origin_protocol_policy(self) -> str:
        """S3 website endpoints only speak HTTP, so they are pinned there."""
        if self.protocol_policy:
            return self.protocol_policy
        return "http-only" if self.type == "s3-website" else "https-only"


@dataclass(frozen=True)
class DistributionOptions:
    """Viewer-side behaviour for a newly created distribution."""

    comment: str = ""
    default_root_object: str = "index.html"
    price_class: str = "PriceClass_All"
    spa: bool = False
    compress: bool = True
    viewer_protocol_policy: str = "redirect-to-https"
    enabled: bool = True
    ipv6: bool = True
    http_version: str = "http2"


def _strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def xml_to_dict(node: ET.Element) -> Any:
    """Convert a CloudFront XML body into plain dicts/lists.

    Repeated child tags collapse into a list, which is what the shapes we read
    (``Items/CNAME``, ``DistributionSummary``) need.
    """
    children = list(node)
    if not children:
        return (node.text or "").strip()
    result: Dict[str, Any] = {}
    for child in children:
        key = _strip_namespace(child.tag)
        value = xml_to_dict(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


class AwsClient:
    """Shared signing/dispatch behaviour for the AWS services used here."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        transport: Optional[Transport] = None,
        region: str = ACM_REGION,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._credentials = credentials
        self._transport = transport or UrllibTransport()
        self._region = region
        self._sleep = sleep

    def _send(
        self,
        method: str,
        url: str,
        *,
        service: str,
        region: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        body: bytes = b"",
    ) -> Response:
        signed = sign(
            method,
            url,
            service=service,
            region=region or self._region,
            credentials=self._credentials,
            headers=headers,
            body=body,
        )
        response = self._transport.request(method, url, headers=signed, body=body or None)
        if not response.ok:
            raise self._error(response)
        return response

    @staticmethod
    def _error(response: Response) -> AwsError:
        code, message = "Unknown", response.text.strip()
        text = response.text
        if text.lstrip().startswith("{"):
            try:
                payload = json.loads(text)
                raw_code = payload.get("__type") or payload.get("code") or "Unknown"
                code = str(raw_code).split("#")[-1]
                message = payload.get("message") or payload.get("Message") or message
            except (ValueError, AttributeError):
                pass
        elif text.lstrip().startswith("<"):
            try:
                root = ET.fromstring(text)
                found = xml_to_dict(root)
                error = found.get("Error", found) if isinstance(found, dict) else {}
                if isinstance(error, dict):
                    code = str(error.get("Code") or code)
                    message = str(error.get("Message") or message)
            except ET.ParseError:
                pass
        return AwsError(response.status, response.url, text, code, message)


class AcmClient(AwsClient):
    """Just enough of ACM to request, inspect, and wait on a certificate."""

    def __init__(self, credentials: Credentials, **kwargs: Any) -> None:
        kwargs.setdefault("region", ACM_REGION)
        super().__init__(credentials, **kwargs)
        self._endpoint = "https://acm.{0}.amazonaws.com/".format(ACM_REGION)

    def _call(self, action: str, payload: Mapping[str, Any]) -> Any:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        response = self._send(
            "POST",
            self._endpoint,
            service="acm",
            region=ACM_REGION,
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": "CertificateManager.{0}".format(action),
            },
            body=body,
        )
        return response.json() or {}

    def list_certificates(self) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        token: Optional[str] = None
        while True:
            payload: Dict[str, Any] = {"MaxItems": 100}
            if token:
                payload["NextToken"] = token
            page = self._call("ListCertificates", payload)
            found.extend(page.get("CertificateSummaryList") or [])
            token = page.get("NextToken")
            if not token:
                return found

    def describe(self, arn: str) -> Certificate:
        detail = (self._call("DescribeCertificate", {"CertificateArn": arn}) or {}).get("Certificate") or {}
        records: List[ValidationRecord] = []
        for option in detail.get("DomainValidationOptions") or []:
            record = option.get("ResourceRecord") or {}
            if record.get("Name") and record.get("Value"):
                records.append(
                    ValidationRecord(
                        domain=option.get("DomainName", ""),
                        name=str(record["Name"]).rstrip("."),
                        value=str(record["Value"]).rstrip("."),
                        type=record.get("Type", "CNAME"),
                    )
                )
        names = [detail.get("DomainName", "")] + list(detail.get("SubjectAlternativeNames") or [])
        return Certificate(
            arn=detail.get("CertificateArn", arn),
            domain=detail.get("DomainName", ""),
            status=detail.get("Status", "UNKNOWN"),
            names=tuple(name for name in dict.fromkeys(names) if name),
            validation=tuple(records),
        )

    def find_reusable(self, domain: str, required: Sequence[str]) -> Optional[Certificate]:
        """Reuse an existing ISSUED or PENDING certificate that covers `required`."""
        apex = normalize_domain(domain)
        candidates = [
            summary
            for summary in self.list_certificates()
            if summary.get("CertificateArn")
            and normalize_domain(summary.get("DomainName") or apex, allow_wildcard=True).lstrip("*.") == apex
        ]
        best: Optional[Certificate] = None
        for summary in candidates:
            certificate = self.describe(summary["CertificateArn"])
            if certificate.status in {"REVOKED", "FAILED", "VALIDATION_TIMED_OUT", "EXPIRED", "INACTIVE"}:
                continue
            if not covers(certificate.names, required):
                continue
            if certificate.issued:
                return certificate
            if best is None and certificate.status == "PENDING_VALIDATION":
                best = certificate
        return best

    def request_certificate(
        self,
        domain: str,
        *,
        wildcard: bool = True,
        extra_names: Sequence[str] = (),
        idempotency_token: Optional[str] = None,
        tags: Optional[Mapping[str, str]] = None,
    ) -> str:
        primary, alternatives = certificate_names(domain, wildcard=wildcard, extra=extra_names)
        payload: Dict[str, Any] = {
            "DomainName": primary,
            "ValidationMethod": "DNS",
            "IdempotencyToken": idempotency_token or _idempotency_token(primary),
            "Options": {"CertificateTransparencyLoggingPreference": "ENABLED"},
        }
        if alternatives:
            payload["SubjectAlternativeNames"] = alternatives
        if tags:
            payload["Tags"] = [{"Key": key, "Value": value} for key, value in tags.items()]
        result = self._call("RequestCertificate", payload)
        arn = result.get("CertificateArn")
        if not arn:
            raise StepError("ACM did not return a certificate ARN for {0}".format(primary))
        return arn

    def wait_for_validation_records(self, arn: str, *, attempts: int = 20, delay: float = 3.0) -> Certificate:
        """ACM populates ResourceRecord a moment after RequestCertificate."""
        certificate = self.describe(arn)
        for _ in range(max(0, attempts - 1)):
            if certificate.issued or certificate.validation:
                return certificate
            self._sleep(delay)
            certificate = self.describe(arn)
        return certificate

    def wait_until_issued(self, arn: str, *, timeout: float = 1800.0, delay: float = 20.0) -> Certificate:
        deadline = time.monotonic() + timeout
        certificate = self.describe(arn)
        while not certificate.issued:
            if certificate.status in {"FAILED", "VALIDATION_TIMED_OUT", "REVOKED"}:
                raise StepError("certificate {0} ended in status {1}".format(arn, certificate.status))
            if time.monotonic() >= deadline:
                raise CertificateWaitTimeout(
                    "已等待 {0:.0f} 秒，证书仍在验证中（{2}）。可稍后重试，无需删除或重新申请证书。证书：{1}".format(
                        timeout, arn, certificate.status
                    )
                )
            self._sleep(delay)
            certificate = self.describe(arn)
        return certificate


def _idempotency_token(domain: str) -> str:
    """ACM allows [A-Za-z0-9] up to 32 chars; keep it stable per domain+hour."""
    stamp = time.strftime("%Y%m%d%H", time.gmtime())
    token = re.sub(r"[^A-Za-z0-9]", "", domain) + stamp
    return token[-32:]


class CloudFrontClient(AwsClient):
    """CloudFront distribution reads plus alias/certificate attachment."""

    def __init__(self, credentials: Credentials, **kwargs: Any) -> None:
        kwargs.setdefault("region", "us-east-1")
        super().__init__(credentials, **kwargs)
        self._base = "https://cloudfront.amazonaws.com/{0}".format(CLOUDFRONT_API_VERSION)

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: Optional[Mapping[str, str]] = None,
    ) -> Response:
        sent = {"Content-Type": "text/xml"} if body else {}
        sent.update(dict(headers or {}))
        return self._send(
            method,
            "{0}{1}".format(self._base, path),
            service="cloudfront",
            region="us-east-1",
            headers=sent,
            body=body,
        )

    @staticmethod
    def _parse(response: Response) -> Dict[str, Any]:
        text = response.text.strip()
        if not text:
            return {}
        root = ET.fromstring(text)
        parsed = xml_to_dict(root)
        # Key the document by its root tag ("Distribution", "DistributionList")
        # so callers can address the shape the API documents.
        return {_strip_namespace(root.tag): parsed}

    def list_distributions(self) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        marker: Optional[str] = None
        while True:
            path = "/distribution?MaxItems=100"
            if marker:
                path += "&Marker={0}".format(marker)
            payload = self._parse(self._call("GET", path)).get("DistributionList") or {}
            items = payload.get("Items") or {}
            found.extend(_as_list(items.get("DistributionSummary") if isinstance(items, dict) else None))
            if str(payload.get("IsTruncated", "false")).lower() != "true":
                return found
            marker = payload.get("NextMarker")
            if not marker:
                return found

    @staticmethod
    def _summary_to_distribution(summary: Mapping[str, Any], etag: Optional[str] = None) -> Distribution:
        aliases = summary.get("Aliases") or {}
        items = aliases.get("Items") if isinstance(aliases, dict) else None
        names = [str(name) for name in _as_list(items.get("CNAME") if isinstance(items, dict) else None) if name]
        viewer = summary.get("ViewerCertificate") or {}
        certificate = viewer.get("ACMCertificateArn") if isinstance(viewer, dict) else None
        return Distribution(
            id=str(summary.get("Id", "")),
            arn=str(summary.get("ARN", "")),
            domain_name=str(summary.get("DomainName", "")),
            status=str(summary.get("Status", "")),
            aliases=tuple(names),
            etag=etag,
            enabled=str(summary.get("Enabled", "true")).lower() != "false",
            certificate_arn=str(certificate) if certificate else None,
            comment=str(summary.get("Comment") or ""),
        )

    def get_distribution(self, distribution_id: str) -> Distribution:
        response = self._call("GET", "/distribution/{0}".format(distribution_id))
        payload = self._parse(response).get("Distribution") or {}
        config = payload.get("DistributionConfig") or {}
        merged = dict(payload)
        if not merged.get("Aliases"):
            merged["Aliases"] = config.get("Aliases")
        if not merged.get("ViewerCertificate"):
            merged["ViewerCertificate"] = config.get("ViewerCertificate")
        if not merged.get("Comment"):
            merged["Comment"] = config.get("Comment")
        return self._summary_to_distribution(merged, response.header("ETag"))

    def get_config(self, distribution_id: str) -> "tuple[Dict[str, Any], str, str]":
        """Return ``(config_dict, etag, raw_config_xml)``."""
        response = self._call("GET", "/distribution/{0}/config".format(distribution_id))
        etag = response.header("ETag") or ""
        if not etag:
            raise StepError("CloudFront did not return an ETag for {0}".format(distribution_id))
        root = ET.fromstring(response.text.strip())
        parsed = xml_to_dict(root)
        return (parsed if isinstance(parsed, dict) else {}), etag, response.text

    def find_by_alias(self, alias: str) -> Optional[Distribution]:
        wanted = normalize_domain(alias, allow_wildcard=True)
        for summary in self.list_distributions():
            distribution = self._summary_to_distribution(summary)
            if any(normalize_domain(name, allow_wildcard=True) == wanted for name in distribution.aliases):
                return distribution
        return None

    def find_by_marker(self, domain: str) -> Optional[Distribution]:
        """Find a distribution this tool created earlier, by comment marker.

        Covers the window where a distribution exists but has no alias yet
        because the certificate had not been issued on the previous run.
        """
        marker = marker_for(domain)
        for summary in self.list_distributions():
            distribution = self._summary_to_distribution(summary)
            if marker in distribution.comment:
                return distribution
        return None

    def create_distribution(
        self,
        domain: str,
        origin: OriginSpec,
        *,
        options: Optional[DistributionOptions] = None,
        aliases: Sequence[str] = (),
        certificate_arn: Optional[str] = None,
        minimum_protocol_version: str = "TLSv1.2_2021",
        remove_aliases: Sequence[str] = (),
        tags: Optional[Mapping[str, str]] = None,
        caller_reference: Optional[str] = None,
    ) -> Distribution:
        """Create a distribution for `domain`, optionally with aliases already on.

        Aliases are only accepted together with an issued certificate; the
        caller attaches them later otherwise.
        """
        settings = options or DistributionOptions()
        if aliases and not certificate_arn:
            raise StepError("cannot create a distribution with aliases before a certificate exists")
        config = build_distribution_config(
            domain,
            origin,
            settings,
            aliases=aliases,
            certificate_arn=certificate_arn,
            minimum_protocol_version=minimum_protocol_version,
            caller_reference=caller_reference or _caller_reference(domain),
        )
        path = "/distribution"
        root = config
        if tags:
            root = ET.Element(_qualified("DistributionConfigWithTags", CLOUDFRONT_NAMESPACE))
            root.append(config)
            _tags_element(root, tags)
            path = "/distribution?WithTags"
        ET.register_namespace("", CLOUDFRONT_NAMESPACE)
        body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        response = self._call("POST", path, body=body)
        parsed = self._parse(response).get("Distribution") or {}
        inner = parsed.get("DistributionConfig") or {}
        merged = dict(parsed)
        for key in ("Aliases", "ViewerCertificate", "Comment"):
            if not merged.get(key):
                merged[key] = inner.get(key)
        created = self._summary_to_distribution(merged, response.header("ETag"))
        if not created.id:
            raise StepError("CloudFront did not return a distribution id for {0}".format(domain))
        return created

    def add_aliases(
        self,
        distribution_id: str,
        aliases: Sequence[str],
        *,
        certificate_arn: Optional[str] = None,
        minimum_protocol_version: str = "TLSv1.2_2021",
        remove_aliases: Sequence[str] = (),
    ) -> Distribution:
        """Attach aliases (and optionally a certificate) to a distribution.

        The config XML is edited in place rather than rebuilt, so unrelated
        settings on the distribution survive untouched.
        """
        wanted = [normalize_domain(alias, allow_wildcard=True) for alias in aliases]
        remove = {normalize_domain(alias, allow_wildcard=True) for alias in remove_aliases}
        _, etag, raw_xml = self.get_config(distribution_id)
        root = ET.fromstring(raw_xml.strip())
        namespace = _namespace_of(root)
        if namespace:
            ET.register_namespace("", namespace)

        aliases_node = _ensure_child(root, "Aliases", namespace, before=("DefaultRootObject", "Origins"))
        items_node = _ensure_child(aliases_node, "Items", namespace)
        existing = {
            normalize_domain(node.text or "", allow_wildcard=True)
            for node in items_node.findall(_qualified("CNAME", namespace))
            if (node.text or "").strip()
        }
        removed = []
        for node in list(items_node.findall(_qualified("CNAME", namespace))):
            alias = normalize_domain(node.text or "", allow_wildcard=True)
            if alias in remove and alias not in wanted:
                items_node.remove(node)
                removed.append(alias)
        added = [alias for alias in wanted if alias not in existing]
        for alias in added:
            ET.SubElement(items_node, _qualified("CNAME", namespace)).text = alias

        quantity_node = _ensure_child(aliases_node, "Quantity", namespace, position=0)
        total = len(items_node.findall(_qualified("CNAME", namespace)))
        quantity_node.text = str(total)
        if total == 0:
            aliases_node.remove(items_node)

        certificate_changed = False
        if certificate_arn:
            viewer = _ensure_child(root, "ViewerCertificate", namespace)
            current = viewer.find(_qualified("ACMCertificateArn", namespace))
            if current is None or (current.text or "").strip() != certificate_arn:
                certificate_changed = True
            _set_text(viewer, "ACMCertificateArn", certificate_arn, namespace)
            _set_text(viewer, "SSLSupportMethod", "sni-only", namespace)
            _set_text(viewer, "MinimumProtocolVersion", minimum_protocol_version, namespace)
            for stale in ("CloudFrontDefaultCertificate", "IAMCertificateId", "Certificate", "CertificateSource"):
                node = viewer.find(_qualified(stale, namespace))
                if node is not None:
                    viewer.remove(node)

        if not added and not removed and not certificate_changed:
            return self.get_distribution(distribution_id)

        payload = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        response = self._call(
            "PUT",
            "/distribution/{0}/config".format(distribution_id),
            body=payload,
            headers={"If-Match": etag},
        )
        parsed = self._parse(response).get("Distribution") or {}
        config = parsed.get("DistributionConfig") or {}
        merged = dict(parsed)
        if not merged.get("Aliases"):
            merged["Aliases"] = config.get("Aliases")
        return self._summary_to_distribution(merged, response.header("ETag"))


def _namespace_of(node: ET.Element) -> str:
    if node.tag.startswith("{"):
        return node.tag[1:].split("}", 1)[0]
    return ""


def _qualified(name: str, namespace: str) -> str:
    return "{{{0}}}{1}".format(namespace, name) if namespace else name


def _ensure_child(
    parent: ET.Element,
    name: str,
    namespace: str,
    *,
    position: Optional[int] = None,
    before: Sequence[str] = (),
) -> ET.Element:
    found = parent.find(_qualified(name, namespace))
    if found is not None:
        return found
    node = ET.Element(_qualified(name, namespace))
    index = position
    if index is None and before:
        for sibling in before:
            existing = parent.find(_qualified(sibling, namespace))
            if existing is not None:
                index = list(parent).index(existing)
                break
    if index is None:
        parent.append(node)
    else:
        parent.insert(index, node)
    return node


def _set_text(parent: ET.Element, name: str, value: str, namespace: str) -> None:
    node = parent.find(_qualified(name, namespace))
    if node is None:
        node = ET.SubElement(parent, _qualified(name, namespace))
    node.text = value


def _caller_reference(domain: str) -> str:
    """Unique per create attempt; CloudFront rejects reuse with a different config."""
    return "domainkit-{0}-{1}".format(normalize_domain(domain), int(time.time()))


def _child(parent: ET.Element, name: str, text: Optional[str] = None) -> ET.Element:
    node = ET.SubElement(parent, _qualified(name, CLOUDFRONT_NAMESPACE))
    if text is not None:
        node.text = text
    return node


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _quantity_items(parent: ET.Element, name: str, values: Sequence[str], item_tag: str) -> None:
    """Emit CloudFront's ``<X><Quantity>n</Quantity><Items>..</Items></X>`` shape."""
    node = _child(parent, name)
    _child(node, "Quantity", str(len(values)))
    if values:
        items = _child(node, "Items")
        for value in values:
            _child(items, item_tag, value)


def _tags_element(parent: ET.Element, tags: Mapping[str, str]) -> None:
    node = _child(parent, "Tags")
    items = _child(node, "Items")
    for key, value in tags.items():
        tag = _child(items, "Tag")
        _child(tag, "Key", str(key))
        _child(tag, "Value", str(value))


def build_distribution_config(
    domain: str,
    origin: OriginSpec,
    options: DistributionOptions,
    *,
    aliases: Sequence[str] = (),
    certificate_arn: Optional[str] = None,
    minimum_protocol_version: str = "TLSv1.2_2021",
    caller_reference: Optional[str] = None,
) -> ET.Element:
    """Build a DistributionConfig element in the order CloudFront's schema wants.

    Element order is not cosmetic here: CloudFront validates the sequence and
    rejects a reordered body with MalformedXML.
    """
    apex = normalize_domain(domain)
    comment = options.comment.strip() or "domainkit managed distribution for {0}".format(apex)
    marker = marker_for(apex)
    if marker not in comment:
        comment = "{0} {1}".format(comment, marker)
    comment = comment[:128]
    origin_id = "domainkit-origin"

    root = ET.Element(_qualified("DistributionConfig", CLOUDFRONT_NAMESPACE))
    _child(root, "CallerReference", caller_reference or _caller_reference(apex))
    _quantity_items(root, "Aliases", [normalize_domain(a, allow_wildcard=True) for a in aliases], "CNAME")
    _child(root, "DefaultRootObject", options.default_root_object)

    origins = _child(root, "Origins")
    _child(origins, "Quantity", "1")
    origin_items = _child(origins, "Items")
    origin_node = _child(origin_items, "Origin")
    _child(origin_node, "Id", origin_id)
    _child(origin_node, "DomainName", origin.domain)
    _child(origin_node, "OriginPath", origin.path)
    _quantity_items(origin_node, "CustomHeaders", [], "OriginCustomHeader")
    custom = _child(origin_node, "CustomOriginConfig")
    _child(custom, "HTTPPort", str(origin.http_port))
    _child(custom, "HTTPSPort", str(origin.https_port))
    _child(custom, "OriginProtocolPolicy", origin.origin_protocol_policy)
    _quantity_items(custom, "OriginSslProtocols", ["TLSv1.2"], "SslProtocol")
    _child(custom, "OriginReadTimeout", "30")
    _child(custom, "OriginKeepaliveTimeout", "5")
    _child(origin_node, "ConnectionAttempts", "3")
    _child(origin_node, "ConnectionTimeout", "10")

    behavior = _child(root, "DefaultCacheBehavior")
    _child(behavior, "TargetOriginId", origin_id)
    trusted = _child(behavior, "TrustedSigners")
    _child(trusted, "Enabled", "false")
    _child(trusted, "Quantity", "0")
    _child(behavior, "ViewerProtocolPolicy", options.viewer_protocol_policy)
    methods = _child(behavior, "AllowedMethods")
    _child(methods, "Quantity", "2")
    method_items = _child(methods, "Items")
    for verb in ("GET", "HEAD"):
        _child(method_items, "Method", verb)
    cached = _child(methods, "CachedMethods")
    _child(cached, "Quantity", "2")
    cached_items = _child(cached, "Items")
    for verb in ("GET", "HEAD"):
        _child(cached_items, "Method", verb)
    _child(behavior, "SmoothStreaming", "false")
    _child(behavior, "Compress", _bool_text(options.compress))
    _quantity_items(behavior, "LambdaFunctionAssociations", [], "LambdaFunctionAssociation")
    _quantity_items(behavior, "FunctionAssociations", [], "FunctionAssociation")
    _child(behavior, "FieldLevelEncryptionId", "")
    _child(behavior, "CachePolicyId", CACHE_POLICY_CACHING_OPTIMIZED)

    if options.spa:
        # Client-side routing: unknown paths must render the app shell.
        responses = _child(root, "CustomErrorResponses")
        _child(responses, "Quantity", "2")
        response_items = _child(responses, "Items")
        for code in ("403", "404"):
            item = _child(response_items, "CustomErrorResponse")
            _child(item, "ErrorCode", code)
            _child(item, "ResponsePagePath", "/index.html")
            _child(item, "ResponseCode", "200")
            _child(item, "ErrorCachingMinTTL", "10")
    else:
        _quantity_items(root, "CustomErrorResponses", [], "CustomErrorResponse")

    _child(root, "Comment", comment)

    logging_node = _child(root, "Logging")
    _child(logging_node, "Enabled", "false")
    _child(logging_node, "IncludeCookies", "false")
    _child(logging_node, "Bucket", "")
    _child(logging_node, "Prefix", "")

    _child(root, "PriceClass", options.price_class)
    _child(root, "Enabled", _bool_text(options.enabled))

    viewer = _child(root, "ViewerCertificate")
    if certificate_arn:
        _child(viewer, "ACMCertificateArn", certificate_arn)
        _child(viewer, "SSLSupportMethod", "sni-only")
        _child(viewer, "MinimumProtocolVersion", minimum_protocol_version)
    else:
        _child(viewer, "CloudFrontDefaultCertificate", "true")

    # Pay as you go: no WAF, no security bundle, so restrictions stay empty.
    restrictions = _child(root, "Restrictions")
    geo = _child(restrictions, "GeoRestriction")
    _child(geo, "RestrictionType", "none")
    _child(geo, "Quantity", "0")

    _child(root, "HttpVersion", options.http_version)
    _child(root, "IsIPV6Enabled", _bool_text(options.ipv6))
    return root
