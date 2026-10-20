"""Test doubles: a scripted transport that records what was sent."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from domainkit.http import Response, Transport


@dataclass
class SentRequest:
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[bytes]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8") if self.body else ""

    def target(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "x-amz-target":
                return value.split(".")[-1]
        return ""


Handler = Callable[[SentRequest], Response]


class FakeTransport(Transport):
    """Matches requests against registered rules, newest rule winning."""

    def __init__(self) -> None:
        self.sent: List[SentRequest] = []
        self._rules: List[Tuple[Callable[[SentRequest], bool], Handler]] = []

    def add(self, predicate: Callable[[SentRequest], bool], handler: Handler) -> "FakeTransport":
        self._rules.insert(0, (predicate, handler))
        return self

    def json_route(
        self,
        method: str,
        pattern: str,
        payload: Any,
        *,
        status: int = 200,
        headers: Optional[Mapping[str, str]] = None,
    ) -> "FakeTransport":
        regex = re.compile(pattern)

        def predicate(request: SentRequest) -> bool:
            return request.method.upper() == method.upper() and bool(regex.search(request.url))

        def handler(request: SentRequest) -> Response:
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            merged = {"Content-Type": "application/json"}
            merged.update(dict(headers or {}))
            return Response(status, merged, body, request.url)

        return self.add(predicate, handler)

    def aws_action(self, action: str, payload: Any, *, status: int = 200) -> "FakeTransport":
        def predicate(request: SentRequest) -> bool:
            return request.target() == action

        def handler(request: SentRequest) -> Response:
            body = json.dumps(payload).encode("utf-8")
            return Response(status, {"Content-Type": "application/x-amz-json-1.1"}, body, request.url)

        return self.add(predicate, handler)

    def xml_route(
        self,
        method: str,
        pattern: str,
        xml: str,
        *,
        status: int = 200,
        etag: Optional[str] = None,
    ) -> "FakeTransport":
        regex = re.compile(pattern)

        def predicate(request: SentRequest) -> bool:
            return request.method.upper() == method.upper() and bool(regex.search(request.url))

        def handler(request: SentRequest) -> Response:
            headers = {"Content-Type": "text/xml"}
            if etag:
                headers["ETag"] = etag
            return Response(status, headers, xml.encode("utf-8"), request.url)

        return self.add(predicate, handler)

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: Optional[float] = None,
    ) -> Response:
        request = SentRequest(method.upper(), url, dict(headers or {}), body)
        self.sent.append(request)
        for predicate, handler in self._rules:
            if predicate(request):
                return handler(request)
        raise AssertionError("no fake route for {0} {1}".format(method, url))

    def requests_matching(self, method: str, fragment: str) -> List[SentRequest]:
        return [
            item
            for item in self.sent
            if item.method.upper() == method.upper() and fragment in item.url
        ]


DISTRIBUTION_CONFIG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<DistributionConfig xmlns="http://cloudfront.amazonaws.com/doc/2020-05-31/">
  <CallerReference>ref-1</CallerReference>
  <Aliases>
    <Quantity>1</Quantity>
    <Items>
      <CNAME>old.example.net</CNAME>
    </Items>
  </Aliases>
  <DefaultRootObject>index.html</DefaultRootObject>
  <Origins>
    <Quantity>1</Quantity>
    <Items>
      <Origin>
        <Id>primary</Id>
        <DomainName>bucket.s3.amazonaws.com</DomainName>
      </Origin>
    </Items>
  </Origins>
{comment}
  <Enabled>true</Enabled>
  <ViewerCertificate>
    <CloudFrontDefaultCertificate>true</CloudFrontDefaultCertificate>
  </ViewerCertificate>
</DistributionConfig>
"""


def _viewer_certificate(certificate_arn: Optional[str]) -> str:
    if not certificate_arn:
        return "      <ViewerCertificate><CloudFrontDefaultCertificate>true</CloudFrontDefaultCertificate></ViewerCertificate>"
    return (
        "      <ViewerCertificate>"
        "<ACMCertificateArn>{0}</ACMCertificateArn>"
        "<SSLSupportMethod>sni-only</SSLSupportMethod>"
        "<MinimumProtocolVersion>TLSv1.2_2021</MinimumProtocolVersion>"
        "</ViewerCertificate>"
    ).format(certificate_arn)


