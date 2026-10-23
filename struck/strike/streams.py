"""Strike WebSockets on STOCK Qt (``PySide6.QtWebSockets.QWebSocket``).

URLs come from the selected network profile (``struck.network``):
* Public: ``wss://<strike host>/ws/price``
  (skills/strike-price-feeds/SKILL.md :8-16), simple subscribe format (:25-36).
* User: ``wss://<strike host>/ws/user-api``
  (skills/strike-userstream/SKILL.md :8-14): ``session.logon`` (:22-34), then
  ``{"method":"subscribe","channel":"userstream"}`` (:41-46), then resync from
  REST (:57, :263-267).

Spec-prescribed behaviour only: split every text frame on ``\\n`` (:17 / :20);
keepalive is the server's ping frame, answered by QWebSocket itself (Qt
replies to pings automatically); reconnect with
``delay = min(30000, 1000 * 2**attempt)`` ms (price-feeds :130-134) via
``QTimer.singleShot``. Messages are emitted AS SERVED (parsed JSON objects);
nothing is merged or computed here. Each open/close goes to the call history
(no headers, no secrets).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QAbstractSocket, QNetworkProxy
from PySide6.QtWebSockets import QWebSocket, QWebSocketProtocol

from pydantic import TypeAdapter

from ..callhistory import CallRecord, _now, append_record, sha256_hex
from ..network import checked_strike_url

_FRAME: TypeAdapter[dict[str, object] | list[dict[str, object]]] = TypeAdapter(
    dict[str, object] | list[dict[str, object]]
)


def split_frame(frame: str) -> list[dict[str, object]]:
    """One frame may carry several JSON objects separated by newlines; array
    streams send a bare array (price-feeds :20-22)."""
    out: list[dict[str, object]] = []
    for line in frame.split("\n"):
        if not line.strip():
            continue
        value = _FRAME.validate_json(line)
        out.extend(value if isinstance(value, list) else [value])
    return out


def _record(source: str, url: str, method: str, detail: str) -> None:
    append_record(
        CallRecord(
            ts_utc=_now(),
            source=source,
            method=method,
            url=url,
            status=0,
            latency_ms=0,
            request_sha256=None,
            response_sha256=sha256_hex(detail.encode()),
            response_bytes=0,
            note=detail,
        )
    )


# Price channels that delivered on Strike TESTNET (strike-market-data / price-feeds).
# ``markPrice@1s`` is the fast per-symbol mark. The two broadcasts cover every symbol.
# ``depth``, ``trade`` and ``kline_{interval}`` are acknowledged; the caller adds the
# kline interval. Subscribe symbols are lowercase; events arrive uppercase.
PRICE_SYMBOL_CHANNELS: tuple[str, ...] = ("depth", "trade", "markPrice@1s")
PRICE_BROADCAST_CHANNELS: tuple[str, ...] = ("!markprice@arr", "!miniticker@arr")
_USER_AUTH_CLOSE = frozenset({4001, 4003, 4401, 4403})
_POLICY = QWebSocketProtocol.CloseCode.CloseCodePolicyViolated


def _private_auth_close(code: QWebSocketProtocol.CloseCode) -> bool:
    """4001/4003/4401/4403 are Strike's codes; Qt's enum only names 1008."""
    number: object = code.value
    return isinstance(number, int) and not isinstance(number, bool) and number in _USER_AUTH_CLOSE


def ws_request(
    method: str,
    channel: str,
    request_id: int,
    symbol: str | None = None,
    *,
    account_id: str | None = None,
    sub_account_id: str | None = None,
    vault_id: str | None = None,
) -> dict[str, object]:
    """One subscribe or unsubscribe body, in the shape the price and user-stream skills send."""
    if method not in ("subscribe", "unsubscribe"):
        raise ValueError(f"websocket method {method!r} is not subscribe or unsubscribe")
    if not channel:
        raise ValueError("websocket channel is empty")
    body: dict[str, object] = {"id": request_id, "method": method, "channel": channel}
    if channel == "rfq":
        if symbol or account_id or sub_account_id or vault_id:
            raise ValueError("rfq subscribe is exactly method, channel, and id")
        return body
    if channel == "userstream":
        if symbol:
            raise ValueError("userstream has no symbol")
        if vault_id and (account_id or sub_account_id):
            raise ValueError("vault watch sends vault_id alone")
        if account_id and sub_account_id:
            raise ValueError("userstream sends account_id or sub_account_id, not both")
        if vault_id:
            body["vault_id"] = vault_id
        if account_id:
            body["account_id"] = account_id
        if sub_account_id:
            body["sub_account_id"] = sub_account_id
        return body
    if account_id or sub_account_id or vault_id:
        raise ValueError(f"{channel} does not take an account selector")
    if channel in PRICE_BROADCAST_CHANNELS:
        return body
    if symbol is None or not symbol:
        raise ValueError(f"{channel} requires a symbol")
    body["symbol"] = symbol.lower()
    return body


class StrikeStream(QObject):
    """One stock ``QWebSocket``. ``on_open`` returns the JSON requests to send
    after (re)connecting (logon / subscribes)."""

    message = Signal(object)  # dict[str, object], as served
    state = Signal(str)

    def __init__(
        self,
        url: str,
        source: str,
        on_open: Callable[[], list[dict[str, object]]],
        *,
        user_stream: bool = False,
    ) -> None:
        super().__init__()
        # Exact-host allowlist (struck.network): only the two Strike deployments' wss URLs.
        self._url, self._source, self._on_open = checked_strike_url(url), source, on_open
        self._user_stream = user_stream
        self._attempt = 0
        self._stopped = False
        self.counts: Counter[str] = Counter()  # served messages per event type `e` (acks as "ack")
        self.ws = QWebSocket()
        # Never an application/system proxy: straight to the allowlisted Strike host.
        self.ws.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.ws.connected.connect(self._connected)
        self.ws.disconnected.connect(self._disconnected)
        self.ws.textMessageReceived.connect(self.feed)
        self.ws.errorOccurred.connect(self._error)

    def _error(self, _code: QAbstractSocket.SocketError) -> None:
        self.state.emit(f"FAILED: {self.ws.errorString()}")

    def open(self) -> None:
        self._stopped = False
        self.state.emit("CONNECTING")
        self.ws.open(QUrl(self._url))

    def close(self) -> None:
        self._stopped = True
        self.ws.close()

    def send(self, request: dict[str, object]) -> None:
        self.ws.sendTextMessage(json.dumps(request, separators=(",", ":")))

    def feed(self, frame: str) -> None:
        for msg in split_frame(frame):
            self.counts[str(msg.get("e", "ack" if "result" in msg else "other"))] += 1
            self.message.emit(msg)

    def log_counts(self) -> str:
        detail = ",".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        _record(self._source, self._url, "WS-COUNTS", detail)
        return detail

    def _connected(self) -> None:
        self._attempt = 0
        _record(self._source, self._url, "WS-OPEN", "connected")
        for request in self._on_open():
            self.send(request)

    def _disconnected(self) -> None:
        reason = self.ws.closeReason()
        code = self.ws.closeCode()
        _record(self._source, self._url, "WS-CLOSE", reason)
        if self._stopped:
            return
        # strike-userstream: those close codes are auth failures. Do not reconnect.
        if self._user_stream and (code == _POLICY or _private_auth_close(code)):
            self._stopped = True
            self.state.emit(f"AUTH CLOSED {code}: {reason or 'not reconnecting'}")
            return
        if self._user_stream:
            if self._attempt >= 10:
                self._stopped = True
                self.state.emit(f"FAILED: user stream stopped after 10 attempts ({reason})")
                return
            delay = 5000
        else:
            delay = min(30000, 1000 << min(self._attempt, 15))  # price-feeds backoff
        self._attempt += 1
        self.state.emit(f"RECONNECTING in {delay} ms ({reason or self.ws.errorString()})")
        QTimer.singleShot(delay, self.open)
