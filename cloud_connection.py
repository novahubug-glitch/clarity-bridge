"""
Cloud Connection
=================
Secure WebSocket connection between Clarity Bridge and Clarity Cloud.

This module handles:
- Authentication with Clarity Cloud (via permanent device_id + device_secret,
  established once during pairing — see device_auth.py. Not a login token;
  Bridge runs unattended so it can't depend on a browser session expiring.)
- Sending market data upstream
- Receiving execution requests downstream
- Automatic reconnection

Contains NO intelligence. It is a pipe.
"""

import json
import os
from typing import AsyncIterator


CLARITY_CLOUD_WS_URL = os.environ.get(
    "CLARITY_CLOUD_WS_URL",
    "wss://algo.clarity.trade/bridge/ws"
)


class CloudConnection:

    def __init__(self, device_id: str, device_secret: str):
        self.device_id     = device_id
        self.device_secret = device_secret
        self._ws           = None
        self._connected    = False

    async def connect(self) -> bool:
        try:
            import websockets

            self._ws = await websockets.connect(
                CLARITY_CLOUD_WS_URL,
                ping_interval = 20,
                ping_timeout  = 30,
            )

            # Authenticate with our permanent device identity —
            # no headers needed, so this works across websockets versions.
            await self._ws.send(json.dumps({
                "type":          "bridge_connect",
                "device_id":     self.device_id,
                "device_secret": self.device_secret,
                "version":       "1.0.0",
            }))

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
