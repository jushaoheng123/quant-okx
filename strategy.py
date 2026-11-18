# -*- coding: utf-8 -*-
"""策略层：所有策略统一输出 Action 列表，回测与实盘共用同一套信号。"""
from indicators import adx, atr, donchian, ema, rsi, sma


class Action:
    """一个待执行动作。kind: enter_long / enter_short / exit / hold"""

    __slots__ = ("idx", "ts", "kind", "price", "stop", "reason", "atr")

    def __init__(self, idx, ts, kind, price, stop=None, reason="", atr_val=None):
        self.idx = idx
        self.ts = ts
        self.kind = kind
        self.price = price
        self.stop = stop
        self.reason = reason
        self.atr = atr_val

    def __repr__(self):
        return f"<{self.kind}@{self.price:.2f} stop={self.stop} {self.reason}>"


class BaseStrategy:
    name = "base"

    def __init__(self, **kw):
        self.params = dict(kw)
        self.warmup = 30

    def signals(self, candles):
        raise NotImplementedError

    # 工具：把序列拆开
    @staticmethod
    def _split(candles):
        return (
            [c["o"] for c in candles],
            [c["h"] for c in candles],
            [c["l"] for c in candles],
            [c["c"] for c in candles],
            [c["v"] for c in candles],
        )


