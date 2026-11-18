# -*- coding: utf-8 -*-
"""实盘/模拟盘主循环。核心：只在【已收盘 K 线】上算信号，绝不看未收盘 bar。

2026-11-16 审查后的改动（原版在 _备份_审查前_20261116\\trader.py）：
  · 熔断与仓位都按「策略权益」算（见 broker.LiveBroker），不再用可用余额
  · K 线窗口在进程内只增不减（起点固定），同一个信号不会因为窗口滑动而前后算得不一样
  · 开仓信号有保鲜期：只认最后一根已收盘 bar 的信号，过了时限或价格跑远了就放弃
  · 先把「这个信号我处理过了」落盘，再下单 → 崩溃重启最多漏一笔，不会重复开仓
  · 每轮对账：策略已经空仓而手里还有仓，就平掉（平仓失败/漏掉的离场信号在这里兜住）
  · 合约上有你手动开的仓时，不在这个合约上开新仓，也不去平它
  · 同一模式只允许一个进程（文件锁）
"""
import json
import os
import sys
import time
import traceback
from datetime import datetime

# Windows 控制台默认 GBK —— 强制 UTF-8，否则日志里的符号/emoji 会炸
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from broker import LiveBroker, PaperBroker
from config import BASE_DIR
from okx_client import OkxClient
from risk import RiskManager
from strategy import build_strategy

LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")


