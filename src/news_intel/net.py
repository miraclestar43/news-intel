"""Safe HTTP fetching: SSRF guards, byte/redirect limits, polite conditional GET."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("ff00::/8"),
]


class UnsafeURLError(Exception):
    pass


def _is_blocked_ip(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    if ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local or ip.is_private:
        return True
    return any(ip in net for net in _BLOCKED_NETWORKS)


def assert_safe_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"disallowed scheme in {url!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError(f"no hostname in {url!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"DNS resolution failed for {host!r}: {exc}") from exc
    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            raise UnsafeURLError(f"{host!r} resolves to disallowed address {ip_str!r}")


@dataclass
class FetchResult:
    status_code: int
    text: str
    headers: httpx.Headers
    not_modified: bool = False


def safe_get(
    url: str,
    *,
    timeout: float = 12.0,
    max_redirects: int = 3,
    max_bytes: int = 5_000_000,
    user_agent: str = "PiNewsIntel/0.1 (+personal use)",
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    assert_safe_url(url)
    headers = {"User-Agent": user_agent}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    with httpx.Client(
        follow_redirects=True,
        max_redirects=max_redirects,
        timeout=timeout,
        headers=headers,
    ) as client:
        with client.stream("GET", url) as resp:
            if resp.status_code == 304:
                return FetchResult(304, "", resp.headers, not_modified=True)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "text" not in content_type and "xml" not in content_type and "json" not in content_type:
                # still allow; many feeds mislabel content-type, but keep the check
                # available for future tightening rather than hard-failing here.
                pass
            chunks = []
            total = 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeURLError(f"response exceeded {max_bytes} bytes")
                chunks.append(chunk)
            body = b"".join(chunks)
            return FetchResult(resp.status_code, body.decode(resp.encoding or "utf-8", errors="replace"), resp.headers)


def retry_after_seconds(exc: httpx.HTTPStatusError) -> float | None:
    """Extracts a numeric delta-seconds Retry-After value (the common case
    for rate-limit responses like GDELT's 429s). An HTTP-date form is not
    handled -- callers fall back to deterministic exponential backoff."""
    value = exc.response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_random_exponential(multiplier=0.5, max=8),
    retry=retry_if_exception(_is_transient),
)
def safe_get_with_retry(*args, **kwargs) -> FetchResult:
    """Two retries (three attempts total) with jitter for transient failures.
    UnsafeURLError and permanent 4xx (other than 304 handling above) are not retried."""
    return safe_get(*args, **kwargs)
