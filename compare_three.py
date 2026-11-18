# -*- coding: utf-8 -*-
"""
BTC / ETH / SNDK 三标的横向对比 —— 同一套策略、同一段风险，看谁真有肉。

两套预设参数：
  A(快) fast16 slow26 adx28 atr2.5 trail5 ext3   ← 15m/1H 短周期用
  B(慢) fast16 slow48 adx18 atr2.5 trail5 ext6   ← 4H 用
统一 risk_pct=3%（只比策略质量，不掺资金约束），费率为 taker 0.05%。
另算每个标的「按交易员真实资金 2.06 USDT 能开多大 / 需要多少风险」。
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
FEE = 0.0005
EQUITY = 2.0619          # 交易员当前权益
RISK = 3.0

PRESETS = {
    "A快": {"fast": 16, "slow": 26, "regime_len": 200, "adx_min": 28.0,
            "atr_mult": 2.5, "trail_mult": 5.0, "max_ext_atr": 3.0},
    "B慢": {"fast": 16, "slow": 48, "regime_len": 200, "adx_min": 18.0,
            "atr_mult": 2.5, "trail_mult": 5.0, "max_ext_atr": 6.0},
}
INSTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SNDK-USDT-SWAP"]
BARS = ["4H", "1H"]

cli = OkxClient(timeout=30)
inst_meta = {}
for iid in INSTS:
    inst_meta[iid] = cli.instrument(iid)

print("═" * 122)
print(f"  BTC / ETH / SNDK 横向对比   单位本金 risk={RISK}%   费率 taker 0.05%")
print("═" * 122)

results = []
for iid in INSTS:
    for bar in BARS:
        path = None
        for cand in (f"candles_{iid}_{bar}_4000.json", f"candles_{iid}_{bar}_3000.json"):
            if os.path.exists(os.path.join(DATA, cand)):
                path = os.path.join(DATA, cand)
                break
        if not path:
            print(f"  {iid} {bar}: 无数据")
            continue
        d = json.load(open(path, encoding="utf-8"))
        if len(d) < 300:
            print(f"  {iid} {bar}: 仅 {len(d)} 根，跳过")
            continue
        for pname, params in PRESETS.items():
            cfg = {
                "initial_equity": 1000.0, "fee_pct": FEE, "slip_pct": 0.0002,
                "risk_pct": RISK, "max_leverage": 20.0, "leverage": 20.0, "mmr": 0.004,
                "max_daily_loss_pct": 100.0, "max_drawdown_pct": 100.0, "bar": bar,
            }
            try:
                r = Backtester(TrendPulse(**params), d, inst_meta[iid], cfg).run()
            except Exception as e:
                print(f"  {iid} {bar} {pname}: 失败 {str(e)[:40]}")
                continue
            results.append({
                "inst": iid, "bar": bar, "preset": pname, "bars": len(d),
                "ret": r["total_return_pct"], "ann": r["annualized_pct"],
                "dd": r["max_drawdown_pct"], "sharpe": r["sharpe"],
                "trades": r["trades"], "win": r["win_rate_pct"],
                "pf": r["profit_factor"], "days": r["days"],
            })

results.sort(key=lambda x: (x["sharpe"], x["ret"]), reverse=True)

print(f"\n  {'标的':<17}{'周期':<5}{'参数':<6}{'根数':>6}{'天数':>7}{'收益%':>9}{'年化%':>9}"
      f"{'回撤%':>8}{'夏普':>7}{'交易':>6}{'胜率%':>8}{'盈亏比':>8}")
print("  " + "─" * 116)
for r in results:
    print(f"  {r['inst']:<17}{r['bar']:<5}{r['preset']:<6}{r['bars']:>6}{r['days']:>7.0f}"
          f"{r['ret']:>+9.2f}{r['ann']:>+9.2f}{r['dd']:>8.2f}{r['sharpe']:>7.2f}"
          f"{r['trades']:>6}{r['win']:>8.2f}{r['pf']:>8.2f}")

# ── 按交易员真实资金算可行性 ──
print("\n" + "═" * 122)
print(f"  按交易员真实权益 {EQUITY} USDT 算：每个标的最少要多少风险才开得出最小一单")
print("═" * 122)
print(f"  {'标的':<17}{'最小单止损亏':>14}{'占权益%':>10}{'20x保证金':>12}{'可用需≥':>10}   判定")
print("  " + "─" * 100)
from indicators import atr
for iid in INSTS:
    try:
        inst = inst_meta[iid]
        cs = [x for x in cli.candles(iid, "4H", 200) if x["confirm"]]
        a = atr([x["h"] for x in cs], [x["l"] for x in cs], [x["c"] for x in cs], 14)[-1]
        px = float(cli.ticker(iid)["last"])
        ct = float(inst["ctVal"])
        mn = float(inst["minSz"])
        loss = mn * ct * a * 2.5
        margin = mn * ct * px / 20
        pct = loss / EQUITY * 100
        ok = "✓ 资金够" if margin <= EQUITY else "✗ 保证金不够"
        print(f"  {iid:<17}{loss:>14.4f}{pct:>9.2f}%{margin:>12.4f}{margin:>10.4f}   {ok}")
    except Exception as e:
        print(f"  {iid:<17} 失败 {str(e)[:40]}")

print("\n" + "═" * 122)
if results:
    best = results[0]
    print(f"  ★ 综合最优：{best['inst']}  {best['bar']}  参数{best['preset']}")
    print(f"    收益 {best['ret']:+.2f}% / {best['days']:.0f}天   夏普 {best['sharpe']:.2f}   "
          f"回撤 {best['dd']:.2f}%   {best['trades']} 笔   胜率 {best['win']:.2f}%")
print("═" * 122)
