# -*- coding: utf-8 -*-
"""
OKX v5 REST 客户端 —— 零第三方依赖（仅标准库）
Pure Python Standard-Library OKX v5 Perpetual Swap Quant Engine

特性：
  · HMAC-SHA256 + Base64 签名（OKX v5 规范）
  · 多域名自动故障切换（www.okx.com 在国内不可达，okx.com 可直连）
  · 客户端限速桶 + 429/5xx 指数退避重试
  · 模拟盘头 x-simulated-trading 自动携带
  · 合约张数/价格按 lotSz / minSz / tickSz 自动对齐
"""
import base64
import hashlib
import hmac
import io
import json
import os
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal


def fmt_num(x):
    """数字 → OKX 接受的十进制字符串。
    str(0.0000123) 会变成 '1.23e-05'，OKX 不认科学计数法；小币种的价格/止损价会踩到。"""
    if isinstance(x, str):
        return x
    s = format(Decimal(repr(float(x))), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ★★ 域名铁律（2026-11-16 实测，踩得很疼）★★
#   www.okx.com  ：正式 API 域名，GET + POST 全支持，但国内 DNS 被污染成 169.254.0.2、直连必挂 → 必须走代理
#   okx.com      ：镜像域名，**只支持 GET**！读行情/查余额都正常，但下单/设杠杆/撤单全部 405 或 50113
#   my.okx.com / www.okx.ac ：DNS 同样被污染
# 曾经因为「okx.com 能读数据」就以为能用，结果所有下单都报 50113 Invalid Sign —— 其实是 POST 被拒。
DEFAULT_HOSTS = [
    "https://www.okx.com",
]

# 唯一活路：socks5 代理 + www.okx.com。本机 v2rayN / xray 的 socks5 入站。
DEFAULT_PROXY = os.environ.get("OKX_PROXY", "socks5h://127.0.0.1:10808")


class OkxError(Exception):
    def __init__(self, code, msg, payload=None):
        self.code = code
        self.msg = msg
        self.payload = payload
        super().__init__(f"[OKX {code}] {msg}")


class RateLimiter:
    """朴素令牌桶：capacity 个令牌，每 refill 秒补 1 个。"""

    def __init__(self, capacity=20, refill=0.12):
        self.capacity = capacity
        self.refill = refill
        self.tokens = float(capacity)
        self.last = time.monotonic()

    def take(self):
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) / self.refill)
            self.last = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            time.sleep(min(0.05, self.refill))


