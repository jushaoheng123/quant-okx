# -*- coding: utf-8 -*-
"""技术指标 —— 纯 Python，无 numpy/pandas 依赖。全部返回与输入等长的 list，不足期为 None。"""


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def true_range(highs, lows, closes):
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            out[i] = highs[i] - lows[i]
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            out[i] = tr
    return out


def atr(highs, lows, closes, period=14):
    """Wilder ATR。"""
    tr = true_range(highs, lows, closes)
    out = [None] * len(closes)
    if len(closes) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(closes)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def adx(highs, lows, closes, period=14):
    """Wilder ADX + DI+/DI-。"""
    n = len(closes)
    out = {"adx": [None] * n, "di_plus": [None] * n, "di_minus": [None] * n}
    if n < period * 2:
        return out
    plus_dm, minus_dm, tr = [0.0] * n, [0.0] * n, [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    sm_tr = sum(tr[1 : period + 1])
    sm_p = sum(plus_dm[1 : period + 1])
    sm_m = sum(minus_dm[1 : period + 1])
    dxs = []
    for i in range(period, n):
        if i > period:
            sm_tr = sm_tr - sm_tr / period + tr[i]
            sm_p = sm_p - sm_p / period + plus_dm[i]
            sm_m = sm_m - sm_m / period + minus_dm[i]
        if sm_tr == 0:
            continue
        dip = 100.0 * sm_p / sm_tr
        dim = 100.0 * sm_m / sm_tr
        out["di_plus"][i] = dip
        out["di_minus"][i] = dim
        denom = dip + dim
        dxs.append(100.0 * abs(dip - dim) / denom if denom else 0.0)
    if len(dxs) >= period:
        prev = sum(dxs[:period]) / period
        base = period + period - 1
        if base < n:
            out["adx"][base] = prev
        for j in range(period, len(dxs)):
            prev = (prev * (period - 1) + dxs[j]) / period
            idx = base + (j - period + 1)
            if idx < n:
                out["adx"][idx] = prev
    return out


def rsi(closes, period=14):
    n = len(closes)
    out = [None] * n
    if n <= period:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def donchian(highs, lows, period=20):
    """唐奇安通道（不含当前 bar，避免未来函数）。"""
    n = len(highs)
    upper, lower = [None] * n, [None] * n
    for i in range(period, n):
        upper[i] = max(highs[i - period : i])
        lower[i] = min(lows[i - period : i])
    return upper, lower


def highest(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = max(values[i - period + 1 : i + 1])
    return out


def lowest(values, period):
    out = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = min(values[i - period + 1 : i + 1])
    return out
