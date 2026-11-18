# -*- coding: utf-8 -*-
"""回测引擎 —— 信号在 bar 收盘产生，成交在下一根 bar 开盘价，杜绝未来函数。"""
from risk import Clock, RiskManager, round_to

BARS_PER_YEAR = {
    "1m": 525600, "3m": 175200, "5m": 105120, "15m": 35040, "30m": 17520,
    "1H": 8760, "2H": 4380, "4H": 2190, "6H": 1460, "12H": 730, "1D": 365,
}
FUNDING_MS = 8 * 3600 * 1000      # 永续合约每 8 小时结算一次资金费（UTC 0/8/16 点）


class _ForcedExit:
    """风控熔断产生的强平动作（实盘里 Trader 熔断时会强平，回测要跟它一致）。"""
    kind = "exit"
    forced_reason = "熔断强平"


class Trade:
    __slots__ = ("side", "entry_ts", "entry_px", "size", "stop", "exit_ts", "exit_px",
                 "pnl", "pnl_pct", "fees", "reason", "bars", "liq_px", "funding")

    def __init__(self, side, entry_ts, entry_px, size):
        self.side = side
        self.entry_ts = entry_ts
        self.entry_px = entry_px
        self.size = size
        self.stop = None
        self.exit_ts = None
        self.exit_px = None
        self.pnl = 0.0
        self.pnl_pct = 0.0
        self.fees = 0.0
        self.reason = ""
        self.bars = 0
        self.liq_px = None
        self.funding = 0.0

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


