# -*- coding: utf-8 -*-
"""
OKX 永续合约量化交易系统 · CLI 入口

用法：
  python run.py doctor                      # 体检：网络 / 合约 / 凭据 / 余额
  python run.py backtest                    # 回测（默认 trend 策略）
  python run.py backtest --strategy breakout --bar 1H --bars 2000
  python run.py compare                     # 三策略同段行情对比
  python run.py tune                        # 参数网格寻优
  python run.py paper                       # 模拟盘（真实行情，虚拟资金）
  python run.py live                        # 实盘 dry-run（只打印不下单）
  python run.py live --real                 # ★ 实盘真实下单 ★
  python run.py status                      # 查看模拟盘/风控状态
  python run.py reset-halt                  # 手动解除风控熔断
"""
import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import datetime

# Windows 控制台默认 GBK，直接输出 ✓ ✗ ═ 会 UnicodeEncodeError —— 强制 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import BASE_DIR, DEFAULT_CONFIG, credentials, has_credentials, load_config, save_config
from engine import Backtester
from okx_client import DEFAULT_HOSTS, OkxClient, OkxError
from strategy import REGISTRY, build_strategy

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

FALLBACK_INST = {
    "instId": "BTC-USDT-SWAP", "ctVal": "0.01", "lotSz": "0.01",
    "minSz": "0.01", "tickSz": "0.1", "maxSz": "1000000", "lever": "100",
}


def banner(t):
    print("\n" + "═" * 68)
    print(f"  {t}")
    print("═" * 68)


def make_client(cfg, private=False):
    c = credentials()
    if private and not (c["api_key"] and c["secret"] and c["passphrase"]):
        print("\n[!] 私有接口需要凭据。请在 quant-okx\\.env 里填：")
        print("    OKX_API_KEY=xxx")
        print("    OKX_API_SECRET=xxx")
        print("    OKX_PASSPHRASE=xxx")
        print("    OKX_SIMULATED=1        # 1=模拟盘(需在OKX网页开通模拟交易) 0=实盘")
        sys.exit(2)
    return OkxClient(
        api_key=c["api_key"], secret=c["secret"], passphrase=c["passphrase"],
        simulated=c["simulated"], hosts=cfg.get("hosts"),
    )


def get_inst(client, inst_id):
    try:
        inst = client.instrument(inst_id)
        return inst or dict(FALLBACK_INST)
    except OkxError as e:
        print(f"[warn] 拉合约元数据失败（{e}），用兜底值 {inst_id}")
        return dict(FALLBACK_INST)


