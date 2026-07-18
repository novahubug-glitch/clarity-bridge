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
import time
import sys
from datetime import datetime

from mt5_connector import MT5Connector
from cloud_connection import CloudConnection


VERSION = "1.0.0"
RECONNECT_DELAY = 5   # seconds between reconnect attempts


class ClarityBridge:

    def __init__(self, user_token: str):
        self.user_token  = user_token
        self.mt5         = MT5Connector()
        self.cloud       = CloudConnection(user_token)
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

    async def _stream_loop(self):
        """Continuously stream market data from MT5 to Cloud."""
        PAIRS = [
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF",
            "AUDUSD", "USDCAD", "NZDUSD", "GBPJPY", "EURJPY",
        ]
        TIMEFRAMES = ["M5", "M15", "H4", "D1"]

        while self.running:
            try:
                payload = {
                    "type":      "market_data",
                    "timestamp": datetime.utcnow().isoformat(),
                    "account":   self.mt5.account_info(),
                    "prices":    {},
                    "candles":   {},
                }

                # Current prices for all pairs
                for pair in PAIRS:
                    price = self.mt5.get_price(pair)
                    if price:
                        payload["prices"][pair] = price

                # Candles for each pair/timeframe
                for pair in PAIRS:
                    payload["candles"][pair] = {}
                    for tf in TIMEFRAMES:
                        candles = self.mt5.get_candles(pair, tf, count=500)
                        if candles is not None:
                            payload["candles"][pair][tf] = candles

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
                    result = self.mt5.place_order(
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
                        "timestamp": datetime.utcnow().isoformat(),
                    })

                elif msg_type == "close_position":
                    result = self.mt5.close_position(msg["ticket"])
                    await self.cloud.send({
                        "type":      "close_result",
                        "request_id": msg.get("request_id"),
                        "result":    result,
                        "timestamp": datetime.utcnow().isoformat(),
                    })

                elif msg_type == "modify_position":
                    result = self.mt5.modify_position(
                        ticket = msg["ticket"],
                        sl     = msg.get("sl"),
                        tp     = msg.get("tp"),
                    )
                    await self.cloud.send({
                        "type":      "modify_result",
                        "request_id": msg.get("request_id"),
                        "result":    result,
                        "timestamp": datetime.utcnow().isoformat(),
                    })

                elif msg_type == "ping":
                    await self.cloud.send({"type": "pong", "timestamp": datetime.utcnow().isoformat()})

            except Exception as e:
                print(f"Execute error: {e}")
                await self.cloud.send({
                    "type":  "error",
                    "error": str(e),
                    "timestamp": datetime.utcnow().isoformat(),
                })


async def main():
    # Token comes from Clarity account (set on first launch via login screen)
    import os
    token = os.environ.get("CLARITY_USER_TOKEN")
    if not token:
        print("Please set CLARITY_USER_TOKEN environment variable.")
        print("Get your token from Clarity Settings → Algo Connection.")
        sys.exit(1)

    bridge = ClarityBridge(user_token=token)
    await bridge.start()


if __name__ == "__main__":
    asyncio.run(main())
