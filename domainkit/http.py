"""Small HTTP layer built on urllib.

Everything that talks to a remote API goes through `Transport`, which keeps the
clients testable: production uses `UrllibTransport`, tests inject a fake.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional

from .errors import DomainKitError

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str = ""

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return default

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.text)


class Transport:
    """Interface implemented by `UrllibTransport` and by test doubles."""

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: Optional[float] = None,
    ) -> Response:
        raise NotImplementedError


class UrllibTransport(Transport):
    """Blocking transport with bounded retries on transient failures."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        max_attempts: int = 4,
        backoff_base: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        user_agent: str = "domainkit/1.0",
    ) -> None:
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base
        self._sleep = sleep
        self._jitter = jitter
        self._user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: Optional[float] = None,
    ) -> Response:
        sent: Dict[str, str] = {"User-Agent": self._user_agent}
        sent.update(dict(headers or {}))
        last_network_error: Optional[BaseException] = None

        for attempt in range(1, self._max_attempts + 1):
            request = urllib.request.Request(url, data=body, method=method.upper())
            for key, value in sent.items():
                request.add_header(key, value)
            try:
                with urllib.request.urlopen(request, timeout=timeout or self._timeout) as raw:
                    response = Response(raw.status, dict(raw.headers.items()), raw.read(), url)
            except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a body
                response = Response(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read(), url)
            except urllib.error.URLError as exc:
                last_network_error = exc
                if attempt == self._max_attempts:
                    raise DomainKitError("network failure calling {0}: {1}".format(url, exc)) from exc
                self._sleep(self._delay(attempt))
                continue

            if response.status in RETRY_STATUSES and attempt < self._max_attempts:
                self._sleep(self._retry_after(response) or self._delay(attempt))
                continue
            return response

        raise DomainKitError("exhausted retries calling {0}: {1}".format(url, last_network_error))

    def _delay(self, attempt: int) -> float:
        return self._backoff_base * (2 ** (attempt - 1)) + self._jitter() * self._backoff_base

    @staticmethod
    def _retry_after(response: Response) -> Optional[float]:
        raw = response.header("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None
