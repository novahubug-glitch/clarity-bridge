"""
Clarity Bridge
===============
Lightweight connector between Clarity Cloud and MetaTrader 5.

This application contains NO trading logic, NO market analysis,
NO decision making, and NO proprietary intelligence.

It does exactly three things:
  1. Reads data from MT5 terminal
  2. Streams that data securely to Clarity Cloud
  3. Receives execution requests from Clarity Cloud and sends them to MT5

That is all.

All intelligence lives in Clarity Cloud.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import time
import sys
import urllib.request
from datetime import datetime, timezone

from mt5_connector import MT5Connector
from cloud_connection import CloudConnection
from version import VERSION
from device_auth import _config_dir
from safety import RequestLedger, load_json, save_json_atomic, sha256_file, update_decision


RECONNECT_DELAY = 5   # seconds between reconnect attempts


def fetch_manifest(cloud_http_url: str) -> dict:
    """Asks Cloud for the current version manifest (never GitHub directly). Fails open: {} on error."""
    try:
        req = urllib.request.Request(f"{cloud_http_url.rstrip('/')}/bridge/version")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read(65536).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"Update check failed (continuing on current version): {e}")
        return {}


MAX_INSTALLER_BYTES = 300 * 1024 * 1024


def download_and_install_update(download_url: str, expected_sha256: str) -> bool:
    """
    Downloads the installer to a private temp directory, VERIFIES its SHA-256 against the
    manifest, and only then runs it. A mismatch deletes the file and nothing is executed.
    Only reached when auto-update is explicitly enabled (CLARITY_BRIDGE_AUTO_UPDATE=1).
    Returns True if the installer was launched (caller must exit), False otherwise.
    """
    workdir = None
    launched = False
    try:
        workdir = tempfile.mkdtemp(prefix="clarity-bridge-update-")      # unpredictable path
        installer_path = os.path.join(workdir, "ClarityBridge-Setup.exe")
        print(f"Downloading update from {download_url}...")
        with urllib.request.urlopen(download_url, timeout=60) as resp, open(installer_path, "wb") as out:
            total = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_INSTALLER_BYTES:
                    raise ValueError("installer larger than expected")
                out.write(chunk)

        if sha256_file(installer_path) != expected_sha256:
            print("Update REFUSED: the downloaded file does not match the published checksum. Nothing was run.")
            return False

        print("Checksum verified. Installing update, Bridge will restart automatically...")
        creationflags = subprocess.DETACHED_PROCESS if sys.platform == "win32" else 0
        subprocess.Popen([installer_path, "/VERYSILENT", "/NORESTART"], close_fds=True, creationflags=creationflags)
        launched = True
        return True
    except Exception as e:
        print(f"Update failed, continuing on current version: {e}")
        return False
    finally:
        if workdir and not launched:        # the installer needs its file; otherwise clean up
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)


class ClarityBridge:

    def __init__(self, device_id: str, device_secret: str):
        self.mt5         = MT5Connector()
        self.cloud       = CloudConnection(device_id, device_secret)
        self.running     = False
        self._state_path = os.path.join(_config_dir(), "state.json")
        st = load_json(self._state_path, {})
        # Persisted so a position that closes while Bridge is OFF is still detected and journaled.
        self._known_tickets = {int(t) for t in st.get("open_tickets", []) if str(t).lstrip("-").isdigit()}
        self._unresolved_closed = {}   # ticket -> retries: closed, but MT5 deal history not visible yet
        self.ledger = RequestLedger(os.path.join(_config_dir(), "executed_requests.json"))
        self._facts_cache = {}         # pair -> (timestamp, facts)

    async def start(self):
        print(f"Clarity Bridge v{VERSION} starting...")
        print("This application contains no trading intelligence.")
        print("All decisions are made by Clarity Cloud.\n")

        while True:
            try:
                await self._run()
            except Exception as e:
                print(f"Bridge error: {e}")
                print(f"Reconnecting in {RECONNECT_DELAY}s...")
                await asyncio.sleep(RECONNECT_DELAY)

    async def _run(self):
        # Step 1: Connect to MT5
        if not self.mt5.connect():
            print("MT5 not available. Is MetaTrader 5 running?")
            await asyncio.sleep(RECONNECT_DELAY)
            return

        print(f"MT5 connected: {self.mt5.account_info()['name']}")

        # Step 2: Connect to Clarity Cloud
        if not await self.cloud.connect():
            print("Cannot reach Clarity Cloud. Check your internet connection.")
            self.mt5.disconnect()
            await asyncio.sleep(RECONNECT_DELAY)
            return

        print("Clarity Cloud connected.")
        print("Bridge active. Streaming market data...\n")
        self.running = True
        await self._replay_recent_results()

        # Step 3: Run both loops concurrently
        await asyncio.gather(
            self._stream_loop(),    # MT5 → Cloud
            self._execute_loop(),   # Cloud → MT5
        )

    def _collect_market_data(self, pairs: list, timeframes: list) -> dict:
        """
        All synchronous, blocking MT5 calls happen here. Called via
        asyncio.to_thread() so the event loop stays free to handle
        WebSocket ping/pong and the execute loop while this runs.
        """
        payload = {
            "type":      "market_data",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "account":   self.mt5.account_info(),
            "prices":    {},
            "candles":   {},
            "positions": self.mt5.get_open_positions(),
        }

        # Detect closes — a ticket that was open last cycle but isn't now
        # closed via SL, TP, or manually in the terminal. This is the common
        # case; explicit close_position() commands are the exception.
        current_tickets = {p["ticket"] for p in payload["positions"]}
        candidates = (self._known_tickets - current_tickets) | (set(self._unresolved_closed) - current_tickets)

        closed_trades = []
        for ticket in candidates:
            info = self.mt5.get_closed_trade(ticket)
            if info:
                closed_trades.append(info)
                self._unresolved_closed.pop(ticket, None)
            else:
                # History not visible yet: retry on later cycles instead of forgetting this close.
                tries = self._unresolved_closed.get(ticket, 0) + 1
                if tries >= 20:                      # ~5 minutes; give up (e.g. ticket from another account)
                    print(f"Could not resolve closed trade {ticket}; giving up")
                    self._unresolved_closed.pop(ticket, None)
                else:
                    self._unresolved_closed[ticket] = tries
        self._known_tickets = current_tickets
        save_json_atomic(self._state_path, {"open_tickets": sorted(current_tickets)})
        payload["closed_trades"] = closed_trades

        # Broker symbol facts (digits, tick value, volume rules) so Cloud can size orders correctly.
        payload["symbols"] = {}
        for pair in pairs:
            f = self._symbol_facts(pair)
            if f:
                payload["symbols"][pair] = f

        for pair in pairs:
            price = self.mt5.get_price(pair)
            if price:
                payload["prices"][pair] = price

        for pair in pairs:
            payload["candles"][pair] = {}
            for tf in timeframes:
                candles = self.mt5.get_candles(pair, tf, count=500)
                if candles is not None:
                    payload["candles"][pair][tf] = candles

        return payload

    def _symbol_facts(self, pair: str):
        """MT5 symbol facts, cached 5 minutes (they rarely change)."""
        now = time.time()
        hit = self._facts_cache.get(pair)
        if hit and now - hit[0] < 300:
            return hit[1]
        f = self.mt5.symbol_facts(pair)
        self._facts_cache[pair] = (now, f)
        return f

    async def _replay_recent_results(self):
        """
        After a reconnect, re-send execution results from the last 10 minutes. If the original
        reply was lost in a network drop, Cloud would otherwise never learn the order filled.
        Cloud journaling is idempotent (keyed by ticket), so a duplicate is harmless.
        """
        for rid, rec in self.ledger.recent(600):
            if isinstance(rec, dict) and rec.get("kind") == "execution_result":
                await self.cloud.send({
                    "type": "execution_result", "request_id": rid, "result": rec["result"],
                    "replayed": True, "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    async def _stream_loop(self):
        """Continuously stream market data from MT5 to Cloud."""
        PAIRS = [
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
            "AUDUSD", "USDCAD", "NZDUSD", "GBPJPY", "EURJPY",
        ]
        TIMEFRAMES = ["M5", "M15", "H4", "D1"]

        while self.running:
            try:
                # Offload every blocking MT5 call to a worker thread —
                # keeps this coroutine non-blocking so the WebSocket
                # connection (ping/pong, execute loop) stays responsive
                # while MT5 is being queried.
                payload = await asyncio.to_thread(self._collect_market_data, PAIRS, TIMEFRAMES)
                sent = await self.cloud.send(payload)

                # send() returns False (not an exception) once the
                # underlying connection is lost — it was silently
                # swallowed here before, with no way for the outer
                # reconnect loop in start() to ever notice. A single
                # WebSocket drop (network blip, a Railway redeploy,
                # anything) would permanently wedge Bridge: this loop
                # kept "running" forever, quietly no-op'ing every 15s,
                # while _execute_loop's receive() had already ended
                # silently on the same dead connection — nothing ever
                # raised, so gather() never completed, so start()'s
                # retry logic never got a chance to run.
                if not sent:
                    raise ConnectionError("Cloud connection lost — triggering full reconnect")

            except ConnectionError:
                raise  # let start()'s outer retry loop handle this
            except Exception as e:
                print(f"Stream error: {e}")

            # Stream every 15 seconds (aligned to M15 candle close approximately)
            await asyncio.sleep(15)

    async def _execute_loop(self):
        """Listen for execution requests from Cloud and send to MT5."""
        async for message in self.cloud.receive():
            try:
                msg = json.loads(message)
                msg_type = msg.get("type")

                if msg_type == "execute_order":
                    rid = str(msg.get("request_id") or "")
                    replayed = False
                    if not rid:
                        # Without an id the order cannot be made idempotent, so it is not placed.
                        result = {"success": False, "error": "Rejected: execute_order without request_id"}
                    else:
                        prior = self.ledger.get(rid)
                        if prior is not None:
                            # Duplicate request: return the ORIGINAL outcome, never re-send to the broker.
                            result, replayed = prior["result"], True
                        else:
                            result = await asyncio.to_thread(
                                self.mt5.place_order,
                                pair      = msg["pair"],
                                direction = msg["direction"],
                                lot_size  = msg["lot_size"],
                                sl        = msg["sl"],
                                tp        = msg["tp"],
                                comment   = msg.get("comment", "Clarity"),
                            )
                            # Record once the broker has answered, or our own checks rejected it.
                            if result.get("success") or "retcode" in result:
                                self.ledger.record(rid, {"kind": "execution_result", "result": result})
                    await self.cloud.send({
                        "type":      "execution_result",
                        "request_id": msg.get("request_id"),
                        "result":    result,
                        "replayed":  replayed,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                elif msg_type == "close_position":
                    result = await asyncio.to_thread(self.mt5.close_position, msg["ticket"])
                    await self.cloud.send({
                        "type":      "close_result",
                        "request_id": msg.get("request_id"),
                        "result":    result,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                elif msg_type == "close_partial":
                    rid = str(msg.get("request_id") or "")
                    prior = self.ledger.get(rid) if rid else None
                    if prior is not None:
                        result = prior["result"]               # a repeated partial close must NOT close more volume
                    elif not rid:
                        result = {"success": False, "error": "Rejected: close_partial without request_id"}
                    else:
                        result = await asyncio.to_thread(
                            self.mt5.close_position_partial, msg["ticket"], msg["volume"],
                        )
                        # Cache SUCCESS only: the Cloud reuses a deterministic id per position, so a cached
                        # failure (e.g. a requote) would block every retry of the partial close.
                        if result.get("success"):
                            self.ledger.record(rid, {"kind": "partial_close_result", "result": result})
                    await self.cloud.send({
                        "type":      "partial_close_result",
                        "request_id": msg.get("request_id"),
                        "ticket":    msg["ticket"],
                        "result":    result,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                elif msg_type == "modify_position":
                    result = await asyncio.to_thread(
                        self.mt5.modify_position,
                        ticket = msg["ticket"],
                        sl     = msg.get("sl"),
                        tp     = msg.get("tp"),
                    )
                    await self.cloud.send({
                        "type":      "modify_result",
                        "request_id": msg.get("request_id"),
                        "result":    result,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })

                elif msg_type == "ping":
                    await self.cloud.send({"type": "pong", "timestamp": datetime.now(timezone.utc).isoformat()})

            except Exception as e:
                print(f"Execute error: {e}")
                await self.cloud.send({
                    "type":  "error",
                    "error": str(e),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })


async def main():
    from device_auth import load_device_credentials, prompt_for_pairing

    cloud_http_url = os.environ.get("CLARITY_CLOUD_HTTP_URL", "https://web-production-1b8fe.up.railway.app")

    manifest = fetch_manifest(cloud_http_url)
    auto = os.environ.get("CLARITY_BRIDGE_AUTO_UPDATE", "").strip() == "1"
    decision = update_decision(auto, manifest, VERSION, cloud_http_url)
    if decision == "notify":
        print(f"A new Bridge version is available ({manifest.get('version')}, you have {VERSION}). "
              "Download it from your Clarity account (Algo > Connect Broker). Automatic install is off.")
    elif decision.startswith("refuse"):
        print(f"Automatic update skipped: {decision.split(':', 1)[1]}")
    elif decision == "install":
        print(f"New version available: {manifest['version']} (current: {VERSION})")
        if download_and_install_update(manifest["download_url"], manifest["sha256"]):
            sys.exit(0)  # installer takes over from here; this process must exit

    device_id, device_secret = load_device_credentials()
    if not device_id:
        # First run — nothing paired yet. Ask for the 6-digit code
        # shown in Clarity's browser UI (Algo -> Connect Broker).
        device_id, device_secret = prompt_for_pairing(cloud_http_url)

    bridge = ClarityBridge(device_id=device_id, device_secret=device_secret)
    await bridge.start()


if __name__ == "__main__":
    asyncio.run(main())
