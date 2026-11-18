# -*- coding: utf-8 -*-
"""
历史回放 —— 把几个月的行情压缩成几秒钟，逐笔打印「如果真跑起来它会怎么打」。

和 engine.Backtester 的区别：
  · Backtester 是「评估工具」，自己算净值、自己配对成交
  · replay 走【真实交易路径】：strategy → RiskManager → PaperBroker → 虚拟账本
    也就是说，风控拒单、张数对齐、手续费扣减、账本落盘，全部是真代码在跑
用法：python replay.py [周期] [根数]
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

from broker import PaperBroker
from config import BASE_DIR, credentials, load_config
from okx_client import OkxClient
from risk import Clock, RiskManager
from strategy import build_strategy

DATA = os.path.join(BASE_DIR, "data")


class ReplayBroker(PaperBroker):
    """把 mark() 换成历史 bar 的价格 —— 其余记账逻辑全部复用真实 PaperBroker。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._hist_px = 0.0
        self.tick = 1

    def mark(self):
        return self._hist_px


def main():
    bar = sys.argv[1] if len(sys.argv) > 1 else None
    bars = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
    cfg = load_config()
    bar = bar or cfg["bar"]
    inst_id = cfg["inst_id"]

    cpath = os.path.join(DATA, f"candles_{inst_id}_{bar}_{bars}.json")
    if not os.path.exists(cpath):
        print(f"[!] 没有缓存 {cpath}，先跑一次回测把数据抓下来")
        sys.exit(1)
    with open(cpath, "r", encoding="utf-8") as f:
        candles = json.load(f)

    client = OkxClient(hosts=cfg["hosts"])
    inst = client.instrument(inst_id)

    params = dict(cfg["strategy_params"])
    strat = build_strategy(cfg["strategy"], **params)
    # ★ 时钟必须跟着历史 bar 走，否则日亏熔断用系统日期 → 永不解除 → 整段回放全被拒
    clock = Clock(candles[0]["ts"] if candles else 0)
    risk = RiskManager(
        risk_per_trade_pct=cfg["risk"]["risk_pct"],
        max_leverage=cfg["risk"]["max_leverage"],
        max_daily_loss_pct=cfg["risk"]["max_daily_loss_pct"],
        max_drawdown_pct=cfg["risk"]["max_drawdown_pct"],
        max_positions=1,
        clock=clock,
    )
    broker = ReplayBroker(
        client, inst_id, inst,
        state_path=os.path.join(DATA, "_replay_state.json"),
        initial_equity=cfg["paper"]["initial_equity"],
        fee_pct=cfg["paper"]["fee_pct"], slip_pct=cfg["paper"]["slip_pct"],
    )
    broker.state["cash"] = cfg["paper"]["initial_equity"]
    broker.state["position"] = None
    broker.state["trades"] = []
    broker.state["initial_equity"] = cfg["paper"]["initial_equity"]

    ct_val = float(inst["ctVal"])
    t0 = datetime.fromtimestamp(candles[0]["ts"] / 1000)
    t1 = datetime.fromtimestamp(candles[-1]["ts"] / 1000)
    days = (candles[-1]["ts"] - candles[0]["ts"]) / 86400000

    print("═" * 78)
    print(f"  历史回放  {inst_id}  {bar}  {len(candles)} 根")
    print(f"  区间 {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M}（{days:.1f} 天）")
    print(f"  策略 {cfg['strategy']}  参数 {json.dumps(params, ensure_ascii=False)}")
    print(f"  初始资金 {cfg['paper']['initial_equity']:.2f} USDT   "
          f"每笔风险 {cfg['risk']['risk_pct']}%   费率 {cfg['paper']['fee_pct']*100:.3f}%")
    print(f"  仓位 {inst['ctVal']} {inst.get('ctValCcy','')}/张  "
          f"最小 {inst['minSz']} 张  lotSz {inst['lotSz']}")
    print("═" * 78)
    print()

    actions = strat.signals(candles)
    by_idx = {}
    for a in actions:
        by_idx.setdefault(a.idx, []).append(a)
    print(f"  策略共产生 {len(actions)} 个信号，开始逐根回放...\n")

    pending = None
    n_open = n_close = 0
    peak = broker.state["cash"]

    for i, cd in enumerate(candles):
        o, h, l, c = cd["o"], cd["h"], cd["l"], cd["c"]
        clock.set(cd["ts"])           # 风控的「今天」跟着这根 bar 走
        ts = datetime.fromtimestamp(cd["ts"] / 1000).strftime("%Y-%m-%d %H:%M")
        pos = broker.get_position()

        # 1) 上一根收盘的信号 → 本根开盘成交
        if pending is not None:
            act = pending
            pending = None
            pos = broker.get_position()
            if act.kind in ("enter_long", "enter_short") and pos is None:
                broker._hist_px = o
                side = 1 if act.kind == "enter_long" else -1
                eq = broker.equity()
                risk.update_equity(eq)
                dec = risk.size_position(eq, o, act.stop, ct_val,
                                         inst["lotSz"], inst["minSz"], inst.get("maxSz"))
                if dec.allow:
                    r = broker.open(side, dec.size, act.stop, act.reason)
                    n_open += 1
                    print(f"{ts}  ▲开{'多' if side==1 else '空'}  "
                          f"{dec.size:>8.2f}张 @{r['fill_px']:>10.2f}  "
                          f"名义{dec.notional:>9.2f}U  {dec.leverage:.2f}x  "
                          f"止损{act.stop:>10.2f}  风险{dec.risk_amt:.2f}U  "
                          f"权益{eq:>9.2f}   [{act.reason}]")
                else:
                    print(f"{ts}  × 风控拒单: {dec.reason}")
            elif act.kind == "exit" and pos is not None:
                broker._hist_px = o
                r = broker.close(act.reason)
                n_close += 1
                print(f"{ts}  ▼平仓  {pos['size']:>8.2f}张 @{r['fill_px']:>10.2f}  "
                      f"盈亏 {r['pnl']:>+9.2f}U   权益{broker.equity():>9.2f}   [{act.reason}]")

        # 2) 本根内止损
        pos = broker.get_position()
        if pos and pos.get("stop"):
            hit = (pos["side"] == 1 and l <= pos["stop"]) or (pos["side"] == -1 and h >= pos["stop"])
            if hit:
                broker._hist_px = pos["stop"]
                r = broker.close("止损")
                n_close += 1
                print(f"{ts}  ▼止损  {pos['size']:>8.2f}张 @{r['fill_px']:>10.2f}  "
                      f"盈亏 {r['pnl']:>+9.2f}U   权益{broker.equity():>9.2f}   [击穿 {pos['stop']:.2f}]")

        # 3) 本根收盘产生新信号
        for act in by_idx.get(i, []):
            if act.kind == "exit" and broker.get_position():
                pending = act
            elif act.kind in ("enter_long", "enter_short") and not broker.get_position():
                pending = act

        broker._hist_px = c
        eq = broker.equity()
        peak = max(peak, eq)
        broker.tick = i + 1

    # 收尾
    pos = broker.get_position()
    if pos:
        broker._hist_px = candles[-1]["c"]
        r = broker.close("回放结束")
        n_close += 1
        print(f"{'':16}  ▼收尾  {pos['size']:>8.2f}张 @{r['fill_px']:>10.2f}  "
              f"盈亏 {r['pnl']:>+9.2f}U   权益{broker.equity():>9.2f}")

    s = broker.summary()
    tr = broker.state["trades"]
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins)
    gl = abs(sum(t["pnl"] for t in losses))
    eq = broker.equity()

    print()
    print("═" * 78)
    print("  回放结算（走的是真实 PaperBroker 记账路径）")
    print("═" * 78)
    print(f"  最终权益      {eq:>12.2f} USDT   （初始 {s['initial_equity']:.2f}）")
    print(f"  总收益        {s['return_pct']:>+11.2f} %")
    print(f"  开仓/平仓     {n_open:>12} / {n_close}")
    print(f"  完整交易      {len(tr):>12} 笔（赢 {len(wins)} / 亏 {len(losses)}）")
    print(f"  胜率          {(len(wins)/len(tr)*100 if tr else 0):>11.2f} %")
    print(f"  盈利因子      {(gw/gl if gl>0 else 0):>12.2f}")
    print(f"  毛利/毛亏     {gw:>+12.2f} / {-gl:.2f}")
    print(f"  手续费合计    {sum(t['fees'] for t in tr):>12.2f} USDT")
    print(f"  峰值权益      {peak:>12.2f} USDT")
    print(f"  最大回撤      {((peak-eq)/peak*100 if peak else 0):>11.2f} %")
    print("═" * 78)

    if tr:
        print("\n  全部交易明细：")
        print("  {:>3} {:>17} {:>4} {:>10} {:>10} {:>11} {:>11}".format(
            "#", "开仓时间", "方向", "张数", "入场", "出场", "盈亏U"))
        print("  " + "─" * 74)
        for n, t in enumerate(tr, 1):
            print("  {:>3} {:>17} {:>4} {:>10.2f} {:>10.2f} {:>11.2f} {:>+11.2f}".format(
                n,
                datetime.fromtimestamp(t["entry_ts"] / 1000).strftime("%Y-%m-%d %H:%M"),
                "多" if t["side"] == 1 else "空",
                t["size"], t["entry_px"], t["exit_px"], t["pnl"]))

    print(f"\n  账本已落盘 → {broker.state_path}\n")


if __name__ == "__main__":
    main()
