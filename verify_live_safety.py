# -*- coding: utf-8 -*-
"""
实盘安全验证 —— 全程离线，用一个「假交易所」把实盘路径上最要命的情况逐条演一遍。

  不连 OKX、不读 .env、不下任何真单。运行：python verify_live_safety.py

verify_engine.py 验的是「公式对不对」；这个文件验的是「出事的时候会不会多下单、漏止损、乱平仓」：
  A. 客户端：时间戳 / 数字格式 / 下单超时不重发
  B. 风控：解除熔断 / 成本计入 / 强平线保护 / 保证金封顶
  C. 实盘通道：止损链 / 账本 / 手动仓位隔离 / 断网对账 / 止损被触发后的结算
  D. 主循环：不重放 / 信号保鲜 / 崩溃不重复下单 / 对账平仓 / 本地止损守护 / 单实例
  E. 回测引擎：止损与强平的先后 / 跳空成交价 / 假盈利 / 资金费 / 浮亏熔断
"""
import importlib.util
import os
import re
import shutil
import sys
import urllib.error

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import trader as trader_mod
from broker import LiveBroker
from engine import Backtester
from okx_client import OkxClient, OkxError, fmt_num
from risk import RiskManager
from strategy import Action

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, "data", "_selftest")      # 本脚本自己的临时目录，每次运行先清空
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)

PASS, FAIL = "✓", "✗"
fails = []
LOGS = []


def check(name, cond, detail=""):
    print(f"  {PASS if cond else FAIL} {name}  {detail}")
    if not cond:
        fails.append(name)


def fake_log(msg, tag="INFO"):
    LOGS.append((tag, msg))


trader_mod.log = fake_log          # 测试日志不写进真正的 logs/ 目录

BAR = 900000                       # 15m
T0 = 1_800_000_000_000             # 恰好是 15m 的整数倍
INST = {"instId": "DOGE-USDT-SWAP", "ctVal": "1000", "lotSz": "0.01", "minSz": "0.01",
        "tickSz": "0.00001", "maxSz": "100000", "lever": "50"}


