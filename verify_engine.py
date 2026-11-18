# -*- coding: utf-8 -*-
"""
风控正确性验证 —— 用合成行情证明：
  1) 止损真的会触发
  2) 单笔亏损 ≈ 设定的 risk_pct（不多不少）
  3) 仓位换算（风险金额 → 合约张数）算得对
  4) 熔断（日亏 / 回撤）真的会拦
任何一条不过 = 不许上实盘。
"""
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from engine import Backtester
from risk import RiskManager, round_to
from strategy import TrendPulse

PASS, FAIL = "✓", "✗"
fails = []


def check(name, cond, detail=""):
    if cond:
        print(f"  {PASS} {name}  {detail}")
    else:
        print(f"  {FAIL} {name}  {detail}")
        fails.append(name)


# ═══════════ 1. 仓位换算 ═══════════
print("\n【1】仓位换算：风险金额 → 合约张数")
rm = RiskManager(risk_per_trade_pct=1.0, max_leverage=3.0)
# 权益 1000，每笔风险 1% = 10 USDT
# 入场 60000，止损 58000（跌 2000），ctVal=0.01 BTC/张
# 每张亏 2000*0.01 = 20 USDT → 10/20 = 0.5 张
dec = rm.size_position(1000.0, 60000.0, 58000.0, 0.01, 0.01, 0.01)
check("张数计算", abs(dec.size - 0.5) < 1e-9, f"期望 0.5 张，实际 {dec.size} 张")
check("实际风险金额", abs(dec.size * 2000 * 0.01 - 10.0) < 1e-9,
      f"期望 10 USDT，实际 {dec.size*2000*0.01:.4f}")
check("名义价值", abs(dec.notional - 0.5 * 0.01 * 60000) < 1e-6, f"{dec.notional:.2f} USDT")
check("杠杆", abs(dec.leverage - 0.3) < 1e-9, f"{dec.leverage:.4f}x")

# 杠杆上限测试：止损极近 → 张数会被杠杆上限砍
dec2 = rm.size_position(1000.0, 60000.0, 59900.0, 0.01, 0.01, 0.01)
# 无杠杆上限时：10/(100*0.01)=10 张 → 名义 6000 = 6x，超过 3x 上限
check("杠杆上限生效", dec2.leverage <= 3.0001, f"{dec2.leverage:.4f}x ≤ 3.0x")
check("杠杆上限砍张数", dec2.size < 10 or not dec2.allow, f"张数 {dec2.size}")

# 止损太近 → 拒单
dec3 = rm.size_position(1000.0, 60000.0, 59999.0, 0.01, 0.01, 0.01)
check("止损过近被拒", not dec3.allow, dec3.reason)

# 资金太小 → 不足最小下单量
dec4 = RiskManager(risk_per_trade_pct=0.01).size_position(10.0, 60000.0, 58000.0, 0.01, 0.01, 0.01)
check("低于最小下单量被拒", not dec4.allow, dec4.reason)

# ═══════════ 2. 熔断 ═══════════
print("\n【2】熔断：日亏损 / 最大回撤")
rm2 = RiskManager(risk_per_trade_pct=1.0, max_daily_loss_pct=3.0, max_drawdown_pct=15.0)
rm2.update_equity(1000.0)
check("正常权益不熔断", not rm2.halted())
rm2.update_equity(980.0)   # 峰值 1000，回撤 2%，日亏 2% → 不熔断
check("亏 2% 不熔断", not rm2.halted(), f"回撤 {((1000-980)/1000*100):.2f}%")
rm2.update_equity(970.0)   # 日亏 3% → 熔断
check("日亏 3% 触发熔断", rm2.halted(), rm2.halt_reason())
rm2.state["day"] = "1999-01-01"   # 假装跨日
rm2.update_equity(970.0)
check("跨日自动解除日亏熔断", not rm2.halted(), rm2.halt_reason())

rm3 = RiskManager(risk_per_trade_pct=1.0, max_daily_loss_pct=99.0, max_drawdown_pct=15.0)
rm3.update_equity(1000.0)
rm3.update_equity(840.0)   # 回撤 16%
check("回撤 16% 触发熔断", rm3.halted(), rm3.halt_reason())
rm3.state["day"] = "1999-01-01"
rm3.update_equity(840.0)
check("回撤熔断跨日不解除", rm3.halted(), rm3.halt_reason())

# ═══════════ 3. 止损真的会打 ═══════════
print("\n【3】回测撮合：止损是否真的触发（用桩策略隔离被测逻辑）")
from strategy import Action