# ══════════════════════════════════ doctor ══════════════════════════════════
def cmd_doctor(args):
    cfg = load_config()
    banner("OKX 链路体检")
    print(f"配置文件：{os.path.join(BASE_DIR, 'config.json')}")

    ok_host = None
    print("\n[1/4] 公共行情连通性")
    for h in cfg.get("hosts", DEFAULT_HOSTS):
        t0 = time.time()
        try:
            c = OkxClient(hosts=[h])
            r = c.request("GET", "/api/v5/market/ticker", {"instId": "BTC-USDT"}, quiet=True)
            px = r[0]["last"] if r else "?"
            print(f"  ✓ {h:<26} {int((time.time()-t0)*1000):>5}ms  BTC={px}")
            ok_host = ok_host or h
        except Exception as e:
            print(f"  ✗ {h:<26} {str(e)[:60]}")

    if not ok_host:
        print("\n[!] 全部域名不可达。检查网络/代理，可在 config.json 的 hosts 里加自定义镜像。")
        return

    print("\n[2/4] 合约元数据")
    client = make_client(cfg)
    inst = get_inst(client, cfg["inst_id"])
    print(f"  {inst['instId']}  ctVal={inst['ctVal']} {inst.get('ctValCcy','')}/张  "
          f"lotSz={inst['lotSz']}  minSz={inst['minSz']}  tickSz={inst['tickSz']}  maxLever={inst.get('lever')}")
    tk = client.ticker(cfg["inst_id"])
    print(f"  最新价 {tk['last']}  买一 {tk['bidPx']}  卖一 {tk['askPx']}  24h量 {tk['vol24h']}")

    print("\n[3/4] K 线可用性")
    for bar in ("15m", "1H", "4H"):
        cs = client.candles(cfg["inst_id"], bar, 300)
        closed = len([c for c in cs if c["confirm"]])
        print(f"  {bar:<5} 取到 {len(cs)} 根，已收盘 {closed} 根")

    print("\n[4/4] 凭据")
    c = credentials()
    if not has_credentials():
        print("  ✗ 未配置 .env（实盘/模拟盘私有接口不可用，回测与行情正常）")
        print("    → 复制 .env.example 为 .env 并填入 OKX API")
        return
    print(f"  ✓ API KEY {c['api_key'][:6]}...{c['api_key'][-4:]}  "
          f"模拟盘={c['simulated']}")
    try:
        pc = make_client(cfg, private=True)
        eq = pc.equity_usdt()
        print(f"  ✓ 账户总权益 {eq:.4f} USD")
        bal = pc.balance()
        if bal:
            for d in bal[0].get("details", [])[:6]:
                av = d.get("availBal") or d.get("cashBal") or "0"
                if float(av) != 0:
                    print(f"      {d['ccy']:<6} 可用 {av:<18} 权益 {d.get('eq', '0')}")
        pos = pc.positions(cfg["inst_id"])
        live_pos = [p for p in pos if abs(float(p.get("pos") or 0)) > 0]
        if live_pos:
            for p in live_pos:
                print(f"  ⚠ 已有持仓 {p['instId']} pos={p['pos']} avgPx={p['avgPx']} upl={p['upl']}")
        else:
            print("  ✓ 当前无持仓")
    except OkxError as e:
        print(f"  ✗ 私有接口报错：{e}")
        if e.code in ("50113", "50111", "50105"):
            print("    → 签名/口令错误：检查 OK-ACCESS-PASSPHRASE 是否与创建 API 时一致")
        if e.code == "50101":
            print("    → 权限不符：API Key 需要「交易」权限")
        if e.code == "401":
            print("    → 模拟盘/实盘环境不匹配：改 OKX_SIMULATED")
    print()


# ══════════════════════════════════ backtest ══════════════════════════════════
def fetch_candles(client, cfg, bar, bars, refresh=False, inst_id=None):
    """inst_id 必须显式传！否则多标的验证时会误读同一个缓存文件。"""
    inst_id = inst_id or cfg["inst_id"]
    cache = os.path.join(DATA_DIR, f"candles_{inst_id}_{bar}_{bars}.json")
    if os.path.exists(cache) and not refresh:
        age_h = (time.time() - os.path.getmtime(cache)) / 3600
        if age_h < 6:
            with open(cache, "r", encoding="utf-8") as f:
                data = json.load(f)
            print(f"  用缓存 K 线 {len(data)} 根（{inst_id} {bar}，{age_h:.1f}h 前）")
            return data
    print(f"  抓取 {inst_id} {bar} 历史 K 线，目标 {bars} 根 ...")
    data = client.fetch_history(inst_id, bar, bars)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"  到手 {len(data)} 根，覆盖 {datetime.fromtimestamp(data[0]['ts']/1000):%Y-%m-%d %H:%M} "
          f"→ {datetime.fromtimestamp(data[-1]['ts']/1000):%Y-%m-%d %H:%M}")
    return data


def print_report(rep, title="回测报告"):
    print(f"\n┌─ {title} " + "─" * max(0, 50 - len(title)))
    print(f"│ 区间        {rep['bars']} 根 bar / {rep['days']:.1f} 天")
    print(f"│ 初始权益    {rep['initial_equity']:.2f} USDT")
    print(f"│ 最终权益    {rep['final_equity']:.2f} USDT")
    print(f"│ 总收益      {rep['total_return_pct']:+.2f}%   年化 {rep['annualized_pct']:+.2f}%")
    print(f"│ 最大回撤    {rep['max_drawdown_pct']:.2f}%")
    print(f"│ 夏普比率    {rep['sharpe']:.2f}")
    print(f"│ 交易次数    {rep['trades']}（多 {rep['longs']} / 空 {rep['shorts']}）")
    print(f"│ 胜率        {rep['win_rate_pct']:.2f}%   盈亏比(盈利因子) {rep['profit_factor']:.2f}")
    print(f"│ 均盈/均亏   {rep['avg_win']:+.2f} / {rep['avg_loss']:+.2f} USDT")
    print(f"│ 平均持仓    {rep['avg_bars']:.1f} 根 bar")
    print(f"│ 手续费合计  {rep['fees_total']:.2f} USDT")
    print("└" + "─" * 58)


