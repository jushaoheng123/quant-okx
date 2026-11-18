# -*- coding: utf-8 -*-
"""
交易通道：Paper（本地虚拟账本，持久化）+ Live（OKX 真实下单）。
两者接口一致，Trader 主循环不关心当前用哪个。
"""
import json
import os
import time
import uuid
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from okx_client import OkxError


class PaperBroker:
    kind = "paper"

    def __init__(self, client, inst_id, inst, state_path, initial_equity=1000.0,
                 fee_pct=0.0005, slip_pct=0.0002):
        self.client = client
        self.inst_id = inst_id
        self.inst = inst
        self.state_path = state_path
        self.fee = fee_pct
        self.slip = slip_pct
        self.state = {
            "cash": float(initial_equity),
            "initial_equity": float(initial_equity),
            "position": None,
            "trades": [],
            "created": int(time.time() * 1000),
        }
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    self.state.update(json.load(f))
            except Exception:
                pass
        self._last_px = 0.0

    # ---- 持久化 ----
    def save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        self.state["updated"] = int(time.time() * 1000)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ---- 行情 ----
    def mark(self):
        t = self.client.ticker(self.inst_id)
        self._last_px = float(t["last"]) if t else self._last_px
        return self._last_px

    # ---- 账户 ----
    def equity(self):
        px = self._last_px or self.mark()
        pos = self.state["position"]
        unreal = 0.0
        if pos:
            unreal = (px - pos["entry_px"]) * pos["side"] * pos["size"] * float(self.inst["ctVal"])
        return self.state["cash"] + unreal

    def avail(self):
        return self.equity()

    def margin_avail(self):
        return None          # 虚拟账本不限保证金

    def foreign_position(self):
        return None

    def cash(self):
        return self.state["cash"]

    def get_position(self):
        pos = self.state["position"]
        if not pos:
            return None
        return dict(pos)

    # ---- 下单 ----
    def open(self, side, size, stop=None, reason="", cl_ord_id=None):
        """side: 1 多 / -1 空。市价成交。"""
        px = self.mark()
        if px <= 0:
            raise RuntimeError("行情不可用，无法成交")
        fill = px * (1 + self.slip * side)
        notional = fill * size * float(self.inst["ctVal"])
        f = notional * self.fee
        self.state["cash"] -= f
        self.state["position"] = {
            "side": side,
            "size": size,
            "entry_px": fill,
            "stop": stop,
            "fee": f,
            "ts": int(time.time() * 1000),
            "reason": reason,
            "cl_ord_id": cl_ord_id or f"xm{uuid.uuid4().hex[:16]}",
        }
        self.save()
        return {"ok": True, "fill_px": fill, "size": size, "fee": f, "order_id": self.state["position"]["cl_ord_id"]}

    def close(self, reason="平仓"):
        pos = self.state["position"]
        if not pos:
            return {"ok": False, "msg": "无持仓"}
        px = self.mark()
        fill = px * (1 - self.slip * pos["side"])
        ct = float(self.inst["ctVal"])
        gross = (fill - pos["entry_px"]) * pos["side"] * pos["size"] * ct
        f = fill * pos["size"] * ct * self.fee
        self.state["cash"] += gross - f
        trade = {
            "side": pos["side"],
            "entry_px": pos["entry_px"],
            "exit_px": fill,
            "size": pos["size"],
            "pnl": gross - f - pos.get("fee", 0.0),
            "fees": f + pos.get("fee", 0.0),
            "entry_ts": pos["ts"],
            "exit_ts": int(time.time() * 1000),
            "reason": reason,
        }
        self.state["trades"].append(trade)
        self.state["position"] = None
        self.save()
        return {"ok": True, "fill_px": fill, "pnl": trade["pnl"], "trade": trade}

    def update_stop(self, stop):
        if self.state["position"]:
            self.state["position"]["stop"] = stop
            self.save()

    def summary(self):
        tr = self.state["trades"]
        wins = [t for t in tr if t["pnl"] > 0]
        eq = self.equity()
        init = self.state["initial_equity"]
        return {
            "kind": "paper",
            "equity": eq,
            "cash": self.state["cash"],
            "initial_equity": init,
            "return_pct": (eq - init) / init * 100 if init else 0,
            "trades": len(tr),
            "win_rate_pct": len(wins) / len(tr) * 100 if tr else 0,
            "realized_pnl": sum(t["pnl"] for t in tr),
            "position": self.state["position"],
        }


