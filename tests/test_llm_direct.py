"""M3: every LLM client Struck builds talks DIRECT: no proxy / CA env.

Integrations that take an httpx client get httpx.Client(trust_env=False) with
default TLS verification; with HTTPS_PROXY and SSL_CERT_FILE set, the built
SDK transport has no proxy mount and the default verify context. Every other
integration refuses to start while any proxy/CA variable is set. No request
is sent by any test here."""

from __future__ import annotations

import importlib.util
import ssl
from pathlib import Path
from typing import cast

import httpx
import pytest

from struck.config import LlmConfig, LlmProvider
from struck.plan import providers
from struck.plan.providers import PROXY_CA_ENV, LlmConfigError, chat_model
from tests.conftest import put_secret


def _poison(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    ca = tmp_path / "evil.pem"
    ca.write_text("not a cert", encoding="utf-8")
    env = {"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}
    env |= {"SSL_CERT_FILE": str(ca), "SSL_CERT_DIR": str(tmp_path)}
    for k, v in env.items():
        monkeypatch.setenv(k, v)  # the SDKs would read os.environ themselves
    return env


def _direct(client: httpx.Client) -> None:
    assert client.trust_env is False and client._mounts == {}
    transport = client._transport
    assert isinstance(transport, httpx.HTTPTransport)
    ctx = transport._pool._ssl_context
    assert ctx is not None and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname


def _needs(module: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(importlib.util.find_spec(module) is None, reason=f"{module} absent")


CASES: list[tuple[LlmProvider, str | None, dict[str, str]]] = [
    ("openai", None, {}),
    ("vllm", "http://127.0.0.1:8010/v1", {}),
    ("azure_openai", "https://res.openai.azure.com", {"OPENAI_API_VERSION": "2024-10-21"}),
]


@_needs("langchain_openai")
@pytest.mark.parametrize(("provider", "url", "keys"), CASES)
def test_openai_family_is_direct_with_proxy_and_ca_env_set(
    provider: LlmProvider,
    url: str | None,
    keys: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    put_secret(f"llm_{provider}", "k")
    env = _poison(monkeypatch, tmp_path) | keys
    for k in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "AZURE_OPENAI_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    from langchain_openai.chat_models.base import BaseChatOpenAI

    m = chat_model(LlmConfig(provider=provider, model="m", base_url=url), env)
    assert isinstance(m, BaseChatOpenAI)
    root = cast("object", m.root_client)
    sdk_http = cast("object", getattr(root, "_client"))  # the openai SDK's transport
    assert isinstance(sdk_http, httpx.Client)
    _direct(sdk_http)


@_needs("langchain_groq")
def test_groq_is_direct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    put_secret("llm_groq", "k")
    env = _poison(monkeypatch, tmp_path)
    kw = providers.direct_client_kwargs("groq")
    client = kw.get("http_client")
    assert isinstance(client, httpx.Client)
    _direct(client)
    from langchain_groq import ChatGroq

    m = chat_model(LlmConfig(provider="groq", model="m", base_url=None), env)
    assert isinstance(m, ChatGroq)
    assert cast("object", m.http_client) is not None


@_needs("langchain_ollama")
def test_ollama_is_direct(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # import first: the ollama package builds an unused module-level default client
    from langchain_ollama import ChatOllama

    env = _poison(monkeypatch, tmp_path)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    m = chat_model(LlmConfig(provider="ollama", model="m", base_url="http://127.0.0.1:11434"), env)
    assert isinstance(m, ChatOllama)
    assert cast("object", m.client_kwargs) == {"trust_env": False, "timeout": 120.0}
    sync = cast("object", m._client)
    inner = cast("object", getattr(sync, "_client"))  # ollama.Client's httpx transport
    assert isinstance(inner, httpx.Client)
    assert inner.trust_env is False and inner._mounts == {}


REFUSING: list[tuple[LlmProvider, str]] = [
    ("anthropic", "langchain_anthropic"),
    ("google_genai", "langchain_google_genai"),
    ("mistralai", "langchain_mistralai"),
    ("cohere", "langchain_cohere"),
    ("openrouter", "langchain_openrouter"),
    ("fireworks", "langchain_fireworks"),
    ("together", "langchain_together"),
]


@pytest.mark.parametrize(("provider", "module"), REFUSING)
@pytest.mark.parametrize("var", PROXY_CA_ENV)
def test_integrations_without_a_client_hook_refuse_proxy_and_ca_env(
    provider: LlmProvider, module: str, var: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if importlib.util.find_spec(module) is None:
        pytest.skip(f"{module} not installed")
    built: list[object] = []

    def fake_init(model: str, **kw: object) -> object:
        built.append(kw)
        return object()

    monkeypatch.setattr(providers, "init_chat_model", fake_init)
    put_secret(f"llm_{provider}", "k")  # the key is present: the proxy/CA var alone refuses
    env = {var: "http://127.0.0.1:9"}
    with pytest.raises(LlmConfigError, match=var):
        chat_model(LlmConfig(provider=provider, model="m", base_url=None), env)
    assert built == []  # nothing built