class TrendPulse(BaseStrategy):
    """
    趋势脉冲 v2（默认主力策略）

    v1 的教训（2026-11-16 实测）：下跌趋势里 9 笔做多全亏 —— 反弹 EMA 金叉是陷阱。
    所以 v2 加了【大周期趋势闸门】：

    · 大方向：价格必须在 EMA(regime_len) 之上才允许做多，之下才允许做空
      → 下跌通道里的反弹金叉一律不做多，从源头掐掉接刀
    · 入场：快线>慢线 + 价格在慢线上方 + ADX 达标 + 离慢线不超过 max_ext_atr 倍 ATR（防追高）
    · 出场：快线下穿慢线（真正的趋势反转） / 大周期转向 / 移动止损
      → 去掉 v1 里「价格短暂跌回慢线就跑」的过敏感出场
    · 移动止损：Chandelier，多单抬到 max(high) - ATR*trail_mult
    """

    name = "trend"

    def __init__(self, fast=12, slow=26, regime_len=200, adx_len=14, adx_min=20.0,
                 atr_len=14, atr_mult=2.0, trail_mult=3.0, max_ext_atr=3.0,
                 allow_short=True, **kw):
        super().__init__(**kw)
        self.fast, self.slow = fast, slow
        self.regime_len = regime_len
        self.adx_len, self.adx_min = adx_len, adx_min
        self.atr_len, self.atr_mult, self.trail_mult = atr_len, atr_mult, trail_mult
        self.max_ext_atr = max_ext_atr
        self.allow_short = allow_short
        self.warmup = max(slow, adx_len * 2, atr_len, min(regime_len, 200)) + 5

    def signals(self, candles):
        o, h, l, c, v = self._split(candles)
        e_fast = ema(c, self.fast)
        e_slow = ema(c, self.slow)
        e_reg = ema(c, self.regime_len) if self.regime_len and len(c) > self.regime_len else [None] * len(c)
        a = atr(h, l, c, self.atr_len)
        ad = adx(h, l, c, self.adx_len)["adx"]

        acts, pos = [], 0
        entry_px, stop_px, hi_run, lo_run = None, None, None, None
        regime_active = e_reg[-1] is not None

        for i in range(len(candles)):
            if None in (e_fast[i], e_slow[i], a[i]):
                continue
            px = c[i]
            regime_up = True if not regime_active else (e_reg[i] is not None and px > e_reg[i])
            regime_dn = True if not regime_active else (e_reg[i] is not None and px < e_reg[i])

            trend_up = e_fast[i] > e_slow[i] and px > e_slow[i]
            trend_dn = e_fast[i] < e_slow[i] and px < e_slow[i]
            adx_ok = ad[i] is not None and ad[i] >= self.adx_min
            crossed_dn = (i > 0 and e_fast[i - 1] is not None and e_slow[i - 1] is not None
                          and e_fast[i - 1] >= e_slow[i - 1] and e_fast[i] < e_slow[i])

            if pos == 1:
                hi_run = h[i] if hi_run is None else max(hi_run, h[i])
                trail = hi_run - a[i] * self.trail_mult
                if stop_px is None or trail > stop_px:
                    stop_px = trail
                reason = None
                if l[i] <= stop_px:
                    reason = "移动止损"
                elif crossed_dn:
                    reason = "EMA死叉反转"
                elif regime_active and e_reg[i] is not None and px < e_reg[i]:
                    reason = "大周期转空"
                elif e_fast[i] < e_slow[i]:
                    reason = "趋势失效"
                if reason:
                    acts.append(Action(i, candles[i]["ts"], "exit", px, reason=reason))
                    pos, entry_px, stop_px, hi_run = 0, None, None, None
                    continue
            elif pos == -1:
                lo_run = l[i] if lo_run is None else min(lo_run, l[i])
                trail = lo_run + a[i] * self.trail_mult
                if stop_px is None or trail < stop_px:
                    stop_px = trail
                reason = None
                if h[i] >= stop_px:
                    reason = "移动止损"
                elif i > 0 and e_fast[i - 1] is not None and e_slow[i - 1] is not None \
                        and e_fast[i - 1] <= e_slow[i - 1] and e_fast[i] > e_slow[i]:
                    reason = "EMA金叉反转"
                elif regime_active and e_reg[i] is not None and px > e_reg[i]:
                    reason = "大周期转多"
                elif e_fast[i] > e_slow[i]:
                    reason = "趋势失效"
                if reason:
                    acts.append(Action(i, candles[i]["ts"], "exit", px, reason=reason))
                    pos, entry_px, stop_px, lo_run = 0, None, None, None
                    continue

            if pos == 0 and adx_ok:
                ext = (px - e_slow[i]) / a[i] if a[i] else 99.0
                if trend_up and regime_up and abs(ext) <= self.max_ext_atr:
                    stop = px - a[i] * self.atr_mult
                    acts.append(Action(i, candles[i]["ts"], "enter_long", px, stop,
                                       "顺势做多(ADX {:.0f})".format(ad[i]), a[i]))
                    pos, entry_px, stop_px, hi_run = 1, px, stop, h[i]
                elif self.allow_short and trend_dn and regime_dn and abs(ext) <= self.max_ext_atr:
                    stop = px + a[i] * self.atr_mult
                    acts.append(Action(i, candles[i]["ts"], "enter_short", px, stop,
                                       "顺势做空(ADX {:.0f})".format(ad[i]), a[i]))
                    pos, entry_px, stop_px, lo_run = -1, px, stop, l[i]
        return acts


