"""LLM providers via the ONE stock factory ``init_chat_model``: stock classes per
provider, loud errors (missing package names the extra, missing key/endpoint),
key status only, Settings. No live LLM call anywhere."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import stat
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langchain_openrouter import ChatOpenRouter
from PySide6.QtWidgets import QApplication

from tests.conftest import config_path_of, unlock_test_store
from struck.config import LlmConfig, LlmProvider
from struck.secrets import delete_secret
from tests.conftest import put_secret
from struck.plan.providers import PROVIDERS, LlmConfigError, chat_model, key_state, llm_config_of
from struck.ui import panels
from struck.ui.panels import SettingsPanel

APP = QApplication.instance() or QApplication([])


def _llm(provider: LlmProvider, base_url: str | None = None, model: str = "m-1") -> LlmConfig:
    return LlmConfig(provider=provider, model=model, base_url=base_url)


def test_openai_openrouter_ollama_vllm_are_stock_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    # keys come only from the (in-memory test) keyring, passed explicitly
    put_secret("llm_openai", "sk-test")
    put_secret("llm_openrouter", "sk-or-test")
    put_secret("llm_vllm", "none")  # explicit, for a key-less server
    assert isinstance(chat_model(_llm("openai")), ChatOpenAI)
    assert isinstance(chat_model(_llm("openrouter")), ChatOpenRouter)
    assert isinstance(chat_model(_llm("ollama", "http://127.0.0.1:11434")), ChatOllama)
    vllm = chat_model(_llm("vllm", "http://127.0.0.1:8010/v1"))
    assert isinstance(vllm, ChatOpenAI) and str(vllm.openai_api_base) == "http://127.0.0.1:8010/v1"
    delete_secret("llm_vllm")
    with pytest.raises(LlmConfigError, match="'llm_vllm'.*not set"):
        chat_model(_llm("vllm", "http://127.0.0.1:8010/v1"))  # no silent "EMPTY"


def test_missing_package_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Self-isolating: NO env shape is assumed — the fake find_spec simulates
    the missing package, so the loud extra-name is proven even with every
    extra installed."""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(
        name: str, package: str | None = None, path: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if name == "langchain_xai":
            return None
        return real_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    with pytest.raises(LlmConfigError, match=r"uv sync --extra llm-xai"):
        chat_model(_llm("xai"), {})


def test_missing_endpoint_or_key_or_model_is_loud() -> None:
    unlock_test_store()
    with pytest.raises(LlmConfigError, match="endpoint"):
        chat_model(_llm("ollama"), {})
    with pytest.raises(LlmConfigError, match="vllm"):
        chat_model(_llm("vllm"), {})
    with pytest.raises(
        LlmConfigError,
        match="'llm_openrouter'.*not set",
    ):
        chat_model(_llm("openrouter"), {})
    with pytest.raises(LlmConfigError, match="model"):
        chat_model(_llm("openai", model=""), {})


def test_key_state_is_set_or_not_set_only() -> None:
    unlock_test_store()
    assert key_state("openai").startswith("secret llm_openai: missing")
    put_secret("llm_openai", "sk-secret")
    assert key_state("openai") == "secret llm_openai: present"
    assert len(PROVIDERS) == 17 and {"openrouter", "ollama", "vllm"} <= set(PROVIDERS)


def test_no_llm_settings_names_what_to_set(tmp_path: Path) -> None:
    with pytest.raises(LlmConfigError, match="Settings tab"):
        llm_config_of(tmp_path / "llm.json")


def test_settings_saves_per_user_0600_never_the_tracked_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlock_test_store()
    monkeypatch.setattr(panels, "UI_LOG", tmp_path / "ui_events.log")
    cfg_before = config_path_of(tmp_path).read_bytes()
    path = tmp_path / "struck" / "llm.json"
    s = SettingsPanel(path)
    s.provider.setCurrentText("openrouter")
    assert s.key_label.text().startswith("secret llm_openrouter: missing")
    s.model_pick.setCurrentText("nvidia/nemotron-3-super-120b-a12b:free")
    s.save()
    assert LlmConfig.model_validate_json(path.read_bytes()) == _llm(
        "openrouter", model="nvidia/nemotron-3-super-120b-a12b:free"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config_path_of(tmp_path).read_bytes() == cfg_before  # the config is never written
    assert "'llm_openrouter'" in s.state.text() and "not set" in s.state.text()
    s.provider.setCurrentText("ollama")
    s.base_url.setText("")
    s.save()
    assert "endpoint" in s.state.text()
    path.chmod(0o666)
    with pytest.raises(LlmConfigError, match="must be 600"):
        llm_config_of(path)
