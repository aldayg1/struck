"""Keyring-only keys (struck.secrets) and their host routing; base_url / StruckConfig URL rules."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from tests.conftest import TEST_CONFIG, put_secret, unlock_test_store
from struck.chat import cli as chat_cli
from struck.callhistory import RecordedHttp
from struck.chain.backends import make_backend
from struck.chain.cli import CardanoCli
from struck.config import ConfigError, LlmConfig, LlmProvider, StruckConfig
from struck.plan import providers
from struck.plan.providers import (
    BASE_URL_ENV,
    OFFICIAL_URLS,
    LlmConfigError,
    chat_model,
    key_state,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",  # remote plain http
        "http://169.254.169.254/latest",  # cloud metadata
        "https://169.254.169.254/v1",
        "ftp://127.0.0.1/x",
        "https://user:pw@openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1?key=x",
        "https://openrouter.ai/api/v1#frag",
    ],
)
def test_base_url_rules_reject(url: str) -> None:
    with pytest.raises(ValidationError):
        LlmConfig(provider="openai", model="m", base_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://openrouter.ai/api/v1",
        "http://127.0.0.1:8010/v1",
        "http://localhost:11434",
        "http://[::1]:8010/v1",
    ],
)
def test_base_url_rules_accept(url: str) -> None:
    assert LlmConfig(provider="openai", model="m", base_url=url).base_url == url


def test_cli_base_url_goes_through_the_same_rule(capsys: pytest.CaptureFixture[str]) -> None:
    rc = chat_cli.main(
        ["--provider", "vllm", "--model", "m", "--base-url", "http://169.254.169.254/v1", "x"]
    )
    assert rc != 0 and "link-local" in capsys.readouterr().out


@pytest.fixture(autouse=True)
def _cli_copy(tmp_path: Path) -> None:
    """A temp copy of a stock binary stands in for cardano-cli (never /mnt/cardano-node)."""
    cli = tmp_path / "cardano-cli-copy"
    shutil.copy("/bin/true", cli)
    cli.chmod(0o755)
    _CLI[0] = str(cli)


_CLI = [""]


def _cfg(**over: str) -> str:
    d: dict[str, object] = {**TEST_CONFIG, "cardano_cli_path": _CLI[0]}
    d.update(over)
    return json.dumps(d)


def test_struck_config_urls_and_cli_path(tmp_path: Path) -> None:
    StruckConfig.model_validate_json(_cfg())
    for over in (
        {"kupo_url": "http://10.0.0.5:1442"},
        {"ogmios_url": "https://ogmios.example.com"},
        {"koios_url": "http://preprod.koios.rest/api/v1"},
    ):
        with pytest.raises(ValidationError):
            StruckConfig.model_validate_json(_cfg(**over))
    relative = StruckConfig.model_validate_json(_cfg(cardano_cli_path="bin/cardano-cli"))
    with pytest.raises(ConfigError, match="cardano_cli_path"):
        relative.checked_cardano_cli  # noqa: B018
    cli = tmp_path / "cardano-cli"
    shutil.copy("/bin/true", cli)
    cli.chmod(0o777)
    writable = StruckConfig.model_validate_json(_cfg(cardano_cli_path=str(cli)))
    with pytest.raises(ConfigError, match="writable"):
        writable.checked_cardano_cli  # noqa: B018
    with pytest.raises(ConfigError, match="writable"):
        CardanoCli(writable)  # the node backend refuses before any work
    # The koios backend never spawns cardano-cli, so it is not refused over it.
    make_backend(writable, "koios", RecordedHttp(5))


def test_secret_key_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    unlock_test_store()
    seen: list[dict[str, object]] = []

    def fake_init(model: str, **kwargs: object) -> object:
        seen.append(kwargs)
        return object()

    monkeypatch.setattr(providers, "init_chat_model", fake_init)
    env: dict[str, str] = {}
    with pytest.raises(LlmConfigError, match="'llm_openrouter'.*struck.keys set llm_openrouter"):
        chat_model(LlmConfig(provider="openrouter", model="m", base_url=None), env)
    put_secret("llm_openrouter", "env-value")
    chat_model(LlmConfig(provider="openrouter", model="m", base_url=None), env)
    # base_url is NEVER None for a hosted provider: the official URL is pinned
    assert seen[-1] == {
        "model_provider": "openrouter",
        "base_url": OFFICIAL_URLS["openrouter"],
        "api_key": "env-value",
    }
    chat_model(
        LlmConfig(provider="openrouter", model="m", base_url="https://openrouter.ai/api/v1"), env
    )
    assert seen[-1].get("api_key") == "env-value"
    n = len(seen)
    with pytest.raises(LlmConfigError, match="allow_custom_host"):
        chat_model(
            LlmConfig(provider="openrouter", model="m", base_url="https://proxy.example.com/v1"),
            env,
        )
    with pytest.raises(LlmConfigError, match="https"):
        chat_model(
            LlmConfig(
                provider="openrouter",
                model="m",
                base_url="http://127.0.0.1:9/v1",
                allow_custom_host=True,
            ),
            env,
        )
    custom = LlmConfig(
        provider="openrouter",
        model="m",
        base_url="https://proxy.example.com/v1",
        allow_custom_host=True,
    )
    # 2b: an opted-in custom host NEVER gets the provider's standard key
    with pytest.raises(LlmConfigError, match="'llm_custom'.*not set"):
        chat_model(custom, env)
    assert len(seen) == n  # nothing built on any refusal
    put_secret("llm_custom", "custom-value")
    chat_model(custom, env)
    assert seen[-1].get("api_key") == "custom-value"
    assert seen[-1].get("base_url") == "https://proxy.example.com/v1"
    vllm = LlmConfig(provider="vllm", model="m", base_url="http://127.0.0.1:8010/v1")
    with pytest.raises(LlmConfigError, match="'llm_vllm'.*not set"):
        chat_model(vllm, {})  # no default key
    put_secret("llm_vllm", "explicit")
    chat_model(vllm, {})
    assert seen[-1].get("api_key") == "explicit"
    chat_model(LlmConfig(provider="ollama", model="m", base_url="http://127.0.0.1:11434"), {})
    assert "api_key" not in seen[-1]


def test_key_state_is_presence_only() -> None:
    unlock_test_store()
    assert key_state("openrouter") == (
        "secret llm_openrouter: missing (run `uv run python -m struck.keys set llm_openrouter`)"
    )
    put_secret("llm_openrouter", "VALUE-xyz")
    assert key_state("openrouter") == "secret llm_openrouter: present"
    assert "VALUE-xyz" not in key_state("openrouter")


@pytest.mark.parametrize(
    ("provider", "var"),
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
        ("groq", "GROQ_API_KEY"),
        ("vllm", "VLLM_API_KEY"),
        ("azure_openai", "AZURE_OPENAI_API_KEY"),
        ("openrouter", "STRUCK_CUSTOM_LLM_API_KEY"),
    ],
)
def test_env_key_vars_are_ignored_even_when_set(
    monkeypatch: pytest.MonkeyPatch, provider: str, var: str
) -> None:
    """A key in the process environment is never used: the keyring entry is missing,
    so the provider refuses, even with the standard variable set."""
    unlock_test_store()
    seen = _fake(monkeypatch)
    monkeypatch.setenv(var, "env-secret-must-not-be-used")
    prov = cast("LlmProvider", provider)
    url = {"vllm": "http://127.0.0.1:8010/v1", "azure_openai": "https://r.openai.azure.com"}
    env = {var: "env-secret-must-not-be-used", "OPENAI_API_VERSION": "2024-10-21"}
    with pytest.raises(LlmConfigError, match="not set"):
        chat_model(LlmConfig(provider=prov, model="m", base_url=url.get(provider)), env)
    assert seen == []
    assert "present" not in key_state(prov)


def test_struck_never_reads_the_) -> None:
    src = "\n".join(p.read_text(encoding="utf-8") for p in Path("struck").rglob("*.py"))
    assert "


def _fake(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    seen: list[dict[str, object]] = []

    def fake_init(model: str, **kwargs: object) -> object:
        seen.append(kwargs)
        return object()

    monkeypatch.setattr(providers, "init_chat_model", fake_init)
    return seen


@pytest.mark.parametrize(
    ("provider", "var"),
    [
        (p, v)
        for p, vs in BASE_URL_ENV.items()
        for v in vs
        if p
        in (
            "openai",
            "anthropic",
            "groq",
            "mistralai",
            "cohere",
            "openrouter",
            "google_genai",
            "ollama",
            "vllm",
        )
    ],
)
def test_sdk_base_url_env_override_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, provider: str, var: str
) -> None:
    seen = _fake(monkeypatch)
    info = providers.PROVIDERS[cast("LlmProvider", provider)]
    url = "http://127.0.0.1:8010/v1" if info.endpoint_required else None
    if info.keyed:
        put_secret(f"llm_{provider}", "k")
    env = {var: "https://evil.example.com"}
    with pytest.raises(LlmConfigError, match=var):
        chat_model(
            LlmConfig(provider=cast("LlmProvider", provider), model="m", base_url=url),
            env,
        )
    assert seen == []


def test_every_official_url_is_pinned_for_installed_hosted_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _fake(monkeypatch)
    for p in ("openai", "anthropic", "groq", "mistralai", "cohere", "openrouter", "google_genai"):
        prov: LlmProvider = p
        put_secret(f"llm_{p}", "k")
        chat_model(LlmConfig(provider=prov, model="m", base_url=None), {})
        assert seen[-1]["base_url"] == OFFICIAL_URLS[prov]


@pytest.mark.parametrize("provider", ["google_vertexai", "bedrock_converse"])
@pytest.mark.parametrize("url", [None, "https://gw.example.com"])
def test_vertex_and_bedrock_are_loudly_unsupported(
    monkeypatch: pytest.MonkeyPatch, provider: str, url: str | None
) -> None:
    """Their SDK credential chains read env vars / credential files: refused outright."""

    def found(name: str) -> object:
        return object()  # pretend the (uninstalled) stock package is present

    monkeypatch.setattr(importlib.util, "find_spec", found)
    seen = _fake(monkeypatch)
    prov = cast("LlmProvider", provider)
    with pytest.raises(LlmConfigError, match="UNSUPPORTED"):
        chat_model(LlmConfig(provider=prov, model="m", base_url=url), {})
    assert seen == [] and "UNSUPPORTED" in key_state(prov)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com",
        "https://openai.azure.com",
        "https://x.openai.azure.com.evil.com",
        "https://a.b.openai.azure.com",
        "https://myres.openai.azure.com:8443",
        "http://myres.openai.azure.com",
        "https://user:pw@myres.openai.azure.com",
        "https://myres.openai.azure.com.",
    ],
)
def test_azure_key_only_to_openai_azure_hosts(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    seen = _fake(monkeypatch)
    put_secret("llm_azure_openai", "az-key")
    env: dict[str, str] = {}
    for allow in (False, True):
        # refused either by LlmConfig's URL check (ValueError) or by chat_model
        with pytest.raises(
            (LlmConfigError, ValueError), match="refused|refusing|not allowed|only to"
        ):
            chat_model(
                LlmConfig(
                    provider="azure_openai", model="m", base_url=url, allow_custom_host=allow
                ),
                env,
            )
    assert seen == []


def test_azure_key_goes_to_its_resource_host(monkeypatch: pytest.MonkeyPatch) -> None:
    unlock_test_store()
    seen = _fake(monkeypatch)
    url = "https://myres.openai.azure.com"
    env = {"OPENAI_API_VERSION": "2024-10-21"}
    with pytest.raises(LlmConfigError, match="'llm_azure_openai'.*not set"):
        chat_model(LlmConfig(provider="azure_openai", model="m", base_url=url), env)
    put_secret("llm_azure_openai", "az")
    chat_model(LlmConfig(provider="azure_openai", model="m", base_url=url), env)
    assert seen[-1]["azure_endpoint"] == url and seen[-1]["api_key"] == "az"
    assert "base_url" not in seen[-1] and seen[-1]["api_version"] == "2024-10-21"
    with pytest.raises(LlmConfigError, match="OPENAI_API_VERSION is not set"):
        chat_model(LlmConfig(provider="azure_openai", model="m", base_url=url), {})


@pytest.mark.skipif(
    importlib.util.find_spec("langchain_openai") is None, reason="langchain-openai not installed"
)
def test_azure_endpoint_builds_the_stock_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real AzureChatOpenAI (no request is sent): it builds with the resource URL
    as azure_endpoint, which base_url never did."""
    for var in ("AZURE_OPENAI_ENDPOINT", "OPENAI_API_BASE", "OPENAI_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    url = "https://myres.openai.azure.com"
    put_secret("llm_azure_openai", "az")
    env = {"OPENAI_API_VERSION": "2024-10-21"}
    m = chat_model(LlmConfig(provider="azure_openai", model="gpt-4o", base_url=url), env)
    from langchain_openai import AzureChatOpenAI

    assert isinstance(m, AzureChatOpenAI)
    assert m.azure_endpoint == url and m.openai_api_base is None


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com./v1",  # trailing dot
        "https://openrouter.ai./api/v1",
        "https://\u0430pi.openai.com/v1",  # IDN homograph (Cyrillic a)
        "https://xn--pi-7kc.openai.com/v1",  # punycode
        "http://2130706433/v1",  # all-numeric (decimal 127.0.0.1)
        "http://127.1/v1",
        "https://0x7f.0.0.1/v1",
        "https://10.0.0.1.5/v1",
    ],
)
def test_checked_url_refuses_trailing_dot_idn_numeric(url: str) -> None:
    with pytest.raises(ValidationError):
        LlmConfig(provider="openai", model="m", base_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com.evil.example/v1",
        "https://evil.api.openai.com/v1",
        "https://openai.com/v1",
    ],
)
def test_subdomain_and_lookalike_hosts_are_custom(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    seen = _fake(monkeypatch)
    with pytest.raises(LlmConfigError, match="allow_custom_host"):
        chat_model(LlmConfig(provider="openai", model="m", base_url=url), {})
    assert seen == []


def test_llm_json_save_and_load_refuse_symlinks_and_bad_modes(tmp_path: Path) -> None:
    from struck.config import load_llm_config, save_llm_config

    d = tmp_path / "cfg"
    d.mkdir(mode=0o700)
    path = d / "llm.json"
    llm = LlmConfig(provider="vllm", model="m", base_url="http://127.0.0.1:8010/v1")
    save_llm_config(llm, path)
    assert (path.stat().st_mode & 0o777) == 0o600 and load_llm_config(path) == llm
    assert [p.name for p in d.iterdir()] == ["llm.json"]  # no temp file left behind
    path.chmod(0o644)
    with pytest.raises(ValueError, match="must be 600"):
        load_llm_config(path)
    path.chmod(0o600)
    target = tmp_path / "elsewhere.json"
    target.write_text(path.read_text())
    target.chmod(0o600)
    link = d / "link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_llm_config(link)
    with pytest.raises(ValueError, match="symlink"):
        save_llm_config(llm, link)
    assert target.read_text() == path.read_text()  # untouched
    d.chmod(0o777)
    with pytest.raises(ValueError, match="group/other-writable"):
        save_llm_config(llm, path)
    with pytest.raises(ValueError, match="group/other-writable"):
        load_llm_config(path)
    d.chmod(0o700)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked_dir = tmp_path / "linkdir"
    linked_dir.symlink_to(real)
    with pytest.raises(ValueError, match="not a symlink"):
        save_llm_config(llm, linked_dir / "llm.json")
