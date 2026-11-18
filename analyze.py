# -*- coding: utf-8 -*-
"""
通用标的分析器：python analyze.py [INST]  （默认 BTC-USDT-SWAP）
多周期技术面 + 资金费率 + 多空比 + 关键位 + 明确结论
"""
import sys
from datetime import datetime, timezone, timedelta

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import credentials
from indicators import adx, atr, ema, rsi
from okx_client import OkxClient

INST = sys.argv[1] if len(sys.argv) > 1 else "BTC-USDT-SWAP"
BARS = ["15m", "1H", "4H", "1D"]
NAME = INST.split("-")[0]

c = credentials()
cli = OkxClient(c["api_key"], c["secret"], c["passphrase"], c["simulated"], timeout=30)
inst = cli.instrument(INST)

tk = cli.ticker(INST)
last = float(tk["last"])
open24 = float(tk.get("open24h") or last)
high24 = float(tk.get("high24h") or last)
low24 = float(tk.get("low24h") or last)
chg24 = (last - open24) / open24 * 100 if open24 else 0

print("═" * 96)
print(f"  {NAME} 永续  {INST}   {datetime.now(timezone(timedelta(hours=8))):%Y-%m-%d %H:%M} 北京时间")
print("═" * 96)
print(f"\n  最新 {last:.2f}   24h: {open24:.2f} → {last:.2f} ({chg24:+.2f}%)   "
      f"高 {high24:.2f} / 低 {low24:.2f}   振幅 {(high24-low24)/low24*100:.2f}%")

print("\n" + "─" * 96)
print(f"  {'周期':<6}{'收盘':>11}{'EMA20':>11}{'EMA50':>11}{'EMA200':>11}{'ADX':>7}{'RSI':>7}"
      f"{'ATR%':>7}   方向")
print("─" * 96)

verdicts, details = {}, {}
for bar in BARS:
    cs = [x for x in cli.candles(INST, bar, 300) if x["confirm"]]
    if len(cs) < 60:
        continue
    h = [x["h"] for x in cs]; l = [x["l"] for x in cs]; cl = [x["c"] for x in cs]
    e20 = ema(cl, 20)[-1]; e50 = ema(cl, 50)[-1]
    e200 = ema(cl, 200)[-1] if len(cl) > 200 else None
    a = atr(h, l, cl, 14)[-1]
    ad = adx(h, l, cl, 14)
    adx_v = ad["adx"][-1] or 0
    dip = ad["di_plus"][-1] or 0
    dim = ad["di_minus"][-1] or 0
    r = rsi(cl, 14)[-1] or 50
    atr_pct = a / cl[-1] * 100

    bull = cl[-1] > e20 and e20 > e50 and (e200 is None or cl[-1] > e200)
    bear = cl[-1] < e20 and e20 < e50 and (e200 is None or cl[-1] < e200)
    if bull:
        d = "多头排列 ↑"
    elif bear:
        d = "空头排列 ↓"
    elif cl[-1] > e50:
        d = "弱多"
    else:
        d = "弱空"
    d += f"  DI+{dip:.0f}/DI-{dim:.0f}"

    e200s = f"{e200:.1f}" if e200 else "  —  "
    print(f"  {bar:<6}{cl[-1]:>11.2f}{e20:>11.2f}{e50:>11.2f}{e200s:>11}{adx_v:>7.1f}"
          f"{r:>7.1f}{atr_pct:>7.2f}   {d}")

    score = 2 if bull else (-2 if bear else (1 if cl[-1] > e50 else -1))
    if adx_v >= 25:
        score *= 1.5
    verdicts[bar] = score
    details[bar] = {"adx": adx_v, "rsi": r, "atr": a, "atr_pct": atr_pct,
                    "e20": e20, "e50": e50, "e200": e200, "close": cl[-1],
                    "high": max(h[-20:]), "low": min(l[-20:]),
                    "high55": max(h[-56:-1]) if len(h) > 56 else max(h[-20:]),
                    "low55": min(l[-56:-1]) if len(l) > 56 else min(l[-20:])}

