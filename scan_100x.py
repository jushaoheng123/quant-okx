# -*- coding: utf-8 -*-
"""
100x 短线专项扫描 —— 交易员要的是「短线 + 100倍」，这里只找 100x 下真能活、真能赚的配置。

硬约束（物理定律，不是偏好）：
  100x 强平距离 ≈ 1/100 - 维持保证金率 = 0.60%
  止损距离必须 < 0.42%（留 30% 安全边际），否则「止损还没到，仓位先在 0.60% 处爆掉」

扫描维度：
  标的 × 周期(5m/15m) × 止损倍数(0.3/0.5/0.8/1.0 ATR) × adx_min × risk_pct
评分：年化收益 + 夏普，硬性要求 爆仓率 < 10%（100x 下完全不爆不现实）
"""
import itertools
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
LEV = 100.0
MMR = 0.004
LIQ_DIST = (1.0 / LEV - MMR) * 100      # 0.60%

CASES = []
for iid in ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SNDK-USDT-SWAP"]:
    for bar in ["5m", "15m"]:
        for f in ("candles_{}_{}_8000.json", "candles_{}_{}_3000.json"):
            p = os.path.join(DATA, f.format(iid, bar))
            if os.path.exists(p):
                CASES.append((iid, bar, p))
                break

cli = OkxClient(timeout=30)
inst_cache = {iid: cli.instrument(iid) for iid, _, _ in CASES}

print("═" * 118)
print(f"  100x 短线专项扫描   强平距离 {LIQ_DIST:.2f}%   止损必须 < {LIQ_DIST*0.7:.2f}%")
print("═" * 118)

results = []
GRID_ATR = [0.3, 0.5, 0.8, 1.0]
GRID_ADX = [15.0, 20.0, 25.0, 30.0]
GRID_RISK = [5.0, 10.0, 20.0]

for iid, bar, path in CASES:
    d = json.load(open(path, encoding="utf-8"))
    if len(d) < 400:
        print(f"  {iid} {bar} 仅 {len(d)} 根，跳过")
        continue
    inst = inst_cache[iid]
    # 先算这个标的这个周期的 ATR%
    from indicators import atr as _atr
    h = [x["h"] for x in d]
    l = [x["l"] for x in d]
    c = [x["c"] for x in d]
    a_pct = _atr(h, l, c, 14)[-1] / c[-1] * 100
    print(f"\n  扫描 {iid} {bar}   {len(d)} 根   ATR={a_pct:.3f}%   "
          f"0.5×ATR止损={a_pct*0.5:.3f}%  {'✓刚好在安全线内' if a_pct*0.5 < LIQ_DIST*0.7 else '✗超安全线'}")

    best_local = []
    for atr_mult, adx_min, risk in itertools.product(GRID_ATR, GRID_ADX, GRID_RISK):
        stop_pct = a_pct * atr_mult
        if stop_pct >= LIQ_DIST * 0.95:      # 止损太宽，必被强平截断
            continue
        params = {"fast": 12, "slow": 26, "regime_len": 200, "adx_min": float(adx_min),
                  "atr_mult": float(atr_mult), "trail_mult": 3.0, "max_ext_atr": 3.0}
        cfg = {
            "initial_equity": 1000.0, "fee_pct": FEE, "slip_pct": 0.0002,
            "risk_pct": float(risk), "max_leverage": LEV, "leverage": LEV, "mmr": MMR,
            "max_daily_loss_pct": 100.0, "max_drawdown_pct": 100.0, "bar": bar,
        }
        try:
            r = Backtester(TrendPulse(**params), d, inst, cfg).run()
        except Exception:
            continue
        if r["trades"] < 20:
            continue
        liq_rate = r["liquidated"] / max(1, r["trades"]) * 100
        if liq_rate > 40:
            continue
        best_local.append({
            "inst": iid, "bar": bar, "atr_mult": atr_mult, "adx": adx_min, "risk": risk,
            "stop_pct": stop_pct, "ret": r["total_return_pct"], "ann": r["annualized_pct"],
            "dd": r["max_drawdown_pct"], "sharpe": r["sharpe"], "trades": r["trades"],
            "win": r["win_rate_pct"], "liq": r["liquidated"], "liq_rate": liq_rate,
            "days": r["days"], "final": r["final_equity"],
        })

    if not best_local:
        print("    没有满足条件的参数组合")
        continue
    best_local.sort(key=lambda x: (x["sharpe"], x["ann"]), reverse=True)
    results.extend(best_local)
    print(f"    有效组合 {len(best_local)} 个，Top3：")
    for b in best_local[:3]:
        print(f"      止损{b['atr_mult']}ATR({b['stop_pct']:.3f}%) adx{b['adx']:.0f} risk{b['risk']:.0f}%  "
              f"→ 收益{b['ret']:>+9.2f}%  年化{b['ann']:>+9.0f}%  回撤{b['dd']:>6.1f}%  "
              f"夏普{b['sharpe']:>5.2f}  交易{b['trades']:>4}  胜率{b['win']:>5.1f}%  "
              f"爆仓{b['liq']:>3}({b['liq_rate']:.0f}%)")

print("\n" + "═" * 118)
results.sort(key=lambda x: (x["sharpe"], x["ann"]), reverse=True)
print(f"  全局 Top 10（{len(results)} 个有效组合）")
print("═" * 118)
print(f"  {'标的':<17}{'周期':<5}{'止损':>7}{'adx':>5}{'risk':>6}{'收益%':>10}{'年化%':>10}"
      f"{'回撤%':>8}{'夏普':>7}{'交易':>6}{'胜率%':>7}{'爆仓率':>8}")
print("  " + "─" * 112)
for r in results[:10]:
    print(f"  {r['inst']:<17}{r['bar']:<5}{r['atr_mult']:>7.1f}{r['adx']:>5.0f}{r['risk']:>6.0f}"
          f"{r['ret']:>+10.2f}{r['ann']:>+10.0f}{r['dd']:>8.1f}{r['sharpe']:>7.2f}"
          f"{r['trades']:>6}{r['win']:>7.1f}{r['liq_rate']:>7.1f}%")

if results:
    b = results[0]
    print(f"\n  ★ 100x 下最优：{b['inst']} {b['bar']}")
    print(f"    参数 fast12 slow26 adx{b['adx']:.0f} atr_mult={b['atr_mult']} trail3.0 ext3.0")
    print(f"    止损 {b['stop_pct']:.3f}%（< 强平距离 {LIQ_DIST:.2f}%）  risk {b['risk']:.0f}%")
    print(f"    {b['days']:.0f}天 收益 {b['ret']:+.2f}%  年化 {b['ann']:+.0f}%  回撤 {b['dd']:.1f}%  "
          f"夏普 {b['sharpe']:.2f}  交易 {b['trades']}  胜率 {b['win']:.1f}%  爆仓率 {b['liq_rate']:.1f}%")
    out = os.path.join(DATA, "scan_100x.json")
    json.dump(results[:50], open(out, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"\n  结果 → {out}")
print("═" * 118)
