# -*- coding: utf-8 -*-
"""
全市场扫描 —— 找出最适合「小资金」的永续合约。

判据：最小一单（minSz 张）打一次止损会亏多少钱？
     亏得越少，账户越小的钱也能开仓。
       最小单止损损失 = minSz × ctVal × ATR × atr_mult
再叠加流动性过滤（24h 成交额），避免选到一开就滑点的垃圾币。

用法：python scan_insts.py [风险比例%] [最少24h成交额/百万USDT]
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

risk_pct = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
min_vol_m = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0

cfg = load_config()
c = credentials()
cli = OkxClient(c["api_key"], c["secret"], c["passphrase"], c["simulated"],
                hosts=["https://www.okx.com"], timeout=25)

eq = cli.equity_usdt()
usdt_avail = 0.0
for row in cli.balance():
    for d in row.get("details", []):
        if d.get("ccy") == "USDT":
            usdt_avail = float(d.get("availBal") or 0)

print("═" * 104)
print(f"  全市场扫描  权益 {eq:.4f} USDT  可用 {usdt_avail:.4f} USDT  "
      f"风险比例 {risk_pct}%  流动性门槛 {min_vol_m}M USDT/24h")
print("═" * 104)

# 1) 一次拿全部合约规格
insts = {x["instId"]: x for x in cli.instruments("SWAP") if x.get("state") == "live"}
print(f"  在线永续合约 {len(insts)} 个")

# 2) 一次拿全部行情
tickers = {}
for t in cli.request("GET", "/api/v5/market/tickers", {"instType": "SWAP"}):
    try:
        tickers[t["instId"]] = {"last": float(t["last"]), "vol24h": float(t["vol24h"]),
                                "volCcy24h": float(t.get("volCcy24h") or 0)}
    except (KeyError, ValueError):
        pass
print(f"  行情快照 {len(tickers)} 个")

# 3) 粗筛：最小名义价值 = minSz × ctVal × price，取最小的，且流动性达标
cand = []
for iid, inst in insts.items():
    tk = tickers.get(iid)
    if not tk or tk["last"] <= 0:
        continue
    try:
        ct = float(inst["ctVal"])
        mn = float(inst["minSz"])
    except (KeyError, ValueError):
        continue
    notional = mn * ct * tk["last"]
    # 24h 成交额（USDT 计）：volCcy24h 对 SWAP 是币量，乘价格
    turnover = tk["volCcy24h"] * tk["last"]
    if turnover < min_vol_m * 1e6:
        continue
    cand.append((iid, notional, turnover, ct, mn, tk["last"]))

cand.sort(key=lambda x: x[1])
top = cand[:24]
print(f"  粗筛出最小名义价值前 {len(top)} 个（已过滤流动性）")

# 4) 精算 ATR（只对候选查 K 线）
rows = []
for iid, notional, turnover, ct, mn, px in top:
    try:
        cs = [x for x in cli.candles(iid, cfg["bar"], 120) if x["confirm"]]
        if len(cs) < 30:
            continue
        a = atr([x["h"] for x in cs], [x["l"] for x in cs], [x["c"] for x in cs], 14)[-1]
        stop = a * cfg["strategy_params"].get("atr_mult", 2.5)
        loss = mn * ct * stop
        need_eq = loss / (risk_pct / 100.0)
        atr_pct = a / px * 100
        rows.append((iid, px, notional, atr_pct, loss, need_eq, turnover))
    except Exception:
        continue

rows.sort(key=lambda r: r[4])

print()
print(f"  {'合约':<18}{'价格':>12}{'最小名义':>10}{'ATR%':>8}{'最小单止损亏损':>14}"
      f"{'需权益':>10}{'24h额M':>9}")
print("  " + "─" * 100)
for iid, px, notional, atr_pct, loss, need_eq, turnover in rows[:15]:
    flag = "✓" if eq >= need_eq else " "
    print(f"  {flag}{iid:<17}{px:>12.6f}{notional:>10.4f}{atr_pct:>7.2f}%"
          f"{loss:>14.6f}{need_eq:>10.2f}{turnover/1e6:>9.0f}")

print("  " + "─" * 100)
fit = [r for r in rows if eq >= r[5]]
if fit:
    print(f"\n  ✓ 当前权益 {eq:.4f} USDT 能开仓的合约共 {len(fit)} 个：")
    for r in fit[:6]:
        print(f"      {r[0]:<18} 最小单止损亏 {r[4]:.6f} USDT  需权益 {r[5]:.2f}")
else:
    best = rows[0] if rows else None
    if best:
        print(f"\n  ✗ 权益 {eq:.4f} 仍不够。最省的是 {best[0]}：")
        print(f"      最小单止损亏 {best[4]:.6f} USDT，按 {risk_pct}% 风险需要权益 {best[5]:.2f} USDT")
        gap = best[5] - eq
        if gap > 0:
            need_pct = best[4] / eq * 100
            print(f"      要么补 {gap:.2f} USDT，要么把 risk_pct 提到 {need_pct:.2f}% 以上（单笔亏账户 {need_pct:.1f}%）")
print("═" * 104)
