"""Persistent HTTP keep-alive pools for OpenAI + ElevenLabs.

Every call to ``urllib.request.urlopen`` opens a fresh TCP + TLS connection,
which costs ~150-250 ms on every wake cycle. Switching to ``urllib3.PoolManager``
keeps connections warm, reusing them across requests and saving that handshake
on every subsequent call.

Each host gets its own ``PoolManager`` with sane defaults (small pool, keep-alive
on). Connections are global to the process — safe across threads (urllib3 is
thread-safe). If a request fails (e.g. server closed the keep-alive socket),
urllib3 transparently reconnects.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import urllib3

log = logging.getLogger(__name__)


_POOLS: dict[str, urllib3.PoolManager] = {}


def _ca_certs_file() -> str | None:
    try:
        import certifi

        return certifi.where()
    except Exception:
        return None


def _pool_for_host(host_key: str) -> urllib3.PoolManager:
    pool = _POOLS.get(host_key)
    if pool is not None:
        return pool
    pool = urllib3.PoolManager(
        num_pools=4,
        maxsize=8,
        block=False,
        retries=urllib3.Retry(total=0, redirect=0),
        ca_certs=_ca_certs_file(),
        cert_reqs="CERT_REQUIRED",
        timeout=urllib3.Timeout(connect=8.0, read=60.0),
        socket_options=[
            # Keep TCP keep-alive on so idle sockets stay warm.
            *(urllib3.connection.HTTPConnection.default_socket_options or []),
        ],
    )
    _POOLS[host_key] = pool
    return pool


def get_pool_for_url(url: str) -> urllib3.PoolManager:
    """Return a process-wide ``PoolManager`` keyed by the host of ``url``."""
    parsed = urlparse(url)
    host = (parsed.hostname or "default").lower()
    return _pool_for_host(host)


def http_request(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float | None = None,
    preload_content: bool = True,
) -> urllib3.BaseHTTPResponse:
    """Issue an HTTP request through the keep-alive pool for ``url``'s host.

    Returns the ``urllib3`` response (raises ``urllib3.exceptions.MaxRetryError``
    or similar on transport failure). Use ``preload_content=False`` for streamed
    responses (e.g. ElevenLabs PCM stream).
    """
    pool = get_pool_for_url(url)
    final_headers = dict(headers or {})
    final_headers.setdefault("Connection", "keep-alive")
    timeout: Any = (
        urllib3.Timeout(connect=8.0, read=float(timeout_s))
        if timeout_s is not None
        else None
    )
    return pool.request(
        method=method.upper(),
        url=url,
        body=body,
        headers=final_headers,
        preload_content=preload_content,
        timeout=timeout,
    )


def prewarm_hosts(urls: list[str]) -> None:
    """Open TLS connections to each host so the next real request is hot.

    Sends a cheap ``HEAD`` request; failures are logged and ignored — pre-warm
    is purely an optimization, never a hard dependency.
    """
    for url in urls:
        try:
            pool = get_pool_for_url(url)
            pool.request(
                method="HEAD",
                url=url,
                headers={"Connection": "keep-alive"},
                timeout=urllib3.Timeout(connect=4.0, read=4.0),
                retries=urllib3.Retry(total=0, redirect=0),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("HTTP prewarm to %s failed (non-fatal): %s", url, exc)
