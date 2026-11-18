# -*- coding: utf-8 -*-
"""
风控与仓位管理 —— 实盘的第一道命门。
原则：先算能亏多少，再算能开多大。任何一次下单都必须先过这里。
"""
import json
import math
import os
import time
from datetime import datetime, timezone


class Clock:
    """
    可注入时钟。
    实盘：不设 ts → 跟系统真实时间。
    回测/回放：每根 bar 调 set(ts_ms) → 风控的「今天」跟着历史 K 线走。
    ★ 不注入时钟的后果：历史回测里"日亏熔断"用的是系统当天日期，
      永不跨日 → 熔断永不解除 → 后面所有信号被拒，回测数字全废。
    """

    def __init__(self, ts_ms=0):
        self.ts_ms = ts_ms

    def set(self, ts_ms):
        self.ts_ms = ts_ms
        return self

    def now(self):
        if self.ts_ms:
            return datetime.fromtimestamp(self.ts_ms / 1000.0, tz=timezone.utc)
        return datetime.now(timezone.utc)

    def today(self):
        return self.now().strftime("%Y-%m-%d")


class RiskDecision:
    __slots__ = ("allow", "size", "notional", "leverage", "reason", "stop", "risk_amt")

    def __init__(self, allow, size=0.0, notional=0.0, leverage=0.0, reason="", stop=None, risk_amt=0.0):
        self.allow = allow
        self.size = size
        self.notional = notional
        self.leverage = leverage
        self.reason = reason
        self.stop = stop
        self.risk_amt = risk_amt

    def __repr__(self):
        tag = "ALLOW" if self.allow else "DENY "
        return (f"[{tag}] sz={self.size} notional={self.notional:.2f} lev={self.leverage:.2f}x "
                f"risk={self.risk_amt:.2f} {self.reason}")


def round_to(v, step, mode="down"):
    """按 step 取整。down/up/nearest。注意 Python 的 int() 是向零取整，ceil 必须用 math.ceil。"""
    if step <= 0:
        return v
    n = v / step
    if mode == "down":
        n = math.floor(n + 1e-9)
    elif mode == "up":
        n = math.ceil(n - 1e-9)
    else:
        n = round(n)
    return round(n * step, 12)


