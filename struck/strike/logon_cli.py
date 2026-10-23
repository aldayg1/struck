"""READ-ONLY proof of the authenticated user stream on the selected network:
read Strike's served clock (GET /v2/time), ``session.logon`` with a timestamp
on THAT clock, subscribe ``userstream``, print status + field NAMES only
(never values), close. No order, no send, no deposit: nothing is written.

  uv run python -m struck.strike.logon_cli [--network <name>] [--seconds N]

Exit 0 = logon 200 and the subscribe was acknowledged; 1 = failed (printed);
2 = no API wallet in Struck's encrypted store."""

from __future__ import annotations

import sys
import time

from PySide6.QtCore import QCoreApplication, QTimer

from ..callhistory import RecordedHttp
from ..config import load_config, network_banner, pop_network_flag, network_help, wants_help
from .account import server_offset_ms
from .auth import StrikeCredentialsError, logon_params
from .venue_auth import wallet_from_secrets
from .market import StrikeMarketClient
from .streams import StrikeStream


def main(argv: list[str]) -> int:
    usage = (
        "usage: python -m struck.strike.logon_cli [--network <name>] [--seconds N]\n"
        + network_help()
    )
    if wants_help(argv):
        print(usage, file=sys.stderr)
        return 0
    args = pop_network_flag(argv)
    if not (args == [] or (len(args) == 2 and args[0] == "--seconds" and args[1].isdigit())):
        print(f"unknown argument(s) {args}\n{usage}", file=sys.stderr)
        return 2
    seconds = int(args[1]) if args else 20
    try:
        wallet = wallet_from_secrets(unlock=True)
    except StrikeCredentialsError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2
    cfg = load_config()
    net = cfg.profile
    print(network_banner(cfg))
    http = RecordedHttp(cfg.timeout_s)
    try:
        offset_ms = server_offset_ms(StrikeMarketClient(http, cfg.strike_api_url))
    finally:
        http.close()
    print(f"GET /v2/time served; clock offset applied to the logon timestamp: {offset_ms} ms")
    app = QCoreApplication.instance() or QCoreApplication([])
    result: list[int] = [1]

    def logon() -> list[dict[str, object]]:
        now_ms = time.time_ns() // 1_000_000 + offset_ms
        return [{"id": 1, "method": "session.logon", "params": logon_params(wallet, now_ms)}]

    stream = StrikeStream(
        net.strike_user_ws,
        f"strike-{net.strike_venue}-userstream",
        logon,
        user_stream=True,
    )

    def done(code: int) -> None:
        result[0] = code
        stream.close()
        print(f"served message counts by type: {stream.log_counts()}")
        app.quit()

    def on_message(msg: dict[str, object]) -> None:
        print(
            f"<- id={msg.get('id')} status={msg.get('status')} e={msg.get('e')} "
            f"fields={sorted(msg)}"
        )
        if msg.get("id") == 1:
            if msg.get("status") != 200:
                print("FAILED: session.logon not accepted")
                done(1)
                return
            print("session.logon: HTTP-style status 200 (authenticated)")
            stream.send({"method": "subscribe", "channel": "userstream", "id": 2})
        elif msg.get("id") == 2:
            ok = "result" in msg
            print("subscribe userstream: acknowledged" if ok else "FAILED: subscribe not acked")
            done(0 if ok else 1)
        elif msg.get("e") == "error":
            print("FAILED: the stream served an error event")
            done(1)

    def on_state(state: str) -> None:
        print(f"stream state: {state}")

    def timed_out() -> None:
        print(f"FAILED: no logon answer in {seconds} s")
        done(1)

    stream.message.connect(on_message)
    stream.state.connect(on_state)
    QTimer.singleShot(seconds * 1000, timed_out)
    stream.open()
    app.exec()
    return result[0]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