def log(msg, tag="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{tag}] {msg}"
    print(line, flush=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"trader-{datetime.now().strftime('%Y%m%d')}.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def acquire_lock(path):
    """独占文件锁，进程活着就一直占着。拿不到 = 已经有一个同模式的机器人在跑。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise RuntimeError(f"已经有一个同模式的机器人在运行（锁文件 {path}）。两个一起跑会各下各的单，拒绝启动。")
    return f


class Trader:
    def __init__(self, cfg, mode="paper", dry_run=None, client=None, data_dir=None, now_ms=None):
        self.cfg = cfg
        self.mode = mode
        self.inst_id = cfg["inst_id"]
        self.bar = cfg["bar"]
        self.data_dir = data_dir or DATA_DIR
        os.makedirs(self.data_dir, exist_ok=True)

        creds = cfg.get("_creds", {})
        self.client = client or OkxClient(
            api_key=creds.get("api_key"),
            secret=creds.get("secret"),
            passphrase=creds.get("passphrase"),
            simulated=creds.get("simulated", True),
            hosts=cfg.get("hosts"),
        )
        try:
            off = self.client.sync_time()
            if abs(off) > 2000:
                log(f"本机时钟与交易所相差 {off} ms，已按交易所时间校准签名", "WARN")
        except Exception as e:
            log(f"时钟校准失败（{e}），按本机时间继续", "WARN")
        self.now_ms = now_ms or getattr(self.client, "now_ms", None) or (lambda: int(time.time() * 1000))

        self.inst = self.client.instrument(self.inst_id)
        if not self.inst:
            raise RuntimeError(f"拿不到合约元数据 {self.inst_id}")
        log(f"合约 {self.inst_id}: ctVal={self.inst['ctVal']} lotSz={self.inst['lotSz']} "
            f"minSz={self.inst['minSz']} tickSz={self.inst['tickSz']} maxLever={self.inst.get('lever')}")

        # ★ 状态文件按「通道」分开：paper / live-real（真钱）/ live-sim（OKX 模拟盘）/ live-dry（只算不下单）
        #   原先 live 三种共用一套文件：先在 OKX 模拟盘（10 万 U）跑过，再切真钱 4 U，
        #   风控会算出 99.99% 回撤直接熔断 —— 和「paper 污染 live」是同一个坑。
        if mode == "paper":
            self.tag = "paper"
            dr = False
        elif mode == "live":
            lv = cfg["live"]
            dr = lv.get("dry_run", True) if dry_run is None else dry_run
            self.tag = "live-" + ("dry" if dr else ("sim" if getattr(self.client, "simulated", False) else "real"))
        else:
            raise ValueError(f"未知模式 {mode}")

        # 策略
        sp = dict(cfg.get("strategy_params") or {})
        if mode == "live" and not cfg["live"].get("allow_short", True):
            sp["allow_short"] = False
        self.strategy = build_strategy(cfg["strategy"], **sp)
        log(f"策略 {cfg['strategy']} 参数 {self.strategy.params or sp} 预热 {self.strategy.warmup} 根")

        # 风控
        rk = cfg["risk"]
        self.risk = RiskManager(
            risk_per_trade_pct=rk["risk_pct"],
            max_leverage=rk["max_leverage"],
            max_daily_loss_pct=rk["max_daily_loss_pct"],
            max_drawdown_pct=rk["max_drawdown_pct"],
            max_positions=rk.get("max_positions", 1),
            state_path=os.path.join(self.data_dir, f"risk_state_{self.tag}.json"),
        )

        # 通道
        self.sizing_kw = {}
        if mode == "paper":
            p = cfg["paper"]
            self.broker = PaperBroker(
                self.client, self.inst_id, self.inst,
                state_path=os.path.join(self.data_dir, "paper_state.json"),
                initial_equity=p["initial_equity"],
                fee_pct=p["fee_pct"], slip_pct=p["slip_pct"],
            )
            self.cost_pct = float(p["fee_pct"]) + float(p["slip_pct"])
            self.lever = None
            log(f"通道 PAPER 虚拟账本，初始权益 {p['initial_equity']} USDT", "OK")
        else:
            lv = cfg["live"]
            safe_inst = self.inst_id.replace("/", "_")
            self.broker = LiveBroker(
                self.client, self.inst_id, self.inst,
                pos_mode=lv.get("pos_mode", "net"),
                td_mode=lv.get("td_mode", "cross"),
                lever=lv.get("lever", 3),
                fee_pct=float(lv.get("fee_pct", 0.0005)),
                dry_run=dr,
                ledger_path=os.path.join(self.data_dir, f"ledger_{self.tag}_{safe_inst}.json"),
                capital_usdt=float(lv.get("capital_usdt", 0) or 0),
                require_exchange_stop=bool(lv.get("require_exchange_stop", True)),
                log=log,
            )
            self.cost_pct = float(lv.get("fee_pct", 0.0005)) + float(lv.get("slip_pct", 0.0002))
            self.lever = float(lv.get("lever", 3))
            if lv.get("td_mode", "cross") == "isolated":
                self.sizing_kw["liq_lever"] = self.lever      # 逐仓：止损必须在强平线以内
            tag = "DRY-RUN（只算不下单）" if dr else (
                "OKX 模拟盘下单" if self.tag == "live-sim" else "★ 真实下单 ★")
            log(f"通道 LIVE {tag}，杠杆 {lv.get('lever', 3)}x，策略本金 "
                f"{self.broker.ledger['base_capital']:.4f} USDT，累计已实现 {self.broker.ledger['realized']:+.4f}", "OK")

        lp = cfg.get("loop") or {}
        self.poll = int(lp.get("poll_seconds", 20))
        self.history_bars = int(lp.get("history_bars", 1200))
        self.max_entry_delay_s = float(lp.get("max_entry_delay_s", 0) or 0)   # 0 = 按周期自动
        self.max_drift_atr = float(lp.get("max_entry_drift_atr", 1.0))
        self._candles = []
        self._lock = None

        self.state_path = os.path.join(self.data_dir, f"{self.tag}_loop_state.json")
        self.state = {"last_action_ts": 0, "last_kind": "", "last_bar_ts": 0}
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    self.state.update(json.load(f))
            except Exception as e:
                log(f"循环状态文件读取失败（{e}），按首次启动处理（历史信号不重放）", "WARN")

    def save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ---------------- K 线 ----------------
    def _load_candles(self):
        """
        K 线窗口在进程内「起点固定、只往后长」。
        原先每轮重新取最近 600 根：窗口一滑动，EMA 的起算点跟着变，策略内部的状态机从头重放，
        同一根 bar 前后两次可能算出不同的信号（上一轮说在这根开仓，下一轮说没有）。
        起点固定后，同一进程里历史信号是确定的；每轮也只需要 1 次请求而不是 2 次以上。
        """
        target = max(self.strategy.warmup + 120, self.history_bars)
        if self._candles:
            fresh = self.client.candles(self.inst_id, self.bar, 100)
            if fresh and fresh[0]["ts"] > self._candles[-1]["ts"]:
                self._candles = []                 # 断档了（断网太久）→ 整段重拉
            elif fresh:
                seen = {c["ts"]: c for c in self._candles}
                for c in fresh:
                    seen[c["ts"]] = c
                self._candles = sorted(seen.values(), key=lambda x: x["ts"])
        if not self._candles:
            got = self.client.candles(self.inst_id, self.bar, 300)
            for _ in range(12):
                if not got or len(got) >= target:
                    break
                older = self.client.candles(self.inst_id, self.bar, 300, after=got[0]["ts"])
                if not older or older[0]["ts"] >= got[0]["ts"]:
                    break
                seen = {c["ts"]: c for c in older + got}
                got = sorted(seen.values(), key=lambda x: x["ts"])
            self._candles = got
        if len(self._candles) > 6000:
            self._candles = self._candles[-3000:]
        return self._candles

    def _entry_deadline_s(self, bar_ms):
        if self.max_entry_delay_s > 0:
            return self.max_entry_delay_s
        return max(self.poll * 2 + 10, min(bar_ms / 1000.0 * 0.25, 300.0))

    # ---------------- 单轮 ----------------
    def run_once(self):
        candles = self._load_candles()
        closed = [c for c in candles if c["confirm"]]
        if len(closed) < self.strategy.warmup + 5:
            log(f"已收盘 K 线只有 {len(closed)} 根，不足预热 {self.strategy.warmup}，本轮跳过", "WARN")
            return
        bar_ms = closed[-1]["ts"] - closed[-2]["ts"]

        last_px = self.broker.mark()                # 同时刷新 paper 账本的浮盈亏用价
        pos = self.broker.get_position()            # 只返回本策略自己的仓（实盘带本地记的止损价）
        eq = self.broker.equity()                   # ★ 策略权益：本金 + 已实现 + 本策略浮盈亏
        alive = self.risk.update_equity(eq)

        if not alive:
            log(f"⛔ 熔断生效：{self.risk.halt_reason()}", "HALT")
            if pos:
                log("熔断且有持仓 → 强制平仓", "HALT")
                r = self.broker.close("熔断强平")
                log(f"平仓结果 {r}", "HALT")
            return

        # 1) 本地止损守护：交易所止损单之外的第二道（价格击穿本地记的止损而仓位还在，就市价平）
        if pos and pos.get("stop"):
            bust = (pos["side"] == 1 and last_px <= pos["stop"]) or \
                   (pos["side"] == -1 and last_px >= pos["stop"])
            if bust:
                log(f"⚡ 价格 {last_px} 击穿止损 {pos['stop']} → 平仓", "STOP")
                r = self.broker.close("止损触发")
                log(f"平仓结果 {r}", "STOP")
                pos = self.broker.get_position()

        # 2) 策略信号
        actions = self.strategy.signals(closed)
        last_bar = closed[-1]["ts"]

        # ★ 首次启动绝不能重放历史信号（否则一口气开平十几次，白送手续费）
        #   基线设到「倒数第二根已收盘 bar」，于是只认最后一根 bar 产生的动作
        if not self.state.get("last_action_ts"):
            self.state["last_action_ts"] = closed[-2]["ts"] if len(closed) >= 2 else 0
            log(f"首次启动：信号基线 = {self.state['last_action_ts']}，历史信号全部作废，不重放", "INIT")

        latest = [a for a in actions if a.ts > self.state["last_action_ts"]]

        if pos:
            pos_str = "{:+d} {}张 @{} 止损{}{}".format(
                pos["side"], pos["size"], pos["entry_px"],
                (pos["stop"] if pos.get("stop") else "未挂"),
                "（交易所止损单未确认）" if pos.get("stop_on_exchange") is None and self.mode == "live" else "",
            )
        else:
            pos_str = "无"
        fp = self.broker.foreign_position()
        log(f"策略权益 {eq:.4f} | 最新价 {last_px} | 持仓 {pos_str} | "
            f"已收盘bar {len(closed)} | 新动作 {len(latest)}"
            + (f" | ⚠ 合约上另有非本策略持仓 {fp['size']} 张" if fp else ""))

        for act in latest:
            # ★ 先落盘「这个信号处理过了」，再执行。
            #   崩在两者之间：开仓信号最多漏一笔（安全的那一头）；离场信号由下面的对账兜住。
            #   原先是先下单后落盘：崩在中间，重启后同一个信号再下一次单。
            self.state["last_action_ts"] = act.ts
            self.state["last_kind"] = act.kind
            self.save_state()
            self.execute(act, pos, last_px, last_bar, bar_ms)
            pos = self.broker.get_position()

        # 3) 对账：策略此刻该是什么方向，手里的仓对不对得上
        if pos and not pos.get("unconfirmed"):
            last = actions[-1] if actions else None
            want = 0 if (last is None or last.kind == "exit") else (1 if last.kind == "enter_long" else -1)
            if want != pos["side"]:
                why = "策略当前应为空仓" if want == 0 else "策略方向与持仓相反"
                log(f"对账不一致（{why}）→ 平仓", "ORDER")
                r = self.broker.close(f"对账平仓：{why}")
                log(f"  平仓回报 {r}", "ORDER")

        self.state["last_bar_ts"] = last_bar
        self.save_state()

    def execute(self, act, pos, last_px, last_bar, bar_ms):
        ct_val = float(self.inst["ctVal"])
        if act.kind in ("enter_long", "enter_short"):
            if pos:
                log(f"已有持仓，忽略开仓信号 {act.kind}", "SKIP")
                return
            fp = self.broker.foreign_position()
            if fp:
                log(f"{self.inst_id} 上有一个不属于本策略的持仓（{fp['size']} 张），不在这个合约上开新仓", "SKIP")
                return
            # 保鲜期：信号在 bar 收盘时产生，回测假设「下一根开盘就成交」。
            # 重启、断网、熔断解除之后捡到的旧信号，价格和止损距离早就不是那回事了。
            age_s = (self.now_ms() - (act.ts + bar_ms)) / 1000.0
            limit_s = self._entry_deadline_s(bar_ms)
            if act.ts != last_bar or age_s > limit_s:
                log(f"开仓信号已过期（信号 bar 收盘后 {age_s:.0f}s，上限 {limit_s:.0f}s）→ 放弃 {act.kind}", "SKIP")
                return
            if act.atr and abs(last_px - act.price) > self.max_drift_atr * act.atr:
                log(f"价格已偏离信号价 {act.price} → {last_px}（超过 {self.max_drift_atr} 倍 ATR）→ 放弃 {act.kind}", "SKIP")
                return

            side = 1 if act.kind == "enter_long" else -1
            base = self.broker.equity()
            kw = dict(self.sizing_kw, side=side, cost_pct=self.cost_pct)
            ma = self.broker.margin_avail()
            if ma is not None and self.lever:
                kw["max_notional_abs"] = ma * self.lever * 0.9    # 保证金不够的单子交易所会拒，先自己封顶
            # 用「现在的价格」到止损的距离算张数（原先用信号 bar 的收盘价，成交价一偏，实际风险就不是 risk_pct 了）
            dec = self.risk.size_position(
                base, last_px, act.stop, ct_val,
                self.inst["lotSz"], self.inst["minSz"], self.inst.get("maxSz"), **kw
            )
            if not dec.allow:
                log(f"风控拒单：{dec.reason}", "RISK")
                return
            side_cn = "做多" if side == 1 else "做空"
            log(f"→ 开{side_cn} {dec.size} 张 | 名义 {dec.notional:.4f} USDT | 杠杆 {dec.leverage:.2f}x | "
                f"止损 {act.stop} | 风险预算 {dec.risk_amt:.4f} USDT | {act.reason}", "ORDER")
            r = self.broker.open(side, dec.size, act.stop, act.reason)
            log(f"  开仓回报 {r}", "ORDER" if r.get("ok") else "ERROR")

        elif act.kind == "exit":
            if not pos:
                return
            log(f"→ 平仓信号：{act.reason}", "ORDER")
            r = self.broker.close(act.reason)
            log(f"  平仓回报 {r}", "ORDER" if r.get("ok") else "ERROR")

    # ---------------- 循环 ----------------
    def loop(self):
        self._lock = acquire_lock(os.path.join(self.data_dir, f"{self.tag}_{self.inst_id}.lock"))
        poll = self.poll
        max_h = float(self.cfg["loop"].get("max_runtime_hours", 0) or 0)
        start = time.time()
        errs = 0
        log(f"主循环启动，轮询 {poll}s，通道 {self.tag}", "RUN")
        while True:
            try:
                self.run_once()
                errs = 0
            except KeyboardInterrupt:
                log("收到 Ctrl+C，退出", "STOP")
                break
            except Exception as e:
                errs += 1
                log(f"本轮异常（连续第 {errs} 次）：{e}\n{traceback.format_exc()}", "ERROR")
                if errs in (5, 20, 100):
                    log(f"已连续 {errs} 轮失败 —— 机器人此刻看不到行情也管不了仓位，"
                        f"持仓只剩交易所上的止损单在保护。请检查代理/网络。", "ERROR")
            if max_h > 0 and (time.time() - start) / 3600 >= max_h:
                log(f"运行 {max_h} 小时到期，退出", "STOP")
                break
            try:
                time.sleep(poll)
            except KeyboardInterrupt:
                log("收到 Ctrl+C，退出", "STOP")
                break
