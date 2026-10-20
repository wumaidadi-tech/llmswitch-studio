# -*- coding: utf-8 -*-
"""探测层：进程状态、llama-server HTTP 指标、显存、日志、历史曲线。

所有 127.0.0.1 请求都必须绕开系统代理 —— 本机装过会接管 localhost 的代理，
一旦走代理就会拿到假的失败响应，界面永远停在"启动中"。
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import paths

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# llama-server 的端口。启动时由 app.py 用 set_port() 改；
# ⚠️ 千万别和 UI 的 API 端口混用 —— Windows 的 SO_REUSEADDR 允许两个进程
#    绑同一端口，结果是 llama-server 悄悄抢走 API 端口，/health 永远拿不到。
PORT = paths.DEFAULT_LLAMA_PORT


def set_port(p):
    global PORT
    PORT = int(p or paths.DEFAULT_LLAMA_PORT)


GD = {"name": "", "driver": "", "total": 0, "used": 0, "util": 0}


def http_json(path, timeout=1.5, port=None):
    url = "http://127.0.0.1:%d%s" % (port or PORT, path)
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        return True, json.loads(body)
    except urllib.error.HTTPError as e:            # 503 = 模型还在加载，也要读 body
        try:
            return False, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return False, None
    except Exception:
        return False, None


def http_text(path, timeout=2.0, port=None):
    url = "http://127.0.0.1:%d%s" % (port or PORT, path)
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def http_post(path, payload, timeout=180, port=None):
    url = "http://127.0.0.1:%d%s" % (port or PORT, path)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, {"error": str(e)}


# ==================== 显卡 ====================
def gpu_info():
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, creationflags=paths.CNW, timeout=4)
        parts = [p.strip() for p in r.stdout.decode("ascii", "ignore").strip().split(",")]
        if len(parts) >= 5:
            GD.update(name=parts[0], driver=parts[1],
                      total=int(float(parts[2])), used=int(float(parts[3])),
                      util=int(float(parts[4])))
    except Exception:
        pass
    return dict(GD)


def vram_free():
    g = gpu_info()
    if not g["total"]:
        return None
    return max(0, g["total"] - g["used"])


# ==================== 服务端指标 ====================
def health():
    ok, d = http_json("/health", timeout=1.0)
    if ok and isinstance(d, dict):
        return "ok" if d.get("status") == "ok" else "loading"
    if isinstance(d, dict) and (d.get("error") or {}).get("code") == 503:
        return "loading"
    return "down"


def props():
    ok, d = http_json("/props", timeout=1.5)
    return d if ok and isinstance(d, dict) else None


def slots():
    """→ (已用 token, 上限, 原始槽位列表)

    b10775 的空闲槽位**不返回占用字段**，只有 is_processing=true 时才有
    n_prompt_tokens。所以拿不到时返回 (None, cap, ...)，由调用方保留上次值。
    """
    ok, d = http_json("/slots", timeout=1.2)
    if not ok or not isinstance(d, list):
        return None, None, []
    used, cap = None, None
    for s in d:
        if not isinstance(s, dict):
            continue
        nc = s.get("n_ctx")
        if isinstance(nc, int) and nc > 0:
            cap = nc
        if not s.get("is_processing"):
            continue
        for k in ("n_prompt_tokens", "n_past", "prompt_n", "n_tokens"):
            v = s.get(k)
            if isinstance(v, int) and v >= 0:
                used = v if used is None else max(used, v)
                break
        nt = s.get("next_token") or {}
        nd = nt.get("n_decoded") if isinstance(nt, dict) else None
        if used is None and isinstance(nd, int):
            used = nd
    return used, cap, d


_RE_METRIC = re.compile(r"^(llamacpp:[a-z_]+)(?:\{[^}]*\})?\s+([0-9.eE+-]+)$", re.M)


def metrics():
    """Prometheus 文本 → dict。没开 --metrics 时返回 {}（不是错误）。"""
    txt = http_text("/metrics", timeout=1.5)
    if not txt:
        return {}
    out = {}
    for k, v in _RE_METRIC.findall(txt):
        try:
            out[k.split(":", 1)[1]] = float(v)
        except ValueError:
            pass
    return out


# ==================== 日志 ====================
_RE_TPS = re.compile(r"\btg =\s*([\d.]+) t/s")
_RE_TPS_EVAL = re.compile(r"(?<!prompt )eval time =.*?([\d.]+) tokens per second")
_RE_ERR = re.compile(r"(error|failed|invalid|unable to|out of memory|no such file|cannot|refus"
                     r"|denied|already in use|failed to)", re.I)
_RE_ERR_SKIP = re.compile(r"(no error|0 errors|ignoring)", re.I)

_ERR_HINTS = (
    (("out of memory", "failed to fit", "insufficient", "oom", "cudamalloc"), "显存不足"),
    (("unknown argument", "invalid argument", "unrecognized", "invalid value"), "启动参数被拒绝"),
    (("no such file", "failed to load model", "unable to load", "cannot open", "not found"), "模型文件读不到"),
    (("already in use", "bind", "address"), "端口被占用"),
    (("permission", "denied", "access is"), "权限不足"),
    (("no such device", "cuda error", "invalid device"), "CUDA 设备异常"),
)


def log_tail(n=200, limit=262144):
    try:
        sz = os.path.getsize(paths.LOG_FILE)
        with open(paths.LOG_FILE, "rb") as f:
            f.seek(max(0, sz - limit))
            raw = f.read().decode("utf-8", "replace")
    except Exception:
        return []
    lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
    return lines[-n:]


def log_error(width=110):
    """从日志尾部挑一句最像失败原因的话"""
    hit = ""
    for line in log_tail(400):
        if len(line) < 8 or not _RE_ERR.search(line) or _RE_ERR_SKIP.search(line):
            continue
        hit = line
    if not hit:
        return ""
    hit = re.sub(r"^[\d.:\s]+[IWED]\s+", "", hit)
    hit = re.sub(r"\s+", " ", hit).strip()
    return (hit[:width] + "…") if len(hit) > width else hit


def error_summary(raw):
    s = str(raw or "").lower()
    for keys, label in _ERR_HINTS:
        if any(k in s for k in keys):
            return label
    return str(raw or "").strip()


def log_stats():
    """日志兜底指标：解码 tok/s 与已用 token（/metrics 没开时用）"""
    tps = None
    lines = log_tail(600)
    for ln in lines:
        m = _RE_TPS.search(ln)
        if m:
            tps = float(m.group(1))
    if tps is None:
        for ln in lines:
            m = _RE_TPS_EVAL.search(ln)
            if m:
                tps = float(m.group(1))
    return tps


# ==================== 历史曲线 ====================
class History:
    """固定长度的采样环，供前端画 sparkline。只存数值，不存对象。"""

    def __init__(self, cap=240):
        self.cap = cap
        self.rows = []
        self.lock = threading.Lock()

    def push(self, row):
        with self.lock:
            self.rows.append(row)
            if len(self.rows) > self.cap:
                del self.rows[:-self.cap]

    def dump(self):
        with self.lock:
            return list(self.rows)


HIST = History()
