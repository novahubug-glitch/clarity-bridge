"""
Clarity Bridge: local safety rules. Standard library only (no MetaTrader5 import),
so every rule is unit-testable.

The Bridge stays thin: it contains NO strategy. These are mechanical checks any
execution client should make before sending an order to a broker, so that a bug or
compromise upstream cannot place an arbitrary order on the user's account.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from decimal import Decimal, ROUND_FLOOR
from typing import Optional
from urllib.parse import urlparse

MAGIC = 20260101
PAIR_RE = re.compile(r"^[A-Z0-9]{3,12}$")
MAX_LOT_DEFAULT = 5.0


def max_lot() -> float:
    try:
        v = float(os.environ.get("CLARITY_BRIDGE_MAX_LOT", MAX_LOT_DEFAULT))
        return v if v > 0 else MAX_LOT_DEFAULT
    except ValueError:
        return MAX_LOT_DEFAULT


def floor_to_step(value: float, step: float) -> float:
    d = (Decimal(repr(round(value, 9))) / Decimal(repr(step))).to_integral_value(rounding=ROUND_FLOOR)
    return float(d * Decimal(repr(step)))


def pick_filling(filling_mode_flags: int, FOK: int, IOC: int, RETURN: int) -> int:
    """
    SYMBOL_FILLING_MODE is a bit mask: 1 = FOK allowed, 2 = IOC allowed.
    Previously IOC was hard-coded, which many brokers reject (retcode 10030).
    """
    if filling_mode_flags & 2:
        return IOC
    if filling_mode_flags & 1:
        return FOK
    return RETURN


def validate_order(pair, direction, lot, sl, tp, price, info: dict) -> Optional[str]:
    """
    info: digits, point, volume_min, volume_max, volume_step, stops_level (points).
    Returns an error string, or None if the order is acceptable.
    """
    if not isinstance(pair, str) or not PAIR_RE.match(pair):
        return "invalid symbol"
    if direction not in ("BUY", "SELL"):
        return "invalid direction"
    for name, v in (("lot", lot), ("sl", sl), ("tp", tp), ("price", price)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
            return f"invalid {name}"
    if lot > max_lot():
        return f"lot size {lot} exceeds this Bridge's safety limit of {max_lot()}"
    if lot > info["volume_max"]:
        return f"lot size {lot} exceeds broker maximum {info['volume_max']}"
    # SL/TP must be on the correct side of the current price
    if direction == "BUY" and not (sl < price < tp):
        return "SL/TP are on the wrong side of the price for a BUY"
    if direction == "SELL" and not (tp < price < sl):
        return "SL/TP are on the wrong side of the price for a SELL"
    min_dist = info.get("stops_level", 0) * info["point"]
    if min_dist and (abs(price - sl) < min_dist or abs(price - tp) < min_dist):
        return "SL or TP is closer than the broker's minimum stop distance"
    return None


def _num(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def validate_modify(direction, bid, ask, cur_sl, cur_tp, new_sl, new_tp, point: float = 0.0) -> Optional[str]:
    """
    Checks a stop/target change on an open Clarity position. Returns an error string, or None if acceptable.

    The Bridge only ever needs to TIGHTEN risk (breakeven, trailing), so it refuses anything else:
      - a stop that is missing, zero, negative or not a number (that would REMOVE the protection);
      - a stop on the wrong side of the market;
      - a stop that moves AWAY from price, i.e. widens the risk, when a stop already exists.
    """
    if direction not in ("BUY", "SELL"):
        return "invalid position direction"
    if new_sl is None and new_tp is None:
        return "nothing to modify"
    if not (_num(bid) and _num(ask) and bid > 0 and ask > 0):
        return "no valid market price"
    eps = max(point, 1e-9) / 2
    if new_sl is not None:
        if not _num(new_sl) or new_sl <= 0:
            return "invalid stop loss (a stop cannot be removed or zeroed)"
        if direction == "BUY" and not new_sl < bid:
            return "stop loss is not below the market for a BUY"
        if direction == "SELL" and not new_sl > ask:
            return "stop loss is not above the market for a SELL"
        if _num(cur_sl) and cur_sl > 0:
            if direction == "BUY" and new_sl < cur_sl - eps:
                return "refused: this would move the stop away from price (wider risk)"
            if direction == "SELL" and new_sl > cur_sl + eps:
                return "refused: this would move the stop away from price (wider risk)"
    if new_tp is not None:
        if not _num(new_tp) or new_tp <= 0:
            return "invalid take profit"
        if direction == "BUY" and not new_tp > ask:
            return "take profit is not above the market for a BUY"
        if direction == "SELL" and not new_tp < bid:
            return "take profit is not below the market for a SELL"
    return None


# ── Idempotency: never execute the same request_id twice ─────────────────────
class RequestLedger:
    """
    Persistent record of executed request_ids -> stored result, so a duplicate
    request (Cloud retry, reconnect) returns the ORIGINAL result and is never
    sent to the broker again. Survives Bridge restarts.
    """
    def __init__(self, path: str, ttl_seconds: int = 86_400, clock=time.time):
        self.path, self.ttl, self._clock = path, ttl_seconds, clock
        self._d: dict = {}
        try:
            with open(path, "r") as f:
                self._d = json.load(f)
        except (OSError, ValueError):
            self._d = {}
        self._purge()

    def _purge(self):
        now = self._clock()
        self._d = {k: v for k, v in self._d.items() if now - v.get("t", 0) < self.ttl}

    def get(self, request_id: str) -> Optional[dict]:
        self._purge()
        v = self._d.get(request_id)
        return v["result"] if v else None

    def record(self, request_id: str, result: dict) -> None:
        self._d[request_id] = {"t": self._clock(), "result": result}
        self._purge()
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self._d, f)
            os.replace(tmp, self.path)          # atomic: no half-written ledger
        except OSError as e:
            print(f"Warning: could not persist request ledger: {e}")

    def recent(self, within_seconds: int) -> list:
        now = self._clock()
        return [(k, v["result"]) for k, v in self._d.items() if now - v["t"] <= within_seconds]


# ── Persistent state (open tickets) ──────────────────────────────────────────
def load_json(path: str, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json_atomic(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError as e:
        print(f"Warning: could not save {path}: {e}")


# ── Update verification ──────────────────────────────────────────────────────
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def update_url_allowed(url: str, cloud_http_url: str) -> bool:
    """HTTPS only, and only the Cloud host we already trust."""
    try:
        u, c = urlparse(url), urlparse(cloud_http_url)
    except ValueError:
        return False
    return u.scheme == "https" and bool(u.hostname) and u.hostname == c.hostname


def update_decision(auto_enabled: bool, manifest: dict, current: str, cloud_http_url: str) -> str:
    """
    Returns one of: 'none', 'notify', 'install', 'refuse:<reason>'.
    Auto-install happens ONLY when explicitly enabled AND the manifest carries a
    SHA-256 AND the URL is https on the trusted Cloud host. Otherwise the user is
    just told a new version exists.
    """
    latest, url = manifest.get("version"), manifest.get("download_url")
    if not latest or not url or latest == current:
        return "none"
    if not auto_enabled:
        return "notify"
    sha = manifest.get("sha256")
    if not (isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha)):
        return "refuse:no checksum in manifest"
    if not update_url_allowed(url, cloud_http_url):
        return "refuse:download URL is not https on the Clarity Cloud host"
    return "install"
