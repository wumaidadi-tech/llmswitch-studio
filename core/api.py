# -*- coding: utf-8 -*-
"""本地 HTTP API + 静态资源服务。

为什么是"本地网页 UI"而不是继续用 GDI 自绘：
  * 液态玻璃的核心是**折射**（backdrop-filter + feDisplacementMap），
    Win32 GDI 没有这个能力，自己写卷积既慢又难看；
  * Chromium 对 `backdrop-filter: url(#filter)` 的支持是目前唯一能做出
    "边缘弯折光线"效果的现实方案；
  * 顺带白送：矢量字体、CSS 过渡、canvas 曲线、可滚动参数面板。

安全：只绑 127.0.0.1，且带随机 token（Cookie + Header 双通道），
防止浏览器里随便一个网页就能 POST 到本机 API。
"""
import json
import mimetypes
import os
import re
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import autostart, clients, config, diagnose, gguf, monitor, paths, server

APP_VERSION = "2.3.0"
REPO_URL = "https://github.com/wumaidadi-tech/llmswitch-studio"
RELEASES_API = "https://api.github.com/repos/wumaidadi-tech/llmswitch-studio/releases/latest"

_live = {                       # 探测层最近一次读数（拿不到时保留旧值）
    "health": "down",
    "tps": 0.0,
    "prefill_tps": 0.0,
    "ctx_used": 0,
    "ctx_cap": 0,
    "req_processing": 0,
    "req_deferred": 0,
    "spec_draft": 0,
    "spec_accepted": 0,
    "prompt_tokens": 0,
    "predicted_tokens": 0,
    "build_info": "",
    "n_ctx": 0,
    "total_slots": 0,
    "sleeping": False,
    "metrics_ok": False,
}
_live_lock = threading.Lock()

# 由 app.py 注入：打开主窗口 / 悬浮窗（迷你窗口）
_window_opener = None
# 由 app.py 注入：退出整个程序（关窗口 + 结束服务循环）
_quitter = None


def set_window_opener(fn):
    global _window_opener
    _window_opener = fn


def set_quitter(fn):
    global _quitter
    _quitter = fn


