"""LLM providers: ONE stock factory, ``langchain.chat_models.init_chat_model``.

No per-provider client code: only the provider id, the model, an optional
endpoint and the stock integration package (an exact-pinned extra in
pyproject) differ. Keys come ONLY from Struck's encrypted secret store
(``struck.secrets``, service "struck", entry ``llm_<provider>``); no
environment variable is ever read for a key. The value is handed explicitly to
the stock class in memory only, never written to a file, log or receipt, and
only to the provider's official host unless the user opts in to a custom https
host (which gets ``llm_custom`` only). Vertex / Bedrock are UNSUPPORTED: their
SDKs read credentials from the environment / credential files. ``vllm`` is the stock ``openai`` integration
with the user's explicit OpenAI-compatible endpoint; ``ollama`` needs the
user's explicit endpoint. No endpoint is ever defaulted or guessed.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypedDict

import httpx
from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from pathlib import Path
from urllib.parse import urlsplit

from ..secrets import LLM_CUSTOM, SecretError, get_secret, has_secret, llm_entry, set_command
from ..config import LLM_CONFIG_PATH, LlmConfig, LlmProvider, checked_url, load_llm_config


@dataclass(frozen=True)
class ProviderInfo:
    init_provider: str  # the init_chat_model model_provider id
    module: str  # the stock integration module
    extra: str  # pyproject extra that installs it
    keyed: bool  # True = needs secret llm_<provider>
    endpoint_required: bool


PROVIDERS: dict[LlmProvider, ProviderInfo] = {
    "openai": ProviderInfo("openai", "langchain_openai", "llm-openai", True, False),
    "anthropic": ProviderInfo(
        "anthropic",
        "langchain_anthropic",
        "llm-anthropic",
        True,
        False,
    ),
    "google_genai": ProviderInfo(
        "google_genai",
        "langchain_google_genai",
        "llm-google-genai",
        True,
        False,
    ),
    "google_vertexai": ProviderInfo(
        "google_vertexai", "langchain_google_vertexai", "llm-google-vertexai", False, False
    ),
    "bedrock_converse": ProviderInfo(
        "bedrock_converse", "langchain_aws", "llm-bedrock", False, False
    ),
    "azure_openai": ProviderInfo("azure_openai", "langchain_openai", "llm-openai", True, True),
    "mistralai": ProviderInfo(
        "mistralai",
        "langchain_mistralai",
        "llm-mistralai",
        True,
        False,
    ),
    "groq": ProviderInfo("groq", "langchain_groq", "llm-groq", True, False),
    "xai": ProviderInfo("xai", "langchain_xai", "llm-xai", True, False),
    "deepseek": ProviderInfo("deepseek", "langchain_deepseek", "llm-deepseek", True, False),
    "fireworks": ProviderInfo("fireworks", "langchain_fireworks", "llm-fireworks", True, False),
    "together": ProviderInfo("together", "langchain_together", "llm-together", True, False),
    "cohere": ProviderInfo("cohere", "langchain_cohere", "llm-cohere", True, False),
    "perplexity": ProviderInfo("perplexity", "langchain_perplexity", "llm-perplexity", True, False),
    "openrouter": ProviderInfo(
        "openrouter",
        "langchain_openrouter",
        "llm-openrouter",
        True,
        False,
    ),
    "ollama": ProviderInfo("ollama", "langchain_ollama", "llm-ollama", False, True),
    # vLLM serves the OpenAI API: stock ChatOpenAI at the user's endpoint.
    # secret llm_vllm = the key the server was started with (--api-key). It is
    # REQUIRED and has no default; for a server started WITHOUT --api-key (it
    # ignores the header) set it explicitly to any value, e.g. "none".
    "vllm": ProviderInfo("openai", "langchain_openai", "llm-openai", True, True),
}


# Each hosted provider's official API base URL: exactly its stock client's default,
# read from the installed SDK/integration source (file:line in the golden provenance record).
# It is ALWAYS passed explicitly, so no SDK env var or profile can redirect a key.
OFFICIAL_URLS: dict[LlmProvider, str] = {
    "openai": "https://api.openai.com/v1",  # openai/_client.py default
    "anthropic": "https://api.anthropic.com",  # anthropic/_client.py:253
    "google_genai": "https://generativelanguage.googleapis.com/",  # google/genai/_api_client.py:889
    "mistralai": "https://api.mistral.ai/v1",  # langchain_mistralai/chat_models.py:725
    "groq": "https://api.groq.com",  # groq/_client.py:87
    "xai": "https://api.x.ai/v1/",  # langchain_xai ChatXAI.xai_api_base default
    "deepseek": "https://api.deepseek.com/v1",  # langchain_deepseek DEFAULT_API_BASE
    "fireworks": "https://api.fireworks.ai/inference/v1",  # fireworks SDK default
    "together": "https://api.together.xyz/v1/",  # langchain_together default
    "cohere": "https://api.cohere.com",  # cohere/environment.py PRODUCTION
    "openrouter": "https://openrouter.ai/api/v1",  # openrouter/sdkconfiguration.py:22
}
OFFICIAL_HOSTS: dict[LlmProvider, str] = {
    p: urlsplit(u).hostname or "" for p, u in OFFICIAL_URLS.items()
}

# SDK / integration env vars that silently redirect a client's base URL (read from
# each installed package's source; the not-installed ones from upstream source).
# If ANY applicable one is set, Struck refuses to start that provider: the user
# must unset it (a custom host is a Settings opt-in, never an env side door).
_OPENAI_SDK = ("OPENAI_BASE_URL", "OPENAI_API_BASE")
_GATEWAY = ("LANGSMITH_GATEWAY",)  # langchain gateway fallback (anthropic/google/fireworks)
BASE_URL_ENV: dict[LlmProvider, tuple[str, ...]] = {
    "openai": (*_OPENAI_SDK, *_GATEWAY),
    "azure_openai": (*_OPENAI_SDK, "AZURE_OPENAI_ENDPOINT"),
    "anthropic": ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL", *_GATEWAY),
    "google_genai": (
        "GOOGLE_GEMINI_BASE_URL",
        "GOOGLE_VERTEX_BASE_URL",
        "GOOGLE_GENAI_USE_VERTEXAI",
        *_GATEWAY,
    ),
    "google_vertexai": ("GOOGLE_VERTEX_BASE_URL", "GOOGLE_GENAI_USE_VERTEXAI"),
    "bedrock_converse": ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME"),
    "mistralai": ("MISTRAL_BASE_URL", "MISTRAL_API_BASE"),
    "groq": ("GROQ_BASE_URL", "GROQ_API_BASE"),
    "xai": ("XAI_API_BASE", *_OPENAI_SDK),
    "deepseek": ("DEEPSEEK_API_BASE", *_OPENAI_SDK),
    "fireworks": ("FIREWORKS_API_BASE", "FIREWORKS_BASE_URL", *_GATEWAY),
    "together": ("TOGETHER_API_BASE", "TOGETHER_BASE_URL", *_OPENAI_SDK),
    "cohere": ("CO_API_URL",),
    "perplexity": ("PERPLEXITY_BASE_URL", "PPLX_API_BASE"),
    "openrouter": ("OPENROUTER_API_BASE", "OPENROUTER_BASE_URL"),
    "ollama": ("OLLAMA_HOST",),
    "vllm": _OPENAI_SDK,
}
# Credential-chain providers: no official single URL; a base_url is a custom host.
_CHAIN = ("google_vertexai", "bedrock_converse")


class LlmConfigError(RuntimeError):
    pass


def llm_config_of(path: Path = LLM_CONFIG_PATH) -> LlmConfig:
    try:
        llm = load_llm_config(path)
    except (ValueError, OSError) as exc:
        raise LlmConfigError(f"LLM settings {path} refused: {exc}") from exc
    if llm is None:
        raise LlmConfigError(
            f"no LLM configured: use the Settings tab (writes {path}, mode 0600) "
            '{"provider": "<one of ' + ", ".join(PROVIDERS) + '>", "model": "<model id>", '
            '"base_url": "<endpoint>" (required for vllm/ollama/azure_openai, else null)}'
        )
    return llm


def key_state(provider: LlmProvider) -> str:
    """'secret <entry>: present' / 'missing (run ...)'; never the value."""
    if provider in _CHAIN:
        return f"{provider} UNSUPPORTED (SDK credential chain reads env/files)"
    if not PROVIDERS[provider].keyed:
        return "no key (ollama)"
    entry = llm_entry(provider)
    try:
        present = has_secret(entry)
    except SecretError as exc:
        return f"secret {entry}: UNAVAILABLE ({exc})"
    return (
        f"secret {entry}: present"
        if present
        else f"secret {entry}: missing (run `{set_command(entry)}`)"
    )


def _key(entry: str) -> str:
    try:
        return get_secret(entry)
    except SecretError as exc:
        raise LlmConfigError(f"{exc} (no key attached; env vars are never read)") from exc


# Proxy / CA environment variables: they re-route or re-trust a client's TLS.
PROXY_CA_ENV = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)  # fmt: skip
# Integrations that take an explicit httpx client: Struck passes
# httpx.Client/AsyncClient(trust_env=False) (default TLS verification), so the
# variables above are provably ignored. Every OTHER provider refuses to start
# while any of them is set (it would read them itself).
_HTTPX_CLIENT = frozenset({"openai", "azure_openai", "vllm", "groq", "xai", "deepseek"})
_OLLAMA_KWARGS = frozenset({"ollama"})  # ollama.Client(**client_kwargs) -> httpx.Client


class DirectClient(TypedDict, total=False):
    http_client: httpx.Client
    http_async_client: httpx.AsyncClient
    client_kwargs: dict[str, bool | float]


def direct_client_kwargs(provider: LlmProvider) -> DirectClient:
    """The integration's own parameters for a direct (no proxy/CA env) transport."""
    if provider in _HTTPX_CLIENT:
        return {
            "http_client": httpx.Client(trust_env=False),
            "http_async_client": httpx.AsyncClient(trust_env=False),
        }
    if provider in _OLLAMA_KWARGS:
        return {"client_kwargs": {"trust_env": False, "timeout": 120.0}}
    return {}


