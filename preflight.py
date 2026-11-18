# -*- coding: utf-8 -*-
"""
实盘预演（preflight）—— 真金白银下单前的最后一道检查。

用【真实账户】+【真实风控】+【真实行情】跑一遍完整决策链，
但最后一步只打印、不下单。确认：
  1. 凭据能用、余额读得到
  2. 策略在当前已收盘 K 线上给出什么信号
  3. 风控算出多少张、要多少保证金、最大亏多少
  4. 那个张数能不能通过 OKX 的最小下单量校验
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
from okx_client import OkxClient, OkxError
from risk import RiskManager
from strategy import build_strategy

cfg = load_config()
c = credentials()
cli = OkxClient(c["api_key"], c["secret"], c["passphrase"], c["simulated"], hosts=cfg["hosts"])

print("═" * 88)
print("  实盘预演（只算不下单）")
print("═" * 88)

# 1) 账户
eq = cli.equity_usdt()
avail = 0.0
for row in cli.balance():
    for d in row.get("details", []):
        if d.get("ccy") == "USDT":
            avail = float(d.get("availBal") or 0)
print(f"\n[1] 账户   权益 {eq:.4f} USDT   可用 {avail:.4f} USDT   模拟盘={c['simulated']}")

pos = cli.positions(cfg["inst_id"])
live_pos = [p for p in pos if abs(float(p.get("pos") or 0)) > 0]
print(f"    {cfg['inst_id']} 现有持仓：" + ("无" if not live_pos else
      ", ".join(f"{p['pos']}张@{p['avgPx']}" for p in live_pos)))

# 2) 行情 + 信号
inst = cli.instrument(cfg["inst_id"])
cs = cli.candles(cfg["inst_id"], cfg["bar"], 320)
closed = [x for x in cs if x["confirm"]]
print(f"\n[2] 行情   {cfg['inst_id']} {cfg['bar']}  取到 {len(cs)} 根，已收盘 {len(closed)} 根")
tk = cli.ticker(cfg["inst_id"])
print(f"    最新价 {tk['last']}   买一 {tk['bidPx']}   卖一 {tk['askPx']}")

strat = build_strategy(cfg["strategy"], **cfg["strategy_params"])
print(f"\n[3] 策略   {cfg['strategy']}  预热 {strat.warmup} 根")
if len(closed) < strat.warmup + 5:
    print(f"    K 线不足（{len(closed)} < {strat.warmup + 5}），无法出信号")
    sys.exit(1)

acts = strat.signals(closed)
last_ts = closed[-1]["ts"]
recent = [a for a in acts if a.ts > closed[-2]["ts"]] if len(closed) >= 2 else acts[-1:]
print(f"    历史上共 {len(acts)} 个信号")
print(f"    最后一根已收盘 bar（{last_ts}）产生的信号：{len(recent)} 个")
for a in recent:
    print(f"      → {a.kind:<12} 价格 {a.price:.6f}  止损 {a.stop}  [{a.reason}]")

# 4) 风控算仓位
rk = cfg["risk"]
rm = RiskManager(
    risk_per_trade_pct=rk["risk_pct"], max_leverage=rk["max_leverage"],
    max_daily_loss_pct=rk["max_daily_loss_pct"], max_drawdown_pct=rk["max_drawdown_pct"],
)
rm.update_equity(eq)
print(f"\n[4] 风控   每笔风险 {rk['risk_pct']}%  杠杆上限 {rk['max_leverage']}x  "
      f"日亏熔断 {rk['max_daily_loss_pct']}%  回撤熔断 {rk['max_drawdown_pct']}%")
print(f"    熔断状态：{'⚠ ' + rm.halt_reason() if rm.halted() else '正常'}")

ct_val = float(inst["ctVal"])
if recent and recent[-1].kind in ("enter_long", "enter_short"):
    a = recent[-1]
    dec = rm.size_position(avail, a.price, a.stop, ct_val,
                           inst["lotSz"], inst["minSz"], inst.get("maxSz"))
    print(f"\n[5] 若现在开仓（用可用余额 {avail:.4f} 算）")
    print(f"    风控结论：{'✓ 允许' if dec.allow else '✗ 拒绝'}")
    if dec.allow:
        side = "多" if a.kind == "enter_long" else "空"
        margin = dec.notional / max(1.0, rk["max_leverage"])
        print(f"    方向 {side}   张数 {dec.size}   名义 {dec.notional:.4f} USDT")
        print(f"    杠杆 {dec.leverage:.2f}x   预计占用保证金 ≈ {margin:.4f} USDT")
        print(f"    止损价 {a.stop:.6f}   打止损亏 ≈ {dec.size*abs(a.price-a.stop)*ct_val:.4f} USDT "
              f"（账户 {dec.size*abs(a.price-a.stop)*ct_val/avail*100:.2f}%）")
        print(f"    剩余可用 ≈ {avail - margin:.4f} USDT")
        ok_size = dec.size >= float(inst["minSz"])
        print(f"    OKX 最小下单量校验：{dec.size} ≥ {inst['minSz']}  "
              f"{'✓ 通过' if ok_size else '✗ 不通过'}")
    else:
        print(f"    原因：{dec.reason}")
else:
    print(f"\n[5] 当前无开仓信号 —— 机器人会继续等下一根 {cfg['bar']} K 线收盘")
    # 顺带算一下「如果下一根给信号」能开多少
    px = float(tk["last"])
    probe_stop = px * 0.975
    dec = rm.size_position(avail, px, probe_stop, ct_val,
                           inst["lotSz"], inst["minSz"], inst.get("maxSz"))
    print(f"    预演：若此刻以 {px:.6f} 开仓、止损 {probe_stop:.6f}（-2.5%）")
    print(f"      {dec}")

print("\n" + "═" * 88)
print(f"  结论：{'可以上实盘（python run.py live --real）' if avail > 0 else '可用余额为 0，先处理持仓'}")
print("═" * 88)