# ══════════════════════════ 假交易所 ══════════════════════════
class FakeOkx:
    """只模拟本系统用到的那几个接口。买卖模式（net）为主，也能演双向持仓。"""
    simulated = False

    def __init__(self):
        self.t = T0 + 5000
        self.px = 0.06
        self.bar_px = {}
        self.avail = 4.0
        self.pos_mode = "net_mode"
        self.position = None          # {"sz": 带符号张数, "avg":, "open_t":, "pos_side":}
        self.manual_long_short = None
        self.orders, self.algos, self.history, self.calls = {}, [], [], []
        self.drop_attached_stop = False
        self.fail_stop_algo = False
        self.net_break_after_fill = False
        self.net_break_before_send = False
        self.reject_order = False
        self.close_fail_once = False
        self.hide_positions = False
        self._n = 0

    # --- 时间 / 行情 ---
    def now_ms(self):
        return self.t

    def advance(self, seconds):
        self.t += int(seconds * 1000)

    def sync_time(self):
        return 0

    def instrument(self, inst_id, inst_type="SWAP"):
        return INST

    def ticker(self, inst_id):
        return {"last": str(self.px)}

    def candles(self, inst_id, bar="15m", limit=300, after=None, before=None):
        out, ts, first = [], self.t // BAR * BAR, T0 - 400 * BAR
        while ts >= first and len(out) < limit:
            if after is None or ts < after:
                p = self.bar_px.get(ts, 0.06)
                out.append({"ts": ts, "o": p, "h": p * 1.001, "l": p * 0.999, "c": p, "v": 1.0,
                            "confirm": ts + BAR <= self.t})
            ts -= BAR
        return sorted(out, key=lambda x: x["ts"])

    # --- 账户 ---
    def balance(self, ccy=None):
        return [{"totalEq": str(self.avail), "details": [{"ccy": "USDT", "availEq": str(self.avail),
                                                          "availBal": str(self.avail)}]}]

    def equity_usdt(self):
        return self.avail

    def account_config(self):
        return {"posMode": self.pos_mode}

    def set_leverage(self, *a, **k):
        self.calls.append(("set_leverage", a))

    def set_pos_mode(self, *a, **k):
        self.calls.append(("set_pos_mode", a))

    def positions(self, inst_id=None):
        if self.hide_positions:
            return []
        rows = []
        if self.position:
            p = self.position
            upl = (self.px - p["avg"]) * p["sz"] * 1000
            rows.append({"instId": inst_id, "mgnMode": "cross", "pos": str(p["sz"]), "posSide": "net",
                         "avgPx": str(p["avg"]), "upl": str(upl), "posId": "1", "closeOrderAlgo": []})
        if self.manual_long_short:
            rows.append(dict(self.manual_long_short, instId=inst_id, mgnMode="cross"))
        return rows

    def positions_history(self, inst_id, inst_type="SWAP", limit=10):
        return list(reversed(self.history))[:limit]

    # --- 交易 ---
    def place_order(self, inst_id, side, sz, ord_type="market", td_mode="cross", px=None,
                    reduce_only=False, pos_side=None, cl_ord_id=None, sl_trigger_px=None,
                    attach_algo_cl_ord_id=None, **kw):
        self.calls.append(("place_order", {"side": side, "sz": sz, "sl": sl_trigger_px, "cl": cl_ord_id}))
        if self.net_break_before_send:
            raise OkxError("NET_AMBIGUOUS", "模拟：请求没发出去就断网")
        if self.reject_order:
            raise OkxError("1", "All operations failed | sCode=51008 余额不足")
        signed = float(sz) * (1 if side == "buy" else -1)
        self.position = {"sz": signed, "avg": self.px, "open_t": self.t}
        self._margin = abs(signed) * 1000 * self.px / 5.0
        self.avail -= self._margin
        self._n += 1
        self.orders[cl_ord_id] = {"ordId": str(self._n), "clOrdId": cl_ord_id, "state": "filled",
                                  "avgPx": str(self.px), "accFillSz": str(sz)}
        if sl_trigger_px and not self.drop_attached_stop:
            self.algos.append({"algoId": f"A{self._n}", "algoClOrdId": attach_algo_cl_ord_id,
                               "ordType": "conditional", "instId": inst_id, "slTriggerPx": str(sl_trigger_px),
                               "side": "sell" if side == "buy" else "buy"})
        if self.net_break_after_fill:
            raise OkxError("NET_AMBIGUOUS", "模拟：订单已成交，回包时断网")
        return [{"ordId": str(self._n), "clOrdId": cl_ord_id, "sCode": "0", "sMsg": ""}]

    def order(self, inst_id, ord_id=None, cl_ord_id=None):
        if cl_ord_id in self.orders:
            return self.orders[cl_ord_id]
        raise OkxError("51603", "Order does not exist")

    def algo_pending(self, inst_id, ord_type="conditional"):
        return [a for a in self.algos if a["ordType"] == ord_type]

    def place_stop_algo(self, inst_id, side, sl_trigger_px, td_mode="cross", pos_side=None,
                        algo_cl_ord_id=None, sl_trigger_px_type="last"):
        self.calls.append(("place_stop_algo", sl_trigger_px))
        if self.fail_stop_algo:
            raise OkxError("51000", "模拟：止损单参数被拒")
        self._n += 1
        self.algos.append({"algoId": f"A{self._n}", "algoClOrdId": algo_cl_ord_id, "ordType": "conditional",
                           "instId": inst_id, "slTriggerPx": str(sl_trigger_px), "side": side})
        return [{"algoId": f"A{self._n}", "sCode": "0"}]

    def cancel_algos(self, inst_id, algo_ids):
        self.calls.append(("cancel_algos", list(algo_ids)))
        self.algos = [a for a in self.algos if a["algoId"] not in algo_ids]

    def _flatten(self, px):
        p = self.position
        pnl = (px - p["avg"]) * p["sz"] * 1000 - (px + p["avg"]) * abs(p["sz"]) * 1000 * 0.0005
        self.history.append({"cTime": str(p["open_t"]), "uTime": str(self.t), "realizedPnl": str(pnl),
                             "closeAvgPx": str(px), "posId": "1"})
        self.avail += self._margin + pnl
        self.position = None
        return pnl

    def close_position(self, inst_id, mgn_mode="cross", pos_side=None, auto_cxl=True):
        self.calls.append(("close_position", pos_side))
        if self.close_fail_once:
            self.close_fail_once = False
            raise OkxError("50001", "模拟：服务暂时不可用")
        if not self.position:
            raise OkxError("51023", "Position does not exist")
        self._flatten(self.px)
        return [{"instId": inst_id}]

    def fire_exchange_stop(self):
        """模拟交易所的止损单被触发：按止损价平掉。"""
        a = next(x for x in self.algos if x.get("slTriggerPx"))
        self.algos.remove(a)
        return self._flatten(float(a["slTriggerPx"]))

    def n(self, name):
        return len([c for c in self.calls if c[0] == name])