def _refuse_env_overrides(provider: LlmProvider, env: Mapping[str, str]) -> None:
    if provider not in _HTTPX_CLIENT | _OLLAMA_KWARGS:
        proxied = [v for v in PROXY_CA_ENV if env.get(v)]
        if proxied:
            raise LlmConfigError(
                f"{', '.join(proxied)} set in the environment: the {provider} integration "
                "cannot take a direct (trust_env=False) HTTP client, so it would route through "
                f"that proxy / trust that CA. Unset it (`unset {' '.join(proxied)}`) to use "
                f"{provider}."
            )
    found = [v for v in BASE_URL_ENV.get(provider, ()) if v in env]
    if found:
        raise LlmConfigError(
            f"{', '.join(found)} set in the environment: it can redirect the {provider} client "
            f"(and its key) to another host. Unset it (`unset {' '.join(found)}`); a custom "
            "host is chosen only in Settings with allow_custom_host"
        )


AZURE_API_VERSION_ENV = "OPENAI_API_VERSION"
AZURE_HOST_SUFFIX = ".openai.azure.com"


def _azure_host(url: str) -> str:
    """AZURE_OPENAI_API_KEY goes ONLY to https://<resource>.openai.azure.com."""
    try:
        checked_url(url, https_only=True)
    except ValueError as exc:
        raise LlmConfigError(f"azure_openai base_url refused, no key attached: {exc}") from exc
    parts = urlsplit(url)
    host = parts.hostname or ""
    resource = host.removesuffix(AZURE_HOST_SUFFIX)
    if (
        not host.endswith(AZURE_HOST_SUFFIX)
        or not resource
        or "." in resource
        or parts.port is not None
    ):
        raise LlmConfigError(
            f"azure_openai base_url {url!r}: the key goes only to https://<resource>"
            f"{AZURE_HOST_SUFFIX} (no port); refusing"
        )
    return url


