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
import time
import sys
from datetime import datetime, timezone

from mt5_connector import MT5Connector
from cloud_connection import CloudConnection


VERSION = "1.0.0"
RECONNECT_DELAY = 5   # seconds between reconnect attempts


class ClarityBridge:

    def __init__(self, device_id: str, device_secret: str):
        self.mt5         = MT5Connector()
        self.cloud       = CloudConnection(device_id, device_secret)
        self.running     = False

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
                await self.cloud.send(payload)

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
                    result = await asyncio.to_thread(
                        self.mt5.place_order,
                        pair      = msg["pair"],
                        direction = msg["direction"],
                        lot_size  = msg["lot_size"],
                        sl        = msg["sl"],
                        tp        = msg["tp"],
                        comment   = msg.get("comment", "Clarity"),
                    )
                    await self.cloud.send({
                        "type":      "execution_result",
                        "request_id": msg.get("request_id"),
                        "result":    result,
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

    cloud_http_url = os.environ.get("CLARITY_CLOUD_HTTP_URL", "https://algo.clarity.trade")

    device_id, device_secret = load_device_credentials()
    if not device_id:
        # First run — nothing paired yet. Ask for the 6-digit code
        # shown in Clarity's browser UI (Algo -> Connect Broker).
        device_id, device_secret = prompt_for_pairing(cloud_http_url)

    bridge = ClarityBridge(device_id=device_id, device_secret=device_secret)
    await bridge.start()


if __name__ == "__main__":
    asyncio.run(main())