def _port_free(port):
    s = socket.socket()
    try:
        s.bind((paths.HOST, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ==================== 检查更新 ====================
_update_cache = {"at": 0.0, "data": None}
_update_lock = threading.Lock()


def _version_key(v):
    nums = [int(x) for x in re.findall(r"\d+", v or "")][:3]
    return tuple(nums + [0] * (3 - len(nums)))


def check_update():
    now = time.time()
    with _update_lock:
        cached = _update_cache["data"]
        if cached and now - _update_cache["at"] < 300:
            return cached
    try:
        req = urllib.request.Request(
            RELEASES_API,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "LLMSwitchStudio/" + APP_VERSION},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.load(resp)
        latest = (d.get("tag_name") or "").lstrip("v")
        out = {
            "ok": True,
            "current": APP_VERSION,
            "latest": latest,
            "newer": _version_key(latest) > _version_key(APP_VERSION),
            "name": d.get("name") or ("v" + latest),
            "url": d.get("html_url") or (REPO_URL + "/releases"),
            "published": d.get("published_at") or "",
            "notes": (d.get("body") or "")[:6000],
        }
        with _update_lock:
            _update_cache["at"] = now
            _update_cache["data"] = out
        return out
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "current": APP_VERSION, "error": str(exc),
                "url": REPO_URL + "/releases"}


# ==================== 探测循环 ====================
def probe_loop(mgr, stop_evt):
    last_gpu = 0.0
    last_heavy = 0.0
    tps_prev = None
    prev_stage = None
    prev_tokens = None
    while not stop_evt.is_set():
        try:
            now = time.time()
            st = mgr.status()
            # 记住"上次退出时在不在跑"，供下次启动恢复。
            # ⚠️ 只在真正发生过状态迁移时写：新进程第一眼就是 stopped，
            # 若无条件写 False，会把上次留下"在跑"的记录抹掉，自动恢复永远不触发。
            if st["stage"] != prev_stage:
                if st["stage"] in ("running", "starting"):
                    config.set_last_running(True, st.get("model"))
                elif st["stage"] == "stopped" and prev_stage in ("running", "starting", "stopping"):
                    config.set_last_running(False)
                prev_stage = st["stage"]
            if now - last_gpu > 2.0:
                monitor.gpu_info()
                last_gpu = now
            if st["stage"] in ("starting", "running", "stopping"):
                h = monitor.health()
                heavy = (now - last_heavy) > 2.5
                m = p = None
                if heavy:
                    m = monitor.metrics()
                    p = monitor.props()
                    last_heavy = now
                with _live_lock:
                    _live["health"] = h
                    if heavy:
                        if m:
                            _live["metrics_ok"] = True
                            _live["prefill_tps"] = round(m.get("prompt_tokens_seconds", 0.0), 1)
                            _live["tps"] = round(m.get("predicted_tokens_seconds", 0.0), 1)
                            _live["req_processing"] = int(m.get("requests_processing", 0))
                            _live["req_deferred"] = int(m.get("requests_deferred", 0))
                            _live["spec_draft"] = int(m.get("spec_decode_num_draft_tokens_total", 0))
                            _live["spec_accepted"] = int(m.get("spec_decode_num_accepted_tokens_total", 0))
                            _live["prompt_tokens"] = int(m.get("prompt_tokens_total", 0))
                            _live["predicted_tokens"] = int(m.get("tokens_predicted_total", 0))
                        else:                      # 没开 --metrics 时退回日志解析
                            _live["metrics_ok"] = False
                            t = monitor.log_stats()
                            if t:
                                _live["tps"] = round(t, 1)
                        if p:
                            _live["build_info"] = str(p.get("build_info") or "")
                            _live["total_slots"] = int(p.get("total_slots") or 0)
                            _live["sleeping"] = bool(p.get("is_sleeping"))
                            ds = p.get("default_generation_settings") or {}
                            nc = ds.get("n_ctx")
                            if isinstance(nc, int) and nc > 0:
                                _live["n_ctx"] = nc
                    busy = _live["req_processing"] > 0
                    sleeping = _live["sleeping"]
                    toks = _live["prompt_tokens"] + _live["predicted_tokens"]
                    metrics_ok = _live["metrics_ok"]

                if st["stage"] == "running" and not sleeping:
                    if busy or (prev_tokens is not None and toks != prev_tokens):
                        mgr.touch_activity()
                    prev_tokens = toks

                # ⚠️ /slots 会重置 llama.cpp 的空闲计时（实测：只轮询 /slots 时永不休眠），
                # 所以只在"确实有请求在处理"或刚活跃 30 秒内才问它。求上下文占用
                # 靠 /props 的 n_ctx 兜底；没开 --metrics 时退回原来的每秒轮询。
                sleep_on = bool((config.STATE.get("lifecycle") or {}).get("idle_sleep"))
                want_slots = (not sleeping) and (
                    busy or (now - mgr.last_activity) < 30 or (not metrics_ok and not sleep_on))
                if want_slots:
                    used, cap, _sl = monitor.slots()
                    with _live_lock:
                        if used is not None:
                            _live["ctx_used"] = used
                        if cap:
                            _live["ctx_cap"] = cap
                monitor.HIST.push([int(now), monitor.GD.get("used", 0),
                                   _live["tps"], _live["ctx_used"]])
            else:
                with _live_lock:
                    _live["health"] = "down"
                    _live["tps"] = 0.0
                    _live["req_processing"] = 0
                    _live["ctx_cap"] = 0
                    _live["sleeping"] = False
        except Exception as exc:                        # noqa: BLE001
            config.log("probe error %r" % (exc,))
        stop_evt.wait(1.0)


# ==================== 快照 ====================
def _lifecycle_public():
    lc = config.lifecycle()
    try:
        lc["autostart"] = autostart.enabled()
        lc["autostart_cmd"] = autostart.command()
    except Exception:
        lc["autostart"] = False
    return lc

def _model_public(entry):
    meta = entry.get("meta") or {}
    arch = meta.get("arch") or ""
    n_layers = meta.get("n_layers") or 0
    n_expert = meta.get("n_expert") or 0
    return {
        "id": entry["id"],
        "label": entry["label"],
        "spec": entry["spec"],
        "quant": entry["quant"],
        "size": entry["size"],
        "dir": entry["dir"],
        "has_mmproj": bool(entry.get("mmproj")),
        "mmproj_name": entry.get("mmproj_name") or "",
        "mmproj_size": entry.get("mmproj_size") or 0,
        "quant_mix": entry.get("quant_mix") or [],
        "has_mtp_file": bool(entry.get("mtp")),
        "has_nextn": bool(entry.get("has_nextn")),
        "arch": arch,
        "n_layers": n_layers,
        "n_expert": n_expert,
        "n_ctx_train": meta.get("n_ctx_train") or 0,
        "error": meta.get("error") or "",
        "loras": [{"name": x["name"], "size": x["size"], "path": x["path"]}
                  for x in (entry.get("loras") or [])],
    }


def snapshot(mgr):
    cfg = config.get_cfg()
    entry = config.model_entry()
    st = mgr.status()
    with _live_lock:
        live = dict(_live)
    g = monitor.gpu_info()
    free = max(0, (g["total"] or 0) - (g["used"] or 0))
    ctx_tok, kv = config.resolve_ctx(cfg, free if g["total"] else None,
                                     int((entry or {}).get("mtp") and 32 or 0))
    need = config.vram_need(int(ctx_tok), kv)
    models = []
    for m in config.MODELS:
        d = _model_public(m)
        d["selected"] = (m["id"] == config.STATE.get("model"))
        d["running"] = (m["id"] == st["model"] and st["stage"] in ("running", "starting"))
        models.append(d)
    live["history"] = monitor.HIST.dump()
    live["gpu"] = g
    # 空闲休眠时我们不去问 /slots（会重置计时），容量从 /props 的 n_ctx 兜底
    if not live.get("ctx_cap") and live.get("n_ctx"):
        live["ctx_cap"] = live["n_ctx"]
    spec_rate = 0.0
    if live["spec_draft"] > 0:
        spec_rate = round(100.0 * live["spec_accepted"] / live["spec_draft"], 1)
    live["spec_rate"] = spec_rate
    llama_port = int(getattr(mgr, "llama_port", paths.DEFAULT_LLAMA_PORT))
    alias = (cfg.get("launch") or {}).get("alias") or "local-model"
    return {
        "ok": True,
        "version": APP_VERSION,
        "ts": time.time(),
        "status": st,
        "current": _model_public(entry) if entry else None,
        "models": models,
        "cfg": cfg,
        "plan": {"ctx_tokens": int(ctx_tok), "kv": kv, "need_mib": need,
                 "vram_total": g["total"], "vram_free": free,
                 "vram_used": g["used"], "vram_util": g["util"]},
        "live": live,
        "specs": {"sampling": config.PARAM_SPECS, "launch": config.LAUNCH_SPECS,
                  "styles": list(config.STYLES.keys()),
                  "style_labels": config.STYLE_LABELS},
        "paths": {"llama_dir": paths.LLAMA_DIR, "models_dir": paths.MODELS_DIR,
                  "log": paths.LOG_FILE, "work_dir": paths.WORK_DIR,
                  "port": mgr.port,
                  "llama_port": getattr(mgr, "llama_port", paths.DEFAULT_LLAMA_PORT),
                  "exe": paths.LLAMA_EXE, "frozen": paths.FROZEN},
        "client": {
            "base_url": "http://127.0.0.1:%d/v1" % llama_port,
            "chat_url": "http://127.0.0.1:%d/v1/chat/completions" % llama_port,
            "root": "http://127.0.0.1:%d/" % llama_port,
            "model": alias,
            "api_key": "123456",
            "wake_url": "http://127.0.0.1:%d/wake?t=<runtime.json 里的 token>" % mgr.port,
        },
        "lifecycle": _lifecycle_public(),
        "presets": config.list_presets(),
        "setup": {"needs_llama_dir": not bool(paths.LLAMA_DIR)},
        "ui": config.STATE.get("ui", {}),
    }


# ==================== HTTP ====================
class Handler(BaseHTTPRequestHandler):
    mgr = None
    token = ""

    server_version = "LLMSwitchStudio/" + APP_VERSION
    protocol_version = "HTTP/1.1"

    # ---- 工具 ----
    def log_message(self, fmt, *args):     # 静音：每秒轮询会刷爆控制台
        pass

    def _cors(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _file(self, rel):
        full = os.path.normpath(os.path.join(paths.WEB_DIR, rel.lstrip("/")))
        if not full.startswith(os.path.normpath(paths.WEB_DIR)):
            return self._json({"ok": False, "error": "bad path"}, 403)
        if not os.path.isfile(full):
            return self._json({"ok": False, "error": "not found"}, 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith((".js", ".mjs")):
            ctype = "text/javascript"
        with open(full, "rb") as f:
            body = f.read()
        # 首页注入 token / 端口，避免 JS 里再猜
        if full.endswith("index.html"):
            body = (body.decode("utf-8")
                    .replace("__TOKEN__", self.token)
                    .replace("__PORT__", str(self.mgr.port))
                    .encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype == "application/json" else ""))
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, q):
        if not self.token:
            return True
        tok = self.headers.get("X-Token") or (q.get("t") or [""])[0]
        if tok == self.token:
            return True
        cookie = self.headers.get("Cookie") or ""
        return ("lst=" + self.token) in cookie

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 2 << 20:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        if p == "/healthz":
            return self._json({"ok": True})
        if p == "/wake":
            # 给脚本/客户端用的「按需唤醒」：GET /wake?t=<token>
            # token 在 LLMSwitchData/runtime.json 里，每次启动都会换。
            if not self._authed(q):
                return self._json({"ok": False, "error": "unauthorized"}, 401)
            st = self.mgr.status()
            if st["stage"] in ("running", "starting"):
                return self._json({"ok": True, "already": True, "status": st})
            ok, msg = self.mgr.start(self.mgr.model_id or config.STATE.get("model"))
            return self._json({"ok": bool(ok), "msg": msg, "status": self.mgr.status()})
        if p.startswith("/api/") and not self._authed(q):
            return self._json({"ok": False, "error": "unauthorized"}, 401)
        try:
            if p == "/api/snapshot":
                return self._json(snapshot(self.mgr))
            if p == "/api/models":
                if (q.get("rescan") or ["0"])[0] == "1":
                    config.MODELS = gguf.scan_models()
                    config.reconcile()
                    config.save()
                return self._json({"ok": True, "models": [_model_public(m) for m in config.MODELS]})
            if p == "/api/logs":
                n = int((q.get("n") or ["200"])[0])
                return self._json({"ok": True, "lines": monitor.log_tail(max(20, min(n, 2000)))})
            if p == "/api/presets":
                return self._json({"ok": True, "presets": config.list_presets(),
                                   "builtin": config.STYLE_LABELS})
            if p == "/api/settings":
                return self._json({"ok": True, "llama_dir": paths.LLAMA_DIR,
                                   "models_dir": paths.MODELS_DIR,
                                   "work_dir": paths.WORK_DIR, "frozen": paths.FROZEN})
            if p == "/api/update":
                return self._json(check_update())
            if p == "/api/diagnose":
                return self._json(diagnose.run(self.mgr))
            if p == "/api/clients":
                return self._json({"ok": True, "targets": clients.targets()})
            if p == "/api/export":
                fmt = (q.get("format") or ["bat"])[0]
                return self._json(self._export(fmt))
            return self._file(p if p != "/" else "index.html")
        except BrokenPipeError:
            return
        except Exception as exc:                        # noqa: BLE001
            config.log("GET %s error %r" % (p, exc))
            return self._json({"ok": False, "error": str(exc)}, 500)

    # ---- POST ----
    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authed(q):
            return self._json({"ok": False, "error": "unauthorized"}, 401)
        p = u.path
        body = self._body()
        try:
            if p == "/api/llama-port":
                try:
                    new_port = int(body.get("port") or 0)
                except Exception:
                    return self._json({"ok": False, "error": "端口无效"}, 400)
                if not 1024 <= new_port <= 65535:
                    return self._json({"ok": False, "error": "端口需在 1024–65535 之间"}, 400)
                old_port = int(getattr(self.mgr, "llama_port", paths.DEFAULT_LLAMA_PORT))
                if new_port == old_port:
                    return self._json({"ok": True, "port": new_port, "previous": old_port,
                                       "restarted": False})
                if self.mgr.stage in ("starting", "stopping"):
                    return self._json({"ok": False, "error": "服务正在启动/停止，请稍后再改端口"}, 400)
                if not _port_free(new_port):
                    return self._json({"ok": False, "error": "端口 %d 已被占用" % new_port}, 400)
                with config._lock:
                    config.STATE["llama_port"] = new_port
                config.save()
                self.mgr.llama_port = new_port
                monitor.set_port(new_port)
                restarted = False
                if self.mgr.stage == "running":
                    ok, _msg = self.mgr.restart(self.mgr.model_id)
                    restarted = bool(ok)
                return self._json({"ok": True, "port": new_port, "previous": old_port,
                                   "restarted": restarted})

            if p == "/api/open-web":
                if self.mgr.stage not in ("starting", "running"):
                    return self._json({"ok": False, "error": "llama-server 还没启动"}, 400)
                url = "http://127.0.0.1:%d/" % int(self.mgr.llama_port)
                try:
                    os.startfile(url)                     # noqa: S606
                except Exception as exc:                  # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)}, 500)
                return self._json({"ok": True, "url": url})

            if p == "/api/open-url":
                url = str(body.get("url") or "")
                if not (url == REPO_URL or url.startswith(REPO_URL + "/")):
                    return self._json({"ok": False, "error": "只允许打开项目页面"}, 400)
                try:
                    os.startfile(url)                     # noqa: S606
                except Exception as exc:                  # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)}, 500)
                return self._json({"ok": True})

            if p == "/api/select":
                mid = body.get("id")
                if not config.model_entry(mid):
                    return self._json({"ok": False, "error": "model not found"}, 404)
                with config._lock:
                    config.STATE["model"] = mid
                config.save()
                return self._json({"ok": True, "snapshot": snapshot(self.mgr)})

            if p == "/api/server":
                act = body.get("action")
                mid = body.get("model")
                if act == "start":
                    ok, msg = self.mgr.start(mid)
                elif act == "stop":
                    ok, msg = self.mgr.stop()
                elif act == "restart":
                    ok, msg = self.mgr.restart(mid)
                elif act == "swap":
                    ok, msg = self.mgr.swap(mid)
                else:
                    return self._json({"ok": False, "error": "unknown action"}, 400)
                return self._json({"ok": ok, "msg": msg, "status": self.mgr.status()})

            if p == "/api/quit":
                if _quitter is None:
                    return self._json({"ok": False, "error": "no quit support"}, 400)
                threading.Timer(0.25, _quitter).start()
                return self._json({"ok": True})

            if p == "/api/wake":
                st = self.mgr.status()
                if st["stage"] in ("running", "starting"):
                    return self._json({"ok": True, "already": True, "status": st})
                ok, msg = self.mgr.start(self.mgr.model_id or config.STATE.get("model"))
                return self._json({"ok": bool(ok), "msg": msg, "status": self.mgr.status()})

            if p == "/api/lifecycle":
                before = config.lifecycle()
                fields = {k: body[k] for k in ("idle_sleep", "idle_minutes", "restore_launch", "tray")
                          if k in body}
                lc = config.set_lifecycle(**fields) if fields else before
                changed = (bool(before.get("idle_sleep")) != bool(lc.get("idle_sleep"))
                           or int(before.get("idle_minutes") or 0) != int(lc.get("idle_minutes") or 0))
                return self._json({"ok": True, "lifecycle": lc,
                                   "needs_restart": bool(changed and self.mgr.stage == "running")})

            if p == "/api/autostart":
                on = bool(body.get("on"))
                try:
                    now = autostart.set_enabled(on)
                except Exception as exc:                # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)}, 500)
                return self._json({"ok": True, "on": bool(now),
                                   "command": autostart.command()})

            if p == "/api/client-write":
                tid = str(body.get("id") or "")
                cfg = config.get_cfg()
                entry = config.model_entry()
                port = int(getattr(self.mgr, "llama_port", paths.DEFAULT_LLAMA_PORT))
                base = "http://127.0.0.1:%d/v1" % port
                model = (cfg.get("launch") or {}).get("alias") or "local-model"
                try:
                    ctx_tok = int(config.resolve_ctx(cfg, None, 0)[0])
                except Exception:
                    ctx_tok = 131072
                images = bool(entry and entry.get("mmproj")
                              and (cfg.get("launch") or {}).get("mmproj"))
                ok, msg, path, bak = clients.write(tid, model, base, "123456", ctx_tok, images)
                config.log("client-write %s -> %s (%s)" % (tid, path, msg))
                return self._json({"ok": bool(ok), "msg": msg, "path": path, "backup": bak})

            if p == "/api/client-test":
                port = int(getattr(self.mgr, "llama_port", 0) or 0)
                ok, d = monitor.http_json("/v1/models", timeout=5, port=port)
                models = []
                if ok and isinstance(d, dict):
                    models = [m.get("id") for m in (d.get("data") or [])
                              if isinstance(m, dict)]
                return self._json({
                    "ok": bool(ok),
                    "models": models,
                    "error": "" if ok else "连不上 llama-server：服务没启动，或端口不是 %d" % port,
                    "url": "http://127.0.0.1:%d/v1/models" % port,
                })

            if p == "/api/config":
                mid = body.get("id") or config.STATE.get("model")
                cfg = config.put_cfg(mid, body.get("cfg") or {})
                return self._json({"ok": True, "cfg": cfg})

            if p == "/api/presets":
                act = body.get("action")
                if act == "save":
                    ok, info = config.save_preset(body.get("name"), body.get("cfg") or {})
                    return self._json({"ok": ok, "name": info,
                                       "presets": config.list_presets()})
                if act == "delete":
                    ok = config.delete_preset(body.get("name") or "")
                    return self._json({"ok": ok, "presets": config.list_presets()})
                if act == "get":
                    cfg = config.preset_cfg(body.get("name") or "")
                    return self._json({"ok": bool(cfg), "cfg": cfg})
                return self._json({"ok": False, "error": "unknown action"}, 400)

            if p == "/api/window":
                if _window_opener is None:
                    return self._json({"ok": False, "error": "no window support"}, 400)
                action = body.get("action") or "open"
                mini = bool(body.get("mini"))
                top = bool(body.get("topmost"))
                w = body.get("w")
                h = body.get("h")
                r = _window_opener(action, mini, top, w, h)
                return self._json({"ok": bool(r), "action": action,
                                   "result": r if isinstance(r, str) else None,
                                   "mini": mini, "topmost": top})

            if p == "/api/settings":
                ld = str(body.get("llama_dir") or "").strip()
                if not paths.set_llama_dir(ld):
                    return self._json({"ok": False, "error": "目录里找不到 llama-server.exe"}, 400)
                with config._lock:
                    config.STATE["llama_dir"] = paths.LLAMA_DIR
                config.MODELS = gguf.scan_models()
                config.reconcile()
                config.save()
                config.log("llama dir set by user: %s" % paths.LLAMA_DIR)
                return self._json({"ok": True, "snapshot": snapshot(self.mgr)})

            if p == "/api/style":
                mid = body.get("id") or config.STATE.get("model")
                cfg = config.apply_style(config.get_cfg(mid), body.get("style"))
                with config._lock:
                    config.STATE["models"][mid] = cfg
                config.save()
                return self._json({"ok": True, "cfg": cfg})

            if p == "/api/reset":
                mid = body.get("id") or config.STATE.get("model")
                cfg = config.default_cfg(mid)
                with config._lock:
                    config.STATE["models"][mid] = cfg
                config.save()
                return self._json({"ok": True, "cfg": cfg})

            if p == "/api/ui":
                ui = body.get("ui") or {}
                with config._lock:
                    for k in ("tab", "theme"):
                        if isinstance(ui.get(k), str):
                            config.STATE["ui"][k] = ui[k]
                    for k in ("mini", "topmost"):
                        if k in ui:
                            config.STATE["ui"][k] = bool(ui[k])
                config.save()
                return self._json({"ok": True, "ui": config.STATE["ui"]})

            if p == "/api/open-log":
                try:
                    os.startfile(paths.LOG_FILE)          # noqa: S606
                except Exception as exc:                  # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)})
                return self._json({"ok": True})

            if p == "/api/reveal":
                target = body.get("target") or "work"
                dest = {"log": paths.LOG_FILE, "work": paths.WORK_DIR,
                        "models": paths.MODELS_DIR or paths.WORK_DIR}.get(target, paths.WORK_DIR)
                try:
                    os.startfile(dest)                    # noqa: S606
                except Exception as exc:                  # noqa: BLE001
                    return self._json({"ok": False, "error": str(exc)})
                return self._json({"ok": True, "path": dest})

            return self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as exc:                          # noqa: BLE001
            config.log("POST %s error %r" % (p, exc))
            return self._json({"ok": False, "error": str(exc)}, 500)

    # ---- 导出 ----
    def _export(self, fmt):
        entry = config.model_entry()
        if entry is None:
            return {"ok": False, "error": "no model"}
        cfg = config.get_cfg()
        free = monitor.vram_free()
        cmd, ctx, kv = self.mgr.build_cmd(entry, cfg, free)
        if fmt == "json":
            return {"ok": True, "text": json.dumps(
                {"model": entry["id"], "cfg": cfg, "ctx": ctx, "kv": kv},
                ensure_ascii=False, indent=2), "kind": "json"}
        if fmt == "sh":
            return {"ok": True, "kind": "sh",
                    "text": "#!/usr/bin/env bash\n" + " \\\n  ".join(
                        "'%s'" % a.replace("'", "'\\''") for a in cmd)}
        lines = ["@echo off", "chcp 65001 >nul",
                 "REM generated by LLMSwitch Studio %s" % APP_VERSION,
                 'cd /d "%s"' % paths.LLAMA_DIR,
                 " ".join(server.esc_bat(a) for a in cmd), "pause"]
        return {"ok": True, "text": "\r\n".join(lines), "kind": "bat"}


# ==================== 启动 ====================
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def make_server(port, token, mgr):
    Handler.mgr = mgr
    Handler.token = token
    return _Server((paths.HOST, port), Handler)
