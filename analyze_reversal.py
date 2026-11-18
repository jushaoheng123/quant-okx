# -*- coding: utf-8 -*-
"""
「先做多再做空」到底划不划算？用 ETH 历史数据算期望值。

核心问题：
  1. 1D/4H RSI 超卖之后，反弹的概率和幅度到底多少？
  2. 在 ADX 强下跌趋势里逆势做多，胜率是多少？
  3. 「先多后空」vs「只等反弹做空」，哪个期望值更高？
"""
import json
import os
import sys
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import credentials
from indicators import adx, atr, ema, rsi
from okx_client import OkxClient

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA, exist_ok=True)
INST = "ETH-USDT-SWAP"

c = credentials()
cli = OkxClient(c["api_key"], c["secret"], c["passphrase"], c["simulated"], timeout=30)


def load(bar, bars):
    p = os.path.join(DATA, f"candles_{INST}_{bar}_{bars}.json")
    if os.path.exists(p):
        age_h = (os.path.getmtime(p) and (__import__("time").time() - os.path.getmtime(p)) / 3600)
        if age_h < 12:
            d = json.load(open(p, encoding="utf-8"))
            print(f"  用缓存 {INST} {bar} {len(d)} 根")
            return d
    print(f"  抓取 {INST} {bar} {bars} 根...")
    d = cli.fetch_history(INST, bar, bars)
    json.dump(d, open(p, "w", encoding="utf-8"))
    return d


# ══════════ 1. 超卖后反弹统计 ══════════
print("═" * 92)
print("  实验一：RSI 超卖之后，价格怎么走？（历史统计，不是猜测）")
print("═" * 92)

for bar, bars in [("4H", 4000), ("1D", 2000)]:
    try:
        d = load(bar, bars)
    except Exception as e:
        print(f"  {bar} 抓取失败 {e}")
        continue
    cl = [x["c"] for x in d]
    r = rsi(cl, 14)
    ad = adx([x["h"] for x in d], [x["l"] for x in d], cl, 14)
    adx_v = ad["adx"]
    e50 = ema(cl, 50)

    # 找 RSI<30 且处于下跌趋势（价格<EMA50）的时点
    samples = []
    for i in range(60, len(d) - 20):
        if r[i] is None or adx_v[i] is None or e50[i] is None:
            continue
        if r[i] < 30 and cl[i] < e50[i]:
            samples.append(i)

    if not samples:
        print(f"\n  {bar}: 历史上没有 RSI<30 且跌破 EMA50 的样本")
        continue

    print(f"\n  【{bar}】历史上 RSI<30 且价格在 EMA50 下方，共 {len(samples)} 次")
    for horizon in (3, 6, 12):
        fwd = []
        for i in samples:
            if i + horizon < len(cl):
                fwd.append((cl[i + horizon] - cl[i]) / cl[i] * 100)
        if not fwd:
            continue
        wins = [x for x in fwd if x > 0]
        avg = sum(fwd) / len(fwd)
        best = max(fwd)
        worst = min(fwd)
        print(f"    之后 {horizon:>2} 根 {bar}："
              f"上涨概率 {len(wins)/len(fwd)*100:>5.1f}%   "
              f"平均 {avg:>+6.2f}%   中位 {sorted(fwd)[len(fwd)//2]:>+6.2f}%   "
              f"最好 {best:>+6.2f}%  最差 {worst:>+6.2f}%")

    # 细分：ADX 强 / 弱
    for label, cond in [("ADX>40(强趋势)", lambda x: x > 40),
                        ("ADX 25-40", lambda x: 25 <= x <= 40),
                        ("ADX<25(弱趋势)", lambda x: x < 25)]:
        sub = [i for i in samples if adx_v[i] and cond(adx_v[i])]
        if len(sub) < 5:
            continue
        fwd = [(cl[i + 6] - cl[i]) / cl[i] * 100 for i in sub if i + 6 < len(cl)]
        if not fwd:
            continue
        wins = [x for x in fwd if x > 0]
        print(f"      {label:<16} 样本 {len(sub):>3} 次  之后6根上涨概率 "
              f"{len(wins)/len(fwd)*100:>5.1f}%  平均 {sum(fwd)/len(fwd):>+6.2f}%")

# ══════════ 2. 逆势做多 vs 顺势做空 的期望值 ══════════
print("\n" + "═" * 92)
print("  实验二：逆势做多 vs 顺势做空，期望值对比")
print("═" * 92)

d = load("4H", 4000)
cl = [x["c"] for x in d]
hi = [x["h"] for x in d]
lo = [x["l"] for x in d]
a = atr(hi, lo, cl, 14)
e20 = ema(cl, 20)
e50 = ema(cl, 50)
adx_v = adx(hi, lo, cl, 14)["adx"]