class BreakoutDonchian(BaseStrategy):
    """唐奇安通道突破（海龟简化版）：突破 N 日高点做多，跌破 M 日低点做空，ATR 止损 + 反向通道离场。"""

    name = "breakout"

    def __init__(self, entry_len=20, exit_len=10, atr_len=14, atr_mult=2.0, allow_short=True, **kw):
        super().__init__(**kw)
        self.entry_len, self.exit_len = entry_len, exit_len
        self.atr_len, self.atr_mult = atr_len, atr_mult
        self.allow_short = allow_short
        self.warmup = max(entry_len, atr_len) + 5

    def signals(self, candles):
        o, h, l, c, v = self._split(candles)
        up, dn = donchian(h, l, self.entry_len)
        xup, xdn = donchian(h, l, self.exit_len)
        a = atr(h, l, c, self.atr_len)

        acts, pos, stop_px = [], 0, None
        for i in range(len(candles)):
            if None in (up[i], dn[i], a[i]):
                continue
            px = c[i]
            if pos == 1:
                if l[i] <= stop_px or (xdn[i] is not None and px < xdn[i]):
                    acts.append(Action(i, candles[i]["ts"], "exit", px, reason="多头突破离场"))
                    pos, stop_px = 0, None
                    continue
            elif pos == -1:
                if h[i] >= stop_px or (xup[i] is not None and px > xup[i]):
                    acts.append(Action(i, candles[i]["ts"], "exit", px, reason="空头突破离场"))
                    pos, stop_px = 0, None
                    continue

            if pos == 0:
                if h[i] > up[i]:
                    stop = px - a[i] * self.atr_mult
                    acts.append(Action(i, candles[i]["ts"], "enter_long", px, stop, f"破{self.entry_len}高", a[i]))
                    pos, stop_px = 1, stop
                elif self.allow_short and l[i] < dn[i]:
                    stop = px + a[i] * self.atr_mult
                    acts.append(Action(i, candles[i]["ts"], "enter_short", px, stop, f"破{self.entry_len}低", a[i]))
                    pos, stop_px = -1, stop
        return acts


class MeanRevert(BaseStrategy):
    """均值回归：RSI 超卖 + 价格偏离 SMA 布林带下轨做多，超买做空。适合震荡市，用 ATR 止损。"""

    name = "meanrev"

    def __init__(self, rsi_len=14, rsi_low=30, rsi_high=70, bb_len=20, bb_k=2.0,
                 atr_len=14, atr_mult=1.8, allow_short=True, **kw):
        super().__init__(**kw)
        self.rsi_len, self.rsi_low, self.rsi_high = rsi_len, rsi_low, rsi_high
        self.bb_len, self.bb_k = bb_len, bb_k
        self.atr_len, self.atr_mult = atr_len, atr_mult
        self.allow_short = allow_short
        self.warmup = max(bb_len, rsi_len, atr_len) + 5

    def signals(self, candles):
        o, h, l, c, v = self._split(candles)
        r = rsi(c, self.rsi_len)
        mid = sma(c, self.bb_len)
        a = atr(h, l, c, self.atr_len)

        acts, pos, stop_px = [], 0, None
        for i in range(len(candles)):
            if None in (r[i], mid[i], a[i]):
                continue
            px = c[i]
            # 标准差
            window = c[i - self.bb_len + 1 : i + 1]
            m = mid[i]
            sd = (sum((x - m) ** 2 for x in window) / len(window)) ** 0.5
            low_band, up_band = m - self.bb_k * sd, m + self.bb_k * sd

            if pos == 1:
                if l[i] <= stop_px or px >= m:
                    acts.append(Action(i, candles[i]["ts"], "exit", px, reason="回归中轨/止损"))
                    pos, stop_px = 0, None
                    continue
            elif pos == -1:
                if h[i] >= stop_px or px <= m:
                    acts.append(Action(i, candles[i]["ts"], "exit", px, reason="回归中轨/止损"))
                    pos, stop_px = 0, None
                    continue

            if pos == 0:
                if r[i] <= self.rsi_low and px <= low_band:
                    stop = px - a[i] * self.atr_mult
                    acts.append(Action(i, candles[i]["ts"], "enter_long", px, stop, "RSI超卖+破下轨", a[i]))
                    pos, stop_px = 1, stop
                elif self.allow_short and r[i] >= self.rsi_high and px >= up_band:
                    stop = px + a[i] * self.atr_mult
                    acts.append(Action(i, candles[i]["ts"], "enter_short", px, stop, "RSI超买+破上轨", a[i]))
                    pos, stop_px = -1, stop
        return acts


REGISTRY = {"trend": TrendPulse, "breakout": BreakoutDonchian, "meanrev": MeanRevert}


def build_strategy(name="trend", **params):
    cls = REGISTRY.get(name)
    if not cls:
        raise ValueError(f"未知策略 {name}，可选：{list(REGISTRY)}")
    return cls(**params)
