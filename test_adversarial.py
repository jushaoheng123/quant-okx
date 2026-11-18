# -*- coding: utf-8 -*-
"""
对重构后 LiveBroker 的对抗测试 —— 专挑异常路径。
对方自己的 verify_live_safety.py 测的是「正常流程」，这里专测「不正常流程」。

攻击点：
  A. 止损单查询接口一直失败（返回 None）→ 会不会留着没确认止损的仓位裸奔？
  B. 查单接口抛非 OkxError 异常 → 会不会把成交的订单误判成"未成交"？
  C. 交易所上有多笔持仓时，_stop_on_exchange 会不会把别人的止损当成自己的？
  D. 账本里有仓、交易所 8 秒查不到（API 抖动）→ 会不会误结算导致重复开仓？
"""
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import broker as B
from okx_client import OkxError

PASS, FAIL, WARN = "✓", "✗", "⚠"
fails, warns = [], []


def check(name, cond, detail="", warn_only=False):
    if cond:
        print(f"  {PASS} {name}  {detail}")
    elif warn_only:
        print(f"  {WARN} {name}  {detail}")
        warns.append(name)
    else:
        print(f"  {FAIL} {name}  {detail}")
        fails.append(name)


class FakeClient:
    """可控的假交易所"""
    def __init__(self):
        self.pos = []
        self.algo_pending_ret = []
        self.algo_query_raises = False
        self.orders = {}
        self.order_raises = None
        self.placed = []
        self.closed = 0
        self.now = 1700000000000

    def now_ms(self):
        return self.now

    def account_config(self):
        return {"posMode": "net_mode"}

    def set_leverage(self, *a, **k):
        return [{"sCode": "0"}]

    def balance(self, ccy=None):
        return [{"details": [{"ccy": "USDT", "availBal": "10.0", "availEq": "10.0", "cashBal": "10.0"}]}]

    def equity_usdt(self):
        return 10.0

    def ticker(self, inst_id):
        return {"last": "100.0", "bidPx": "99.9", "askPx": "100.1"}

    def positions(self, inst_id=None):
        return list(self.pos)

    def algo_pending(self, inst_id, ot):
        if self.algo_query_raises:
            raise OkxError("NET", "查询超时")
        return list(self.algo_pending_ret)

    def order(self, inst_id, cl_ord_id=None, ord_id=None):
        if self.order_raises:
            raise self.order_raises
        return self.orders.get(cl_ord_id)

    def place_order(self, inst_id, side, sz, **kw):
        self.placed.append({"side": side, "sz": sz, **kw})
        self.orders[kw.get("cl_ord_id")] = {
            "ordId": "123", "avgPx": "100.0", "accFillSz": str(sz), "state": "filled",
            "clOrdId": kw.get("cl_ord_id"),
        }
        self.pos = [{"instId": inst_id, "pos": str(sz) if side == "buy" else str(-float(sz)),
                     "avgPx": "100.0", "upl": "0", "posId": "P1", "mgnMode": "cross",
                     "posSide": "net"}]
        return [{"sCode": "0", "ordId": "123"}]

    def place_stop_algo(self, inst_id, side, trigger, **kw):
        self.algo_pending_ret.append({"algoClOrdId": kw.get("algo_cl_ord_id"), "algoId": "A1",
                                      "slTriggerPx": str(trigger), "side": side})
        return [{"sCode": "0", "algoId": "A1"}]

    def cancel_algos(self, inst_id, ids):
        return [{"sCode": "0"}]

    def close_position(self, inst_id, mgn, pos_side=None):
        self.closed += 1
        self.pos = []
        return [{"sCode": "0"}]

    def positions_history(self, inst_id, limit=10):
        return [{"uTime": str(self.now), "cTime": str(self.now - 5000),
                 "realizedPnl": "-0.5", "closeAvgPx": "99.5"}]


def make_broker(cli, **kw):
    inst = {"instId": "TEST-USDT-SWAP", "ctVal": "0.01", "lotSz": "0.01",
            "minSz": "0.01", "tickSz": "0.1"}
    logs = []
    br = B.LiveBroker(cli, "TEST-USDT-SWAP", inst, pos_mode="net", td_mode="cross",
                      lever=10, dry_run=False, ledger_path=None,
                      capital_usdt=10.0, log=lambda m, t="INFO": logs.append(f"[{t}] {m}"),
                      sleep=lambda s: None)
    return br, logs


print("═" * 90)
print("  LiveBroker 对抗测试（专挑异常路径）")
print("═" * 90)

# ══════════ A. 止损查询一直失败 → 会不会裸奔？ ══════════
print("\n【A】止损单查询接口一直失败（返回 None）")
cli = FakeClient()
cli.algo_query_raises = True
cli.pos = []
br, logs = make_broker(cli)
r = br.open(1, 0.01, stop=99.0, reason="测试")
print(f"    开仓结果 ok={r.get('ok')}  stop_on_exchange={r.get('stop_on_exchange')}")
print(f"    是否触发了平仓: closed={cli.closed}")
if r.get("ok") and r.get("stop_on_exchange") is None and cli.closed == 0:
    check("止损查询失败时仍持仓且不平仓", False,
          "★ 查询失败(None) 被当成「可能已挂」，仓位继续持有 → 交易所上可能真的没止损",
          warn_only=True)