class RiskManager:
    def __init__(
        self,
        risk_per_trade_pct=1.0,      # 每笔最大亏损占权益 %
        max_leverage=3.0,            # 名义价值 / 权益 上限
        max_daily_loss_pct=3.0,      # 单日亏损熔断
        max_drawdown_pct=15.0,       # 全局回撤熔断（从峰值算）
        max_positions=1,             # 最大同时持仓
        min_risk_reward=1.2,         # 最小盈亏比
        max_notional_pct=300.0,      # 单笔名义价值占权益 % 上限（兜底）
        state_path=None,
        clock=None,
    ):
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_leverage = max_leverage
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.max_positions = max_positions
        self.min_risk_reward = min_risk_reward
        self.max_notional_pct = max_notional_pct
        self.state_path = state_path
        self.clock = clock or Clock()
        self.state = {
            "peak_equity": 0.0,
            "day": self._today(),
            "day_start_equity": 0.0,
            "halted": False,
            "halt_reason": "",
            "halt_ts": 0,
        }
        if state_path and os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    self.state.update(json.load(f))
            except Exception as e:
                # 状态文件读坏 = 峰值/熔断记录全丢，必须让人看见，不能悄悄当没事
                print(f"  [warn] 风控状态文件 {state_path} 读取失败（{e}），峰值与熔断记录从零开始")

    def _today(self):
        return self.clock.today()

    def _now_ms(self):
        return int(self.clock.now().timestamp() * 1000)

    def save(self):
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"       # 先写临时文件再替换：写到一半断电不会留下半截 JSON
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ---------- 熔断 ----------
    def update_equity(self, equity):
        """每轮先喂当前权益，更新峰值 / 日初权益 / 熔断判定。"""
        s = self.state
        today = self._today()
        if today != s["day"]:
            s["day"] = today
            s["day_start_equity"] = equity
            # 新的一天自动解除「日亏熔断」，但「回撤熔断」不解除
            if s["halt_reason"].startswith("日亏"):
                s["halted"] = False
                s["halt_reason"] = ""
        if s["day_start_equity"] <= 0:
            s["day_start_equity"] = equity
        if equity > s["peak_equity"]:
            s["peak_equity"] = equity

        if s["peak_equity"] > 0:
            dd = (s["peak_equity"] - equity) / s["peak_equity"] * 100.0
            if dd >= self.max_drawdown_pct and not s["halted"]:
                s["halted"] = True
                s["halt_reason"] = f"回撤熔断 {dd:.2f}% >= {self.max_drawdown_pct}%"
                s["halt_ts"] = self._now_ms()
        if s["day_start_equity"] > 0:
            day_pnl = (equity - s["day_start_equity"]) / s["day_start_equity"] * 100.0
            if day_pnl <= -self.max_daily_loss_pct and not s["halted"]:
                s["halted"] = True
                s["halt_reason"] = f"日亏熔断 {day_pnl:.2f}% <= -{self.max_daily_loss_pct}%"
                s["halt_ts"] = self._now_ms()
        self.save()
        return not s["halted"]

    def halted(self):
        return bool(self.state.get("halted"))

    def halt_reason(self):
        return self.state.get("halt_reason", "")

    def reset_halt(self, by="manual"):
        """解除熔断，并把峰值/日初权益清零重新起算。
        不清零的话，下一轮 update_equity 用旧峰值一算，回撤还是超线 → 立刻又熔断，等于没解除。"""
        self.state["halted"] = False
        self.state["halt_reason"] = ""
        self.state["halt_ts"] = 0
        self.state["peak_equity"] = 0.0
        self.state["day_start_equity"] = 0.0
        self.save()
        return f"熔断已解除（{by}），峰值与日初权益从当前权益重新起算"

    # ---------- 仓位 ----------
    def size_position(self, equity, entry, stop, ct_val, lot_sz, min_sz, max_sz=None,
                      price_precision_guard=True, side=None, cost_pct=0.0,
                      liq_lever=None, mmr=0.004, max_notional_abs=None):
        """
        风险百分比法：
          风险金额 = equity * risk_pct%
          每张合约的止损损失 = (|entry - stop| + 来回手续费滑点) * ctVal
          张数 = 风险金额 / 每张损失
        再套上杠杆上限与最小下单量，向下取整。

        可选参数（不传 = 和原来完全一样）：
          side             1 多 / -1 空。传了就检查止损在不在正确的一侧
          cost_pct         单边「手续费+滑点」比例。传了就把来回成本算进每张损失，实亏才不超预算
          liq_lever        逐仓杠杆。传了就拒掉「止损比强平线还远」的单子（那种止损等不到）
          max_notional_abs 名义价值的绝对上限（实盘用：可用保证金 × 杠杆）
        """
        if equity <= 0:
            return RiskDecision(False, reason="权益<=0")
        if self.halted():
            return RiskDecision(False, reason=f"熔断中：{self.halt_reason()}")
        if not stop or entry <= 0:
            return RiskDecision(False, reason="缺止损价，拒开仓")
        if side is not None and ((side == 1 and stop >= entry) or (side == -1 and stop <= entry)):
            # 信号出来后价格已经穿过止损位。不拦的话：回测会把它记成一笔「止损价高于入场价」的假盈利，
            # 实盘会带着一个立刻触发的止损开仓。
            return RiskDecision(False, reason="止损在入场价的错误一侧（价格已穿过止损位）", stop=stop)

        risk_per_unit_px = abs(entry - stop)
        if risk_per_unit_px <= 0:
            return RiskDecision(False, reason="止损=入场价")
        if risk_per_unit_px / entry < 0.0005:
            # 止损太近，容易被噪音打掉，同时张数会爆大
            return RiskDecision(False, reason=f"止损过近（{risk_per_unit_px/entry*100:.3f}%）")
        if liq_lever and float(liq_lever) > 0:
            liq_dist = 1.0 / float(liq_lever) - float(mmr)
            if risk_per_unit_px / entry >= liq_dist * 0.8:
                return RiskDecision(
                    False, stop=stop,
                    reason=f"止损距离 {risk_per_unit_px/entry*100:.2f}% 贴近或超过 {float(liq_lever):.0f}x "
                           f"逐仓的强平距离 {liq_dist*100:.2f}%，止损等不到就先爆仓")

        risk_amt = equity * self.risk_per_trade_pct / 100.0
        loss_per_contract = (risk_per_unit_px + (entry + stop) * float(cost_pct or 0.0)) * float(ct_val)
        raw_sz = risk_amt / loss_per_contract

        # 杠杆上限：名义价值 <= equity * max_leverage
        max_notional = min(equity * self.max_leverage, equity * self.max_notional_pct / 100.0)
        if max_notional_abs is not None:
            max_notional = min(max_notional, max(0.0, float(max_notional_abs)))
        max_sz_by_lev = max_notional / (entry * float(ct_val))
        sz = min(raw_sz, max_sz_by_lev)

        sz = round_to(sz, float(lot_sz), "down")
        if max_sz:
            sz = min(sz, round_to(float(max_sz), float(lot_sz), "down"))
        if sz < float(min_sz):
            return RiskDecision(
                False,
                size=0.0,
                reason=f"算得 {sz} 张 < 最小 {min_sz} 张（风险预算 {risk_amt:.2f} USDT 太小，"
                       f"按此止损需 {float(min_sz)*loss_per_contract:.2f} USDT 风险）",
                stop=stop,
            )

        notional = sz * float(ct_val) * entry
        lev = notional / equity
        return RiskDecision(True, sz, notional, lev, "通过风控", stop, risk_amt)

    def check_reward_risk(self, entry, stop, target):
        if not target:
            return True
        rr = abs(target - entry) / max(1e-12, abs(entry - stop))
        return rr >= self.min_risk_reward