def cmd_backtest(args):
    cfg = load_config()
    bar = args.bar or cfg["bar"]
    bars = args.bars or cfg["backtest"]["history_bars"]
    strategy = args.strategy or cfg["strategy"]
    equity = args.equity or cfg["backtest"]["initial_equity"]

    banner(f"回测  {cfg['inst_id']}  {strategy}  {bar}")
    client = make_client(cfg)
    inst = get_inst(client, cfg["inst_id"])
    candles = fetch_candles(client, cfg, bar, bars, args.refresh)
    if len(candles) < 120:
        print("[!] K 线太少，回测无意义")
        return

    params = dict(cfg.get("strategy_params") or {})
    if args.strict:
        params = {}  # 用策略默认参数
    strat = build_strategy(strategy, **params)
    print(f"  参数 {strat.params or params}")

    bt_cfg = {
        "initial_equity": equity,
        "fee_pct": cfg["backtest"]["fee_pct"],
        "slip_pct": cfg["backtest"]["slip_pct"],
        "risk_pct": cfg["risk"]["risk_pct"],
        "max_leverage": cfg["risk"]["max_leverage"],
        "max_daily_loss_pct": cfg["risk"]["max_daily_loss_pct"],
        "max_drawdown_pct": cfg["risk"]["max_drawdown_pct"],
        "bar": bar,
        # 资金费（每 8 小时一次）。config.json 的 backtest.funding_pct_8h，写 0 就不计
        "funding_pct_8h": cfg["backtest"].get("funding_pct_8h", 0.0),
    }
    bt = Backtester(strat, candles, inst, bt_cfg)
    rep = bt.run()
    print_report(rep, f"{strategy} · {inst['instId']} · {bar}")
    print(f"  （已计资金费 {bt_cfg['funding_pct_8h']*100:.3f}%/8h，合计 {rep.get('funding_total', 0):.2f} USDT；"
          f"熔断按含浮盈亏的权益逐根判定，与实盘一致）")

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(DATA_DIR, f"backtest-{strategy}-{bar}-{ts}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in rep.items() if k != "curve"}, f, indent=2, ensure_ascii=False)
    csv_path = os.path.join(DATA_DIR, f"curve-{strategy}-{bar}-{ts}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ts", "datetime", "equity", "drawdown_pct"])
        for p in rep["curve"]:
            w.writerow([p["ts"], datetime.fromtimestamp(p["ts"] / 1000).strftime("%Y-%m-%d %H:%M"),
                        round(p["equity"], 4), round(p["drawdown"], 4)])
    print(f"\n  明细 → {out}")
    print(f"  净值曲线 → {csv_path}")

    # 最近 10 笔
    trades = rep["trade_list"]
    if trades:
        print("\n  最近 10 笔：")
        print("  {:>4} {:>12} {:>8} {:>10} {:>10} {:>10} {:>10}".format(
            "方向", "开仓时间", "张数", "入场", "出场", "盈亏U", "原因"))
        for t in trades[-10:]:
            print("  {:>4} {:>12} {:>8.2f} {:>10.2f} {:>10.2f} {:>+10.2f} {:>10}".format(
                "多" if t["side"] == 1 else "空",
                datetime.fromtimestamp(t["entry_ts"] / 1000).strftime("%Y-%m-%d %H:%M"),
                t["size"], t["entry_px"], t["exit_px"] or 0, t["pnl"], (t["reason"] or "")[:9]))
    print()