weights = {"1D": 3.0, "4H": 2.5, "1H": 1.5, "15m": 1.0}
tw = sum(weights.get(b, 1) for b in verdicts)
total = sum(verdicts[b] * weights.get(b, 1) for b in verdicts) / tw if tw else 0
print(f"\n  加权综合分 {total:+.2f}   (正=偏多 负=偏空 |分|<0.5=方向不明)")

# 关键位
print("\n" + "─" * 96)
print("  关键价位")
dh = details.get("4H") or details.get("1H")
d1 = details.get("1H")
if dh:
    print(f"    4H 近20根区间  {dh['low']:.2f} ~ {dh['high']:.2f}   "
          f"（现价位于 {(last-dh['low'])/(dh['high']-dh['low'])*100:.0f}%）")
    print(f"    4H 近55根区间  {dh['low55']:.2f} ~ {dh['high55']:.2f}")
    print(f"    4H EMA20 {dh['e20']:.2f} / EMA50 {dh['e50']:.2f}"
          + (f" / EMA200 {dh['e200']:.2f}" if dh['e200'] else ""))
d1d = details.get("1D")
if d1d:
    print(f"    1D 近20根区间  {d1d['low']:.2f} ~ {d1d['high']:.2f}")
    print(f"    1D EMA20 {d1d['e20']:.2f} / EMA50 {d1d['e50']:.2f}"
          + (f" / EMA200 {d1d['e200']:.2f}" if d1d['e200'] else ""))

# 资金费率 & 多空比
print("\n" + "─" * 96)
print("  资金费率 / 情绪")
try:
    fr = cli.request("GET", "/api/v5/public/funding-rate", {"instId": INST})
    if fr:
        cur = float(fr[0]["fundingRate"]) * 100
        nts = int(fr[0].get("nextFundingTime") or 0)
        print(f"    当前费率 {cur:+.4f}%   年化 {cur*3*365:+.1f}%   "
              f"{'多头付钱→多头拥挤' if cur > 0 else '空头付钱→空头拥挤'}")
        if nts:
            print(f"    下次结算 {datetime.fromtimestamp(nts/1000, tz=timezone(timedelta(hours=8))):%H:%M}")
except Exception:
    pass
try:
    ls = cli.request("GET", "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                     {"ccy": NAME, "period": "1H"})
    if ls:
        print(f"    多空账户人数比 {float(ls[0][1]):.3f}   "
              f"{'多头人数占优' if float(ls[0][1]) > 1 else '空头人数占优'}")
except Exception:
    pass

# 结论
print("\n" + "═" * 96)
print("  结论")
print("═" * 96)
if total >= 1.2:
    bias, act = "偏多", "做多 / 持多"
elif total >= 0.5:
    bias, act = "弱偏多", "轻仓做多 / 回踩做多"
elif total <= -1.2:
    bias, act = "偏空", "做空 / 持空"
elif total <= -0.5:
    bias, act = "弱偏空", "轻仓做空 / 反弹做空"
else:
    bias, act = "方向不明（震荡）", "不做 / 等突破"

print(f"  综合倾向：{bias}      建议：{act}")

if dh and d1:
    sd = d1["atr"] * 1.5
    print(f"\n  做多的关键位：")
    print(f"    入场参考  现价 {last:.2f} / 回踩 {d1['e20']:.2f}（1H EMA20）")
    print(f"    止损参考  {last - sd:.2f}（1.5×ATR, -{sd/last*100:.2f}%）")
    print(f"    目标参考  {dh['high']:.2f}（4H近20根高）/ {dh['high55']:.2f}（55根高）")
    print(f"\n  做空的关键位：")
    print(f"    入场参考  现价 {last:.2f} / 反弹 {d1['e20']:.2f}")
    print(f"    止损参考  {last + sd:.2f}（1.5×ATR, +{sd/last*100:.2f}%）")
    print(f"    目标参考  {dh['low']:.2f}（4H近20根低）/ {dh['low55']:.2f}")

    print(f"\n  波动率：4H ATR = {dh['atr']:.2f} = {dh['atr_pct']:.2f}%")
    for lev in (20, 50, 100):
        liq = (1.0/lev - 0.004) * 100
        print(f"    {lev}x 强平距离 {liq:>5.2f}%   一根4H K线波动 {dh['atr_pct']*lev:>6.1f}% 保证金")
print("═" * 96)