def new_broker(fake, name, **kw):
    b = LiveBroker(fake, INST["instId"], INST, lever=20, ledger_path=os.path.join(TMP, name + ".json"),
                   log=fake_log, sleep=fake.advance, **kw)
    return b


# ══════════════════════════ A. 客户端 ══════════════════════════
print("\n【A】okx_client：时间戳 / 数字格式 / 下单超时不重发")
c = OkxClient(api_key="k", secret="s", passphrase="p", max_retries=1)
ts = c._ts()
check("时间戳格式 ISO 毫秒 Z", bool(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", ts)), ts)
check("小数不出科学计数法", fmt_num(0.0000123) == "0.0000123" and "e" not in fmt_num(1.5e-7),
      f"{fmt_num(0.0000123)} / {fmt_num(1.5e-7)}（str() 会给出 {str(0.0000123)}）")
check("张数格式干净", fmt_num(0.26) == "0.26" and fmt_num(5.0) == "5", f"{fmt_num(0.26)} / {fmt_num(5.0)}")

sent = []


def broken_raw(url, method, headers, body_bytes):
    sent.append(method)
    raise urllib.error.URLError("模拟超时")


c._raw = broken_raw
c._limiter.take = lambda: None
try:
    c.place_order("DOGE-USDT-SWAP", "buy", 0.07, sl_trigger_px=0.0585)
    code = "没有报错"
except OkxError as e:
    code = e.code
check("下单超时：只发 1 次，不自动重发", sent.count("POST") == 1 and code == "NET_AMBIGUOUS",
      f"发了 {sent.count('POST')} 次，错误码 {code}")
sent.clear()
try:
    c.ticker("DOGE-USDT-SWAP")
except OkxError:
    pass
check("查询超时：照常重试", sent.count("GET") == 2, f"GET 发了 {sent.count('GET')} 次（max_retries=1）")

bodies = []
c._raw = lambda url, method, headers, body: bodies.append(body) or '{"code":"1","msg":"All operations failed","data":[{"sCode":"51008","sMsg":"Insufficient balance"}]}'
try:
    c.place_order("DOGE-USDT-SWAP", "buy", 0.07, sl_trigger_px=0.0000585)
    msg = ""
except OkxError as e:
    msg = str(e)
check("拒单原因带出 sCode", "51008" in msg, msg)
check("下单报文里止损价是十进制", b'"slTriggerPx":"0.0000585"' in bodies[0], bodies[0].decode()[:160])

# ══════════════════════════ B. 风控 ══════════════════════════
print("\n【B】risk：解除熔断 / 成本 / 强平线 / 保证金封顶")
rm = RiskManager(max_daily_loss_pct=99.0, max_drawdown_pct=15.0)
rm.update_equity(1000.0)
rm.update_equity(800.0)
check("回撤 20% 熔断", rm.halted(), rm.halt_reason())
rm.reset_halt("测试")
rm.update_equity(800.0)
check("解除后不会立刻又熔断", not rm.halted(), f"峰值重新起算为 {rm.state['peak_equity']}")

rm = RiskManager(risk_per_trade_pct=1.0, max_leverage=20.0)
d0 = rm.size_position(1000.0, 100.0, 99.0, 1.0, 0.01, 0.01)
d1 = rm.size_position(1000.0, 100.0, 99.0, 1.0, 0.01, 0.01, side=1, cost_pct=0.0007)
loss1 = d1.size * (1.0 + 199.0 * 0.0007)
check("计入来回成本后张数变小，止损实亏不超预算", d1.size < d0.size and loss1 <= 10.0 + 1e-9,
      f"{d0.size} → {d1.size} 张，含成本实亏 {loss1:.2f} ≤ 10")
check("止损在错误一侧被拒", not rm.size_position(1000.0, 97.0, 98.0, 1.0, 0.01, 0.01, side=1).allow)
d2 = rm.size_position(1000.0, 100.0, 95.0, 1.0, 0.01, 0.01, side=1, liq_lever=20)
check("逐仓 20x：5% 的止损比强平线还远 → 拒单", not d2.allow, d2.reason)
d3 = rm.size_position(1000.0, 100.0, 99.0, 1.0, 0.01, 0.01, side=1, max_notional_abs=500.0)
check("可用保证金封顶", d3.allow and d3.notional <= 500.0 + 1e-9, f"名义 {d3.notional:.2f} ≤ 500")

# ══════════════════════════ C. 实盘通道 ══════════════════════════
print("\n【C】LiveBroker：止损链 / 账本 / 手动仓位隔离 / 断网对账")
fx = FakeOkx()
b = new_broker(fx, "c1")
check("不改账户的持仓模式", fx.n("set_pos_mode") == 0)
check("策略本金 = 启动时的可用保证金", abs(b.ledger["base_capital"] - 4.0) < 1e-9, f"{b.ledger['base_capital']}")
r = b.open(1, 0.07, 0.0584961, "测试开多")
pos = b.get_position()
check("开仓成功并确认成交", r["ok"] and pos and pos["size"] == 0.07, f"{r}")
check("持仓带止损价（原先恒为 None）", pos and pos["stop"] == 0.0585, f"stop={pos and pos['stop']}（对齐 tick，往入场价方向）")
check("交易所上确认有止损单", r.get("stop_on_exchange") is True)
check("开仓占用保证金不算亏损", fx.avail < 3.7 and abs(b.equity() - 4.0) < 1e-6,
      f"可用保证金 4.00 → {fx.avail:.2f}（-{(4-fx.avail)/4*100:.0f}%），策略权益仍 {b.equity():.4f}")

b2 = new_broker(fx, "c1")          # 模拟重启：同一本账本
p2 = b2.get_position()
check("重启后仓位和止损价都还在", p2 and p2["stop"] == 0.0585 and p2["size"] == 0.07)

fx.px = 0.0612
b.get_position()
check("浮盈计入策略权益", abs(b.equity() - (4.0 + 0.0012 * 0.07 * 1000)) < 1e-6, f"{b.equity():.4f}")
fx.algos.append({"algoId": "MANUAL", "algoClOrdId": "", "ordType": "conditional", "instId": INST["instId"],
                 "slTriggerPx": "0.05", "side": "sell"})
r = b.close("测试平仓")
check("平仓成功，盈亏取自交易所记录", r["ok"] and r["trade"]["pnl_src"] == "交易所记录", f"pnl={r.get('pnl'):+.4f}")
check("自己的残留止损单已撤，手动挂的不碰", [a["algoId"] for a in fx.algos] == ["MANUAL"],
      f"剩余 {[a['algoId'] for a in fx.algos]}")
check("已实现盈亏进账本", abs(b.ledger["realized"] - r["pnl"]) < 1e-12 and b.ledger["position"] is None)

# 可用保证金被别的仓位占光时启动
fx = FakeOkx()
fx.avail = 0.0256
b = new_broker(fx, "c0")
check("可用保证金只剩零头 → 不把零头记成本金", b.ledger["base_capital"] == 0 and b.equity() <= 0,
      next((m for tg, m in reversed(LOGS) if tg == "WARN"), "")[:60])
fx.avail = 4.0
fx.advance(301)
check("腾出保证金后自动重新读取本金", abs(b.equity() - 4.0) < 1e-9, f"{b.equity()}")

# 手动仓位隔离
fx = FakeOkx()
fx.position = {"sz": 5.0, "avg": 0.06, "open_t": fx.t}      # 你手动开的 5 张多单
fx._margin = 0.0
b = new_broker(fx, "c2")
check("手动开的仓不认领", b.get_position() is None and b.foreign_position() is not None)
r = b.close("熔断强平")
check("平仓指令不会平掉手动仓", (not r["ok"]) and fx.n("close_position") == 0 and fx.position is not None, f"{r}")

# 双向持仓模式下的空单
fx = FakeOkx()
fx.pos_mode = "long_short_mode"
fx.manual_long_short = {"pos": "5", "posSide": "short", "avgPx": "0.06", "upl": "0", "posId": "9"}
b = new_broker(fx, "c3")
b.get_position()
check("双向持仓模式：空单识别为空（原先看正负号会当成多）", b.foreign_position()["side"] == -1 and b.pos_mode == "long_short")

# 止损没挂上 → 补挂
fx = FakeOkx()
fx.drop_attached_stop = True
b = new_broker(fx, "c4")
r = b.open(1, 0.07, 0.0585)
check("附带止损没挂上 → 自动补挂独立止损单", r["ok"] and fx.n("place_stop_algo") == 1 and len(fx.algos) == 1, f"{r}")

# 止损挂不上 → 不留裸仓
fx = FakeOkx()
fx.drop_attached_stop = True
fx.fail_stop_algo = True
b = new_broker(fx, "c5")
r = b.open(1, 0.07, 0.0585)
check("止损怎么都挂不上 → 立即平仓，不留裸仓", (not r["ok"]) and fx.position is None and b.ledger["position"] is None,
      r.get("msg", ""))

# 断网：订单其实成交了
fx = FakeOkx()
fx.net_break_after_fill = True
b = new_broker(fx, "c6")
r = b.open(1, 0.07, 0.0585)
check("回包断网但已成交 → 查单认领，只下 1 次单", r["ok"] and fx.n("place_order") == 1 and b.get_position() is not None, f"{r}")

# 断网：订单没发出去
fx = FakeOkx()
fx.net_break_before_send = True
b = new_broker(fx, "c7")
r = b.open(1, 0.07, 0.0585)
check("请求没发出去 → 确认无单，在途记录清掉", (not r["ok"]) and b.ledger["pending_open"] is None and fx.position is None,
      r.get("msg", ""))

# 交易所止损触发
fx = FakeOkx()
b = new_broker(fx, "c8")
b.open(1, 0.07, 0.0585)
fx.hide_positions = True
fx.advance(3)
p = b.get_position()
check("刚开仓几秒查不到持仓 → 不误判为已平", p is not None and p["unconfirmed"] and b.ledger["position"] is not None)
fx.hide_positions = False
b.get_position()
fx.advance(60)
fx.px = 0.0584
ex_pnl = fx.fire_exchange_stop()
b.get_position()
fx.advance(20)
p = b.get_position()
check("交易所止损触发 → 账本按交易所记录结算", p is None and abs(b.ledger["realized"] - ex_pnl) < 1e-12,
      f"已实现 {b.ledger['realized']:+.4f} USDT，策略权益 {b.equity():.4f}")


# ══════════════════════════ D. 主循环 ══════════════════════════
print("\n【D】Trader：不重放 / 信号保鲜 / 崩溃不重复下单 / 对账 / 本地止损守护")


class Scripted:
    """脚本策略：指定哪根 bar 出什么信号。"""
    warmup = 10
    params = {}

    def __init__(self):
        self.script = {}

    def signals(self, candles):
        out = []
        for i, cd in enumerate(candles):
            s = self.script.get(cd["ts"])
            if s:
                out.append(Action(i, cd["ts"], s[0], cd["c"], s[1], "脚本信号", 0.0005))
        return out


CFG = {
    "inst_id": INST["instId"], "bar": "15m", "strategy": "trend", "strategy_params": {},
    "risk": {"risk_pct": 3.0, "max_leverage": 20.0, "max_daily_loss_pct": 8.0, "max_drawdown_pct": 50.0},
    "paper": {"initial_equity": 1000.0, "fee_pct": 0.0005, "slip_pct": 0.0002},
    "live": {"lever": 20, "td_mode": "cross", "pos_mode": "net", "dry_run": False, "allow_short": True},
    "loop": {"poll_seconds": 20, "max_runtime_hours": 0},
}


def new_trader(fake, strat, sub):
    d = os.path.join(TMP, sub)
    trader_mod.build_strategy = lambda name, **kw: strat
    t = trader_mod.Trader(CFG, mode="live", dry_run=False, client=fake, data_dir=d)
    t.broker._sleep = fake.advance
    return t


LAST = T0 - BAR                    # 启动时「最后一根已收盘 bar」的时间戳

# D1 首次启动不重放
fx, st = FakeOkx(), Scripted()
st.script = {T0 - 30 * BAR: ("enter_long", 0.0585), T0 - 20 * BAR: ("exit", None), T0 - 10 * BAR: ("enter_long", 0.0585)}
t = new_trader(fx, st, "d1")
t.run_once()
check("首次启动：历史信号一个都不执行", fx.n("place_order") == 0 and fx.position is None)
check("状态文件按通道命名", os.path.exists(os.path.join(TMP, "d1", "live-real_loop_state.json")), t.tag)

# D2 新鲜信号只下一次；开仓后不误熔断
fx, st = FakeOkx(), Scripted()
st.script = {LAST: ("enter_long", 0.0585)}
t = new_trader(fx, st, "d2")
t.run_once()
check("最后一根 bar 的新信号 → 下单 1 次", fx.n("place_order") == 1 and fx.position is not None,
      f"{fx.calls[-1] if fx.calls else ''}")
fx.advance(20)
t.run_once()
t.run_once()
check("后续轮询不重复下单", fx.n("place_order") == 1)
check("开仓后不被「保证金占用」误判熔断强平", not t.risk.halted() and fx.position is not None,
      f"可用保证金跌了 {(4-fx.avail)/4*100:.0f}%（日亏线 8%），策略权益 {t.broker.equity():.4f}")

# D9 本地止损守护（接着 D2 的仓位）：价格击穿止损而交易所止损单没动作
fx.px = 0.0583
t.run_once()
check("实盘本地止损守护生效：击穿即平", fx.position is None and t.broker.ledger["position"] is None,
      f"已实现 {t.broker.ledger['realized']:+.4f}")

# D3 过期信号
fx, st = FakeOkx(), Scripted()
st.script = {LAST: ("enter_long", 0.0585)}
fx.advance(600)                    # bar 收盘已过 10 分钟才启动
t = new_trader(fx, st, "d3")
t.run_once()
check("bar 收盘 10 分钟后才看到的开仓信号 → 放弃", fx.n("place_order") == 0,
      next((m for tg, m in reversed(LOGS) if tg == "SKIP"), ""))

# D3b 价格跑远
fx, st = FakeOkx(), Scripted()
st.script = {LAST: ("enter_long", 0.0585)}
fx.px = 0.0608                     # 信号价 0.06，ATR 0.0005，已偏 1.6 倍
t = new_trader(fx, st, "d3b")
t.run_once()
check("现价偏离信号价超过 1 倍 ATR → 放弃", fx.n("place_order") == 0)

# D4 崩溃在「落盘之后、下单之前/之中」
fx, st = FakeOkx(), Scripted()
st.script = {LAST: ("enter_long", 0.0585)}
t = new_trader(fx, st, "d4")


def crash(*a, **k):
    raise RuntimeError("模拟进程崩溃")


t.broker.open = crash
try:
    t.run_once()
except RuntimeError:
    pass
t = new_trader(fx, st, "d4")       # 重启
t.run_once()
check("崩溃重启后同一个信号不会再下单", fx.n("place_order") == 0 and t.state["last_action_ts"] == LAST)

# D5 崩溃在「下单成功之后」：重启后认领，不重复
fx, st = FakeOkx(), Scripted()
st.script = {LAST: ("enter_long", 0.0585)}
fx.net_break_after_fill = True
t = new_trader(fx, st, "d5")
t.broker._order_outcome = lambda cl, tries=6: ("unknown", None)     # 查单也查不了
t.run_once()
t = new_trader(fx, st, "d5")       # 重启
fx.advance(20)
t.run_once()
p = t.broker.get_position()
check("下单后失联 → 重启按在途记录认领，止损价在，不重复下单",
      fx.n("place_order") == 1 and p is not None and p["stop"] == 0.0585)

# D6 离场 + 对账
fx, st = FakeOkx(), Scripted()
st.script = {LAST: ("enter_long", 0.0585)}
t = new_trader(fx, st, "d6")
t.run_once()
fx.t = T0 + BAR + 5000             # 又收了一根 bar，这根出离场信号
st.script[T0] = ("exit", None)
fx.close_fail_once = True          # 第一次平仓请求失败
t.run_once()
check("离场时平仓请求失败 → 同一轮对账立刻补平", fx.n("close_position") == 2 and fx.position is None
      and t.broker.ledger["position"] is None, f"平仓请求共 {fx.n('close_position')} 次（第 1 次失败）")

# D8 有手动仓位时不开新仓
fx, st = FakeOkx(), Scripted()
fx.position = {"sz": 5.0, "avg": 0.06, "open_t": fx.t}
fx._margin = 0.0
st.script = {LAST: ("enter_long", 0.0585)}
t = new_trader(fx, st, "d8")
t.run_once()
check("合约上有手动仓位 → 不开新仓、不动它", fx.n("place_order") == 0 and fx.n("close_position") == 0
      and fx.position["sz"] == 5.0)

# D11 模拟盘（paper）通道：浮盈亏要跟着行情走
fx, st = FakeOkx(), Scripted()
st.script = {LAST: ("enter_long", 0.0585)}
trader_mod.build_strategy = lambda name, **kw: st
t = trader_mod.Trader(CFG, mode="paper", client=fx, data_dir=os.path.join(TMP, "d11"))
t.run_once()
pp = t.broker.get_position()
eq0 = t.broker.equity()
fx.px = 0.0594
fx.advance(20)
t.run_once()
eq1 = t.broker.equity()
check("模拟盘：开仓并且权益随行情变动（原先浮盈亏冻结在开仓那一刻）",
      pp is not None and eq1 < eq0 - 1e-9, f"{pp and pp['size']} 张，权益 {eq0:.2f} → {eq1:.2f}")

# D10 单实例锁
lk = os.path.join(TMP, "x.lock")
h1 = trader_mod.acquire_lock(lk)
try:
    trader_mod.acquire_lock(lk)
    second = "拿到了（不应该）"
except RuntimeError:
    second = "被拒"
h1.close()
check("同一通道第二个进程被拒绝启动", second == "被拒", second)


# ══════════════════════════ E. 回测引擎 ══════════════════════════
print("\n【E】engine：止损与强平的先后 / 跳空 / 假盈利 / 资金费 / 浮亏熔断")
OLD = None
old_path = os.path.join(HERE, "_备份_审查前_20261116", "engine.py")
if os.path.exists(old_path):
    spec = importlib.util.spec_from_file_location("engine_before_review", old_path)
    OLD = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(OLD)


class OneShot:
    warmup = 10

    def __init__(self, idx, stop, kind="enter_long"):
        self.idx, self.stop, self.kind = idx, stop, kind

    def signals(self, cds):
        return [Action(self.idx, cds[self.idx]["ts"], self.kind, cds[self.idx]["c"], self.stop, "桩")]


def flat(n, step, px=100.0, base=1_799_971_200_000):      # base 是 UTC 0 点
    return [{"ts": base + i * step, "o": px, "h": px * 1.0005, "l": px * 0.9995, "c": px, "v": 1.0,
             "confirm": True} for i in range(n)]


E_INST = {"instId": "T", "ctVal": "1", "lotSz": "0.01", "minSz": "0.01", "maxSz": "1000000"}


def ecfg(**kw):
    c0 = {"initial_equity": 1000.0, "fee_pct": 0.0005, "slip_pct": 0.0002, "risk_pct": 1.0,
          "max_leverage": 20.0, "leverage": 20.0, "max_daily_loss_pct": 100.0, "max_drawdown_pct": 100.0,
          "bar": "15m"}
    c0.update(kw)
    return c0


def run_both(strat, cds, cfg):
    new = Backtester(strat, cds, E_INST, cfg).run()
    old = OLD.Backtester(strat, cds, E_INST, cfg).run() if OLD else None
    return new, old


# E1 止损 2%、20x 强平线 4.6%，一根长下影同时扫过两者
cds = flat(60, BAR)
cds[30].update(l=90.0, c=92.0)
new, old = run_both(OneShot(20, 98.0), cds, ecfg())
t1 = new["trade_list"][0]
check("止损在强平线以内：判止损，不判爆仓", t1["reason"] == "止损" and abs(t1["exit_px"] - 98.0 * 0.9998) < 1e-9,
      f"新：{t1['reason']} 亏 {t1['pnl']:.2f}" + (f" ｜ 改前：{old['trade_list'][0]['reason']} 亏 {old['trade_list'][0]['pnl']:.2f}" if old else ""))

# E2 止损 6% 比强平线还远
new, old = run_both(OneShot(20, 94.0), cds, ecfg())
check("止损比强平线还远：判爆仓", "爆仓" in new["trade_list"][0]["reason"], new["trade_list"][0]["reason"])

# E3 跳空开在止损位之外
cds = flat(60, BAR)
for k in range(30, 60):
    cds[k].update(o=96.0, h=96.05, l=95.95, c=96.0)
new, old = run_both(OneShot(20, 98.0), cds, ecfg())
t3 = new["trade_list"][0]
check("跳空越过止损位：按开盘价成交，不是按止损价", abs(t3["exit_px"] - 96.0 * 0.9998) < 1e-9,
      f"新：出场 {t3['exit_px']:.2f} 亏 {t3['pnl']:.2f}" + (f" ｜ 改前：出场 {old['trade_list'][0]['exit_px']:.2f} 亏 {old['trade_list'][0]['pnl']:.2f}" if old else ""))

# E4 信号后开盘就跳到止损位下方
cds = flat(60, BAR)
for k in range(21, 60):
    cds[k].update(o=97.0, h=97.05, l=96.95, c=97.0)
new, old = run_both(OneShot(20, 98.0), cds, ecfg())
check("开盘价已在止损位之外：不开仓", new["trades"] == 0,
      "新：0 笔" + (f" ｜ 改前：{old['trades']} 笔，记了 {old['trade_list'][0]['pnl']:+.2f} 的假盈利" if old and old["trades"] else ""))

# E5 资金费
H4 = 4 * 3600 * 1000
cds = flat(60, H4)
new = Backtester(OneShot(20, 90.0), cds, E_INST, ecfg(bar="4H", funding_pct_8h=0.0001)).run()
t5 = new["trade_list"][0]
n_settle = sum(1 for i in range(22, 60) if cds[i]["ts"] // (2 * H4) != cds[i - 1]["ts"] // (2 * H4))
expect = n_settle * 100.0 * t5["size"] * 0.0001
check("资金费按 8 小时结算次数扣", abs(new["funding_total"] - expect) < 1e-9 and expect > 0,
      f"持仓跨 {n_settle} 次结算，扣 {new['funding_total']:.4f} USDT")
new0 = Backtester(OneShot(20, 90.0), cds, E_INST, ecfg(bar="4H")).run()
check("不配置资金费时结果不变", new0["funding_total"] == 0 and new0["final_equity"] > new["final_equity"])

# E6 浮亏触发日亏熔断 → 强平（和实盘一致）
cds = flat(60, BAR)
for k in range(25, 60):
    cds[k].update(o=93.0, h=93.05, l=92.95, c=93.0)
cds[25].update(o=100.0, h=100.0)
new, old = run_both(OneShot(20, 90.0), cds, ecfg(risk_pct=5.0, max_daily_loss_pct=3.0,
                                                 max_leverage=5.0, leverage=5.0))
t6 = new["trade_list"][0]
check("持仓浮亏超过日亏线 → 熔断强平", t6["reason"] == "熔断强平",
      f"新：{t6['reason']} 亏 {t6['pnl']:.2f}" + (f" ｜ 改前：{old['trade_list'][0]['reason']}（浮亏不触发熔断）" if old else ""))

print("\n" + "═" * 60)
if fails:
    print(f"  {FAIL} 失败 {len(fails)} 项：{fails}")
    sys.exit(1)
print(f"  {PASS} 实盘安全验证全部通过（离线，未连接交易所）")
print("═" * 60)