def cmd_compare(args):
    cfg = load_config()
    bar = args.bar or cfg["bar"]
    bars = args.bars or cfg["backtest"]["history_bars"]
    banner(f"三策略对比  {cfg['inst_id']}  {bar}")
    client = make_client(cfg)
    inst = get_inst(client, cfg["inst_id"])
    candles = fetch_candles(client, cfg, bar, bars, args.refresh)
    bt_cfg = {
        "initial_equity": args.equity or cfg["backtest"]["initial_equity"],
        "fee_pct": cfg["backtest"]["fee_pct"], "slip_pct": cfg["backtest"]["slip_pct"],
        "risk_pct": cfg["risk"]["risk_pct"], "max_leverage": cfg["risk"]["max_leverage"],
        "max_daily_loss_pct": 100.0, "max_drawdown_pct": 100.0, "bar": bar,
    }
    rows = []
    for name in REGISTRY:
        try:
            rep = Backtester(build_strategy(name), candles, inst, bt_cfg).run()
            rows.append((name, rep))
        except Exception as e:
            print(f"  {name} 失败：{e}")
    rows.sort(key=lambda x: x[1]["annualized_pct"], reverse=True)
    print("\n  {:<10}{:>10}{:>12}{:>10}{:>8}{:>8}{:>10}".format(
        "策略", "总收益%", "年化%", "回撤%", "夏普", "交易数", "胜率%"))
    for name, r in rows:
        print("  {:<10}{:>+10.2f}{:>+12.2f}{:>10.2f}{:>8.2f}{:>8}{:>10.2f}".format(
            name, r["total_return_pct"], r["annualized_pct"], r["max_drawdown_pct"],
            r["sharpe"], r["trades"], r["win_rate_pct"]))
    print()


def cmd_tune(args):
    """粗网格寻优：以夏普为主指标，年化收益为副指标。"""
    cfg = load_config()
    bar = args.bar or cfg["bar"]
    bars = args.bars or cfg["backtest"]["history_bars"]
    banner(f"参数寻优  {cfg['inst_id']}  {bar}")
    client = make_client(cfg)
    inst = get_inst(client, cfg["inst_id"])
    candles = fetch_candles(client, cfg, bar, bars, args.refresh)
    bt_cfg = {
        "initial_equity": args.equity or cfg["backtest"]["initial_equity"],
        "fee_pct": cfg["backtest"]["fee_pct"], "slip_pct": cfg["backtest"]["slip_pct"],
        "risk_pct": cfg["risk"]["risk_pct"], "max_leverage": cfg["risk"]["max_leverage"],
        "max_daily_loss_pct": 100.0, "max_drawdown_pct": 100.0, "bar": bar,
    }

    grid = []
    if args.strategy == "trend":
        for fast in (8, 12, 16, 21):
            for slow in (34, 48, 55):
                if fast >= slow:
                    continue
                for adx_min in (15.0, 20.0, 25.0):
                    for atr_mult in (1.5, 2.0, 2.5):
                        grid.append({"fast": fast, "slow": slow, "adx_min": adx_min,
                                     "atr_mult": atr_mult, "trail_mult": 3.0})
    else:
        grid = [{}]

    total = len(grid)
    print(f"  共 {total} 组参数，逐组回测 ...")
    results = []
    for n, p in enumerate(grid, 1):
        try:
            rep = Backtester(build_strategy(args.strategy, **p), candles, inst, bt_cfg).run()
            if rep["trades"] < 5:
                continue
            results.append((p, rep))
        except Exception:
            continue
        print(f"  {n}/{total}  已评 {len(results)} 组，最佳夏普 "
              f"{max([r['sharpe'] for _, r in results], default=0):.2f}", end="\r")
    print(" " * 70, end="\r")

    results.sort(key=lambda x: (x[1]["sharpe"], x[1]["annualized_pct"]), reverse=True)
    print(f"\n  有效组合 {len(results)} 组，Top 10（按夏普）：")
    print("  {:<6}{:>8}{:>8}{:>8}{:>6}{:>20}{:>10}{:>8}".format(
        "排名", "快线", "慢线", "ATR倍", "ADX", "总收益% / 回撤%", "夏普", "交易数"))
    for i, (p, r) in enumerate(results[:10], 1):
        cell = "{:+.1f}% / {:.1f}%".format(r["total_return_pct"], r["max_drawdown_pct"])
        print("  {:<6}{:>8}{:>8}{:>8}{:>6.0f}{:>20}{:>10.2f}{:>8}".format(
            i, str(p.get("fast", "-")), str(p.get("slow", "-")), str(p.get("atr_mult", "-")),
            float(p.get("adx_min", 0)), cell, r["sharpe"], r["trades"]))
    if results:
        best = results[0]
        print(f"\n  最优参数：{json.dumps(best[0], ensure_ascii=False)}")
        print("  写入 config.json 请执行：python run.py apply --strategy "
              + args.strategy + " --params '" + json.dumps(best[0]) + "'")
    print()