class OkxClient:
    def __init__(
        self,
        api_key=None,
        secret=None,
        passphrase=None,
        simulated=True,
        host=None,
        hosts=None,
        timeout=15,
        max_retries=4,
        debug=False,
        proxy="default",
        proxy_mode="on",
    ):
        self.api_key = (api_key or "").strip()
        self.secret = (secret or "").strip()
        self.passphrase = (passphrase or "").strip()
        self.simulated = bool(simulated)
        self.timeout = timeout
        self.max_retries = max_retries
        self.debug = debug
        self._hosts = [h.rstrip("/") for h in (hosts or ([host] if host else DEFAULT_HOSTS))]
        self._dead = set()
        self._limiter = RateLimiter()
        self._ssl = ssl.create_default_context()
        self._ctx = None  # 允许外部注入 ProxyHandler opener
        # 代理：auto=直连优先，被阻断后自动切代理；on=强制代理；off=纯直连
        self.proxy = DEFAULT_PROXY if proxy == "default" else proxy
        self.proxy_mode = proxy_mode
        self._proxy_active = (proxy_mode == "on")
        self._session = None          # 代理通道复用同一个连接，别每次请求都重新握手
        self._time_offset_ms = 0      # 交易所时间 - 本机时间（sync_time() 校准）

    # ---------------- 底层 ----------------
    def now_ms(self):
        """按交易所时钟校准过的当前毫秒时间。"""
        return int(time.time() * 1000) + self._time_offset_ms

    def _ts(self):
        # 只取一次时间：原先取两次，跨秒时会拼出「上一秒 + 000 毫秒」这种倒退 1 秒的时间戳
        now = datetime.fromtimestamp(self.now_ms() / 1000.0, tz=timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def sync_time(self):
        """用 /public/time 校准本机时钟偏差。本机时钟偏 30 秒以上，所有私有请求都会被拒（50102）。"""
        t0 = time.time()
        rows = self.server_time()
        t1 = time.time()
        self._time_offset_ms = int(int(rows[0]["ts"]) - (t0 + t1) / 2 * 1000)
        return self._time_offset_ms

    def _sign(self, ts, method, request_path, body_str):
        prehash = f"{ts}{method.upper()}{request_path}{body_str}"
        mac = hmac.new(self.secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _hosts_alive(self):
        alive = [h for h in self._hosts if h not in self._dead]
        return alive or list(self._hosts)

    def _raw(self, url, method, headers, body_bytes):
        if self._proxy_active and self.proxy:
            return self._raw_proxy(url, method, headers, body_bytes)
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
        opener = self._ctx or urllib.request.build_opener()
        with opener.open(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8")

    def _raw_proxy(self, url, method, headers, body_bytes):
        """走 socks5（需要 requests + PySocks）。直连被 SNI 阻断时的唯一活路。"""
        try:
            import requests
        except ImportError:
            raise OkxError("NO_REQUESTS", "走代理需要 requests 库")
        s = self._session
        if s is None:
            s = requests.Session()
            s.trust_env = False          # 忽略系统代理环境变量，只用我们指定的
            self._session = s
        proxies = {"http": self.proxy, "https": self.proxy}
        try:
            r = s.request(method.upper(), url, headers=headers, data=body_bytes,
                          proxies=proxies, timeout=self.timeout)
        except Exception as e:
            self._session = None         # 连接可能已坏，下次重建
            raise urllib.error.URLError(f"proxy {self.proxy} 不可用: {e}")
        if r.status_code >= 400:
            raise urllib.error.HTTPError(url, r.status_code, r.reason, None,
                                         io.BytesIO(r.content))
        return r.text

    def request(self, method, path, params=None, auth=False, body=None, quiet=False, idempotent=None):
        """path 形如 /api/v5/xxx，params 为 dict（会自动拼到 query，且计入签名）。

        idempotent：重发一遍有没有副作用。GET 默认可重发；POST 默认不可。
        ★ 下单这类 POST 一旦网络中断/超时，绝不能自动重发 —— 交易所可能已经成交了，
          而 clOrdId 在订单成交后可以复用，重发 = 再开一倍仓位。
          这时抛 NET_AMBIGUOUS，由调用方用 clOrdId 去查单确认。
        """
        method = method.upper()
        safe_retry = (method == "GET") if idempotent is None else bool(idempotent)
        params = {k: v for k, v in (params or {}).items() if v is not None}
        query = urllib.parse.urlencode(params, safe="")
        request_path = path + (f"?{query}" if query else "")
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""

        last_err = None
        for attempt in range(self.max_retries + 1):
            host = self._hosts_alive()[0]
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "quant-okx/1.0",
                "Accept": "application/json",
            }
            if auth:
                if not (self.api_key and self.secret and self.passphrase):
                    raise OkxError("NO_CRED", "私有接口需要 api_key / secret / passphrase")
                ts = self._ts()
                headers.update(
                    {
                        "OK-ACCESS-KEY": self.api_key,
                        "OK-ACCESS-SIGN": self._sign(ts, method, request_path, body_str),
                        "OK-ACCESS-TIMESTAMP": ts,
                        "OK-ACCESS-PASSPHRASE": self.passphrase,
                    }
                )
                if self.simulated:
                    headers["x-simulated-trading"] = "1"

            url = host + request_path
            body_bytes = body_str.encode("utf-8") if body_str else None
            self._limiter.take()
            try:
                text = self._raw(url, method, headers, body_bytes)
                try:
                    data = json.loads(text)
                except ValueError:
                    raise urllib.error.URLError(f"返回的不是 JSON（多半是代理/网关的页面）: {text[:80]!r}")
                code = str(data.get("code"))
                if code == "0":
                    return data.get("data", [])
                # 业务错误：不重试（除限流类）
                if code in ("50011", "50040", "50061"):
                    last_err = OkxError(code, data.get("msg"), data)
                    time.sleep(0.4 * (attempt + 1))
                    continue
                # 下单类接口的真实拒单原因在 data[0].sCode / sMsg 里，外层只有一句 "All operations failed"
                msg = data.get("msg") or ""
                rows = data.get("data") or []
                if rows and isinstance(rows[0], dict) and rows[0].get("sCode") not in (None, "", "0"):
                    msg = f"{msg} | sCode={rows[0].get('sCode')} {rows[0].get('sMsg')}"
                raise OkxError(code, msg, data)
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                try:
                    data = json.loads(text)
                except Exception:
                    data = {}
                code = str(data.get("code", e.code))
                msg = data.get("msg", text[:200])
                # 5xx / 429 重试
                # 429 = 被限流、请求没被处理，重发安全；5xx 对 POST 结果不明，只有可重发的请求才重试
                if (e.code == 429 or (safe_retry and e.code in (500, 502, 503, 504))) \
                        and attempt < self.max_retries:
                    last_err = OkxError(code, msg, data)
                    time.sleep((0.5 * (2 ** attempt)) + random.random() * 0.3)
                    continue
                # 域名级失败（404/403 常见于镜像域名不支持私有路由）→ 换域名
                if e.code in (403, 404) and len(self._hosts_alive()) > 1:
                    self._dead.add(host)
                    last_err = OkxError(code, msg, data)
                    continue
                raise OkxError(code, msg, data)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if not safe_retry:
                    if not self._proxy_active and self.proxy and self.proxy_mode == "auto":
                        self._proxy_active = True      # 下一次请求走代理，但这一次不重发
                    raise OkxError("NET_AMBIGUOUS",
                                   f"{method} {path} 网络中断，无法确定交易所是否已收到: {e}")
                # ★ 直连被阻断（TLS SNI 被 RST / 握手超时）→ 自动切代理，只切一次
                if not self._proxy_active and self.proxy and self.proxy_mode == "auto":
                    self._proxy_active = True
                    self._dead.clear()          # 换路径后域名重新试
                    if not quiet:
                        print(f"  [net] 直连被阻断（{e}），自动切换到代理 {self.proxy}")
                    continue
                last_err = OkxError("NET", f"{host} 不可达: {e}")
                self._dead.add(host)
                if not quiet:
                    print(f"  [warn] {host} 失败，切换域名重试 ({e})")
                if attempt < self.max_retries:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                raise last_err
        raise last_err or OkxError("UNKNOWN", "请求失败")

    # ---------------- 公共行情 ----------------
    def server_time(self):
        return self.request("GET", "/api/v5/public/time", quiet=True)

    def instruments(self, inst_type="SWAP", inst_id=None):
        return self.request(
            "GET", "/api/v5/public/instruments", {"instType": inst_type, "instId": inst_id}, quiet=True
        )

    def instrument(self, inst_id, inst_type="SWAP"):
        rows = self.instruments(inst_type, inst_id)
        return rows[0] if rows else None

    def ticker(self, inst_id):
        rows = self.request("GET", "/api/v5/market/ticker", {"instId": inst_id}, quiet=True)
        return rows[0] if rows else None

    def candles(self, inst_id, bar="15m", limit=300, after=None, before=None):
        """返回最近 K 线，OKX 返回倒序，这里翻正成 [ts,o,h,l,c,vol,...] 升序。"""
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after:
            params["after"] = str(after)
        if before:
            params["before"] = str(before)
        rows = self.request("GET", "/api/v5/market/candles", params)
        return self._norm_candles(rows)

    def history_candles(self, inst_id, bar="15m", limit=100, after=None):
        params = {"instId": inst_id, "bar": bar, "limit": str(limit)}
        if after:
            params["after"] = str(after)
        rows = self.request("GET", "/api/v5/market/history-candles", params)
        return self._norm_candles(rows)

    @staticmethod
    def _norm_candles(rows):
        out = []
        for r in rows:
            # OKX 字段: ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm
            out.append(
                {
                    "ts": int(r[0]),
                    "o": float(r[1]),
                    "h": float(r[2]),
                    "l": float(r[3]),
                    "c": float(r[4]),
                    "v": float(r[5]) if len(r) > 5 else 0.0,
                    "confirm": (r[8] if len(r) > 8 else "1") == "1",
                }
            )
        out.sort(key=lambda x: x["ts"])
        return out

    def fetch_history(self, inst_id, bar="15m", total=1500, page=300, progress=True):
        """
        两阶段分页抓历史 K 线（实测 /market/candles 翻到约 1440 根就被截断，
        更早的数据只有 /market/history-candles 有）：

          阶段 1  candles         300 根/页  —— 吃最近的，快
          阶段 2  history-candles 100 根/页  —— 往前推更早的，慢但能到底
        """
        seen = {}
        after = None

        def _push(batch):
            nonlocal after
            for c in batch:
                seen[c["ts"]] = c
            earliest = batch[0]["ts"]
            if after is not None and earliest >= after:
                return False           # 没有进展，别再翻
            after = earliest
            return True

        # 阶段 1
        for _ in range(max(2, total // 300 + 2)):
            if len(seen) >= total:
                break
            batch = self.candles(inst_id, bar, 300, after=after)
            if not batch or not _push(batch):
                break
            if progress:
                print(f"  已抓 {len(seen)} 根 ...", end="\r")
            time.sleep(0.1)

        # 阶段 2
        for _ in range(max(2, total // 100 + 3)):
            if len(seen) >= total:
                break
            batch = self.history_candles(inst_id, bar, 100, after=after)
            if not batch or not _push(batch):
                break
            if progress:
                print(f"  已抓 {len(seen)} 根 ...", end="\r")
            time.sleep(0.1)

        if progress:
            print(" " * 40, end="\r")
        data = sorted(seen.values(), key=lambda x: x["ts"])
        return data[-total:] if len(data) > total else data

    # ---------------- 私有账户 ----------------
    def balance(self, ccy=None):
        return self.request("GET", "/api/v5/account/balance", {"ccy": ccy}, auth=True)

    def equity_usdt(self):
        """账户总权益（USD，OKX 的 eqUsd 已折算）。"""
        rows = self.balance()
        if not rows:
            return 0.0
        return float(rows[0].get("totalEq") or 0.0)

    def positions(self, inst_id=None):
        return self.request("GET", "/api/v5/account/positions", {"instId": inst_id}, auth=True)

    def set_leverage(self, inst_id, lever, mgn_mode="cross", pos_side=None):
        body = {"instId": inst_id, "lever": str(lever), "mgnMode": mgn_mode}
        # 官方文档：posSide 只在「逐仓 + 双向持仓」时需要（原先写反成了 cross）
        if pos_side and mgn_mode == "isolated":
            body["posSide"] = pos_side
        return self.request("POST", "/api/v5/account/set-leverage", auth=True, body=body, idempotent=True)

    def set_pos_mode(self, pos_mode="net"):
        return self.request("POST", "/api/v5/account/set-position-mode", auth=True,
                            body={"posMode": pos_mode}, idempotent=True)

    def account_config(self):
        """账户配置：posMode（long_short_mode / net_mode）、acctLv 等。"""
        rows = self.request("GET", "/api/v5/account/config", auth=True)
        return rows[0] if rows else {}

    def positions_history(self, inst_id, inst_type="SWAP", limit=10):
        """已平仓位记录。realizedPnl = pnl + fee + fundingFee + liqPenalty + settledPnl（含手续费和资金费）。"""
        return self.request("GET", "/api/v5/account/positions-history",
                            {"instType": inst_type, "instId": inst_id, "limit": str(limit)}, auth=True)

    def algo_pending(self, inst_id, ord_type="conditional"):
        """未触发的策略委托（止盈止损）。ord_type: conditional（单向）/ oco（止盈止损一对）。"""
        return self.request("GET", "/api/v5/trade/orders-algo-pending",
                            {"ordType": ord_type, "instId": inst_id}, auth=True)

    def place_stop_algo(self, inst_id, side, sl_trigger_px, td_mode="cross", pos_side=None,
                        algo_cl_ord_id=None, sl_trigger_px_type="last"):
        """独立止损单：触发后市价全平（closeFraction=1）；仓位没了自动撤（cxlOnClosePos）。
        side 是平仓方向（多单止损 = sell）。"""
        body = {
            "instId": inst_id, "tdMode": td_mode, "side": side, "ordType": "conditional",
            "closeFraction": "1", "slTriggerPx": fmt_num(sl_trigger_px), "slOrdPx": "-1",
            "slTriggerPxType": sl_trigger_px_type, "reduceOnly": True, "cxlOnClosePos": True,
        }
        if pos_side:
            body["posSide"] = pos_side
        if algo_cl_ord_id:
            body["algoClOrdId"] = str(algo_cl_ord_id)[:32]
        return self.request("POST", "/api/v5/trade/order-algo", auth=True, body=body)

    def cancel_algos(self, inst_id, algo_ids):
        body = [{"instId": inst_id, "algoId": str(a)} for a in algo_ids]
        return self.request("POST", "/api/v5/trade/cancel-algos", auth=True, body=body, idempotent=True)

    def place_order(
        self,
        inst_id,
        side,
        sz,
        ord_type="market",
        td_mode="cross",
        px=None,
        reduce_only=False,
        pos_side=None,
        cl_ord_id=None,
        tp_trigger_px=None,
        sl_trigger_px=None,
        tp_ord_px="-1",
        sl_ord_px="-1",
        attach_algo_cl_ord_id=None,
        sl_trigger_px_type=None,
    ):
        body = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": fmt_num(sz),
        }
        if px is not None:
            body["px"] = fmt_num(px)
        if reduce_only:
            body["reduceOnly"] = "true"
        if pos_side:
            body["posSide"] = pos_side
        if cl_ord_id:
            body["clOrdId"] = str(cl_ord_id)[:32]
        if tp_trigger_px or sl_trigger_px:
            attach = {}
            if tp_trigger_px:
                attach.update({"tpTriggerPx": fmt_num(tp_trigger_px), "tpOrdPx": fmt_num(tp_ord_px)})
            if sl_trigger_px:
                attach.update({"slTriggerPx": fmt_num(sl_trigger_px), "slOrdPx": fmt_num(sl_ord_px)})
                if sl_trigger_px_type:
                    attach["slTriggerPxType"] = sl_trigger_px_type
            if attach_algo_cl_ord_id:
                attach["attachAlgoClOrdId"] = str(attach_algo_cl_ord_id)[:32]
            body["attachAlgoOrds"] = [attach]
        # 不自动重发：见 request() 的说明
        return self.request("POST", "/api/v5/trade/order", auth=True, body=body)

    def close_position(self, inst_id, mgn_mode="cross", pos_side=None, auto_cxl=True):
        """市价全平。auto_cxl：有挂着的平仓单挡路时先自动撤掉。全平是幂等的，可以重发。"""
        body = {"instId": inst_id, "mgnMode": mgn_mode}
        if pos_side:
            body["posSide"] = pos_side
        if auto_cxl:
            body["autoCxl"] = True
        return self.request("POST", "/api/v5/trade/close-position", auth=True, body=body, idempotent=True)

    def order(self, inst_id, ord_id=None, cl_ord_id=None):
        rows = self.request(
            "GET", "/api/v5/trade/order", {"instId": inst_id, "ordId": ord_id, "clOrdId": cl_ord_id}, auth=True
        )
        return rows[0] if rows else None

    def pending_orders(self, inst_id=None):
        return self.request("GET", "/api/v5/trade/orders-pending", {"instId": inst_id}, auth=True)

    # ---------------- 工具 ----------------
    @staticmethod
    def align_size(size, lot_sz, min_sz, max_sz=None):
        """向下取整到 lotSz 的整数倍；不足 minSz 返回 0。"""
        if size <= 0:
            return 0.0
        lot = float(lot_sz)
        step = round(size / lot, 12)
        whole = int(step)  # 向下
        val = round(whole * lot, 12)
        if max_sz and val > float(max_sz):
            val = round(int(float(max_sz) / lot) * lot, 12)
        if val < float(min_sz):
            return 0.0
        return val

    @staticmethod
    def align_price(px, tick_sz, side=None):
        tick = float(tick_sz)
        if tick <= 0:
            return px
        n = px / tick
        if side == "buy_below":
            n = int(n)
        elif side == "sell_above":
            n = -int(-n)
        else:
            n = round(n)
        return round(n * tick, 12)