def _custom_host(llm: LlmConfig) -> str:
    """Checks for a user-opted-in custom host; returns the URL (https only)."""
    if llm.base_url is None:
        raise LlmConfigError(f"{llm.provider}: custom host needs llm.base_url (no default)")
    url = llm.base_url
    try:
        checked_url(url, https_only=True)
    except ValueError as exc:
        raise LlmConfigError(f"{llm.provider} custom host refused, no key attached: {exc}") from exc
    if not llm.allow_custom_host:
        official = OFFICIAL_HOSTS.get(llm.provider, "none (credential-chain provider)")
        raise LlmConfigError(
            f"{llm.provider} base_url host {urlsplit(url).hostname!r} is not its official API "
            f"host ({official}): set allow_custom_host=true in Settings to use it "
            f"(it then gets secret {LLM_CUSTOM} only, never the provider's own key)"
        )
    return url


def chat_model(llm: LlmConfig, env: Mapping[str, str] | None = None) -> BaseChatModel:
    """The stock chat model for ``llm`` via init_chat_model. Missing package,
    endpoint or key, or an SDK base-URL env override = raises, naming what to set.

    * hosted provider, official host: base_url = OFFICIAL_URLS[provider] (always
      explicit, never None) + secret ``llm_<provider>``;
    * hosted provider, other host: allow_custom_host + https, and the key is
      secret ``llm_custom`` only;
    * Vertex / Bedrock: UNSUPPORTED (their SDK credential chains read env/files);
    * azure_openai: secret ``llm_azure_openai`` only to an https
      ``<resource>.openai.azure.com`` host;
    * vllm / ollama: the user's explicit endpoint (vllm: secret ``llm_vllm``,
      required, no default).

    ``env`` is consulted ONLY to refuse proxy/CA/base-URL overrides, never for a key."""
    env = os.environ if env is None else env
    info = PROVIDERS[llm.provider]
    if not llm.model.strip():
        raise LlmConfigError('llm.model is empty: set the model id ("llm.model")')
    if llm.provider in _CHAIN:
        raise LlmConfigError(
            f"{llm.provider} is UNSUPPORTED: its SDK reads credentials from the environment / "
            "credential files (ADC, AWS chain), and Struck takes secrets only from its "
            "encrypted secret store. Pick another provider."
        )
    if importlib.util.find_spec(info.module) is None:
        raise LlmConfigError(
            f"provider {llm.provider} needs the stock package {info.module}: "
            f"install the extra with `uv sync --extra {info.extra}` (or pip install 'struck[{info.extra}]')"
        )
    _refuse_env_overrides(llm.provider, env)
    if info.endpoint_required and not llm.base_url:
        raise LlmConfigError(
            f"llm.base_url is not set: {llm.provider} needs YOUR endpoint; Struck has no default"
        )
    if llm.provider == "vllm":
        vllm_key = _key(llm_entry("vllm"))
        return init_chat_model(
            llm.model,
            model_provider="openai",
            base_url=llm.base_url,
            api_key=vllm_key,
            **direct_client_kwargs("vllm"),
        )
    if llm.provider == "ollama":
        return init_chat_model(
            llm.model,
            model_provider="ollama",
            base_url=llm.base_url,
            # Explicit operator-selected 90K context. Omitting this lets Ollama
            # choose 4096 on a 12GB GPU, which cannot hold the planner request.
            num_ctx=90000,
            num_predict=4096,
            reasoning=False,
            **direct_client_kwargs("ollama"),
        )
    if llm.provider == "azure_openai":
        if llm.base_url is None:
            raise LlmConfigError(
                "azure_openai needs llm.base_url = https://<resource>.openai.azure.com (no default)"
            )
        url = _azure_host(llm.base_url)
        key = _key(llm_entry("azure_openai"))
        version = env.get(AZURE_API_VERSION_ENV) or None
        if version is None:
            raise LlmConfigError(
                f"{AZURE_API_VERSION_ENV} is not set: export the Azure OpenAI api-version your "
                "deployment serves (stock AzureChatOpenAI variable; Struck sets no default)"
            )
        # AzureChatOpenAI takes the resource URL as azure_endpoint (base_url would skip
        # the /openai/deployments/... routing); the key goes only to that host.
        return init_chat_model(
            llm.model,
            model_provider="azure_openai",
            azure_endpoint=url,
            api_key=key,
            api_version=version,
            **direct_client_kwargs("azure_openai"),
        )
    if llm.provider == "perplexity":
        raise LlmConfigError(
            "perplexity: the stock langchain_perplexity client takes no base_url, so Struck "
            "cannot pin its key to the official host; pick another provider"
        )
    official = OFFICIAL_URLS.get(llm.provider)
    if llm.base_url is not None:
        try:
            checked_url(llm.base_url)
        except ValueError as exc:
            raise LlmConfigError(f"llm.base_url refused, no key attached: {exc}") from exc
    host = urlsplit(llm.base_url).hostname if llm.base_url else None
    custom = host is not None and (official is None or host != OFFICIAL_HOSTS[llm.provider])
    if custom:
        url = _custom_host(llm)
        entry = LLM_CUSTOM
    elif official is not None:
        url = official if llm.base_url is None else llm.base_url
        if not info.keyed:
            raise LlmConfigError(f"{llm.provider}: no secret entry is defined: refusing")
        entry = llm_entry(llm.provider)
    else:
        raise LlmConfigError(f"{llm.provider}: no official URL and no custom host: refusing")
    key = _key(entry)
    if llm.provider == "cohere":  # langchain-cohere takes cohere_api_key, not api_key
        return init_chat_model(llm.model, model_provider="cohere", base_url=url, cohere_api_key=key)
    return init_chat_model(
        llm.model,
        model_provider=info.init_provider,
        base_url=url,
        api_key=key,
        **direct_client_kwargs(llm.provider),
    )


