# Clarity Bridge

Local application that connects your MetaTrader 5 terminal to Clarity Cloud. It contains no trading
intelligence: Clarity Cloud decides, the Bridge only relays prices and carries out validated orders.

## What it does
- Streams prices, candles, positions and broker symbol details from MT5 to Clarity Cloud.
- Executes orders Cloud requests, after local safety checks (see below).
- Reports closed trades so they appear in your Clarity Journal.

## Local safety checks (v1.1)
- Never executes the same `request_id` twice (persisted across restarts).
- Rejects orders above `CLARITY_BRIDGE_MAX_LOT` (default 5.0), with invalid or wrong-side SL/TP,
  below the broker minimum, or when a Clarity position on that symbol is already open.
- Rounds volume down to the broker step and prices to the symbol digits; uses a fill mode the broker allows.

## Updates
Bridge tells you when a new version exists and does NOT install it. Automatic install is off by default.
Set `CLARITY_BRIDGE_AUTO_UPDATE=1` to enable it; it then requires an https URL on the Clarity Cloud host and a
matching SHA-256 before anything is run.

## Environment
| Variable | Default | Purpose |
|---|---|---|
| `CLARITY_CLOUD_HTTP_URL` | production Cloud | Cloud base URL |
| `CLARITY_BRIDGE_MAX_LOT` | `5.0` | Local maximum lot per order |
| `CLARITY_BRIDGE_AUTO_UPDATE` | off | `1` enables verified auto-install |

State files live in `%APPDATA%\ClarityBridge` (`device.json`, `state.json`, `executed_requests.json`).

## Tests
`python -m unittest discover -s tests -t .`
