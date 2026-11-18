# -*- coding: utf-8 -*-
"""
小资金标的批量验证 —— 用同一套参数扫多个合约，挑出「能用 3.5 USDT 跑且真赚钱」的。

每行给出：收益 / 回撤 / 夏普 / 交易数 / 胜率 / 盈亏比 / 最小单止损亏损 / 需要权益 / 能否开仓
用法：python batch_verify.py [参数JSON或preset名]
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
from indicators import atr
from okx_client import OkxClient
from run import fetch_candles
from strategy import TrendPulse

# 按「流动性 / 最小名义」综合挑选的候选
CANDIDATES = os.environ.get(
    "XM_LIST",
    "DOGE-USDT-SWAP,STRK-USDT-SWAP,ICP-USDT-SWAP,BERA-USDT-SWAP,"
    "GALA-USDT-SWAP,APE-USDT-SWAP,ACT-USDT-SWAP,MAGIC-USDT-SWAP,"
    "PI-USDT-SWAP,BLUR-USDT-SWAP",
).split(",")
BARS = int(os.environ.get("XM_BARS", "1500"))

DOGE_BEST = {
    "fast": 16, "slow": 26, "regime_len": 200, "adx_len": 14, "adx_min": 28.0,
    "atr_len": 14, "atr_mult": 2.5, "trail_mult": 5.0, "max_ext_atr": 6.0, "allow_short": True,
}

cfg = load_config()
params = DOGE_BEST
if len(sys.argv) > 1:
    arg = sys.argv[1]
    if os.path.exists(arg):
        with open(arg, "r", encoding="utf-8") as f:
            params = json.load(f)
    else:
        params = json.loads(arg)

c = credentials()
cli = OkxClient(c["api_key"], c["secret"], c["passphrase"], c["simulated"],
                hosts=["https://www.okx.com"], timeout=25)
risk_pct = cfg["risk"]["risk_pct"]
eq = cli.equity_usdt()

print("═" * 128)
print(f"  小资金标的批量验证   权益 {eq:.4f} USDT   风险 {risk_pct}%   周期 {cfg['bar']}   样本 {BARS} 根")
print(f"  参数 {json.dumps(params, ensure_ascii=False)}")
print("═" * 128)
print(f"  {'合约':<17}{'根数':>6}{'收益%':>9}{'年化%':>9}{'回撤%':>8}{'夏普':>7}"
      f"{'交易':>6}{'胜率%':>8}{'盈亏比':>8}{'最小单亏U':>11}{'需权益':>9}{'可开':>6}")
print("  " + "─" * 124)

rows = []
for iid in CANDIDATES:
    try:
        inst = cli.instrument(iid)
        data = fetch_candles(cli, {"inst_id": iid}, cfg["bar"], BARS, inst_id=iid)
        if len(data) < 260:
            print(f"  {iid:<17}{len(data):>6}  K线不足（<260），跳过")
            continue
        bt_cfg = {
            "initial_equity": 1000.0, "fee_pct": cfg["backtest"]["fee_pct"],
            "slip_pct": cfg["backtest"]["slip_pct"], "risk_pct": risk_pct,
            "max_leverage": cfg["risk"]["max_leverage"],
            "max_daily_loss_pct": cfg["risk"]["max_daily_loss_pct"],
            "max_drawdown_pct": cfg["risk"]["max_drawdown_pct"],
            "bar": cfg["bar"],
        }
        rep = Backtester(TrendPulse(**params), data, inst, bt_cfg).run()
        # 最小单止损亏损
        cs = [x for x in data[-120:] if x["confirm"]]
        a = atr([x["h"] for x in cs], [x["l"] for x in cs], [x["c"] for x in cs], 14)[-1]
        mn = float(inst["minSz"])
        ct = float(inst["ctVal"])
        loss = mn * ct * a * params.get("atr_mult", 2.5)
        need = loss / (risk_pct / 100.0)
        can = "✓" if eq >= need else "✗"
        rows.append((iid, rep, loss, need, eq >= need))
        print(f"  {iid:<17}{len(data):>6}{rep['total_return_pct']:>+9.2f}{rep['annualized_pct']:>+9.2f}"
              f"{rep['max_drawdown_pct']:>8.2f}{rep['sharpe']:>7.2f}{rep['trades']:>6}"
              f"{rep['win_rate_pct']:>8.2f}{rep['profit_factor']:>8.2f}{loss:>11.6f}{need:>9.3f}{can:>6}")
    except Exception as e:
        print(f"  {iid:<17}  失败 {str(e)[:60]}")

print("  " + "─" * 124)

ok = [r for r in rows if r[4] and r[1]["trades"] >= 15 and r[1]["total_return_pct"] > 0]
if ok:
    ok.sort(key=lambda r: (r[1]["sharpe"], r[1]["total_return_pct"]), reverse=True)
    print(f"\n  ★ 能开仓 + 交易≥15笔 + 正收益 的标的 {len(ok)} 个，按夏普排序：")
    for i, (iid, rep, loss, need, _) in enumerate(ok[:5], 1):
        print(f"    {i}. {iid:<18} 收益 {rep['total_return_pct']:+7.2f}%  "
              f"回撤 {rep['max_drawdown_pct']:5.2f}%  夏普 {rep['sharpe']:5.2f}  "
              f"胜率 {rep['win_rate_pct']:5.2f}%  交易 {rep['trades']:3d}  需权益 {need:.2f} U")
    best = ok[0]
    print(f"\n  → 推荐标的：{best[0]}")
    print(f"    预计收益 {best[1]['total_return_pct']:+.2f}% / {BARS}根，"
          f"按 3.46 USDT 折算约 {3.46*best[1]['total_return_pct']/100:+.4f} USDT")
else:
    print("\n  ✗ 没有同时满足「能开仓 + 交易≥15 + 正收益」的标的")
    fits = [r for r in rows if r[4]]
    if fits:
        print("    能开仓的有：", ", ".join(f"{r[0]}({r[1]['total_return_pct']:+.2f}%)" for r in fits))
print("═" * 128)
