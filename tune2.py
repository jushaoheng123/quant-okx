# -*- coding: utf-8 -*-
"""
针对性参数扫描 —— 找 trend v2 在【多周期】上都稳的参数（抗过拟合）
评分：总收益为主、夏普为辅，硬性要求 交易数>=8 且 最大回撤<=20%
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

from config import BASE_DIR, credentials, load_config
from engine import Backtester
from okx_client import OkxClient
from run import fetch_candles, get_inst
from strategy import TrendPulse

cfg = load_config()
client = OkxClient(hosts=cfg["hosts"])
inst = get_inst(client, cfg["inst_id"])

BARS = int(os.environ.get("XM_BARS", "1500"))
TIMEFRAMES = os.environ.get("XM_TF", "15m,1H,4H").split(",")

data = {}
for tf in TIMEFRAMES:
    data[tf] = fetch_candles(client, cfg, tf, BARS)
    print(f"  {tf}: {len(data[tf])} 根  "
          f"{data[tf][0]['ts']} → {data[tf][-1]['ts']}")

GRID = {
    "fast": [8, 12, 16, 21],
    "slow": [26, 34, 48],
    "regime_len": [100, 200],
    "adx_min": [18.0, 22.0, 28.0],
    "atr_mult": [1.5, 2.0, 2.5],
    "trail_mult": [3.0, 5.0],
    "max_ext_atr": [3.0, 6.0],
}
MAX_DD = float(os.environ.get("XM_MAXDD", "20"))
MIN_TRADES = int(os.environ.get("XM_MINC", "8"))
keys = list(GRID)
combos = [dict(zip(keys, v)) for v in itertools.product(*[GRID[k] for k in keys])]
combos = [c for c in combos if c["fast"] < c["slow"]]
print(f"\n  组合总数 {len(combos)} × 周期 {len(TIMEFRAMES)} = {len(combos)*len(TIMEFRAMES)} 次回测\n")

results = []
for n, ps in enumerate(combos, 1):
    per_tf, ok = {}, True
    for tf in TIMEFRAMES:
        bt_cfg = {
            "initial_equity": 1000.0, "fee_pct": cfg["backtest"]["fee_pct"],
            "slip_pct": cfg["backtest"]["slip_pct"], "risk_pct": cfg["risk"]["risk_pct"],
            "max_leverage": cfg["risk"]["max_leverage"],
            "max_daily_loss_pct": 100.0, "max_drawdown_pct": 100.0, "bar": tf,
        }
        try:
            rep = Backtester(TrendPulse(**ps), data[tf], inst, bt_cfg).run()
        except Exception:
            ok = False
            break
        per_tf[tf] = {k: rep[k] for k in
                      ("total_return_pct", "annualized_pct", "max_drawdown_pct",
                       "sharpe", "trades", "win_rate_pct", "profit_factor")}
    if not ok:
        continue
    tr = [v["trades"] for v in per_tf.values()]
    dds = [v["max_drawdown_pct"] for v in per_tf.values()]
    rets = [v["total_return_pct"] for v in per_tf.values()]
    if min(tr) < MIN_TRADES or max(dds) > MAX_DD:
        continue
    score = sum(rets) / len(rets) + 0.5 * sum(v["sharpe"] for v in per_tf.values()) / len(per_tf)
    results.append({"params": ps, "score": score, "avg_ret": sum(rets) / len(rets),
                    "worst_ret": min(rets), "max_dd": max(dds), "per_tf": per_tf})
    if n % 20 == 0:
        print(f"  {n}/{len(combos)}  有效 {len(results)}  当前最佳 {max([r['score'] for r in results], default=0):+.2f}", end="\r")

print(" " * 70, end="\r")
results.sort(key=lambda x: x["score"], reverse=True)

print(f"\n  有效组合 {len(results)} 个（要求每周期交易数>=8、回撤<=20%）\n")
print(f"  {'排名':<5}{'均收益%':>10}{'最差%':>9}{'最大回撤%':>11}{'评分':>8}   参数")
for i, r in enumerate(results[:15], 1):
    print(f"  {i:<5}{r['avg_ret']:>+10.2f}{r['worst_ret']:>+9.2f}{r['max_dd']:>11.2f}{r['score']:>+8.2f}   "
          + json.dumps(r["params"], ensure_ascii=False))
    for tf, v in r["per_tf"].items():
        print(f"        └ {tf:<4} 收益{v['total_return_pct']:+7.2f}%  回撤{v['max_drawdown_pct']:5.2f}%  "
              f"夏普{v['sharpe']:6.2f}  交易{v['trades']:3d}  胜率{v['win_rate_pct']:5.2f}%")

out = os.path.join(BASE_DIR, "data", "tune2_result.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(results[:50], f, indent=2, ensure_ascii=False)
print(f"\n  结果 → {out}")
if results:
    print(f"\n  最优参数 JSON：{json.dumps(results[0]['params'])}")
