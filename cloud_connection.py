"""
Cloud Connection
=================
Secure WebSocket connection between Clarity Bridge and Clarity Cloud.

This module handles:
- Authentication with Clarity Cloud
- Sending market data upstream
- Receiving execution requests downstream
- Automatic reconnection

Contains NO intelligence. It is a pipe.
"""

import asyncio
import json
import os
from typing import AsyncIterator


CLARITY_CLOUD_URL = os.environ.get(
    "CLARITY_CLOUD_URL",
    "wss://algo.clarity.trade/bridge/ws"
)


class CloudConnection:

    def __init__(self, user_token: str):
        self.user_token  = user_token
        self._ws         = None
        self._connected  = False

    async def connect(self) -> bool:
        try:
            import websockets

            self._ws = await websockets.connect(
                CLARITY_CLOUD_URL,
                extra_headers={"Authorization": f"Bearer {self.user_token}"},
                ping_interval = 20,
                ping_timeout  = 10,
            )

            # Send handshake
            await self._ws.send(json.dumps({
                "type":    "bridge_connect",
                "version": "1.0.0",
            }))

            # Wait for acknowledgement
            response = json.loads(await self._ws.recv())
            if response.get("type") != "connected":
                print(f"Handshake failed: {response}")
                return False

            self._connected = True
            return True

        except ImportError:
            print("websockets not installed. Run: pip install websockets")
            return False
        except Exception as e:
            print(f"Cloud connection error: {e}")
            return False

    async def send(self, payload: dict) -> bool:
        if not self._connected or self._ws is None:
            return False
        try:
            await self._ws.send(json.dumps(payload))
            return True
        except Exception as e:
            print(f"Send error: {e}")
            self._connected = False
            return False

    async def receive(self) -> AsyncIterator[str]:
        """Async generator — yields messages from Cloud."""
        if not self._connected or self._ws is None:
            return
        try:
            async for message in self._ws:
                yield message
        except Exception as e:
            print(f"Receive error: {e}")
            self._connected = False

    async def disconnect(self):
        if self._ws:
            await self._ws.close()
            self._connected = False
