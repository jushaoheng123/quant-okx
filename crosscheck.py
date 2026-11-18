# -*- coding: utf-8 -*-
"""
跨标的验证 —— 防止「参数只对 BTC 有效」的过拟合。
同一套参数在不同标的、不同周期上分别回测，全绿才算真的稳。
"""
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import credentials, load_config
from engine import Backtester
from okx_client import OkxClient
from run import fetch_candles, get_inst
from strategy import TrendPulse

cfg = load_config()
client = OkxClient(hosts=cfg["hosts"])

INSTS = os.environ.get("XM_INSTS", "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP").split(",")
TFS = os.environ.get("XM_TF", "1H").split(",")
BARS = int(os.environ.get("XM_BARS", "3000"))
PARAMS = cfg["strategy_params"]

print(f"\n验证参数：{json.dumps(PARAMS, ensure_ascii=False)}\n")
print(f"{'标的':<18}{'周期':<6}{'总收益%':>10}{'年化%':>10}{'回撤%':>9}{'夏普':>8}{'交易':>6}{'胜率%':>8}{'盈亏比':>8}")
print("─" * 84)

rows = []
for inst_id in INSTS:
    inst = get_inst(client, inst_id)
    for tf in TFS:
        try:
            data = fetch_candles(client, cfg, tf, BARS, inst_id=inst_id)
            bt_cfg = {
                "initial_equity": 1000.0, "fee_pct": cfg["backtest"]["fee_pct"],
                "slip_pct": cfg["backtest"]["slip_pct"], "risk_pct": cfg["risk"]["risk_pct"],
                "max_leverage": cfg["risk"]["max_leverage"],
                # ★ 用真实熔断配置，不关掉 —— 否则结果偏乐观
                "max_daily_loss_pct": cfg["risk"]["max_daily_loss_pct"],
                "max_drawdown_pct": cfg["risk"]["max_drawdown_pct"],
                "bar": tf,
            }
            rep = Backtester(TrendPulse(**PARAMS), data, inst, bt_cfg).run()
            rows.append((inst_id, tf, rep))
            print(f"{inst_id:<18}{tf:<6}{rep['total_return_pct']:>+10.2f}{rep['annualized_pct']:>+10.2f}"
                  f"{rep['max_drawdown_pct']:>9.2f}{rep['sharpe']:>8.2f}{rep['trades']:>6}"
                  f"{rep['win_rate_pct']:>8.2f}{rep['profit_factor']:>8.2f}")
        except Exception as e:
            print(f"{inst_id:<18}{tf:<6}  失败：{e}")

if rows:
    pos = sum(1 for _, _, r in rows if r["total_return_pct"] > 0)
    print("─" * 84)
    print(f"  盈利组合 {pos}/{len(rows)}，平均收益 "
          f"{sum(r['total_return_pct'] for _, _, r in rows)/len(rows):+.2f}%，"
          f"最差 {min(r['total_return_pct'] for _, _, r in rows):+.2f}%，"
          f"最大回撤 {max(r['max_drawdown_pct'] for _, _, r in rows):.2f}%")
    tag = "_".join(TFS) + f"_{BARS}"
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", f"crosscheck_{tag}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump([{"inst": i, "tf": t, "rep": {k: v for k, v in r.items() if k != "curve"}}
                   for i, t, r in rows], f, indent=2, ensure_ascii=False)
    print(f"  明细 → {out}\n")