def distribution_list_xml(
    distribution_id: str = "E1234567890ABC",
    aliases=("old.example.net",),
    certificate_arn: Optional[str] = None,
    comment: str = "",
) -> str:
    items = "".join("<CNAME>{0}</CNAME>".format(alias) for alias in aliases)
    comment_xml = "<Comment>{0}</Comment>".format(comment) if comment else ""
    return """<?xml version="1.0" encoding="UTF-8"?>
<DistributionList xmlns="http://cloudfront.amazonaws.com/doc/2020-05-31/">
  <Marker></Marker>
  <MaxItems>100</MaxItems>
  <IsTruncated>false</IsTruncated>
  <Quantity>1</Quantity>
  <Items>
    <DistributionSummary>
      <Id>{id}</Id>
      <ARN>arn:aws:cloudfront::111122223333:distribution/{id}</ARN>
      <Status>Deployed</Status>
      <DomainName>d111111abcdef8.cloudfront.net</DomainName>
      <Aliases>
        <Quantity>{count}</Quantity>
        <Items>{items}</Items>
      </Aliases>
{viewer}
{comment}
      <Enabled>true</Enabled>
    </DistributionSummary>
  </Items>
</DistributionList>
""".format(
        id=distribution_id,
        count=len(aliases),
        items=items,
        viewer=_viewer_certificate(certificate_arn),
        comment=comment_xml,
    )


def distribution_xml(
    distribution_id: str = "E1234567890ABC",
    aliases=("old.example.net",),
    certificate_arn: Optional[str] = None,
    comment: str = "",
) -> str:
    items = "".join("<CNAME>{0}</CNAME>".format(alias) for alias in aliases)
    comment_xml = "<Comment>{0}</Comment>".format(comment) if comment else ""
    return """<?xml version="1.0" encoding="UTF-8"?>
<Distribution xmlns="http://cloudfront.amazonaws.com/doc/2020-05-31/">
  <Id>{id}</Id>
  <ARN>arn:aws:cloudfront::111122223333:distribution/{id}</ARN>
  <Status>Deployed</Status>
  <DomainName>d111111abcdef8.cloudfront.net</DomainName>
  <DistributionConfig>
    <Aliases>
      <Quantity>{count}</Quantity>
      <Items>{items}</Items>
    </Aliases>
{viewer}
{comment}
    <Enabled>true</Enabled>
  </DistributionConfig>
</Distribution>
""".format(
        id=distribution_id,
        count=len(aliases),
        items=items,
        viewer=_viewer_certificate(certificate_arn),
        comment=comment_xml,
    )


def distribution_config_xml(
    aliases=("old.example.net",),
    certificate_arn: Optional[str] = None,
    comment: str = "managed",
) -> str:
    """A full DistributionConfig, the shape ``GET /config`` returns."""
    items = "".join("<CNAME>{0}</CNAME>".format(alias) for alias in aliases)
    comment_xml = "<Comment>{0}</Comment>".format(comment) if comment else ""
    return """<?xml version="1.0" encoding="UTF-8"?>
<DistributionConfig xmlns="http://cloudfront.amazonaws.com/doc/2020-05-31/">
  <CallerReference>ref-1</CallerReference>
  <Aliases>
    <Quantity>{count}</Quantity>
    <Items>{items}</Items>
  </Aliases>
  <DefaultRootObject>index.html</DefaultRootObject>
  <Origins>
    <Quantity>1</Quantity>
    <Items>
      <Origin>
        <Id>primary</Id>
        <DomainName>bucket.s3.amazonaws.com</DomainName>
      </Origin>
    </Items>
  </Origins>
{comment}
  <Enabled>true</Enabled>
{viewer}
</DistributionConfig>
""".format(
        count=len(aliases),
        items=items,
        viewer=_viewer_certificate(certificate_arn),
        comment=comment_xml,
    )