class LiveBroker:
    """
    OKX 真实下单通道。

    核心是一本【本地账本】（ledger，落盘）：只有本策略自己开的仓才记在账本里。
      · 止损价记在账本里 → Trader 的本地止损守护在实盘也生效（原先 stop 恒为 None，守护形同虚设）
      · 策略权益 = 本金 + 已实现盈亏 + 本策略持仓的浮盈亏
        → 熔断只看本策略自己的盈亏。账上别的手动仓位怎么波动都不会误触发；
          开仓占用保证金也不会被当成「亏损」（用可用余额当权益时，一开仓就会被判日亏熔断并强平）
      · 账本里没有、交易所却有的持仓 = 你手动开的仓 → 不认领、不平、并且暂停在这个合约上开新仓
      · 开仓前先落盘「在途订单」，网络中断或进程崩溃后靠它对账，不重复下单
    """

    kind = "live"

    def __init__(self, client, inst_id, inst, pos_mode="net", td_mode="cross", lever=3,
                 fee_pct=0.0005, slip_pct=0.0, dry_run=False, ledger_path=None,
                 capital_usdt=0.0, require_exchange_stop=True, log=None, sleep=None):
        self.client = client
        self.inst_id = inst_id
        self.inst = inst
        self.pos_mode = pos_mode
        self.td_mode = td_mode
        self.lever = lever
        self.fee = fee_pct
        self.slip = slip_pct
        self.dry_run = dry_run
        self.require_exchange_stop = require_exchange_stop
        self._log = log or (lambda msg, tag="INFO": print(f"  [{tag}] {msg}"))
        self._sleep = sleep or time.sleep
        self._last_px = 0.0
        self._ex_pos = None           # 最近一次查到的、属于本策略的交易所持仓
        self._foreign = None          # 最近一次查到的、不属于本策略的持仓
        self._missing_since = 0       # 账本有仓但交易所查不到：第一次发现的时间
        self._warned_size = False
        self._cap_checked = -10 ** 15  # 上次尝试读取本金的时间
        self.ledger_path = None if dry_run else ledger_path
        self.ledger = {
            "base_capital": 0.0, "realized": 0.0, "position": None, "pending_open": None,
            "trades": [], "created": self._now(),
        }
        if self.ledger_path and os.path.exists(self.ledger_path):
            try:
                with open(self.ledger_path, "r", encoding="utf-8") as f:
                    self.ledger.update(json.load(f))
            except Exception as e:
                # 账本读坏了不能当没事：里面可能记着一个在场的仓位
                raise RuntimeError(f"实盘账本 {self.ledger_path} 读取失败（{e}）。"
                                   f"请先人工确认交易所上有没有本策略的持仓，再处理这个文件。")
        self._init_account()
        self._init_capital(capital_usdt)

    # ---- 基础 ----
    def _now(self):
        fn = getattr(self.client, "now_ms", None)
        return fn() if fn else int(time.time() * 1000)

    def _save(self):
        if not self.ledger_path:
            return
        os.makedirs(os.path.dirname(self.ledger_path), exist_ok=True)
        self.ledger["updated"] = self._now()
        tmp = self.ledger_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.ledger, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.ledger_path)

    def _pos_side(self, side):
        if self.pos_mode == "long_short":
            return "long" if side == 1 else "short"
        return None

    def _init_account(self):
        # 只读取账户的持仓模式并照它来，不去改它：
        # 改持仓模式是账户级操作，会影响账上其他合约的手动仓位（原先是直接 set，失败了还不吭声）。
        try:
            cfg = self.client.account_config()
            real = {"long_short_mode": "long_short", "net_mode": "net"}.get(cfg.get("posMode"))
            if real and real != self.pos_mode:
                self._log(f"账户实际持仓模式是 {real}，配置写的是 {self.pos_mode} → 按账户实际的来", "WARN")
                self.pos_mode = real
        except Exception as e:
            self._log(f"读取账户配置失败（{e}），按配置的持仓模式 {self.pos_mode} 继续", "WARN")
        if self.dry_run:
            return
        try:
            self.client.set_leverage(self.inst_id, self.lever, self.td_mode)
        except Exception as e:
            self._log(f"设置杠杆失败（{e}），沿用账户原有杠杆", "WARN")

    def _init_capital(self, capital_usdt=0.0):
        """策略本金：配置里写了 live.capital_usdt 就用它；否则取第一次启动时账上的可用保证金，之后不再变。"""
        if capital_usdt and float(capital_usdt) > 0:
            if abs(self.ledger["base_capital"] - float(capital_usdt)) > 1e-9:
                self.ledger["base_capital"] = float(capital_usdt)
                self._save()
            return
        if self.ledger["base_capital"] > 0 or self.ledger.get("position"):
            return
        now = self._now()
        if now - self._cap_checked < 300000:      # 没本金时每 5 分钟重看一次，别每轮都查
            return
        self._cap_checked = now
        try:
            base = self.margin_avail()
        except Exception as e:
            self._log(f"读取可用保证金失败（{e}），策略本金暂时为 0，读到之前不会开仓", "WARN")
            return
        # 可用保证金连最小一张单都开不起（多半是被账上别的仓位占着）→ 不能把这个数记成本金，
        # 否则之后腾出钱来，本金还是这个零头，风控按它算，永远开不了仓。
        try:
            need = float(self.inst["minSz"]) * float(self.inst["ctVal"]) * self.mark() / float(self.lever or 1)
        except Exception:
            need = 0.0
        if base < need * 2:
            self._log(f"账上可用保证金只有 {base:.4f} USDT，最小一张单约需 {need:.4f} USDT 保证金 → "
                      f"暂不记策略本金、不会开仓。腾出保证金后会自动重新读取，"
                      f"或在 config.json 写 live.capital_usdt 固定本金", "WARN")
            return
        if base > 0:
            self.ledger["base_capital"] = base
            self._save()
            self._log(f"策略本金按当前可用保证金记为 {base:.4f} USDT（想固定就在 config.json 写 live.capital_usdt）")

    def _align_stop(self, stop, side):
        """止损价对齐到 tickSz，往入场价方向取整（多单向上、空单向下）→ 实际风险只会比算的小。"""
        tick = Decimal(str(self.inst.get("tickSz") or "0"))
        if tick <= 0:
            return float(stop)
        q = (Decimal(repr(float(stop))) / tick).to_integral_value(
            rounding=ROUND_CEILING if side == 1 else ROUND_FLOOR)
        return float(q * tick)

    # ---- 行情 / 账户 ----
    def mark(self):
        t = self.client.ticker(self.inst_id)
        self._last_px = float(t["last"]) if t else self._last_px
        return self._last_px

    def equity(self):
        """策略权益 = 本金 + 已实现盈亏 + 本策略持仓浮盈亏。先调 get_position() 再调它，浮盈亏才是新的。"""
        if self.ledger["base_capital"] <= 0:
            self._init_capital()
        upl = self._ex_pos["upl"] if (self._ex_pos and self.ledger.get("position")) else 0.0
        return self.ledger["base_capital"] + self.ledger["realized"] + upl

    def account_equity(self):
        """整个账户的总权益（含账上其他仓位），只用于展示。"""
        return self.client.equity_usdt()

    def margin_avail(self):
        """账上此刻能拿来开仓的保证金（USDT）。只用来给仓位封顶，不拿它当权益。"""
        rows = self.client.balance("USDT")
        if not rows:
            return 0.0
        for d in rows[0].get("details", []):
            if d.get("ccy") == "USDT":
                return float(d.get("availEq") or d.get("availBal") or d.get("cashBal") or 0.0)
        return 0.0

    def avail(self):
        return self.equity()

    def cash(self):
        return self.margin_avail()

    def foreign_position(self):
        """这个合约上不属于本策略的持仓（你手动开的）。有的话 Trader 不在这个合约上开新仓。"""
        return self._foreign

    # ---- 持仓 ----
    def _exchange_positions(self):
        out = []
        for p in self.client.positions(self.inst_id):
            if p.get("instId") and p.get("instId") != self.inst_id:
                continue
            if p.get("mgnMode") and p.get("mgnMode") != self.td_mode:
                continue
            sz = float(p.get("pos") or 0.0)
            if abs(sz) <= 0:
                continue
            # 双向持仓模式下 pos 恒为正，方向看 posSide；买卖模式下看 pos 的正负号
            ps = p.get("posSide")
            side = 1 if ps == "long" else -1 if ps == "short" else (1 if sz > 0 else -1)
            out.append({
                "side": side, "size": abs(sz), "entry_px": float(p.get("avgPx") or 0.0),
                "upl": float(p.get("upl") or 0.0), "pos_id": p.get("posId"),
                "liq_px": p.get("liqPx"), "raw": p,
            })
        return out

    def get_position(self):
        """只返回【本策略自己的】持仓（账本里有记录的），带本地记的止损价。"""
        exs = self._exchange_positions()
        led = self.ledger.get("position")
        pend = self.ledger.get("pending_open")
        now = self._now()
        mine = None
        if led:
            mine = next((e for e in exs if e["side"] == led["side"]), None)
        elif pend:
            cand = next((e for e in exs if e["side"] == pend["side"]), None)
            if cand:
                # 下单后网络断了/进程崩了，但订单其实成交了 → 认领，补记账本
                led = self._record_position(pend, cand["entry_px"], cand["size"], None)
                mine = cand
                self._log(f"在途订单 {pend['cl_ord_id']} 已在交易所成交，补记账本（止损 {led['stop']}）", "WARN")
                self._ensure_stop(led)
            elif now - pend["ts"] > 120000:
                self._log(f"在途订单 {pend['cl_ord_id']} 两分钟内没有成交迹象，作废", "WARN")
                self.ledger["pending_open"] = None
                self._save()
        self._foreign = next((e for e in exs if e is not mine), None)

        if led and not mine:
            # 账本有仓、交易所没有：止损被触发了，或者被手动平掉了。
            # 刚开仓的头几秒持仓可能还没出现在查询结果里，所以要连续 8 秒以上查不到才认定。
            self._ex_pos = None
            if not self._missing_since:
                self._missing_since = now
            if now - self._missing_since >= 8000 and now - led["ts"] >= 8000:
                trade = self._settle(led, "交易所止损或外部平仓", led.get("stop"))
                self._log(f"本策略持仓已不在交易所 → 结算 {trade['pnl']:+.4f} USDT（{trade['pnl_src']}）", "STOP")
                self._cancel_own_algos(led)
                return None
            return self._view(led, None, unconfirmed=True)
        self._missing_since = 0
        self._ex_pos = mine
        if not led:
            return None
        if abs(mine["size"] - led["size"]) > 1e-9 and not self._warned_size:
            self._warned_size = True
            self._log(f"交易所持仓 {mine['size']} 张与账本 {led['size']} 张不一致（有手动加减仓？）按交易所的算", "WARN")
        return self._view(led, mine)

    @staticmethod
    def _view(led, ex, unconfirmed=False):
        return {
            "side": led["side"],
            "size": ex["size"] if ex else led["size"],
            "entry_px": (ex["entry_px"] if ex and ex["entry_px"] else led["entry_px"]),
            "stop": led.get("stop"),
            "upl": ex["upl"] if ex else 0.0,
            "pos_id": ex["pos_id"] if ex else None,
            "ts": led["ts"],
            "stop_on_exchange": led.get("stop_on_exchange"),
            "unconfirmed": unconfirmed,
        }

    def _record_position(self, pend, entry_px, size, order):
        led = {
            "side": pend["side"], "size": size, "entry_px": entry_px, "stop": pend["stop"],
            "ts": self._now(), "cl_ord_id": pend["cl_ord_id"], "algo_cl_ids": [pend["algo_cl_id"]],
            "reason": pend.get("reason", ""), "ord_id": (order or {}).get("ordId"),
            "stop_on_exchange": None,
        }
        self.ledger["position"] = led
        self.ledger["pending_open"] = None
        self._missing_since = 0
        self._warned_size = False
        self._save()
        return led

    # ---- 止损链 ----
    def _stop_on_exchange(self, side):
        """
        交易所上有没有【本策略这张仓位】的止损单：
          True  确认有（且能对上本策略自己挂的单号）
          False 确认没有
          None  查不了

        ★ 必须只认自己的 algoClOrdId。原先只判断「这个合约上有没有 slTriggerPx」，
          如果同一合约上还有你手动开的仓、且它自己挂了止损，就会被误判成
          「本策略的止损已挂」→ 不补挂 → 一旦那个止损被撤，本策略就裸奔了。
          （对抗测试验证）
        """
        close_side = "sell" if side == 1 else "buy"
        led = self.ledger.get("position") or {}
        if not led:
            # 账本里没有本策略的持仓 → 不存在「本策略的止损单」。
            # 不能因为这个合约上恰好有别人（手动仓）的止损就返回 True。
            return False
        own = set(led.get("algo_cl_ids") or [])
        strict = bool(own)          # 账本登记过单号 → 严格按单号认；没有 → 退化为宽松（兼容老账本）
        try:
            for p in self.client.positions(self.inst_id):
                for a in (p.get("closeOrderAlgo") or []):
                    if not a.get("slTriggerPx"):
                        continue
                    if strict and a.get("algoClOrdId") not in own:
                        continue
                    return True
            for ot in ("conditional", "oco"):
                for a in self.client.algo_pending(self.inst_id, ot):
                    if not a.get("slTriggerPx"):
                        continue
                    if a.get("side", close_side) != close_side:
                        continue
                    if strict and a.get("algoClOrdId") not in own:
                        continue
                    return True
            return False
        except Exception as e:
            self._log(f"查询止损单失败（{e}）", "WARN")
            return None

    def _ensure_stop(self, led):
        """确认止损真的挂在交易所上；确认没有就补挂一张独立止损单。结果记进账本。"""
        st = None
        for _ in range(3):
            st = self._stop_on_exchange(led["side"])
            if st:
                break
            self._sleep(1.0)
        if st is False:
            algo_cl = f"xs{uuid.uuid4().hex[:16]}"
            try:
                self.client.place_stop_algo(
                    self.inst_id, "sell" if led["side"] == 1 else "buy", led["stop"],
                    td_mode=self.td_mode, pos_side=self._pos_side(led["side"]), algo_cl_ord_id=algo_cl)
                led.setdefault("algo_cl_ids", []).append(algo_cl)
                st = True
                self._log(f"开仓单附带的止损没挂上，已补挂独立止损单 @ {led['stop']}", "WARN")
            except Exception as e:
                self._log(f"补挂止损单失败（{e}）", "ERROR")
        led["stop_on_exchange"] = st
        self._save()
        if st is None:
            # 查不到 ≠ 已挂上。这时只剩 Trader 的本地止损守护在兜底（每轮轮询价格）。
            # 进程一挂就真裸奔了，所以必须喊出来让人工看一眼。
            self._log(f"⚠ 无法确认交易所止损单是否存在（查询失败）。"
                      f"当前只剩本地止损守护（止损价 {led['stop']}）。"
                      f"请到 OKX 上确认「止损委托」有没有这张单，或直接手动平仓。", "ERROR")
        return st

    def _cancel_own_algos(self, led):
        """撤掉本策略自己挂的、还没触发的止损单（只认自己的编号，不碰你手动挂的）。"""
        own = set(led.get("algo_cl_ids") or [])
        if not own:
            return
        try:
            ids = []
            for ot in ("conditional", "oco"):
                for a in self.client.algo_pending(self.inst_id, ot):
                    if a.get("algoClOrdId") in own and a.get("algoId"):
                        ids.append(a["algoId"])
            if ids:
                self.client.cancel_algos(self.inst_id, ids)
                self._log(f"已撤掉残留的止损单 {len(ids)} 张")
        except Exception as e:
            self._log(f"清理残留止损单失败（{e}），请到 OKX 上看一眼「策略委托」", "WARN")

    # ---- 下单 ----
    def _order_outcome(self, cl, tries=6):
        """用 clOrdId 查单：('filled', 订单) / ('none', None) 确认没成交 / ('unknown', None) 查不了。"""
        seen, missing = None, 0
        for _ in range(tries):
            try:
                od = self.client.order(self.inst_id, cl_ord_id=cl)
                if od:
                    seen = od
                    filled = float(od.get("accFillSz") or 0.0)
                    state = od.get("state")
                    if state == "filled" or (state in ("canceled", "mmp_canceled") and filled > 0):
                        return "filled", od
                    if state in ("canceled", "mmp_canceled"):
                        return "none", None
                else:
                    missing += 1
            except OkxError as e:
                code = str(e.code)
                if len(code) == 5 and code.isdigit():      # 业务错误码（如订单不存在）
                    missing += 1
            except Exception:
                pass
            self._sleep(1.0)
        if seen is None and missing >= tries:
            return "none", None
        return "unknown", None

    def open(self, side, size, stop=None, reason="", cl_ord_id=None):
        cl = cl_ord_id or f"xm{uuid.uuid4().hex[:16]}"
        order_side = "buy" if side == 1 else "sell"
        if self.dry_run:
            px = self.mark()
            return {"ok": True, "dry_run": True, "fill_px": px, "size": size, "order_id": cl}
        if not stop:
            return {"ok": False, "msg": "实盘不允许无止损开仓"}
        if self.ledger.get("position") or self.ledger.get("pending_open"):
            return {"ok": False, "msg": "账本里已有持仓或在途订单，不重复开仓"}
        stop_px = self._align_stop(stop, side)
        pend = {"cl_ord_id": cl, "algo_cl_id": f"xs{uuid.uuid4().hex[:16]}", "side": side,
                "size": size, "stop": stop_px, "ts": self._now(), "reason": reason}
        # ① 先把「我要下这张单」落盘，再发单
        self.ledger["pending_open"] = pend
        self._save()
        try:
            res = self.client.place_order(
                self.inst_id, order_side, size, ord_type="market", td_mode=self.td_mode,
                pos_side=self._pos_side(side), cl_ord_id=cl, sl_trigger_px=stop_px,
                attach_algo_cl_ord_id=pend["algo_cl_id"],
            )
            r = res[0] if res else {}
            if r.get("sCode", "0") != "0":
                self.ledger["pending_open"] = None
                self._save()
                return {"ok": False, "cl_ord_id": cl, "sCode": r.get("sCode"), "sMsg": r.get("sMsg")}
        except OkxError as e:
            code = str(e.code)
            ambiguous = code in ("NET_AMBIGUOUS", "NET") or (len(code) == 3 and code.startswith("5"))
            if not ambiguous:
                self.ledger["pending_open"] = None
                self._save()
                return {"ok": False, "cl_ord_id": cl, "sCode": code, "sMsg": e.msg}
            self._log(f"下单请求结果不明（{e}），用 clOrdId 查单确认", "WARN")
        # ② 查单确认成交，拿真实成交价和成交张数
        outcome, od = self._order_outcome(cl)
        if outcome == "none":
            self.ledger["pending_open"] = None
            self._save()
            return {"ok": False, "cl_ord_id": cl, "msg": "订单未成交"}
        if outcome == "unknown":
            return {"ok": False, "cl_ord_id": cl, "msg": "订单结果查不到，在途记录已保留，下一轮按交易所持仓对账"}
        fill_px = float(od.get("avgPx") or 0.0)
        fill_sz = float(od.get("accFillSz") or size)
        led = self._record_position(pend, fill_px, fill_sz, od)
        # ③ 确认止损真的挂上了
        st = self._ensure_stop(led)
        if st is False and self.require_exchange_stop:
            c = self.close("止损单挂不上，不留裸仓")
            return {"ok": False, "cl_ord_id": cl, "msg": "止损单挂不上，已立即平仓", "close": c}
        return {"ok": True, "order_id": od.get("ordId"), "cl_ord_id": cl, "size": fill_sz,
                "fill_px": fill_px, "stop": stop_px, "stop_on_exchange": st}

    def close(self, reason="平仓"):
        pos = self.get_position()
        if not pos:
            return {"ok": False, "msg": "无本策略持仓"}
        if self.dry_run:
            return {"ok": True, "dry_run": True, "msg": f"将平 {pos['size']} 张"}
        led = self.ledger["position"]
        px = 0.0
        try:
            px = self.mark()
        except Exception:
            pass
        err = None
        try:
            self.client.close_position(self.inst_id, self.td_mode, self._pos_side(led["side"]))
        except OkxError as e:
            err = e          # 可能是仓位刚好已经被止损单平掉，往下查了再说
        gone = False
        for _ in range(6):
            try:
                if not any(e["side"] == led["side"] for e in self._exchange_positions()):
                    gone = True
                    break
            except Exception:
                pass
            self._sleep(1.0)
        if not gone:
            return {"ok": False, "msg": f"平仓后仓位仍在（{err or '等待成交'}），下一轮重试", "pos": pos}
        trade = self._settle(led, reason, px or led.get("stop"))
        self._cancel_own_algos(led)
        return {"ok": True, "pnl": trade["pnl"], "trade": trade, "pos": pos}

    def _settle(self, led, reason, est_exit_px):
        """仓位结束后记账。优先用交易所的历史持仓记录（含手续费和资金费），拿不到再估算。"""
        pnl, exit_px, src = None, float(est_exit_px or 0.0), "估算"
        for _ in range(4):
            try:
                for r in self.client.positions_history(self.inst_id, limit=10):   # 最新的在前
                    if int(r.get("uTime") or 0) >= led["ts"] - 5000 \
                            and int(r.get("cTime") or 0) >= led["ts"] - 120000:
                        pnl = float(r.get("realizedPnl") or 0.0)
                        exit_px = float(r.get("closeAvgPx") or exit_px)
                        src = "交易所记录"
                        break
            except Exception:
                pass
            if pnl is not None:
                break
            self._sleep(1.5)
        if pnl is None:
            ct = float(self.inst["ctVal"])
            gross = (exit_px - led["entry_px"]) * led["side"] * led["size"] * ct
            pnl = gross - (led["entry_px"] + exit_px) * led["size"] * ct * self.fee
        trade = {
            "side": led["side"], "size": led["size"], "entry_px": led["entry_px"], "exit_px": exit_px,
            "stop": led.get("stop"), "pnl": pnl, "pnl_src": src, "entry_ts": led["ts"],
            "exit_ts": self._now(), "reason": reason, "open_reason": led.get("reason", ""),
        }
        self.ledger["realized"] += pnl
        self.ledger["trades"] = (self.ledger["trades"] + [trade])[-500:]
        self.ledger["position"] = None
        self._ex_pos = None
        self._missing_since = 0
        self._save()
        return trade

    def update_stop(self, stop):
        """只改本地记的止损价（本地守护用）。交易所上的止损单仍在开仓时的位置，不会跟着动。"""
        led = self.ledger.get("position")
        if not led:
            return {"ok": False, "msg": "无本策略持仓"}
        led["stop"] = self._align_stop(stop, led["side"])
        self._save()
        return {"ok": True, "note": "已更新本地止损价；交易所止损单未移动"}

    def summary(self):
        pos = self.get_position()
        return {
            "kind": "live" + ("(dry-run)" if self.dry_run else ""),
            "equity": self.equity(),
            "base_capital": self.ledger["base_capital"],
            "realized_pnl": self.ledger["realized"],
            "trades": len(self.ledger["trades"]),
            "position": pos,
            "foreign_position": bool(self._foreign),
        }
