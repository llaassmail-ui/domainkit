"""DNS provider contracts shared by the onboarding workflow."""

from __future__ import annotations

from typing import List, Optional, Protocol, Sequence

from .godaddy import DnsRecord


class DnsProvider(Protocol):
    """The DNS operations DomainKit needs from a registrar."""

    def get_records(
        self, domain: str, record_type: Optional[str] = None, name: Optional[str] = None
    ) -> List[dict]: ...

    def upsert_records(self, domain: str, records: Sequence[DnsRecord]) -> List[DnsRecord]: ...


def has_forwarding(provider: object) -> bool:
    """Whether a provider can manage apex HTTP forwarding."""
    return callable(getattr(provider, "set_forwarding", None))
