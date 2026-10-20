"""domainkit: onboard a GoDaddy domain onto CloudFront and GitLab CI.

Automates the repetitive path of: buy a domain, request an ACM certificate,
validate it over DNS, create or attach a CloudFront distribution, publish the
site records, then create the GitLab branch and the
``CLOUDFRONT_<DOMAIN>_<TLD>`` CI variable. Single domains and JSON batches.
"""

from __future__ import annotations

__version__ = "2.0.0"

from .aws import Distribution, DistributionOptions, OriginSpec
from .batch import BatchError, BatchResult, load_batch, run_batch, validate_item
from .config import Settings
from .errors import (
    AwsError,
    ConfigError,
    DnsError,
    DomainKitError,
    GitLabError,
    HttpError,
    StepError,
)
from .godaddy import DnsRecord, apex_redirect_url, site_records
from .spaceship import SpaceshipClient
from .naming import (
    DomainNameError,
    branch_name,
    certificate_names,
    normalize_domain,
    registrable_domain,
    service_domain,
    variable_key,
)
from .workflow import Onboarder, Outcome, Plan, RunReport, StepResult

__all__ = [
    "__version__",
    "AwsError",
    "BatchError",
    "BatchResult",
    "ConfigError",
    "DistributionOptions",
    "Distribution",
    "DnsError",
    "DnsRecord",
    "DomainKitError",
    "DomainNameError",
    "GitLabError",
    "HttpError",
    "Onboarder",
    "OriginSpec",
    "Outcome",
    "Plan",
    "RunReport",
    "Settings",
    "StepError",
    "StepResult",
    "SpaceshipClient",
    "apex_redirect_url",
    "branch_name",
    "certificate_names",
    "load_batch",
    "normalize_domain",
    "registrable_domain",
    "service_domain",
    "run_batch",
    "site_records",
    "validate_item",
    "variable_key",
]
