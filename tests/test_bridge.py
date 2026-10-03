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


def connector(**kw):
    c = MT5Connector(); c._mt5 = FakeMT5(**kw); c._connected = True
    c._symbol_map = {"EURUSD": "EURUSDm"}
    return c


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
        c = connector(); c._mt5._positions = [Obj(symbol="EURUSDm", sl=0, tp=0, type=0)]
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


if __name__ == "__main__":
    unittest.main(verbosity=1)
