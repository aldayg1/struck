"""E00 ACCESS proof driver: ONE process, human unlock, signed reads + user WS.

  uv run python -m struck.access_cli [--network <name>] [--seconds N] [--resync]

Sequence, all inside the process that received the master password (the unlock
is never inherited by another process, so everything runs here):

1. ``struck.secrets.unlock_from_terminal``: masked master-password prompt at a
   REAL terminal (run this CLI from the operator's own terminal window).
2. Signed GETs on the selected network's host; each raw served body is saved:
   /v2/account, /v2/positions, /v2/balances, /v2/portfolio; the signed history
   reads /v2/history/order, /v2/history/fill, /v2/history/funding,
   /v2/history/transaction, /v2/history/position; strategy GET /v2/algo/twap
   and GET /v2/openOrders (raw bytes; the exact parse families live in the
   production ``struck.strike`` modules). GET /v2/time is captured too and its
   offset drives the logon timestamp.
3. The user WebSocket (``net.strike_user_ws``): served-clock ``session.logon``,
   ``subscribe`` on ``userstream``, then ``--seconds`` of served frames stored
   AS SERVED (whole-file sha256 in ``ws.manifest.json``).
4. ``--resync`` (AUTH02): closes the phase-1 socket and opens a SECOND full
   user-WS session in the same process (logon+subscribe), re-serves the signed
   account/positions/balances reads, and writes ``resync.manifest.json``
   correlating both sessions and proving no stale frame replay (min event E).

No writes, no orders, no approvals anywhere. Headers are never recorded; no
secret value is printed. Exit 0 = signed reads ok + logon 200 + subscribe ack
(resync also clean when requested); 1 = loud failure (receipts kept and named);
2 = usage/config refusal. No network selected = the standard fail-closed
refusal from ``struck.config``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr
from PySide6.QtCore import QCoreApplication, QTimer

from .callhistory import CallFailed, RecordedHttp, sha256_hex
from .config import (
    RECEIPTS_DIR,
    load_config,
    network_banner,
    network_help,
    pop_network_flag,
    wants_help,
)
from .strike.account import StrikeAuthedClient
from .strike.auth import StrikeCredentialsError, logon_params
from .strike.market import StrikeMarketClient
from .strike.streams import StrikeStream
from .strike.venue_auth import wallet_from_secrets

_SIGNED_READS: tuple[tuple[str, str], ...] = (
    ("account", "/v2/account"),
    ("positions", "/v2/positions"),
    ("balances", "/v2/balances"),
    ("portfolio", "/v2/portfolio"),
    ("history-order", "/v2/history/order?limit=20"),
    ("history-fill", "/v2/history/fill?limit=20"),
    ("history-funding", "/v2/history/funding?limit=20"),
    ("history-transaction", "/v2/history/transaction?limit=20"),
    ("history-position", "/v2/history/position?limit=20"),
    ("strategy-twap", "/v2/algo/twap"),
    ("openOrders", "/v2/openOrders"),
)


class _UsageError(ValueError):
    """Unknown operation or params: usage is printed and the CLI exits 2."""


class WsManifest(BaseModel):
    """Provenance of the user-stream capture. No secret-bearing content."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    network: StrictStr
    url: StrictStr
    logon_acknowledged: StrictInt  # 0/1
    subscribe_acked: StrictInt  # 0/1
    seconds: StrictInt
    frames_file: StrictStr
    frames_bytes: StrictInt
    frames_sha256: StrictStr
    counts: dict[str, int]