# 场景：价格在 EMA50 下方（空头结构）+ ADX>40（强趋势）→ 此刻做多 / 做空
long_trades, short_trades = [], []
for i in range(60, len(cl) - 24):
    if None in (a[i], e20[i], e50[i], adx_v[i]):
        continue
    if cl[i] >= e50[i] or adx_v[i] < 40:
        continue
    entry = cl[i]
    stop_d = a[i] * 1.5
    # 做多：止损 entry-1.5ATR，目标 entry+2ATR（2:1.33）
    long_stop = entry - stop_d
    long_tgt = entry + a[i] * 2.0
    # 做空：止损 entry+1.5ATR，目标 entry-4ATR
    short_stop = entry + stop_d
    short_tgt = entry - a[i] * 4.0
    # 往后 24 根里看先碰哪个（保守：同根都碰算止损）
    for j in range(i + 1, min(i + 25, len(cl))):
        # 多
        if not long_trades or len(long_trades) <= len(short_trades):
            pass
        break
    # 简化：分别独立判定
    l_res = None
    for j in range(i + 1, min(i + 25, len(cl))):
        hit_stop = lo[j] <= long_stop
        hit_tgt = hi[j] >= long_tgt
        if hit_stop and hit_tgt:
            l_res = -1.5; break     # 同根都碰→保守算亏
        if hit_stop:
            l_res = -1.5; break
        if hit_tgt:
            l_res = +2.0; break
    if l_res is not None:
        long_trades.append(l_res)
    s_res = None
    for j in range(i + 1, min(i + 25, len(cl))):
        hit_stop = hi[j] >= short_stop
        hit_tgt = lo[j] <= short_tgt
        if hit_stop and hit_tgt:
            s_res = -1.5; break
        if hit_stop:
            s_res = -1.5; break
        if hit_tgt:
            s_res = +4.0; break
    if s_res is not None:
        short_trades.append(s_res)

def stat(name, arr, rr):
    if not arr:
        print(f"  {name}: 无样本")
        return
    wins = [x for x in arr if x > 0]
    losses = [x for x in arr if x < 0]
    wr = len(wins) / len(arr) * 100
    exp = sum(arr) / len(arr)
    gw = sum(wins)
    gl = abs(sum(losses))
    print(f"  {name:<22} 样本 {len(arr):>4} 次  胜率 {wr:>5.1f}%  "
          f"平均每笔 {exp:>+6.3f}×ATR  盈利因子 {(gw/gl if gl else 0):>5.2f}  "
          f"{'✓ 正期望' if exp > 0 else '✗ 负期望'}")

print("\n  场景：4H 价格跌破 EMA50 + ADX>40（强下跌趋势中）")
print(f"  共找到 {len(long_trades)+len(short_trades)} 个符合条件的入场点\n")
stat("逆势做多(止损1.5ATR)", long_trades, 2.0/1.5)
stat("顺势做空(止损1.5ATR)", short_trades, 4.0/1.5)

# ══════════ 3. 「先多后空」vs「只做空」 ══════════
print("\n" + "═" * 92)
print("  实验三：交易员的方案 —— 先做多赚反弹，再反手做空")
print("═" * 92)
print("""
  方案A（先多后空）：
    第1步 做多：1818 → 目标 1854（4H EMA20），赚 +2.0%
    第2步 做空：1854 → 目标 1709，赚 -7.8%（空单盈利）
    合计理论收益：+9.8% 价格波动
    但需要：两次判断都正确 + 两次进出共 4 次手续费

  方案B（只等反弹做空）：
    等价格自己反弹到 1854 附近，直接做空 → 1709
    理论收益：+7.8%
    只需：一次判断

  差异：方案A 多赚 2.0%，代价是多一次判断风险 + 多 2 次手续费
""")

# 手续费成本对比（20x 杠杆）
fee_taker = 0.0005
for lev in (10, 20):
    fee_per_round = fee_taker * 2 * lev * 100   # 占保证金%
    print(f"  {lev}x 杠杆下：单次进出（2笔）手续费 = 保证金的 {fee_per_round:.1f}%")
    print(f"      方案A（2次进出/4笔）= {fee_per_round*2:.1f}% 保证金")
    print(f"      方案B（1次进出/2笔）= {fee_per_round:.1f}% 保证金")

print("\n  → 做多赚的 2.0% 价格 = 多少保证金？")
for lev in (10, 20):
    gross = 2.0 * lev
    fee = fee_taker * 2 * lev * 100
    print(f"      {lev}x：毛赚 {gross:.0f}% 保证金 − 手续费 {fee:.1f}% = 净赚 {gross-fee:.1f}% 保证金")

print("\n" + "═" * 92)
