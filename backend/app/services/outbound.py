"""Validated outbound HTTP for user-controlled provider base URLs."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx


class _PinnedNetworkBackend:
    def __init__(self, backend) -> None:
        self.backend = backend
        self.targets: dict[tuple[str, int], str] = {}

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        target = self.targets.get((str(host).lower(), int(port)))
        if not target:
            raise OSError("Unvalidated outbound destination")
        return await self.backend.connect_tcp(
            target, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise OSError("Unix sockets are not allowed for provider requests")

    async def sleep(self, seconds):
        await self.backend.sleep(seconds)


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except (OSError, socket.gaierror):
        raise OutboundError("AI_UPSTREAM_UNREACHABLE", "无法解析 AI 服务地址", retryable=True) from None
    if not addresses or any(_forbidden_address(address) for address in addresses):
        raise OutboundError("AI_BASE_URL_FORBIDDEN", "服务地址解析到不允许的网络地址", status_code=422)
    return addresses


class OutboundError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 502, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


def _forbidden_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv6Address) and ip.scope_id is not None)
    )


def validate_base_url(base_url: str, *, allow_http: bool = False) -> str:
    try:
        parsed = urlparse(base_url.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise OutboundError("AI_BASE_URL_FORBIDDEN", "服务地址格式不安全", status_code=422) from None
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if parsed.scheme not in allowed_schemes or not host:
        raise OutboundError("AI_BASE_URL_FORBIDDEN", "服务地址必须使用允许的 HTTPS 地址", status_code=422)
    if parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query:
        raise OutboundError("AI_BASE_URL_FORBIDDEN", "服务地址格式不安全", status_code=422)
    if port is not None and port not in {80, 443}:
        raise OutboundError("AI_BASE_URL_FORBIDDEN", "服务地址端口不被允许", status_code=422)
    host = host.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise OutboundError("AI_BASE_URL_FORBIDDEN", "服务地址不允许使用本地域名", status_code=422)
    url = parsed.geturl()
    try:
        _origin(url)
    except (httpx.InvalidURL, ValueError):
        raise OutboundError("AI_BASE_URL_FORBIDDEN", "服务地址格式不安全", status_code=422) from None
    return url


def _origin(url: str) -> tuple[str, str, int]:
    parsed = httpx.URL(url)
    return parsed.scheme, parsed.raw_host.decode("ascii"), parsed.port or (443 if parsed.scheme == "https" else 80)


def _pinned_transport():
    transport = httpx.AsyncHTTPTransport(trust_env=False)
    pool = getattr(transport, "_pool", None)
    network = getattr(pool, "_network_backend", None)
    if not callable(getattr(network, "connect_tcp", None)):
        return transport, None
    pinned = _PinnedNetworkBackend(network)
    try:
        pool._network_backend = pinned
    except (AttributeError, TypeError):
        return transport, None
    return transport, pinned if pool._network_backend is pinned else None


async def request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: dict,
    timeout: float = 20,
    max_redirects: int = 3,
    allow_http: bool = False,
) -> tuple[dict, float]:
    """Make a request without proxies, with pinned DNS and revalidated redirects."""
    current = validate_base_url(url, allow_http=allow_http)
    started = asyncio.get_running_loop().time()
    try:
        origin = _origin(current)
        _, host, port = origin
        transport, pinned = _pinned_transport()
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            if pinned is None:
                raise OutboundError("AI_OUTBOUND_PIN_UNAVAILABLE", "AI 出站安全传输不可用", status_code=503)
            for _ in range(max_redirects + 1):
                addresses = await asyncio.to_thread(_resolve_public_addresses, host, port)
                # A keep-alive connection may still use the initial address.
                target_address = pinned.targets.setdefault((host, port), addresses[0])
                if target_address not in addresses:
                    raise OutboundError("AI_BASE_URL_FORBIDDEN", "AI 服务解析地址发生变化", status_code=422)
                response = await client.request(method, current, headers=headers, json=json)
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise OutboundError("AI_UPSTREAM_PROTOCOL_ERROR", "AI 服务返回了无效重定向")
                    target = validate_base_url(urljoin(current, location), allow_http=allow_http)
                    if _origin(target) != origin:
                        raise OutboundError("AI_BASE_URL_FORBIDDEN", "AI 服务重定向到了不允许的地址", status_code=422)
                    current = target
                    continue
                if response.status_code in {401, 403}:
                    raise OutboundError("AI_UPSTREAM_AUTH_FAILED", "AI 服务鉴权失败", status_code=502)
                if response.status_code == 429:
                    raise OutboundError("AI_UPSTREAM_RATE_LIMITED", "AI 服务请求过于频繁", retryable=True)
                if 500 <= response.status_code <= 599:
                    raise OutboundError("AI_UPSTREAM_UNAVAILABLE", "AI 服务暂时不可用", retryable=True)
                if response.status_code >= 400:
                    raise OutboundError("AI_UPSTREAM_PROTOCOL_ERROR", "AI 服务拒绝了请求")
                try:
                    value = response.json()
                except (ValueError, TypeError):
                    raise OutboundError("AI_UPSTREAM_PROTOCOL_ERROR", "AI 服务返回格式无法识别") from None
                if not isinstance(value, dict):
                    raise OutboundError("AI_UPSTREAM_PROTOCOL_ERROR", "AI 服务返回格式无法识别")
                return value, round((asyncio.get_running_loop().time() - started) * 1000, 1)
            raise OutboundError("AI_UPSTREAM_PROTOCOL_ERROR", "AI 服务重定向次数超过限制")
    except OutboundError:
        raise
    except httpx.TimeoutException:
        raise OutboundError("AI_UPSTREAM_TIMEOUT", "AI 服务请求超时", status_code=504, retryable=True) from None
    except httpx.HTTPError:
        raise OutboundError("AI_UPSTREAM_UNREACHABLE", "无法连接 AI 服务", retryable=True) from None