class ResyncManifest(BaseModel):
    """AUTH02 reconnect/resync evidence: TWO user-WS sessions in ONE process.
    Phase 2 reconnects from scratch (new socket, logon, subscribe), then the
    signed account reads are re-served and correlated against phase 1."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    network: StrictStr
    url: StrictStr
    phase1_frames_sha256: StrictStr
    phase2_frames_sha256: StrictStr
    phase1_counts: dict[str, int]
    phase2_counts: dict[str, int]
    phase1_min_event_ms: StrictInt | None  # min data-frame E served (None: none served)
    phase2_min_event_ms: StrictInt | None
    unparsed_phase2_lines: StrictInt  # served lines that carried no parseable JSON object
    stale_free: bool  # every phase-2 data E >= phase-1 min E: no stale replay
    resync_reads: dict[str, str]  # route name -> sha256 of the phase-2 served body
    resync_matches_phase1: dict[str, bool]


def _usage() -> str:
    return (
        "usage: python -m struck.access_cli [--network <name>] [--seconds N] [--resync]\n"
        "ONE process: masked unlock at this terminal, signed account/balances/"
        "positions/portfolio reads, signed history reads, then the user-WS "
        "logon+subscribe. --resync adds the AUTH02 reconnect/resync phase: a "
        "second full WS session in this process plus correlated REST re-reads. "
        "READ-ONLY.\n" + network_help()
    )


def _parse_args(argv: list[str]) -> tuple[int, bool]:
    value = 30
    resync = False
    it = iter(argv)
    for arg in it:
        if arg == "--seconds":
            raw = next(it, None)
            if raw is None or not raw.isdigit():
                raise _UsageError("--seconds needs a whole number")
            value = int(raw)
        elif arg.startswith("--seconds="):
            raw = arg.split("=", 1)[1]
            if not raw.isdigit():
                raise _UsageError("--seconds needs a whole number")
            value = int(raw)
        elif arg == "--resync":
            resync = True
        else:
            raise _UsageError(f"unknown argument {arg!r}")
    if not 1 <= value <= 600:
        raise _UsageError(f"--seconds must be 1..600, got {value}")
    return value, resync


class _AccessStream(StrikeStream):
    """StrikeStream, plus verbatim frame capture BEFORE standard splitting."""

    def __init__(
        self,
        url: str,
        source: str,
        on_open: Callable[[], list[dict[str, object]]],
        frames_path: Path,
    ) -> None:
        super().__init__(url, source, on_open, user_stream=True)
        self._frames_path = frames_path

    def feed(self, frame: str) -> None:
        if frame.strip():
            with self._frames_path.open("a", encoding="utf-8") as fh:
                fh.write(frame + "\n")
        super().feed(frame)


def _write_ws_manifest(
    out: Path,
    cfg_network: str,
    url: str,
    logon_ok: bool,
    sub_ok: bool,
    seconds: int,
    counts: "dict[str, int]",
    frames_name: str = "userstream.frames.txt",
) -> Path:
    frames_path = out / frames_name
    frames = frames_path.read_bytes() if frames_path.exists() else b""
    digest = sha256_hex(frames) if frames else ""
    manifest = WsManifest(
        network=cfg_network,
        url=url,
        logon_acknowledged=1 if logon_ok else 0,
        subscribe_acked=1 if sub_ok else 0,
        seconds=seconds,
        frames_file=frames_name,
        frames_bytes=len(frames),
        frames_sha256=digest,
        counts=dict(counts),
    )
    path = out / "ws.manifest.json"
    path.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def _min_event_ms(frames_path: Path) -> tuple[int | None, int]:
    """(min data-frame E served, unparsed line count) from a verbatim capture."""
    values: list[int] = []
    unparsed = 0
    if frames_path.exists():
        for line in frames_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                loaded: object = json.loads(stripped)
            except json.JSONDecodeError:
                unparsed += 1
                continue
            if isinstance(loaded, dict):
                raw = loaded.get("E")
                if isinstance(raw, bool):
                    continue
                if isinstance(raw, int):
                    values.append(raw)
                elif isinstance(raw, float) and raw.is_integer():
                    values.append(int(raw))
    return (min(values) if values else None), unparsed


def main(argv: list[str]) -> int:
    if wants_help(argv):
        print(_usage())
        return 0
    args = pop_network_flag(argv)  # --network + the loud fail-closed config check
    try:
        seconds, resync = _parse_args(args)
    except _UsageError as exc:
        print(f"{exc}\n{_usage()}", file=sys.stderr)
        return 2
    try:
        wallet = wallet_from_secrets(unlock=True)  # masked prompt at THIS terminal
    except StrikeCredentialsError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    cfg = load_config()
    out = RECEIPTS_DIR / "e00" / f"access-{cfg.network}-{time.strftime('%Y%m%dT%H%M%S')}"
    out.mkdir(parents=True, exist_ok=True)
    http = RecordedHttp(cfg.timeout_s)
    try:
        print(network_banner(cfg))
        print(f"Strike REST: {cfg.strike_api_url}")
        print(f"receipts: {out}")
        signed = StrikeAuthedClient(http, cfg.strike_api_url, wallet)
        for name, path in _SIGNED_READS:
            served = signed.request("GET", path)
            file_path = out / f"{name}.json"
            file_path.write_bytes(served.content)
            print(
                f"GET {path} -> HTTP {served.status}, {len(served.content)} bytes, "
                f"sha256 {served.sha256} -> {file_path}"
            )
        market = StrikeMarketClient(http, cfg.strike_api_url)
        time_read = market.server_time()
        offset_ms = time_read.value.serverTime - time.time_ns() // 1_000_000
        (out / "server-time.json").write_bytes(time_read.served.content)
        print(f"GET /v2/time served; clock offset {offset_ms} ms (server-time.json captured)")
    except CallFailed as exc:
        (out / "FAILED.txt").write_text(f"signed read failed: {exc}\n", encoding="utf-8")
        print(f"FAILED (signed reads): {exc}", file=sys.stderr)
        print(f"receipts so far: {out}")
        return 1
    finally:
        http.close()

    app = QCoreApplication.instance() or QCoreApplication([])
    phase = {"n": 1, "code": 1}
    logon_ok = {"ok": False}
    sub_ok = {"ok": False}
    frames_path = out / "userstream.frames.txt"
    resync_frames = out / "userstream.resync.frames.txt"
    resync_window = min(seconds, 15)
    resync_reads: dict[str, str] = {}
    resync_matches: dict[str, bool] = {}
    phase1_digest = {"sha256": ""}

    def on_open() -> list[dict[str, object]]:
        now_ms = time.time_ns() // 1_000_000 + offset_ms
        return [{"id": 1, "method": "session.logon", "params": logon_params(wallet, now_ms)}]

    stream = _AccessStream(
        cfg.profile.strike_user_ws,
        f"e00-access-{cfg.network}-userstream",
        on_open,
        frames_path,
    )

    def final(code: int) -> None:
        phase["code"] = code
        manifest = _write_ws_manifest(
            out,
            cfg.network,
            cfg.profile.strike_user_ws,
            logon_ok["ok"],
            sub_ok["ok"],
            seconds if phase["n"] == 1 else resync_window,
            dict(stream.counts),
            frames_name="userstream.frames.txt" if phase["n"] == 1 else "userstream.resync.frames.txt",
        )
        stream.close()
        print(f"served message counts by type: {stream.log_counts()}")
        print(f"ws manifest: {manifest}")
        if code == 0:
            print(f"E00 receipts complete: {out}")
        else:
            print(f"E00 FAILED: receipts kept at {out}", file=sys.stderr)
        app.quit()

    def resync_reads_now() -> None:
        """AUTH02 resync: re-serve the signed account reads AFTER the reconnect."""
        http2 = RecordedHttp(cfg.timeout_s)
        try:
            signed2 = StrikeAuthedClient(http2, cfg.strike_api_url, wallet)
            for name, path in _SIGNED_READS:
                if name not in ("account", "positions", "balances"):
                    continue
                served2 = signed2.request("GET", path)
                target = out / f"{name}.resync.json"
                target.write_bytes(served2.content)
                resync_reads[name] = served2.sha256
                phase1_file = out / f"{name}.json"
                resync_matches[name] = (
                    phase1_digest["sha256"] == ""
                    or served2.sha256 == sha256_hex(phase1_file.read_bytes())
                )
                print(
                    f"resync GET {path} -> HTTP {served2.status}, "
                    f"sha256 {served2.sha256}, matches phase 1: {resync_matches[name]}"
                )
        except CallFailed as exc:
            (out / "FAILED.resync.txt").write_text(f"resync read failed: {exc}\n", encoding="utf-8")
            print(f"FAILED (resync reads): {exc}", file=sys.stderr)
        finally:
            http2.close()

    def start_resync() -> None:
        """Close phase 1 and open a brand-new WS session in this same process."""
        phase1_digest["sha256"] = sha256_hex(frames_path.read_bytes())
        p1_min, _ = _min_event_ms(frames_path)
        stream.close()
        print("resync: phase-1 socket closed; opening a fresh user-WS session")
        phase["n"] = 2
        logon_ok["ok"] = False
        sub_ok["ok"] = False

        stream2 = _AccessStream(
            cfg.profile.strike_user_ws,
            f"e00-access-{cfg.network}-userstream-resync",
            on_open,
            resync_frames,
        )

        def abort_resync() -> None:
            phase["code"] = 1
            stream2.close()
            print("E00 FAILED: resync phase aborted; receipts kept", file=sys.stderr)
            app.quit()

        def on_message2(msg: "dict[str, object]") -> None:
            if msg.get("id") == 1 and not logon_ok["ok"]:
                if msg.get("status") != 200:
                    print("FAILED: resync session.logon not accepted")
                    abort_resync()
                    return
                print("resync session.logon: status 200; subscribing")
                logon_ok["ok"] = True
                stream2.send({"method": "subscribe", "channel": "userstream", "id": 2})
            elif msg.get("id") == 2 and not sub_ok["ok"]:
                if "result" not in msg:
                    print("FAILED: resync subscribe not acknowledged")
                    abort_resync()
                    return
                print("resync subscribe: acknowledged; re-serving signed reads")
                sub_ok["ok"] = True
                resync_reads_now()
                QTimer.singleShot(resync_window * 1000, finish_resync)
            elif msg.get("e") == "error":
                print(f"FAILED: resync stream served an error event: fields={sorted(msg)}")
                abort_resync()

        def finish_resync() -> None:
            p2_min, unparsed = _min_event_ms(resync_frames)
            stale_free = bool(
                p1_min is None or p2_min is None or p2_min >= p1_min
            )
            manifest = ResyncManifest(
                network=cfg.network,
                url=cfg.profile.strike_user_ws,
                phase1_frames_sha256=phase1_digest["sha256"],
                phase2_frames_sha256=sha256_hex(resync_frames.read_bytes()),
                phase1_counts=dict(stream.counts),
                phase2_counts=dict(stream2.counts),
                phase1_min_event_ms=p1_min,
                phase2_min_event_ms=p2_min,
                unparsed_phase2_lines=unparsed,
                stale_free=stale_free,
                resync_reads=resync_reads,
                resync_matches_phase1=resync_matches,
            )
            (out / "resync.manifest.json").write_text(
                manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            stream2.close()
            print(f"resync manifest: stale_free={stale_free} -> {out / 'resync.manifest.json'}")
            if resync_reads and stale_free:
                print(f"E00 receipts complete (with resync): {out}")
                app.quit()
            else:
                print("E00 FAILED: resync phase incomplete", file=sys.stderr)
                app.quit()

        def on_state2(state: str) -> None:
            print(f"resync stream state: {state}")

        stream2.message.connect(on_message2)
        stream2.state.connect(on_state2)
        QTimer.singleShot((resync_window + 15) * 1000, finish_resync)
        stream2.open()

    def on_message(msg: "dict[str, object]") -> None:
        if msg.get("id") == 1:
            if logon_ok["ok"]:
                return  # duplicate logon response: already handled
            if msg.get("status") != 200:
                print("FAILED: session.logon not accepted")
                final(1)
                return
            print("session.logon: status 200 (authenticated); subscribing")
            logon_ok["ok"] = True
            stream.send({"method": "subscribe", "channel": "userstream", "id": 2})
        elif msg.get("id") == 2:
            if "result" in msg and not sub_ok["ok"]:
                print("subscribe userstream: acknowledged")
                sub_ok["ok"] = True
                if logon_ok["ok"]:
                    if resync and phase["n"] == 1:
                        QTimer.singleShot(seconds * 1000, start_resync)
                    else:
                        QTimer.singleShot(seconds * 1000, lambda: final(0))
            elif sub_ok["ok"]:
                print("duplicate subscribe ack ignored")
            else:
                print("FAILED: subscribe not acknowledged")
                final(1)
        elif msg.get("e") == "error":
            print(f"FAILED: stream served an error event: fields={sorted(msg)}")
            final(1)

    def on_state(state: str) -> None:
        print(f"stream state: {state}")

    def on_hard_timeout() -> None:
        if not sub_ok["ok"]:
            print(f"FAILED: no completed logon+subscribe within {seconds + 15} s")
            final(1)
            return
        if phase["code"] != 0 and phase["n"] == 1 and not resync:
            print(f"FAILED: capture window ended early; state={stream.log_counts()}")
            final(1)

    stream.message.connect(on_message)
    stream.state.connect(on_state)
    QTimer.singleShot((seconds + 15) * 1000, on_hard_timeout)
    stream.open()
    app.exec()
    return phase["code"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
