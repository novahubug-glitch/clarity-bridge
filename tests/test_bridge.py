import os, sys, json, tempfile, hashlib, types, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import safety
from mt5_connector import MT5Connector


class Obj(types.SimpleNamespace): pass


class FakeMT5:
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_RETCODE_DONE = 10009
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 3
    SYMBOL_TRADE_MODE_DISABLED = 0

    def __init__(self, digits=5, step=0.01, vmin=0.01, filling=2, positions=()):
        self.sent, self._positions, self.deals = [], list(positions), []
        self._info = Obj(digits=digits, point=10 ** -digits, volume_min=vmin, volume_max=100.0, volume_step=step,
                         trade_stops_level=10, filling_mode=filling, trade_mode=4, trade_tick_size=10 ** -digits,
                         trade_tick_value=1.0, trade_tick_value_loss=1.25, trade_contract_size=100000, visible=True)
    def symbol_info(self, s): return self._info
    def symbol_info_tick(self, s): return Obj(ask=1.10002, bid=1.09998)
    def symbols_get(self): return [Obj(name="EURUSDm"), Obj(name="EURUSD")]
    def symbol_select(self, s, e): return True
    def positions_get(self, **kw): return self._positions
    def order_send(self, req):
        self.sent.append(req); return Obj(retcode=10009, order=777, price=req.get("price", 0), comment="ok")
    def last_error(self): return (1, "x")
    def history_deals_get(self, position=None): return self.deals
    def copy_rates_from_pos(self, sym, tf, start_pos, count):
        self.rates_requests = getattr(self, "rates_requests", []) + [(sym, tf, start_pos, count)]
        return [{"time": 1700000000 + 900 * i, "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005, "tick_volume": 10}
                for i in range(3)]


def connector(**kw):
    c = MT5Connector(); c._mt5 = FakeMT5(**kw); c._connected = True
    c._symbol_map = {"EURUSD": "EURUSDm"}
    return c


class CandleTests(unittest.TestCase):
    def test_closed_candles_only_by_default(self):
        c = connector(); out = c.get_candles("EURUSD", "M15", 500)
        self.assertEqual(c._mt5.rates_requests[0][2], 1)          # start at the last CLOSED candle, not the forming one
        self.assertEqual(len(out), 3)
    def test_forming_candle_only_when_explicitly_requested(self):
        c = connector(); c.get_candles("EURUSD", "M5", 100, include_forming=True)
        self.assertEqual(c._mt5.rates_requests[0][2], 0)


class OrderTests(unittest.TestCase):
    def test_normalises_digits_and_volume_and_sanitises_comment(self):
        c = connector(); r = c.place_order("EURUSD", "BUY", 0.456, 1.09810000001, 1.10400000009, "x" * 60)
        self.assertTrue(r["success"]); q = c._mt5.sent[0]
        self.assertEqual(q["volume"], 0.45)                      # floored to step, never rounded up
        self.assertEqual(q["sl"], 1.0981); self.assertEqual(q["tp"], 1.104)
        self.assertLessEqual(len(q["comment"]), 31)
    def test_filling_mode_follows_broker(self):
        for flags, want in ((2, 1), (1, 0), (0, 2)):
            c = connector(filling=flags); c.place_order("EURUSD", "BUY", 0.1, 1.098, 1.104)
            self.assertEqual(c._mt5.sent[0]["type_filling"], want, flags)
    def test_wrong_side_sl_rejected_and_nothing_sent(self):
        c = connector(); r = c.place_order("EURUSD", "BUY", 0.1, 1.2, 1.3)
        self.assertFalse(r["success"]); self.assertEqual(c._mt5.sent, [])
    def test_lot_above_bridge_limit_rejected(self):
        os.environ["CLARITY_BRIDGE_MAX_LOT"] = "1.0"
        try:
            c = connector(); r = c.place_order("EURUSD", "BUY", 2.0, 1.098, 1.104)
            self.assertFalse(r["success"]); self.assertEqual(c._mt5.sent, [])
        finally: del os.environ["CLARITY_BRIDGE_MAX_LOT"]
    def test_below_broker_minimum_rejected(self):
        c = connector(vmin=0.1, step=0.1); self.assertFalse(c.place_order("EURUSD", "BUY", 0.05, 1.098, 1.104)["success"])
    def test_duplicate_position_on_symbol_blocked(self):
        c = connector(positions=[Obj(magic=safety.MAGIC)])
        r = c.place_order("EURUSD", "BUY", 0.1, 1.098, 1.104)
        self.assertFalse(r["success"]); self.assertEqual(c._mt5.sent, [])
    def test_manual_position_does_not_block(self):
        c = connector(positions=[Obj(magic=0)]); self.assertTrue(c.place_order("EURUSD", "BUY", 0.1, 1.098, 1.104)["success"])
    def test_symbol_facts_reports_broker_values(self):
        f = connector().symbol_facts("EURUSD")
        self.assertEqual((f["digits"], f["volume_step"], f["tick_value_loss"]), (5, 0.01, 1.25))
    def test_modify_rounds_to_digits(self):
        c = connector(); c._mt5._positions = [Obj(symbol="EURUSDm", sl=0, tp=0, type=0, magic=safety.MAGIC)]
        c._mt5.positions_get = lambda **kw: c._mt5._positions
        c.modify_position(5, sl=1.0950000000123); self.assertEqual(c._mt5.sent[0]["sl"], 1.095)


class ClosedTradeTests(unittest.TestCase):
    def test_totals_include_partials_and_entry_commission(self):
        c = connector(); M = c._mt5
        M.deals = [Obj(entry=0, profit=0, commission=-0.7, swap=0, price=1.1, time=1, fee=0),     # entry
                   Obj(entry=1, profit=30, commission=-0.35, swap=0, price=1.103, time=2, fee=0),  # partial
                   Obj(entry=1, profit=10, commission=-0.35, swap=-0.2, price=1.102, time=3, fee=0)]
        t = c.get_closed_trade(5)
        self.assertEqual(t["profit"], 40)                         # was 10 (last exit deal only)
        self.assertAlmostEqual(t["commission"], -1.4); self.assertAlmostEqual(t["swap"], -0.2)
        self.assertTrue(t["totals"]); self.assertEqual(t["close_price"], 1.102)


class SafetyTests(unittest.TestCase):
    def test_ledger_idempotent_and_persistent(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "l.json")
        a = safety.RequestLedger(p); a.record("r1", {"kind": "execution_result", "result": {"success": True}})
        b = safety.RequestLedger(p)                              # simulates a Bridge restart
        self.assertEqual(b.get("r1")["result"], {"success": True}); self.assertIsNone(b.get("r2"))
    def test_ledger_expires(self):
        t = [0]; d = tempfile.mkdtemp(); l = safety.RequestLedger(os.path.join(d, "l.json"), ttl_seconds=10, clock=lambda: t[0])
        l.record("r", {}); t[0] = 11; self.assertIsNone(l.get("r"))
    def test_update_policy(self):
        cloud = "https://cloud.example.com"; sha = "a" * 64
        m = {"version": "1.2.0", "download_url": cloud + "/bridge/download", "sha256": sha}
        self.assertEqual(safety.update_decision(False, m, "1.1.0", cloud), "notify")          # default: never auto-install
        self.assertEqual(safety.update_decision(True, m, "1.1.0", cloud), "install")
        self.assertEqual(safety.update_decision(True, m, "1.2.0", cloud), "none")
        self.assertTrue(safety.update_decision(True, {**m, "sha256": None}, "1.1.0", cloud).startswith("refuse"))
        self.assertTrue(safety.update_decision(True, {**m, "download_url": "http://cloud.example.com/x"}, "1.1.0", cloud).startswith("refuse"))
        self.assertTrue(safety.update_decision(True, {**m, "download_url": "https://evil.example/x"}, "1.1.0", cloud).startswith("refuse"))
        self.assertTrue(safety.update_decision(True, {**m, "sha256": "ZZ"}, "1.1.0", cloud).startswith("refuse"))
    def test_sha256(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "f"); open(p, "wb").write(b"abc")
        self.assertEqual(safety.sha256_file(p), hashlib.sha256(b"abc").hexdigest())
    def test_floor_step_float_noise(self):
        self.assertEqual(safety.floor_to_step(0.49999999999, 0.01), 0.5)


class ModifyProtection(unittest.TestCase):
    """Phase 6: the Bridge may only tighten risk on its own positions. Bid 1.09998 / ask 1.10002 in FakeMT5."""
    def _pos(self, **kw):
        return Obj(symbol="EURUSDm", ticket=5, type=kw.pop("type", 0), sl=kw.pop("sl", 1.0950), tp=kw.pop("tp", 1.1100),
                   volume=kw.pop("volume", 0.2), magic=kw.pop("magic", safety.MAGIC))
    def _c(self, **pos):
        c = connector(); c._mt5._positions = [self._pos(**pos)]; c._mt5.positions_get = lambda **kw: c._mt5._positions; return c

    def test_tightening_a_buy_stop_is_allowed(self):
        c = self._c(); r = c.modify_position(5, sl=1.0970)
        self.assertTrue(r["success"]); self.assertEqual(c._mt5.sent[0]["sl"], 1.097)
    def test_zero_stop_would_remove_protection_and_is_refused(self):
        c = self._c(); r = c.modify_position(5, sl=0)
        self.assertFalse(r["success"]); self.assertEqual(c._mt5.sent, [])
    def test_negative_or_non_numeric_stop_refused(self):
        for bad in (-1.0, float("nan"), True):
            c = self._c(); self.assertFalse(c.modify_position(5, sl=bad)["success"]); self.assertEqual(c._mt5.sent, [])
    def test_widening_a_buy_stop_refused(self):
        c = self._c(sl=1.0950); r = c.modify_position(5, sl=1.0900)
        self.assertFalse(r["success"]); self.assertIn("wider", r["error"]); self.assertEqual(c._mt5.sent, [])
    def test_widening_a_sell_stop_refused_and_tightening_allowed(self):
        c = self._c(type=1, sl=1.1050, tp=1.0900); self.assertFalse(c.modify_position(5, sl=1.1100)["success"])
        self.assertTrue(c.modify_position(5, sl=1.1030)["success"])
    def test_stop_on_wrong_side_of_market_refused(self):
        c = self._c(); self.assertFalse(c.modify_position(5, sl=1.2000)["success"]); self.assertEqual(c._mt5.sent, [])
    def test_position_not_opened_by_clarity_is_never_touched(self):
        c = self._c(magic=0)
        for r in (c.modify_position(5, sl=1.0970), c.close_position(5), c.close_position_partial(5, 0.1)):
            self.assertFalse(r["success"]); self.assertIn("not opened by Clarity", r["error"])
        self.assertEqual(c._mt5.sent, [])

class PartialCloseNeverClosesEverything(unittest.TestCase):
    def _c(self, volume):
        c = connector(); c._mt5._positions = [Obj(symbol="EURUSDm", ticket=5, type=0, sl=1.095, tp=1.11, volume=volume, magic=safety.MAGIC)]
        c._mt5.positions_get = lambda **kw: c._mt5._positions; return c
    def test_minimum_lot_position_cannot_be_partially_closed(self):
        c = self._c(0.01); r = c.close_position_partial(5, 0.01)
        self.assertFalse(r["success"]); self.assertEqual(c._mt5.sent, [])
    def test_request_larger_than_position_is_not_a_full_close(self):
        c = self._c(0.2); r = c.close_position_partial(5, 5.0)
        self.assertFalse(r["success"]); self.assertEqual(c._mt5.sent, [])
    def test_normal_partial_still_works(self):
        c = self._c(0.2); r = c.close_position_partial(5, 0.1)
        self.assertTrue(r["success"]); self.assertEqual(c._mt5.sent[0]["volume"], 0.1)

class PositionSnapshotErrors(unittest.TestCase):
    """An MT5 error is not the same as 'nothing is open'."""
    def test_connector_reports_none_on_error_and_empty_list_when_flat(self):
        c = connector(); c._mt5.positions_get = lambda **kw: None
        self.assertIsNone(c.get_open_positions())
        c._mt5.positions_get = lambda **kw: []
        self.assertEqual(c.get_open_positions(), [])
    def test_error_does_not_close_known_tickets_or_wipe_state(self):
        import main, tempfile
        main._config_dir = lambda: tempfile.mkdtemp()
        b = main.ClarityBridge("d", "s"); b._known_tickets = {5, 6}
        class M:
            def account_info(self): return {"balance": 1000}
            def get_open_positions(self): return None
            def get_closed_trade(self, t): raise AssertionError("must not look for closes during an MT5 error")
            def symbol_facts(self, p): return None
            def get_price(self, p): return None
            def get_candles(self, *a, **k): return None
        b.mt5 = M()
        payload = b._collect_market_data(["EURUSD"], ["M5"])
        self.assertFalse(payload["positions_ok"]); self.assertEqual(payload["closed_trades"], [])
        self.assertEqual(b._known_tickets, {5, 6})                 # remembered, so real closes are still detected later
    def test_healthy_snapshot_is_flagged_ok(self):
        import main, tempfile
        main._config_dir = lambda: tempfile.mkdtemp()
        b = main.ClarityBridge("d", "s")
        class M:
            def account_info(self): return {"balance": 1000}
            def get_open_positions(self): return []
            def get_closed_trade(self, t): return None
            def symbol_facts(self, p): return None
            def get_price(self, p): return None
            def get_candles(self, *a, **k): return None
        b.mt5 = M(); self.assertTrue(b._collect_market_data(["EURUSD"], ["M5"])["positions_ok"])


class ValidateModifyUnit(unittest.TestCase):
    def test_table(self):
        v = safety.validate_modify
        self.assertIsNone(v("BUY", 1.0999, 1.1001, 1.095, 1.11, 1.097, None, 0.00001))
        self.assertIsNone(v("BUY", 1.0999, 1.1001, 0, 0, 1.097, None, 0.00001))          # no stop yet: setting one is fine
        self.assertIsNotNone(v("BUY", 1.0999, 1.1001, 1.095, 1.11, None, None))           # nothing to do
        self.assertIsNotNone(v("SELL", 1.0999, 1.1001, 1.105, 1.09, 1.106, None, 0.00001))
        self.assertIsNotNone(v("BUY", 0, 1.1001, 1.095, 1.11, 1.097, None))               # no valid price
        self.assertIsNotNone(v("BUY", 1.0999, 1.1001, 1.095, 1.11, None, 1.05))           # target on the wrong side
        self.assertIsNone(v("BUY", 1.0999, 1.1001, 1.095, 1.11, 1.095, None, 0.00001))   # same stop: harmless no-op


if __name__ == "__main__":
    unittest.main(verbosity=1)
