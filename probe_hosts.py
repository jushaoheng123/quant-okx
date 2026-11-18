# -*- coding: utf-8 -*-
"""探测哪个域名支持 POST（下单/设杠杆），必须走代理才能解析 www.okx.com"""
import base64
import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import requests

from config import credentials

c = credentials()
PROXY = {"http": "socks5h://127.0.0.1:10808", "https": "socks5h://127.0.0.1:10808"}


def iso():
    n = datetime.now(timezone.utc)
    return n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"


def call(host, method, path, body=None, use_proxy=True):
    ts = iso()
    bs = json.dumps(body) if body is not None else ""
    msg = f"{ts}{method}{path}{bs}"
    sig = base64.b64encode(
        hmac.new(c["secret"].encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "OK-ACCESS-KEY": c["api_key"],
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": c["passphrase"],
        "Content-Type": "application/json",
    }
    s = requests.Session()
    s.trust_env = False
    proxies = PROXY if use_proxy else None
    if method == "GET":
        r = s.get(host + path, headers=headers, proxies=proxies, timeout=25)
    else:
        r = s.post(host + path, headers=headers, data=bs.encode(), proxies=proxies, timeout=25)
    return r.status_code, r.text


HOSTS = ["https://www.okx.com", "https://okx.com"]

print("=" * 90)
print("  域名 POST 支持探测（GET 成功不代表 POST 能通）")
print("=" * 90)

for host in HOSTS:
    for use_proxy in (True, False):
        tag = "代理" if use_proxy else "直连"
        try:
            st, tx = call(host, "GET", "/api/v5/account/balance", use_proxy=use_proxy)
            print(f"  GET  {host:<24} {tag}  {st}  {tx[:50]}")
        except Exception as e:
            print(f"  GET  {host:<24} {tag}  失败 {type(e).__name__}: {str(e)[:60]}")
    print()

print("=" * 90)
print("  POST 设杠杆（DOGE 10x）—— 这才是下单能力的真实验证")
print("=" * 90)
for host in HOSTS:
    for use_proxy in (True, False):
        tag = "代理" if use_proxy else "直连"
        try:
            st, tx = call(host, "POST", "/api/v5/account/set-leverage",
                          {"instId": "DOGE-USDT-SWAP", "lever": "10", "mgnMode": "cross"},
                          use_proxy=use_proxy)
            print(f"  POST {host:<24} {tag}  {st}  {tx[:170]}")
        except Exception as e:
            print(f"  POST {host:<24} {tag}  失败 {type(e).__name__}: {str(e)[:60]}")
    print()

print("=" * 90)
print("  确认 DOGE 杠杆现值")
print("=" * 90)
try:
    st, tx = call("https://www.okx.com", "GET",
                  "/api/v5/account/leverage-info?instId=DOGE-USDT-SWAP&mgnMode=cross")
    print(f"  {st}  {tx[:220]}")
except Exception as e:
    print("  失败", e)
