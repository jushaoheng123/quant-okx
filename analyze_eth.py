# -*- coding: utf-8 -*-
"""
ETH 永续合约多周期技术分析 —— 给做多/做空的量化依据
不是感觉，是数据。每个结论都标出它来自哪个指标。
"""
import sys
from datetime import datetime, timezone, timedelta

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import credentials
from indicators import adx, atr, ema, rsi, sma
from okx_client import OkxClient

INST = "ETH-USDT-SWAP"
BARS = ["15m", "1H", "4H", "1D"]

c = credentials()
cli = OkxClient(c["api_key"], c["secret"], c["passphrase"], c["simulated"], timeout=30)
inst = cli.instrument(INST)
ct_val = float(inst["ctVal"])

tk = cli.ticker(INST)
last = float(tk["last"])
open24 = float(tk.get("open24h") or last)
high24 = float(tk.get("high24h") or last)
low24 = float(tk.get("low24h") or last)
chg24 = (last - open24) / open24 * 100 if open24 else 0
vol24 = float(tk.get("volCcy24h") or 0)

print("═" * 92)
print(f"  ETH 永续合约分析   {INST}   {datetime.now(timezone(timedelta(hours=8))):%Y-%m-%d %H:%M:%S} 北京时间")
print("═" * 92)
print(f"\n  最新价 {last:.2f}    24h {open24:.2f} → {last:.2f}  ({chg24:+.2f}%)")
print(f"  24h 高 {high24:.2f}  低 {low24:.2f}   振幅 {(high24-low24)/low24*100:.2f}%   成交额 {vol24*last/1e6:.0f}M USDT")

# ─────────── 多周期技术面 ───────────
print("\n" + "─" * 92)
print(f"  {'周期':<6}{'收盘':>10}{'EMA20':>10}{'EMA50':>10}{'EMA200':>10}{'ADX':>7}{'RSI':>7}"
      f"{'ATR%':>7}   方向判定")
print("─" * 92)

verdicts = {}
details = {}
for bar in BARS:
    cs = [x for x in cli.candles(INST, bar, 300) if x["confirm"]]
    if len(cs) < 60:
        print(f"  {bar:<6} 数据不足")
        continue
    h = [x["h"] for x in cs]
    l = [x["l"] for x in cs]
    cl = [x["c"] for x in cs]

    e20 = ema(cl, 20)[-1]
    e50 = ema(cl, 50)[-1]
    e200 = ema(cl, 200)[-1] if len(cl) > 200 else None
    a = atr(h, l, cl, 14)[-1]
    ad = adx(h, l, cl, 14)
    adx_v = ad["adx"][-1] or 0
    dip = ad["di_plus"][-1] or 0
    dim = ad["di_minus"][-1] or 0
    r = rsi(cl, 14)[-1] or 50
    atr_pct = a / cl[-1] * 100

    # 方向判定：EMA 排列 + DI 方向
    bull = cl[-1] > e20 and e20 > e50 and (e200 is None or cl[-1] > e200)
    bear = cl[-1] < e20 and e20 < e50 and (e200 is None or cl[-1] < e200)
    if bull:
        d = "多头排列 ↑"
    elif bear:
        d = "空头排列 ↓"
    elif cl[-1] > e50:
        d = "弱多（在50上方）"
    else:
        d = "弱空（在50下方）"
    if dip > dim:
        d += f" DI+{dip:.0f}>DI-{dim:.0f}"
    else:
        d += f" DI-{dim:.0f}>DI+{dip:.0f}"

    e200_s = f"{e200:.2f}" if e200 else "  —  "
    print(f"  {bar:<6}{cl[-1]:>10.2f}{e20:>10.2f}{e50:>10.2f}{e200_s:>10}{adx_v:>7.1f}"
          f"{r:>7.1f}{atr_pct:>7.2f}   {d}")

    # 打分：趋势方向 * 趋势强度
    score = 0
    if bull:
        score += 2
    elif bear:
        score -= 2
    elif cl[-1] > e50:
        score += 1
    else:
        score -= 1
    if adx_v >= 25:
        score = score * 1.5   # 强趋势加权
    verdicts[bar] = score
    details[bar] = {"adx": adx_v, "rsi": r, "atr": a, "atr_pct": atr_pct,
                    "e20": e20, "e50": e50, "e200": e200, "close": cl[-1],
                    "high": max(h[-20:]), "low": min(l[-20:]),
                    "dip": dip, "dim": dim, "n": len(cs)}

# ─────────── 综合打分 ───────────
print("\n" + "─" * 92)
weights = {"1D": 3.0, "4H": 2.5, "1H": 1.5, "15m": 1.0}
total_w = sum(weights.get(b, 1) for b in verdicts)
total = sum(verdicts[b] * weights.get(b, 1) for b in verdicts) / total_w if total_w else 0
print(f"  加权综合分：{total:+.2f}   （正=偏多，负=偏空，|分|<0.5 视为方向不明）")
print(f"  权重：1D×3.0  4H×2.5  1H×1.5  15m×1.0")

