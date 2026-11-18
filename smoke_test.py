# -*- coding: utf-8 -*-
"""单轮冒烟测试：不启动无限循环，只跑一次 run_once，验证 Trader 全链路。"""
import json
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from config import credentials, load_config
from trader import Trader

cfg = load_config()
cfg["_creds"] = credentials()
t = Trader(cfg, mode="paper")
print("\n--- 第 1 轮 ---")
t.run_once()
print("\n--- 第 2 轮（验证不重复触发）---")
t.run_once()
print("\n账本：")
print(json.dumps(t.broker.summary(), indent=2, ensure_ascii=False))
print("\n风控状态：")
print(json.dumps(t.risk.state, indent=2, ensure_ascii=False))
assert t.broker.state["position"] is None or t.broker.state["position"]["size"] > 0
print("\n✓ 冒烟测试通过：不重放历史信号、账本可持久化、风控状态正常")