else:
    check("止损查询失败时的处理", True, f"stop_on_exchange={r.get('stop_on_exchange')} closed={cli.closed}")

# ══════════ B. 查单抛非 OkxError 异常 ══════════
print("\n【B】查单接口抛非 OkxError 异常（订单其实成交了）")
cli = FakeClient()
br, logs = make_broker(cli)
# 先让 place_order 成功入账，然后 order() 抛异常
_orig_order = cli.order
def bad_order(inst_id, cl_ord_id=None, ord_id=None):
    raise ValueError("json 解析炸了")   # 非 OkxError
cli.order = bad_order
r = br.open(1, 0.01, stop=99.0, reason="测试")
print(f"    开仓结果: ok={r.get('ok')} msg={r.get('msg')}")
print(f"    账本 pending_open = {br.ledger.get('pending_open') is not None}")
print(f"    账本 position     = {br.ledger.get('position') is not None}")
check("非 OkxError 异常被吞后仍能识别成交", br.ledger.get("position") is not None,
      f"position={br.ledger.get('position') is not None}, pending={br.ledger.get('pending_open') is not None}",
      warn_only=True)

# ══════════ C. 多持仓时止损归属误判 ══════════
print("\n【C】合约上有多笔持仓，其中一笔是手动仓且挂了止损")
cli = FakeClient()
br, logs = make_broker(cli)
cli.pos = [
    {"instId": "TEST-USDT-SWAP", "pos": "0.05", "avgPx": "100.0", "upl": "0", "posId": "MANUAL",
     "mgnMode": "cross", "posSide": "net",
     "closeOrderAlgo": [{"slTriggerPx": "90.0"}]},     # 手动仓自带的止损
]
st = br._stop_on_exchange(1)
print(f"    _stop_on_exchange(多) = {st}   （该合约上只有手动仓的止损，本策略没有仓）")
check("不会把手动仓的止损当成自己的", st is False,
      f"返回 {st}；若为 True 说明看错归属 → 本策略会以为有止损而裸奔", warn_only=True)

# ══════════ D. 8 秒查不到就结算 ══════════
print("\n【D】账本有仓、交易所 8 秒查不到（API 抖动）")
cli = FakeClient()
br, logs = make_broker(cli)
cli.pos = [{"instId": "TEST-USDT-SWAP", "pos": "0.01", "avgPx": "100.0", "upl": "0",
            "posId": "P1", "mgnMode": "cross", "posSide": "net"}]
cli.algo_pending_ret = [{"algoClOrdId": "x", "algoId": "A1", "slTriggerPx": "99.0", "side": "sell"}]
r = br.open(1, 0.01, stop=99.0, reason="测试")
print(f"    开仓后 position 在账本: {br.ledger.get('position') is not None}")
# 模拟交易所 API 抖动：仓位查不到
cli.pos = []
pos1 = br.get_position()          # 第 1 次查不到 → 应返回 unconfirmed
print(f"    第1次查不到 → returned={pos1 is not None}, unconfirmed={pos1.get('unconfirmed') if pos1 else None}")
cli.now += 9000                    # 过 9 秒
pos2 = br.get_position()
print(f"    9 秒后仍查不到 → returned={pos2}, 账本position={br.ledger.get('position') is not None}")
print(f"    已结算的 trades 数 = {len(br.ledger['trades'])}")
check("API 抖动 9 秒会触发结算（存在误判风险）", True,
      f"结算了 {len(br.ledger['trades'])} 笔；若真实仓位还在，下一轮会被当 foreign 仓 → 不再管理",
      warn_only=True)

# ══════════ E. 正常路径回归 ══════════
print("\n【E】正常路径回归（确认没被改坏）")
cli = FakeClient()
br, logs = make_broker(cli)
r = br.open(1, 0.01, stop=99.0, reason="测试")
check("正常开仓成功", r.get("ok"), f"fill={r.get('fill_px')} stop={r.get('stop')}")
eq = br.equity()
check("策略权益 = 本金+已实现+浮盈亏", abs(eq - 10.0) < 1e-6, f"{eq:.4f}")
p = br.get_position()
check("持仓带止损价", p and p.get("stop") == 99.0, f"stop={p.get('stop') if p else None}")
c = br.close("测试平仓")
check("平仓成功并记账", c.get("ok"), f"pnl={c.get('pnl')}, 来源={c.get('trade',{}).get('pnl_src')}")
check("平仓后权益已更新", abs(br.equity() - 9.5) < 1e-6, f"{br.equity():.4f}")

print("\n" + "═" * 90)
if fails:
    print(f"  {FAIL} 硬失败 {len(fails)} 项：{fails}")
print(f"  {WARN} 需要关注的边界问题 {len(warns)} 项：{warns}")
if not fails:
    print(f"  {PASS} 没有硬失败 —— 正常路径正确，边界问题见上")
print("═" * 90)
