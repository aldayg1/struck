"""M2: Struck's own network clients ignore proxy/CA environment variables.
httpx (RecordedHttp) is built with trust_env=False; the Strike QWebSockets use
an explicit NoProxy. Nothing here reaches the network."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ssl
from pathlib import Path

import httpx
import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QNetworkProxy

from struck.callhistory import RecordedHttp
from struck.network import PROFILES
from struck.strike.streams import StrikeStream

APP = QCoreApplication.instance() or QCoreApplication([])
POISON = {
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "ALL_PROXY": "socks5://127.0.0.1:9",
    "https_proxy": "http://127.0.0.1:9",
    "http_proxy": "http://127.0.0.1:9",
    "all_proxy": "socks5://127.0.0.1:9",
}


def test_recorded_http_ignores_proxy_and_ca_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bogus_ca = tmp_path / "evil-ca.pem"
    bogus_ca.write_text("not a cert", encoding="utf-8")
    for k, v in POISON.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("SSL_CERT_FILE", str(bogus_ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    client = RecordedHttp(5, tmp_path / "calls.jsonl")._client  # builds despite the bogus CA
    assert client.trust_env is False
    assert client._mounts == {}  # no proxy transport mounted for any scheme
    transport = client._transport
    assert isinstance(transport, httpx.HTTPTransport)
    ctx = transport._pool._ssl_context  # default verify: system CA bundle, hostname checked
    assert ctx is not None and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    client.close()
    # The same env DOES change a default client, so the assertions above mean something:
    with pytest.raises(ssl.SSLError):
        httpx.Client()  # SSL_CERT_FILE honoured -> bogus CA refused
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.delenv("SSL_CERT_DIR")
    monkeypatch.delenv("ALL_PROXY")  # a default client would need socksio for these
    monkeypatch.delenv("all_proxy")
    trusting = httpx.Client()
    assert trusting._mounts != {}  # proxies honoured
    trusting.close()


def test_strike_websockets_use_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    QNetworkProxy.setApplicationProxy(
        QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, "127.0.0.1", 9)
    )
    try:
        for p in PROFILES.values():
            for url in (p.strike_price_ws, p.strike_user_ws):
                s = StrikeStream(url, "test", list)
                assert s.ws.proxy().type() == QNetworkProxy.ProxyType.NoProxy
    finally:
        QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.DefaultProxy))