def cmd_preset(args):
    """切换预设参数档（经实测验证的两套配置）"""
    p = os.path.join(BASE_DIR, "presets.json")
    with open(p, "r", encoding="utf-8") as f:
        presets = json.load(f)["presets"]
    if not args.name:
        print("\n可用预设：")
        for k, v in presets.items():
            print(f"\n  【{k}】{v['label']}")
            print(f"    周期 {v['bar']} / 标的 {v['inst_id']} / 策略 {v['strategy']}")
            print(f"    参数 {json.dumps(v['strategy_params'], ensure_ascii=False)}")
            print(f"    实测 {v['evidence']}")
        print(f"\n  切换：python run.py preset <名字>\n")
        return
    key = args.name
    if key not in presets:
        print(f"[!] 没有预设 {key}，可选：{list(presets)}")
        return
    v = presets[key]
    cfg = load_config()
    cfg["bar"] = v["bar"]
    cfg["inst_id"] = v["inst_id"]
    cfg["strategy"] = v["strategy"]
    cfg["strategy_params"] = dict(v["strategy_params"])
    save_config(cfg)
    print(f"✓ 已切换到【{key}】{v['label']}")
    print(f"  周期 {v['bar']} / 标的 {v['inst_id']}")
    print(f"  参数 {json.dumps(v['strategy_params'], ensure_ascii=False)}")


def cmd_apply(args):
    cfg = load_config()
    cfg["strategy"] = args.strategy
    cfg["strategy_params"] = json.loads(args.params)
    save_config(cfg)
    print(f"✓ 已写入 config.json：strategy={args.strategy} params={cfg['strategy_params']}")


# ══════════════════════════════════ paper / live ══════════════════════════════════
def cmd_paper(args):
    from trader import Trader
    cfg = load_config()
    cfg["_creds"] = credentials()
    if args.bar:
        cfg["bar"] = args.bar
    if args.strategy:
        cfg["strategy"] = args.strategy
    if args.runtime:
        cfg["loop"]["max_runtime_hours"] = args.runtime
    banner("模拟盘（真实行情 / 虚拟资金）")
    t = Trader(cfg, mode="paper")
    t.loop()


def cmd_live(args):
    from trader import Trader
    cfg = load_config()
    cfg["_creds"] = credentials()
    if not has_credentials():
        print("[!] 实盘必须先在 .env 配置 OKX API 凭据")
        sys.exit(2)
    if args.bar:
        cfg["bar"] = args.bar
    if args.strategy:
        cfg["strategy"] = args.strategy
    if args.runtime:
        cfg["loop"]["max_runtime_hours"] = args.runtime
    real = bool(args.real)
    if real:
        banner("★ 实盘真实下单 ★")
        print(f"  合约 {cfg['inst_id']} | 杠杆 {cfg['live']['lever']}x | "
              f"每笔风险 {cfg['risk']['risk_pct']}% | 日亏熔断 {cfg['risk']['max_daily_loss_pct']}%")
        print(f"  5 秒后启动，Ctrl+C 中止 ...")
        for i in range(5, 0, -1):
            print(f"  {i}", end=" \r")
            time.sleep(1)
        print(" " * 10)
    else:
        banner("实盘 dry-run（只算不下单）")
    t = Trader(cfg, mode="live", dry_run=not real)
    t.loop()


def cmd_status(args):
    import glob
    banner("运行状态")
    # 状态文件按通道分开存：paper / live-real（真钱）/ live-sim（OKX 模拟盘）/ live-dry
    # （原先这里读的是 risk_state.json，而机器人写的是 risk_state_<通道>.json，永远显示「不存在」）
    names = ["paper_state.json"]
    for pat in ("risk_state_*.json", "*_loop_state.json", "ledger_*.json"):
        names += sorted(os.path.basename(p) for p in glob.glob(os.path.join(DATA_DIR, pat)))
    for name in names:
        p = os.path.join(DATA_DIR, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("trades"), list) and len(d["trades"]) > 5:
                d = dict(d, trades=f"（共 {len(d['trades'])} 笔，只显示最后 5 笔）", last_trades=d["trades"][-5:])
            print(f"\n--- {name} ---")
            print(json.dumps(d, indent=2, ensure_ascii=False)[:2400])
        else:
            print(f"\n--- {name} 不存在（还没跑过）---")
    print()