# ─────────── 关键位 ───────────
print("\n" + "─" * 92)
print("  关键价位")
d4 = details.get("4H") or details.get("1H")
if d4:
    print(f"    4H/1H 近 20 根区间：{d4['low']:.2f} ~ {d4['high']:.2f}")
    print(f"    4H EMA20 {d4['e20']:.2f} / EMA50 {d4['e50']:.2f}"
          + (f" / EMA200 {d4['e200']:.2f}" if d4['e200'] else ""))
    rng = d4["high"] - d4["low"]
    if rng > 0:
        pos_in_range = (last - d4["low"]) / rng * 100
        print(f"    当前价在近 20 根区间的位置：{pos_in_range:.1f}%（0%=区间底 100%=区间顶）")

# 唐奇安通道（20/55）
for bar in ("4H", "1D"):
    cs = [x for x in cli.candles(INST, bar, 300) if x["confirm"]]
    if len(cs) < 60:
        continue
    hs = [x["h"] for x in cs]
    ls = [x["l"] for x in cs]
    up20, dn20 = max(hs[-21:-1]), min(ls[-21:-1])
    up55, dn55 = max(hs[-56:-1]) if len(hs) > 56 else up20, min(ls[-56:-1]) if len(ls) > 56 else dn20
    print(f"    {bar} 前20根高低：{dn20:.2f} / {up20:.2f}"
          + (f"   前55根高低：{dn55:.2f} / {up55:.2f}" if len(hs) > 56 else ""))

# ─────────── 资金费率（多空情绪）───────────
print("\n" + "─" * 92)
print("  资金费率（正=多头付钱给空头=多头拥挤；负=空头付钱=空头拥挤）")
try:
    fr = cli.request("GET", "/api/v5/public/funding-rate", {"instId": INST})
    if fr:
        cur = float(fr[0]["fundingRate"]) * 100
        nxt = float(fr[0].get("nextFundingRate") or 0) * 100
        nts = int(fr[0].get("nextFundingTime") or 0)
        print(f"    当前费率 {cur:+.4f}%   预测下期 {nxt:+.4f}%")
        if nts:
            print(f"    下次结算 {datetime.fromtimestamp(nts/1000, tz=timezone(timedelta(hours=8))):%H:%M}")
        print(f"    年化约 {cur*3*365:+.1f}%")
except Exception as e:
    print(f"    查询失败 {e}")

# ─────────── 多空持仓人数比 ───────────
try:
    ls = cli.request("GET", "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                     {"ccy": "ETH", "period": "1H"})
    if ls:
        ratio = float(ls[0][1])
        print(f"\n  多空账户人数比（最新 1H）：{ratio:.3f}   "
              f"{'多头人数占优' if ratio > 1 else '空头人数占优'}")
        for row in ls[:4]:
            print(f"    {datetime.fromtimestamp(int(row[0])/1000, tz=timezone(timedelta(hours=8))):%m-%d %H:%M}  {float(row[1]):.3f}")
except Exception:
    pass

# ─────────── 结论 ───────────
print("\n" + "═" * 92)
print("  结论")
print("═" * 92)

if total >= 1.2:
    bias = "偏多"
    action = "做多"
elif total >= 0.5:
    bias = "弱偏多"
    action = "轻仓做多 / 回踩做多"
elif total <= -1.2:
    bias = "偏空"
    action = "做空"
elif total <= -0.5:
    bias = "弱偏空"
    action = "轻仓做空 / 反弹做空"
else:
    bias = "方向不明（震荡）"
    action = "不做 / 等突破"

print(f"  综合倾向：{bias}   建议动作：{action}")

# 关键风险位
d1 = details.get("1H") or details.get("4H")
dh = details.get("4H") or details.get("1H")
if d1 and dh:
    a1 = details.get("1H", {}).get("atr", 0) or details.get("4H", {}).get("atr", 0)
    sd = a1 * 1.5
    print(f"\n  若做多：")
    print(f"    参考入场  现价 {last:.2f} 或回踩 {details.get('1H',{}).get('e20', last):.2f}（1H EMA20）")
    print(f"    止损参考  {last - sd:.2f}（1.5×ATR ≈ -{sd/last*100:.2f}%）")
    print(f"    目标参考  {dh['high']:.2f}（近 20 根高点）/ {dh['high']+sd:.2f}")
    print(f"\n  若做空：")
    print(f"    参考入场  现价 {last:.2f} 或反弹 {details.get('1H',{}).get('e20', last):.2f}（1H EMA20）")
    print(f"    止损参考  {last + sd:.2f}（1.5×ATR ≈ +{sd/last*100:.2f}%）")
    print(f"    目标参考  {dh['low']:.2f}（近 20 根低点）/ {dh['low']-sd:.2f}")

# 波动率提示（客观数据，不是劝退）
if dh:
    print(f"\n  波动率参考：4H ATR = {dh['atr']:.2f} USDT = {dh['atr_pct']:.2f}%")
    print(f"    10x 杠杆下，一根 4H K 线的正常波动 = 保证金 ±{dh['atr_pct']*10:.0f}%")
    print(f"    20x 杠杆下 = ±{dh['atr_pct']*20:.0f}%     50x 下 = ±{dh['atr_pct']*50:.0f}%")
    print(f"    100x 下 = ±{dh['atr_pct']*100:.0f}%  ← 超过 100% 就是爆仓")
print("═" * 92)