def list_models(llm: LlmConfig, env: Mapping[str, str] | None = None) -> list[str]:
    """The provider's SERVED model ids, from the provider SDK's own stock
    model-list endpoint, routed with EXACTLY chat_model's host/key rules.
    The model id is a served choice in the Settings tab, never typed text:
    a host that cannot list its models says so loudly instead."""
    env = os.environ if env is None else env
    info = PROVIDERS[llm.provider]
    if llm.provider in _CHAIN:
        raise LlmConfigError(f"{llm.provider} is UNSUPPORTED (SDK credential chain)")
    if importlib.util.find_spec(info.module) is None:
        raise LlmConfigError(
            f"provider {llm.provider} needs the stock package {info.module}: "
            f"install the extra with `uv sync --extra {info.extra}`"
        )
    _refuse_env_overrides(llm.provider, env)
    if llm.provider == "perplexity":
        raise LlmConfigError(
            "perplexity: the stock langchain_perplexity client takes no base_url, "
            "so Struck cannot pin the listing request; pick another provider"
        )
    if llm.provider == "ollama":
        if not llm.base_url:
            raise LlmConfigError("ollama: llm.base_url is not set (no default)")
        import ollama

        try:
            served = ollama.Client(host=llm.base_url, trust_env=False).list()
            return sorted(m.model for m in served.models if m.model)
        except (ollama.ResponseError, httpx.HTTPError) as exc:
            raise LlmConfigError(f"ollama model list failed: {exc}") from exc
    import httpx2
    import openai

    def direct() -> httpx2.Client:
        """The SDK's own HTTP client type (openai 3.x depends on httpx2), no
        proxy/CA environment: the same direct transport chat_model uses."""
        return httpx2.Client(trust_env=False)

    try:
        if llm.provider == "vllm":
            if not llm.base_url:
                raise LlmConfigError("vllm: llm.base_url is not set (no default)")
            client = openai.OpenAI(
                base_url=llm.base_url,
                api_key=_key(llm_entry("vllm")),
                http_client=direct(),
            )
        elif llm.provider == "azure_openai":
            if llm.base_url is None:
                raise LlmConfigError(
                    "azure_openai needs llm.base_url = https://<resource>.openai.azure.com"
                )
            version = env.get(AZURE_API_VERSION_ENV) or None
            if version is None:
                raise LlmConfigError(f"{AZURE_API_VERSION_ENV} is not set (no default)")
            client = openai.AzureOpenAI(
                azure_endpoint=_azure_host(llm.base_url),
                api_key=_key(llm_entry("azure_openai")),
                api_version=version,
                http_client=direct(),
            )
        else:
            if llm.base_url is not None:
                try:
                    checked_url(llm.base_url)
                except ValueError as exc:
                    raise LlmConfigError(f"llm.base_url refused, no key attached: {exc}") from exc
            official = OFFICIAL_URLS.get(llm.provider)
            host = urlsplit(llm.base_url).hostname if llm.base_url else None
            custom = host is not None and (official is None or host != OFFICIAL_HOSTS[llm.provider])
            if custom:
                url, entry = _custom_host(llm), LLM_CUSTOM
            elif official is not None:
                url = official if llm.base_url is None else llm.base_url
                if not info.keyed:
                    raise LlmConfigError(f"{llm.provider}: no secret entry is defined: refusing")
                entry = llm_entry(llm.provider)
            else:
                raise LlmConfigError(
                    f"{llm.provider}: no official URL and no custom host: refusing"
                )
            client = openai.OpenAI(
                base_url=url, api_key=_key(entry), http_client=direct()
            )
        return sorted(m.id for m in client.models.list())
    except (openai.OpenAIError, httpx.HTTPError) as exc:
        raise LlmConfigError(f"{llm.provider} model list failed: {exc}") from exc