def cmd_reset_halt(args):
    import glob
    from risk import RiskManager
    # 原先这里解除的是 risk_state.json —— 机器人根本不读这个文件，所以「解除熔断」一直是空操作。
    paths = sorted(glob.glob(os.path.join(DATA_DIR, "risk_state_*.json")))
    want = getattr(args, "tag", None)
    if want:
        paths = [p for p in paths if os.path.basename(p) == f"risk_state_{want}.json"]
    if not paths:
        print("没有找到风控状态文件（risk_state_<通道>.json），没有可解除的熔断")
        return
    print("注意：机器人正在运行时解除无效（它内存里的状态会把文件覆盖回去），请先停掉机器人。")
    for p in paths:
        rm = RiskManager(state_path=p)
        name = os.path.basename(p)
        if rm.halted():
            why = rm.halt_reason()
            print(f"  {name}：{why} → {rm.reset_halt('交易员手动')}")
        else:
            print(f"  {name}：没有熔断，不动")


def cmd_init(args):
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path) and not args.force:
        print(f"config.json 已存在，--force 覆盖")
        return
    save_config(DEFAULT_CONFIG, path)
    print(f"✓ 已生成 {path}")
    env = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env):
        with open(env, "w", encoding="utf-8") as f:
            f.write("# OKX API 凭据 —— 只填这里，别写进 config.json\n"
                    "OKX_API_KEY=\nOKX_API_SECRET=\nOKX_PASSPHRASE=\n"
                    "# 1=模拟盘（需先在 OKX 网页开通「模拟交易」并创建模拟盘 API）  0=实盘\n"
                    "OKX_SIMULATED=1\n")
        print(f"✓ 已生成 {env}（请填入三行凭据）")


def main():
    ap = argparse.ArgumentParser(description="OKX 永续合约量化交易系统", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("init", help="生成 config.json 与 .env").add_argument("--force", action="store_true")
    sub.add_parser("doctor", help="体检")

    b = sub.add_parser("backtest", help="回测")
    b.add_argument("--strategy", choices=list(REGISTRY))
    b.add_argument("--bar")
    b.add_argument("--bars", type=int)
    b.add_argument("--equity", type=float)
    b.add_argument("--refresh", action="store_true")
    b.add_argument("--strict", action="store_true", help="用策略默认参数，不用 config.json 里的")

    c = sub.add_parser("compare", help="多策略对比")
    c.add_argument("--bar"); c.add_argument("--bars", type=int)
    c.add_argument("--equity", type=float); c.add_argument("--refresh", action="store_true")

    t = sub.add_parser("tune", help="参数寻优")
    t.add_argument("--strategy", default="trend", choices=list(REGISTRY))
    t.add_argument("--bar"); t.add_argument("--bars", type=int)
    t.add_argument("--equity", type=float); t.add_argument("--refresh", action="store_true")

    a = sub.add_parser("apply", help="把最优参数写进 config.json")
    a.add_argument("--strategy", required=True, choices=list(REGISTRY))
    a.add_argument("--params", required=True, help="JSON 字符串")

    pr = sub.add_parser("preset", help="切换实测验证过的参数预设")
    pr.add_argument("name", nargs="?", help="预设名，不带则列出全部")

    p = sub.add_parser("paper", help="模拟盘")
    p.add_argument("--bar"); p.add_argument("--strategy", choices=list(REGISTRY))
    p.add_argument("--runtime", type=float, help="运行小时数，0=不限")

    l = sub.add_parser("live", help="实盘")
    l.add_argument("--real", action="store_true", help="真的下单（不加则 dry-run）")
    l.add_argument("--bar"); l.add_argument("--strategy", choices=list(REGISTRY))
    l.add_argument("--runtime", type=float)

    sub.add_parser("status", help="查看状态")
    rh = sub.add_parser("reset-halt", help="解除熔断")
    rh.add_argument("--tag", help="只解除某个通道：paper / live-real / live-sim / live-dry；不写则解除全部已熔断的")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return
    fn = {
        "init": cmd_init, "doctor": cmd_doctor, "backtest": cmd_backtest,
        "compare": cmd_compare, "tune": cmd_tune, "apply": cmd_apply,
        "preset": cmd_preset,
        "paper": cmd_paper, "live": cmd_live, "status": cmd_status,
        "reset-halt": cmd_reset_halt,
    }[args.cmd]
    try:
        fn(args)
    except OkxError as e:
        print(f"\n[OKX 错误] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已中止")
    except Exception as e:
        import traceback
        print(f"\n[异常] {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
