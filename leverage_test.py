# -*- coding: utf-8 -*-
"""
杠杆压力测试 —— 回答交易员最关心的问题：100x 到底能不能玩？

关键：高杠杆不放大收益，只缩小强平距离。
  仓位大小由「风险预算 / 止损距离」决定，跟杠杆无关。
  杠杆唯一的作用是决定「要占多少保证金」和「离爆仓多远」。
  所以：杠杆越高 → 保证金越省 → 但强平线越近。
  当「强平距离 < 止损距离」时，止损永远等不到，先爆仓。
"""
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from engine import Backtester
from okx_client import OkxClient
from strategy import TrendPulse

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BAR = sys.argv[1] if len(sys.argv) > 1 else "15m"
BARS = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
PARAMS = {"fast": 21, "slow": 34, "regime_len": 200, "adx_min": 28.0,
          "atr_mult": 2.5, "trail_mult": 5.0, "max_ext_atr": 3.0}
FEE = 0.0005
MMR = 0.004

cpath = os.path.join(DATA, f"candles_DOGE-USDT-SWAP_{BAR}_{BARS}.json")
if not os.path.exists(cpath):
    print(f"没有 {cpath}")
    sys.exit(1)
d = json.load(open(cpath, encoding="utf-8"))

cli = OkxClient(timeout=30)
inst = cli.instrument("DOGE-USDT-SWAP")

print("═" * 106)
print(f"  杠杆压力测试   DOGE-USDT-SWAP {BAR}  {len(d)} 根   参数 adx28/atr2.5")
print(f"  仓位由「风险3% / 止损2.5×ATR」决定 → 与杠杆无关；杠杆只决定保证金占用和强平距离")
print("═" * 106)
print(f"  {'杠杆':<7}{'强平距离':>10}{'收益%':>10}{'回撤%':>9}{'夏普':>8}"
      f"{'交易':>6}{'爆仓次数':>10}{'爆仓占比':>10}{'胜率%':>8}{'爆仓亏U':>10}")
print("  " + "─" * 102)

rows = []
for lev in [3, 5, 10, 20, 50, 75, 100]:
    cfg = {
        "initial_equity": 1000.0, "fee_pct": FEE, "slip_pct": 0.0002, "risk_pct": 3.0,
        "max_leverage": float(lev), "leverage": float(lev), "mmr": MMR,
        "max_daily_loss_pct": 100.0, "max_drawdown_pct": 100.0, "bar": BAR,
    }
    r = Backtester(TrendPulse(**PARAMS), d, inst, cfg).run()
    liq = r["liquidated"]
    liq_loss = sum(t["pnl"] for t in r["trade_list"] if "爆仓" in (t.get("reason") or ""))
    dist = (1.0 / lev - MMR) * 100
    rows.append((lev, dist, r, liq, liq_loss))
    print(f"  {lev}x{'':<4}{dist:>9.2f}%{r['total_return_pct']:>+10.2f}{r['max_drawdown_pct']:>9.2f}"
          f"{r['sharpe']:>8.2f}{r['trades']:>6}{liq:>10}{liq/max(1,r['trades'])*100:>9.1f}%"
          f"{r['win_rate_pct']:>8.2f}{liq_loss:>10.2f}")

print("  " + "─" * 102)

base = rows[0][2]["total_return_pct"]
print("\n  【解读】")
print(f"    3x 基准收益 {base:+.2f}%")
for lev, dist, r, liq, ll in rows[1:]:
    delta = r["total_return_pct"] - base
    tag = ""
    if liq > 0:
        tag = f"  ← 爆仓 {liq} 次，比 3x 少赚 {-delta:.2f} 个百分点"
    print(f"    {lev:>3}x  强平距离 {dist:>5.2f}%  收益 {r['total_return_pct']:>+8.2f}%"
          f"（相对 3x {delta:>+8.2f}%）{tag}")

# 找出「止损距离」和「安全杠杆上限」
stop_pct_estimate = None
if rows and rows[0][2]["trade_list"]:
    ts = [t for t in rows[0][2]["trade_list"] if t["stop"]]
    if ts:
        rs = [abs(t["entry_px"] - t["stop"]) / t["entry_px"] * 100 for t in ts if t["entry_px"]]
        stop_pct_estimate = sum(rs) / len(rs)
if stop_pct_estimate:
    safe_lev = 1.0 / (stop_pct_estimate / 100.0 + MMR)
    print(f"\n  【关键结论】")
    print(f"    本策略平均止损距离 {stop_pct_estimate:.3f}%")
    print(f"    要让「止损先于强平触发」，杠杆必须 < {safe_lev:.0f}x")
    print(f"    （公式：1 / (止损% + 维持保证金率)）")
    print(f"    → 超过 {safe_lev:.0f}x，止损就形同虚设，仓位会在止损前爆掉。")

print("\n" + "═" * 106)
