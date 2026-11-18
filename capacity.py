# -*- coding: utf-8 -*-
"""
仓位容量测算 —— 回答一个很实际的问题：
「我这个账户的钱，够不够这套策略开出最小一单？」

逻辑：
  最小下单量（minSz 张）打一次止损，会亏掉多少钱？
  这个钱如果已经超过「权益 × risk_pct%」，风控就会拒单 —— 说明钱不够。
  要让最小单可开，需要的权益 = 最小单止损损失 / risk_pct%
"""
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import credentials, load_config
from indicators import atr
from okx_client import OkxClient
from risk import RiskManager

CANDS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP",
         "DOGE-USDT-SWAP", "JCT-USDT-SWAP"]

cfg = load_config()
c = credentials()
cli = OkxClient(c["api_key"], c["secret"], c["passphrase"], c["simulated"], hosts=cfg["hosts"])

eq = cli.equity_usdt()
bal = cli.balance()
usdt_avail = 0.0
for row in bal:
    for d in row.get("details", []):
        if d.get("ccy") == "USDT":
            usdt_avail = float(d.get("availBal") or 0)

risk_pct = cfg["risk"]["risk_pct"]
max_lev = cfg["risk"]["max_leverage"]
print("═" * 96)
print(f"  账户权益 {eq:.4f} USDT    USDT 可用 {usdt_avail:.4f}    "
      f"每笔风险 {risk_pct}%   杠杆上限 {max_lev}x   周期 {cfg['bar']}")
print("═" * 96)
print(f"  {'合约':<16}{'ctVal':>10}{'最小张':>8}{'价格':>12}{'1张名义':>10}"
      f"{'ATR':>10}{'最小单止损损失':>15}{'开1张最小单需要权益':>22}")
print("─" * 96)

rows = []
for inst_id in CANDS:
    try:
        inst = cli.instrument(inst_id)
        tk = cli.ticker(inst_id)
        cs = cli.candles(inst_id, cfg["bar"], 120)
        cs = [x for x in cs if x["confirm"]]
        if len(cs) < 30:
            continue
        h = [x["h"] for x in cs]
        l = [x["l"] for x in cs]
        cl = [x["c"] for x in cs]
        a = atr(h, l, cl, 14)[-1]
        ct_val = float(inst["ctVal"])
        min_sz = float(inst["minSz"])
        px = float(tk["last"])
        notional_1 = ct_val * px
        stop_dist = a * cfg["strategy_params"].get("atr_mult", 2.5)
        loss_min = min_sz * ct_val * stop_dist
        need_eq = loss_min / (risk_pct / 100.0)
        rows.append((inst_id, ct_val, min_sz, px, notional_1, a, loss_min, need_eq))
        flag = "✓" if eq >= need_eq else "✗"
        print(f"  {flag} {inst_id:<14}{ct_val:>10.4f}{min_sz:>8.2f}{px:>12.6f}"
              f"{notional_1:>10.2f}{a:>10.4f}{loss_min:>15.4f}{need_eq:>22.2f}")
    except Exception as e:
        print(f"    {inst_id:<16} 查询失败 {e}")

print("─" * 96)
ok = [r for r in rows if eq >= r[7]]
if ok:
    best = min(ok, key=lambda r: r[7])
    print(f"\n  当前 {eq:.4f} USDT 能开的最小单合约：{best[0]}（需要权益 {best[7]:.2f}，你有 {eq:.2f}）")
else:
    need = min(rows, key=lambda r: r[7]) if rows else None
    print(f"\n  ✗ 当前 {eq:.4f} USDT 一个都开不出来。")
    if need:
        print(f"    最省钱的合约是 {need[0]}：开最小 1 单，止损一次亏 {need[6]:.4f} USDT，")
        print(f"    按 {risk_pct}% 风险预算，需要权益 ≥ {need[7]:.2f} USDT。")

print(f"\n  【按可用余额 {usdt_avail:.4f} USDT 算】")
if rows:
    best2 = min(rows, key=lambda r: r[6])
    print(f"    能开出最小单的合约需要止损损失 ≤ {usdt_avail * risk_pct / 100:.6f} USDT，")
    print(f"    最便宜的是 {best2[0]}，单张最小单止损就要亏 {best2[6]:.6f} USDT —— "
          f"{'够' if usdt_avail * risk_pct / 100 >= best2[6] else '不够'}")

print("\n" + "═" * 96)
print("  建议：要让本系统在 1% 风险下正常跑起来，最少需要多少权益？")
if rows:
    for r in sorted(rows, key=lambda x: x[7])[:3]:
        print(f"    {r[0]:<16} 需要 ≥ {r[7]:>10.2f} USDT")
print("  （或者调高 config.json 的 risk.risk_pct —— 但那等于放大单笔亏损，不推荐）")
print("═" * 96)
