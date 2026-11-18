# -*- coding: utf-8 -*-
"""配置加载：config.json（策略与风控）+ .env（凭据）。凭据永不写进 config.json。"""
import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")

DEFAULT_CONFIG = {
    "inst_id": "BTC-USDT-SWAP",
    "bar": "15m",
    "strategy": "trend",
    "strategy_params": {
        "fast": 12,
        "slow": 26,
        "adx_len": 14,
        "adx_min": 20.0,
        "atr_len": 14,
        "atr_mult": 2.0,
        "trail_mult": 3.0,
        "allow_short": True,
    },
    "risk": {
        "risk_pct": 1.0,
        "max_leverage": 3.0,
        "max_daily_loss_pct": 3.0,
        "max_drawdown_pct": 15.0,
        "max_positions": 1,
    },
    "backtest": {
        "initial_equity": 1000.0,
        "fee_pct": 0.0005,
        "slip_pct": 0.0002,
        "history_bars": 1500,
        "funding_pct_8h": 0.0001,    # 资金费率/8小时（0.01%），按多空都付保守计；写 0 = 不计
    },
    "paper": {
        "initial_equity": 1000.0,
        "fee_pct": 0.0005,
        "slip_pct": 0.0002,
    },
    "live": {
        "enabled": False,
        "lever": 3,
        "td_mode": "cross",
        "pos_mode": "net",
        "dry_run": True,
        "allow_short": True,
        "capital_usdt": 0,               # 划给本策略的本金；0 = 首次启动时取账上可用保证金并记住
        "require_exchange_stop": True,   # 确认交易所上没有止损单、又补挂不上时，立即平仓不留裸仓
        "fee_pct": 0.0005,               # 吃单手续费，算仓位时计入来回成本
        "slip_pct": 0.0002,
    },
    "loop": {
        "poll_seconds": 20, "max_runtime_hours": 0,
        "history_bars": 1200,            # 启动时拉多少根 K 线做指标预热（越长，EMA 起算点的影响越小）
        "max_entry_delay_s": 0,          # 开仓信号保鲜期（秒）；0 = 自动（周期的 1/4，最多 300 秒）
        "max_entry_drift_atr": 1.0,      # 现价离信号价超过几倍 ATR 就放弃开仓
    },
    "hosts": ["https://www.okx.com", "https://my.okx.com", "https://www.okx.ac", "https://www.okx.cab"],
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        cfg = _deep_merge(cfg, user)
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_env(path=ENV_PATH):
    """极简 .env 解析：KEY=VALUE，忽略 # 注释。同时读进 os.environ。"""
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if v:
                    data[k] = v
    for k, v in data.items():
        os.environ.setdefault(k, v)
    return data


def credentials():
    load_env()
    return {
        "api_key": os.environ.get("OKX_API_KEY", "").strip(),
        "secret": os.environ.get("OKX_API_SECRET", "").strip(),
        "passphrase": os.environ.get("OKX_PASSPHRASE", "").strip(),
        "simulated": os.environ.get("OKX_SIMULATED", "1").strip() in ("1", "true", "True", "yes"),
    }


def has_credentials():
    c = credentials()
    return bool(c["api_key"] and c["secret"] and c["passphrase"])