class Backtester:
    def __init__(self, strategy, candles, inst, cfg):
        """
        cfg: dict —— initial_equity / fee_pct / slip_pct / risk_pct / max_leverage /
              allow_short / bar / max_daily_loss_pct / max_drawdown_pct
              funding_pct_8h  每 8 小时的资金费率（如 0.0001 = 0.01%），默认 0 = 不计。
                              按「多空都付」保守计：趋势策略顺势持仓时通常正好站在付费的一边。
              mtm_fuse        True（默认）= 每根 bar 用含浮盈亏的权益喂风控、熔断时强平，和实盘一致
        inst: 合约元数据 dict（ctVal / lotSz / minSz / maxSz）
        """
        self.strategy = strategy
        self.candles = candles
        self.inst = inst
        self.cfg = cfg
        self.bar = cfg.get("bar", "15m")
        self._idx = {cd["ts"]: i for i, cd in enumerate(candles)}

    def run(self):
        cfg = self.cfg
        cds = self.candles
        if len(cds) < self.strategy.warmup + 10:
            raise RuntimeError(f"K 线不足：{len(cds)} 根，策略至少需要 {self.strategy.warmup + 10} 根")

        equity = float(cfg.get("initial_equity", 1000.0))
        peak = equity
        fee = float(cfg.get("fee_pct", 0.0005))
        slip = float(cfg.get("slip_pct", 0.0002))
        ct_val = float(self.inst["ctVal"])
        lot_sz = float(self.inst["lotSz"])
        min_sz = float(self.inst["minSz"])
        max_sz = self.inst.get("maxSz")
        # ★ 杠杆与维持保证金率 —— 用来模拟强平爆仓
        #   高杠杆下「强平距离 < 止损距离」时，止损永远等不到，先爆仓。
        #   不模拟这个，高杠杆回测结果全是假的。
        lever = float(cfg.get("leverage") or cfg.get("max_leverage") or 0)
        mmr = float(cfg.get("mmr") or 0.004)
        funding = float(cfg.get("funding_pct_8h") or 0.0)
        mtm_fuse = bool(cfg.get("mtm_fuse", True))

        # ★ 回测的风控必须用「K线时间」当时钟，否则日亏熔断永不跨日、永不解除
        clock = Clock(cds[0]["ts"] if cds else 0)
        rm = RiskManager(
            risk_per_trade_pct=cfg.get("risk_pct", 1.0),
            max_leverage=cfg.get("max_leverage", 3.0),
            max_daily_loss_pct=cfg.get("max_daily_loss_pct", 3.0),
            max_drawdown_pct=cfg.get("max_drawdown_pct", 15.0),
            max_positions=1,
            clock=clock,
        )

        actions = self.strategy.signals(cds)
        by_idx = {}
        for a in actions:
            by_idx.setdefault(a.idx, []).append(a)

        trades, curve = [], []
        pos = None            # 当前持仓 Trade
        pending = None        # 待下根开盘执行的动作
        n_liquidated = 0      # 爆仓次数

        for i in range(len(cds)):
            bar = cds[i]
            o, h, l, c = bar["o"], bar["h"], bar["l"], bar["c"]
            clock.set(bar["ts"])          # 风控的「今天」跟着这根 bar 走

            # ---- 1) 处理上一根收盘产生的动作（本根开盘成交）----
            if pending is not None:
                act = pending
                pending = None
                if act.kind in ("enter_long", "enter_short") and pos is None:
                    side = 1 if act.kind == "enter_long" else -1
                    fill = o * (1 + slip * side)
                    rm.update_equity(equity)
                    # side 必传：开盘价已经跳过止损位时要拒单，否则会记成一笔「止损价高于入场价」的假盈利
                    dec = rm.size_position(equity, fill, act.stop, ct_val, lot_sz, min_sz, max_sz, side=side)
                    if dec.allow:
                        pos = Trade(side, bar["ts"], fill, dec.size)
                        pos.stop = act.stop
                        pos.reason = act.reason
                        equity -= fill * dec.size * ct_val * fee
                        pos.fees += fill * dec.size * ct_val * fee
                elif act.kind == "exit" and pos is not None:
                    why = getattr(act, "forced_reason", None) or "信号离场"
                    equity, pos, t = self._close(equity, pos, o * (1 - slip * pos.side), bar["ts"], fee, ct_val, why)
                    t.bars = i - self._bar_index(t.entry_ts)
                    trades.append(t)

            # ---- 2) 资金费：持仓跨过结算时刻（UTC 0/8/16 点）就付一次 ----
            if pos is not None and funding > 0 and i > 0 and pos.entry_ts != bar["ts"] \
                    and bar["ts"] // FUNDING_MS != cds[i - 1]["ts"] // FUNDING_MS:
                fund = o * pos.size * ct_val * funding
                equity -= fund
                pos.fees += fund
                pos.funding += fund

            # ---- 3) 本根内离场：止损 / 强平 ----
            #   价格是连续走过去的：多单往下跌，止损位和强平位谁离入场价近，谁先到。
            #   所以不是「一律先判强平」——止损在强平线以内时，一定是止损先成交；
            #   只有止损比强平线还远（高杠杆 + 宽止损），或者开盘直接跳过强平线，才是爆仓。
            if pos is not None:
                entry_bar = pos.entry_ts == bar["ts"]
                stop_hit = (pos.side == 1 and l <= pos.stop) or (pos.side == -1 and h >= pos.stop)
                liq, liq_hit = None, False
                if lever > 0:
                    if pos.side == 1:
                        liq = pos.entry_px * (1.0 - 1.0 / lever + mmr)
                        liq_hit = l <= liq
                    else:
                        liq = pos.entry_px * (1.0 + 1.0 / lever - mmr)
                        liq_hit = h >= liq
                stop_first = stop_hit and (not liq_hit or (pos.stop - liq) * pos.side >= 0)
                if stop_first and liq_hit and not entry_bar and (o - liq) * pos.side <= 0:
                    stop_first = False        # 开盘就跳到强平线之外，止损单来不及
                if stop_first:
                    px = pos.stop
                    if not entry_bar and (o - pos.stop) * pos.side < 0:
                        px = o                # 跳空开在止损位之外：只能按开盘价成交，不是按止损价
                    fill = px * (1 - slip * pos.side)
                    equity, pos, t = self._close(equity, pos, fill, bar["ts"], fee, ct_val, "止损")
                    t.bars = i - self._bar_index(t.entry_ts)
                    trades.append(t)
                elif liq_hit:
                    margin = pos.entry_px * pos.size * ct_val / lever
                    equity -= margin          # 隔离保证金：亏光这份保证金，不多亏
                    pos.exit_ts = bar["ts"]
                    pos.exit_px = liq
                    pos.pnl = -margin - pos.fees
                    pos.pnl_pct = -100.0
                    pos.reason = f"爆仓({lever:.0f}x 强平@{liq:.6f})"
                    pos.bars = i - self._bar_index(pos.entry_ts)
                    pos.liq_px = liq
                    trades.append(pos)
                    pos = None
                    n_liquidated += 1

            # ---- 4) 本根收盘产生新动作 ----
            for act in by_idx.get(i, []):
                if act.kind == "exit" and pos is not None:
                    pending = act
                elif act.kind in ("enter_long", "enter_short") and pos is None:
                    pending = act
                elif act.kind == "exit" and pos is None:
                    pass

            # ---- 4) 记录净值 ----
            unreal = 0.0
            if pos is not None:
                unreal = (c - pos.entry_px) * pos.side * pos.size * ct_val
            eq_now = equity + unreal
            peak = max(peak, eq_now)
            curve.append({"ts": bar["ts"], "equity": eq_now, "drawdown": (peak - eq_now) / peak * 100 if peak else 0})

            # ---- 6) 逐根喂风控：和实盘一样按「含浮盈亏的权益」判熔断，熔断时下一根开盘强平 ----
            #   原先只在开仓那一刻喂已实现权益：持仓中的浮亏再大也不熔断，日初权益也不是真的日初。
            if mtm_fuse and not rm.update_equity(eq_now) and pos is not None:
                pending = _ForcedExit()

        # 收尾平仓
        if pos is not None:
            last = cds[-1]
            equity, pos, t = self._close(equity, pos, last["c"] * (1 - slip * pos.side), last["ts"], fee, ct_val, "回测结束")
            t.bars = len(cds) - 1 - self._bar_index(t.entry_ts)
            trades.append(t)
            curve[-1]["equity"] = equity

        return self._report(equity, trades, curve, cfg)

    def _bar_index(self, ts):
        return self._idx.get(ts, 0)

    @staticmethod
    def _close(equity, pos, fill, ts, fee, ct_val, reason):
        gross = (fill - pos.entry_px) * pos.side * pos.size * ct_val
        f = fill * pos.size * ct_val * fee
        equity += gross - f
        pos.exit_ts = ts
        pos.exit_px = fill
        pos.pnl = gross - f - pos.fees
        pos.fees += f
        base = pos.entry_px * pos.size * ct_val
        pos.pnl_pct = pos.pnl / base * 100 if base else 0.0
        pos.reason = reason
        return equity, None, pos

    def _report(self, final_eq, trades, curve, cfg):
        init = float(cfg.get("initial_equity", 1000.0))
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        max_dd = max((p["drawdown"] for p in curve), default=0.0)

        # 夏普：基于每根 bar 的净值收益
        rets = []
        for j in range(1, len(curve)):
            prev = curve[j - 1]["equity"]
            if prev > 0:
                rets.append((curve[j]["equity"] - prev) / prev)
        sharpe = 0.0
        if len(rets) > 2:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            sd = var ** 0.5
            if sd > 0:
                sharpe = (mean / sd) * (BARS_PER_YEAR.get(self.bar, 35040) ** 0.5)

        total_ret = (final_eq - init) / init * 100
        days = max(1e-9, (self.candles[-1]["ts"] - self.candles[0]["ts"]) / 86400000)
        ann = ((final_eq / init) ** (365.0 / days) - 1) * 100 if final_eq > 0 and days > 1 else 0.0

        return {
            "initial_equity": init,
            "final_equity": final_eq,
            "total_return_pct": total_ret,
            "annualized_pct": ann,
            "max_drawdown_pct": max_dd,
            "sharpe": sharpe,
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": (len(wins) / len(trades) * 100) if trades else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
            "avg_bars": (sum(t.bars for t in trades) / len(trades)) if trades else 0.0,
            "fees_total": sum(t.fees for t in trades),
            "funding_total": sum(t.funding for t in trades),
            "days": days,
            "bars": len(self.candles),
            "longs": len([t for t in trades if t.side == 1]),
            "liquidated": len([t for t in trades if "爆仓" in (t.reason or "")]),
            "shorts": len([t for t in trades if t.side == -1]),
            "trade_list": [t.as_dict() for t in trades],
            "curve": curve,
        }
