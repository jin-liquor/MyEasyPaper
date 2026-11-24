from types import SimpleNamespace

import httpx
import pytest

from app.services import outbound
from app.services.outbound import OutboundError, _resolve_public_addresses, validate_base_url


def _dns_result(address: str):
    return [(0, 0, 0, "", (address, 443))]


def test_rejects_dns_answer_sets_containing_private_address(monkeypatch):
    monkeypatch.setattr(
        outbound.socket,
        "getaddrinfo",
        lambda *args, **kwargs: _dns_result("8.8.8.8") + _dns_result("127.0.0.1"),
    )
    with pytest.raises(OutboundError) as error:
        _resolve_public_addresses("provider.invalid", 443)
    assert error.value.code == "AI_BASE_URL_FORBIDDEN"


def test_rejects_credentials_local_hosts_and_unapproved_http():
    for url in (
        "https://user:pass@provider.invalid/v1",
        "https://localhost/v1",
        "http://provider.invalid/v1",
        "https://provider.invalid/v1?token=x",
    ):
        with pytest.raises(OutboundError):
            validate_base_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("location", "second_address", "expected_calls"),
    [
        ("https://other.invalid/next", "8.8.8.8", 1),
        ("https://provider.invalid:80/next", "8.8.8.8", 1),
        ("http://provider.invalid/next", "8.8.8.8", 1),
        ("/next", "10.0.0.2", 1),
        ("/next", "1.1.1.1", 1),
        ("/next", "8.8.8.8", 2),
    ],
)
async def test_redirect_requires_same_origin_and_unchanged_public_pin(
    monkeypatch,
    location,
    second_address,
    expected_calls,
):
    calls = []
    pinned = SimpleNamespace(targets={})

    def reply(request):
        assert pinned.targets == {("provider.invalid", 443): "8.8.8.8"}
        assert request.headers["Authorization"] == "Bearer fixture"
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(307, headers={"Location": location})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(outbound, "_pinned_transport", lambda: (httpx.MockTransport(reply), pinned))
    answers = iter(["8.8.8.8", second_address])
    monkeypatch.setattr(outbound.socket, "getaddrinfo", lambda *args, **kwargs: _dns_result(next(answers)))
    args = ("POST", "https://provider.invalid/v1/chat/completions")
    kwargs = {"headers": {"Authorization": "Bearer fixture"}, "json": {}, "allow_http": True}
    if expected_calls == 2:
        result, _ = await outbound.request_json(*args, **kwargs)
        assert result == {"ok": True}
    else:
        with pytest.raises(OutboundError) as error:
            await outbound.request_json(*args, **kwargs)
        assert error.value.code == "AI_BASE_URL_FORBIDDEN"
    assert calls == [args[1], "https://provider.invalid/next"][:expected_calls]


@pytest.mark.asyncio
async def test_request_never_reaches_transport_without_pinning(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("request reached network or DNS without pinning")

    monkeypatch.setattr(outbound, "_pinned_transport", lambda: (httpx.MockTransport(unexpected), None))
    monkeypatch.setattr(outbound.socket, "getaddrinfo", unexpected)
    with pytest.raises(OutboundError) as error:
        await outbound.request_json("GET", "https://provider.invalid/v1/models", headers={}, json={})
    assert error.value.code == "AI_OUTBOUND_PIN_UNAVAILABLE"


@pytest.mark.asyncio
async def test_httpx_dials_only_resolved_address_via_installed_pin(monkeypatch):
    transport, pinned = outbound._pinned_transport()
    assert pinned is not None
    calls = []

    async def connect(host, port, **kwargs):
        calls.append((host, port))
        raise httpx.ConnectError("synthetic network failure")

    monkeypatch.setattr(pinned.backend, "connect_tcp", connect)
    monkeypatch.setattr(outbound, "_pinned_transport", lambda: (transport, pinned))
    monkeypatch.setattr(outbound.socket, "getaddrinfo", lambda *args, **kwargs: _dns_result("8.8.8.8"))
    with pytest.raises(OutboundError) as error:
        await outbound.request_json("GET", "https://provider.invalid/v1/models", headers={}, json={})
    assert error.value.code == "AI_UPSTREAM_UNREACHABLE"
    assert calls == [("8.8.8.8", 443)]
