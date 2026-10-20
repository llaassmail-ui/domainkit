"""Exception types shared by the domainkit package."""

from __future__ import annotations

from typing import Optional


class DomainKitError(Exception):
    """Base class for every error raised by domainkit."""


class ConfigError(DomainKitError):
    """The profile file or the environment is unusable."""


class HttpError(DomainKitError):
    """A remote API answered with a non-success status."""

    def __init__(self, status: int, url: str, body: str, message: Optional[str] = None) -> None:
        self.status = status
        self.url = url
        self.body = body
        super().__init__(message or "HTTP {0} for {1}: {2}".format(status, url, body[:400]))


class AwsError(HttpError):
    """An AWS API returned a modeled error."""

    def __init__(self, status: int, url: str, body: str, code: str, message: str) -> None:
        self.code = code
        super().__init__(status, url, body, "AWS {0} ({1}): {2}".format(code, status, message))


class GitLabError(HttpError):
    """The GitLab API refused a request."""


class DnsError(DomainKitError):
    """A DNS record could not be reconciled."""


class StepError(DomainKitError):
    """An onboarding step cannot continue."""


class CertificateWaitTimeout(StepError):
    """Local polling ended while ACM validation is still pending."""
