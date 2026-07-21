"""
MT5 Connector
==============
Reads data from the local MetaTrader 5 terminal.
Sends execution requests to MT5.

Contains NO intelligence. NO decisions. NO analysis.
It is a data pipe between MT5 and Clarity Bridge.
"""

import json
import time
from typing import Optional


class MT5Connector:

    def __init__(self):
        self._mt5       = None
        self._connected = False
        self._selected_symbols = set()  # cache — avoid redundant symbol_select calls

    def _ensure_symbol(self, pair: str) -> bool:
        """
        MT5 only returns candle/tick data for symbols that are explicitly
        'selected' (visible in Market Watch). Without this, copy_rates_from_pos()
        and symbol_info_tick() silently return None even for valid symbols.
        """
        if pair in self._selected_symbols:
            return True

        info = self._mt5.symbol_info(pair)
        if info is None:
            print(f"MT5: '{pair}' doesn't exist under this exact name for this broker. "
                  f"last_error={self._mt5.last_error()}")
            return False

        if not info.visible:
            if not self._mt5.symbol_select(pair, True):
                print(f"MT5: symbol_select('{pair}') failed. last_error={self._mt5.last_error()}")
                return False

        self._selected_symbols.add(pair)
        return True

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5

            if not mt5.initialize():
                return False

            info = mt5.account_info()
            if info is None:
                return False

            self._connected = True
            return True

        except ImportError:
            print("MetaTrader5 not installed. Run: pip install MetaTrader5")
            return False
        except Exception as e:
            print(f"MT5 connect error: {e}")
            return False

    def disconnect(self):
        if self._mt5 and self._connected:
            self._mt5.shutdown()
            self._connected = False

    def account_info(self) -> dict:
        if not self._connected:
            return {}
        info = self._mt5.account_info()
        if info is None:
            return {}
        return {
            "name":    info.name,
            "broker":  info.company,
            "server":  info.server,
            "balance": info.balance,
            "equity":  info.equity,
            "margin":  info.margin,
            "free_margin": info.margin_free,
            "currency": info.currency,
        }

    def get_price(self, pair: str) -> Optional[dict]:
        if not self._connected:
            return None
        if not self._ensure_symbol(pair):
            return None
        tick = self._mt5.symbol_info_tick(pair)
        if tick is None:
            return None
        return {
            "bid":    tick.bid,
            "ask":    tick.ask,
            "spread": round(tick.ask - tick.bid, 5),
            "time":   tick.time,
        }

    def get_candles(self, pair: str, timeframe: str, count: int = 500) -> Optional[list]:
        if not self._connected:
            return None
        if not self._ensure_symbol(pair):
            return None

        TF_MAP = {
            "M1": 1, "M5": 5, "M15": 15, "M30": 30,
            "H1": 16385, "H4": 16388, "D1": 16408, "W1": 32769,
        }
        tf = TF_MAP.get(timeframe)
        if tf is None:
            return None

        # A freshly-selected symbol sometimes needs a moment for MT5 to sync
        # history from the broker server — retry briefly before giving up.
        rates = None
        for attempt in range(3):
            rates = self._mt5.copy_rates_from_pos(pair, tf, 0, count)
            if rates is not None and len(rates) > 0:
                break
            time.sleep(1)

        if rates is None or len(rates) == 0:
            print(f"MT5: no {timeframe} candles for {pair} after retries. "
                  f"last_error={self._mt5.last_error()}")
            return None

        return [
            {
                "time":   int(r["time"]),
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
                "volume": int(r["tick_volume"]),
            }
            for r in rates
        ]

    def get_open_positions(self) -> list:
        if not self._connected:
            return []
        positions = self._mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket":    p.ticket,
                "pair":      p.symbol,
                "direction": "BUY" if p.type == 0 else "SELL",
                "lot_size":  p.volume,
                "open_price": p.price_open,
                "sl":        p.sl,
                "tp":        p.tp,
                "profit":    p.profit,
                "comment":   p.comment,
                "open_time": p.time,
            }
            for p in positions
        ]

    def place_order(
        self,
        pair:      str,
        direction: str,
        lot_size:  float,
        sl:        float,
        tp:        float,
        comment:   str = "Clarity",
    ) -> dict:
        if not self._connected:
            return {"success": False, "error": "MT5 not connected"}

        self._ensure_symbol(pair)
        order_type = self._mt5.ORDER_TYPE_BUY if direction == "BUY" else self._mt5.ORDER_TYPE_SELL

        tick = self._mt5.symbol_info_tick(pair)
        if tick is None:
            return {"success": False, "error": f"No price for {pair}"}

        price = tick.ask if direction == "BUY" else tick.bid

        request = {
            "action":       self._mt5.TRADE_ACTION_DEAL,
            "symbol":       pair,
            "volume":       lot_size,
            "type":         order_type,
            "price":        price,
            "sl":           sl,
            "tp":           tp,
            "deviation":    10,
            "magic":        20260101,
            "comment":      comment,
            "type_time":    self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }

        result = self._mt5.order_send(request)
        if result is None:
            return {"success": False, "error": str(self._mt5.last_error())}

        return {
            "success":  result.retcode == self._mt5.TRADE_RETCODE_DONE,
            "retcode":  result.retcode,
            "ticket":   result.order,
            "price":    result.price,
            "comment":  result.comment,
        }

    def close_position(self, ticket: int) -> dict:
        if not self._connected:
            return {"success": False, "error": "MT5 not connected"}

        position = self._mt5.positions_get(ticket=ticket)
        if not position:
            return {"success": False, "error": f"Position {ticket} not found"}

        pos = position[0]
        close_type = self._mt5.ORDER_TYPE_SELL if pos.type == 0 else self._mt5.ORDER_TYPE_BUY
        tick = self._mt5.symbol_info_tick(pos.symbol)
        price = tick.bid if pos.type == 0 else tick.ask

        request = {
            "action":       self._mt5.TRADE_ACTION_DEAL,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "position":     ticket,
            "price":        price,
            "deviation":    10,
            "magic":        20260101,
            "comment":      "Clarity close",
            "type_time":    self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }

        result = self._mt5.order_send(request)
        if result is None:
            return {"success": False, "error": str(self._mt5.last_error())}

        return {
            "success": result.retcode == self._mt5.TRADE_RETCODE_DONE,
            "retcode": result.retcode,
            "price":   result.price,
        }

    def modify_position(self, ticket: int, sl: float = None, tp: float = None) -> dict:
        if not self._connected:
            return {"success": False, "error": "MT5 not connected"}

        position = self._mt5.positions_get(ticket=ticket)
        if not position:
            return {"success": False, "error": f"Position {ticket} not found"}

        pos = position[0]
        request = {
            "action":   self._mt5.TRADE_ACTION_SLTP,
            "symbol":   pos.symbol,
            "position": ticket,
            "sl":       sl if sl is not None else pos.sl,
            "tp":       tp if tp is not None else pos.tp,
        }

        result = self._mt5.order_send(request)
        if result is None:
            return {"success": False, "error": str(self._mt5.last_error())}

        return {"success": result.retcode == self._mt5.TRADE_RETCODE_DONE}