class StubStrategy:
    """只在一个位置发一次开仓，之后绝不主动离场 —— 把「止损」这一条路径单独隔离出来测。"""
    warmup = 50

    def __init__(self, enter_idx=100, stop_pct=0.03):
        self.enter_idx = enter_idx
        self.stop_pct = stop_pct

    def signals(self, candles):
        cd = candles[self.enter_idx]
        px = cd["c"]
        return [Action(self.enter_idx, cd["ts"], "enter_long", px, px * (1 - self.stop_pct), "桩：开多")]


# 合成行情：缓涨 300 根 → 一根断头铡刀暴跌 → 继续跌
candles = []
ts = 1700000000000
px = 60000.0
for i in range(300):
    o = px
    px = px * 1.001
    h = px * 1.0015
    l = o * 0.9985
    candles.append({"ts": ts + i * 14400000, "o": o, "h": h, "l": l, "c": px, "v": 100.0, "confirm": True})
crash_o = px
px = px * 0.85
candles.append({"ts": ts + 300 * 14400000, "o": crash_o, "h": crash_o * 1.001,
                "l": px, "c": px, "v": 900.0, "confirm": True})
for i in range(20):
    o = px
    px = px * 0.995
    candles.append({"ts": ts + (301 + i) * 14400000, "o": o, "h": o * 1.001,
                    "l": px * 0.999, "c": px, "v": 100.0, "confirm": True})

inst = {"instId": "TEST-USDT-SWAP", "ctVal": "0.01", "lotSz": "0.01",
        "minSz": "0.01", "tickSz": "0.1", "maxSz": "1000000"}
bt_cfg = {"initial_equity": 1000.0, "fee_pct": 0.0005, "slip_pct": 0.0002,
          "risk_pct": 1.0, "max_leverage": 3.0, "max_daily_loss_pct": 100.0,
          "max_drawdown_pct": 100.0, "bar": "4H"}

rep = Backtester(StubStrategy(enter_idx=100, stop_pct=0.03), candles, inst, bt_cfg).run()
check("产生了交易", rep["trades"] > 0, f"{rep['trades']} 笔")
stop_trades = [t for t in rep["trade_list"] if "止损" in (t["reason"] or "")]
check("止损单被触发", len(stop_trades) > 0,
      f"{len(stop_trades)} 笔止损，全部原因：{[t['reason'] for t in rep['trade_list']]}")

# 单笔亏损应 ≈ 1% 风险预算（10 USDT）；除非跳空穿价，那会超
if stop_trades:
    t = stop_trades[0]
    check("止损成交价 = 止损价（含滑点）", abs(t["exit_px"] - t["stop"] * (1 - 0.0002)) < 1e-6,
          f"出场 {t['exit_px']:.2f} vs 止损 {t['stop']:.2f}")
    risk_budget = rep["initial_equity"] * 1.0 / 100.0
    check("单笔亏损接近风险预算", -t["pnl"] <= risk_budget * 3,
          f"亏 {t['pnl']:.2f} USDT（预算 {risk_budget:.2f}，跳空可超）")

# 无止损的仓位应当被风控直接拒绝 —— 系统不允许「裸仓」存在
class NoStopStub(StubStrategy):
    def signals(self, cds):
        cd = cds[self.enter_idx]
        return [Action(self.enter_idx, cd["ts"], "enter_long", cd["c"], None, "桩：无止损")]


rep2 = Backtester(NoStopStub(enter_idx=100), candles, inst, bt_cfg).run()
check("无止损被风控拒绝开仓", rep2["trades"] == 0,
      f"无止损版本交易 {rep2['trades']} 笔，权益 {rep2['final_equity']:.2f}（强制止损 = 不允许裸仓）")

check("有净值曲线", len(rep["curve"]) == len(candles), f"{len(rep['curve'])} 点")
check("最大回撤被记录", rep["max_drawdown_pct"] >= 0, f"{rep['max_drawdown_pct']:.2f}%")

# ═══════════ 4. 边界 ═══════════
print("\n【4】边界条件")
check("round_to 向下取整", round_to(0.567, 0.01) == 0.56, f"{round_to(0.567, 0.01)}")
check("round_to 向上取整", round_to(0.561, 0.01, 'up') == 0.57, f"{round_to(0.561, 0.01, 'up')}")
try:
    Backtester(TrendPulse(), candles[:20], inst, bt_cfg).run()
    check("K线不足应报错", False, "没有报错")
except RuntimeError as e:
    check("K线不足报错", True, str(e)[:50])

print("\n" + "═" * 60)
if fails:
    print(f"  {FAIL} 失败 {len(fails)} 项：{fails}")
    sys.exit(1)
print(f"  {PASS} 全部风控验证通过 —— 可以上实盘")
print("═" * 60)
