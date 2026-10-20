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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, gguf, monitor, paths, server

APP_VERSION = "2.0.0"

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
}
_live_lock = threading.Lock()

# 由 app.py 注入：打开主窗口 / 悬浮窗（迷你窗口）
_window_opener = None


def set_window_opener(fn):
    global _window_opener
    _window_opener = fn


# ==================== 探测循环 ====================
def probe_loop(mgr, stop_evt):
    last_gpu = 0.0
    last_heavy = 0.0
    tps_prev = None
    while not stop_evt.is_set():
        try:
            now = time.time()
            st = mgr.status()
            if now - last_gpu > 2.0:
                monitor.gpu_info()
                last_gpu = now
            if st["stage"] in ("starting", "running", "stopping"):
                h = monitor.health()
                used, cap, _sl = monitor.slots()
                with _live_lock:
                    _live["health"] = h
                    if used is not None:
                        _live["ctx_used"] = used
                    if cap:
                        _live["ctx_cap"] = cap
                    if now - last_heavy > 3.0:
                        m = monitor.metrics()
                        if m:
                            _live["prefill_tps"] = round(m.get("prompt_tokens_seconds", 0.0), 1)
                            _live["tps"] = round(m.get("predicted_tokens_seconds", 0.0), 1)
                            _live["req_processing"] = int(m.get("requests_processing", 0))
                            _live["req_deferred"] = int(m.get("requests_deferred", 0))
                            _live["spec_draft"] = int(m.get("spec_decode_num_draft_tokens_total", 0))
                            _live["spec_accepted"] = int(m.get("spec_decode_num_accepted_tokens_total", 0))
                            _live["prompt_tokens"] = int(m.get("prompt_tokens_total", 0))
                            _live["predicted_tokens"] = int(m.get("tokens_predicted_total", 0))
                        else:                      # 没开 --metrics 时退回日志解析
                            t = monitor.log_stats()
                            if t:
                                _live["tps"] = round(t, 1)
                        p = monitor.props()
                        if p:
                            _live["build_info"] = str(p.get("build_info") or "")
                            _live["total_slots"] = int(p.get("total_slots") or 0)
                            ds = p.get("default_generation_settings") or {}
                            nc = ds.get("n_ctx")
                            if isinstance(nc, int):
                                _live["n_ctx"] = nc
                        last_heavy = now
                monitor.HIST.push([int(now), monitor.GD.get("used", 0),
                                   _live["tps"], _live["ctx_used"]])
            else:
                with _live_lock:
                    _live["health"] = "down"
                    _live["tps"] = 0.0
                    _live["req_processing"] = 0
                    _live["ctx_cap"] = 0
        except Exception as exc:                        # noqa: BLE001
            config.log("probe error %r" % (exc,))
        stop_evt.wait(1.0)


# ==================== 快照 ====================
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
        "has_mtp_file": bool(entry.get("mtp")),
        "has_nextn": bool(entry.get("has_nextn")),
        "arch": arch,
        "n_layers": n_layers,
        "n_expert": n_expert,
        "n_ctx_train": meta.get("n_ctx_train") or 0,
        "error": meta.get("error") or "",
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
    spec_rate = 0.0
    if live["spec_draft"] > 0:
        spec_rate = round(100.0 * live["spec_accepted"] / live["spec_draft"], 1)
    live["spec_rate"] = spec_rate
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
                  "log": paths.LOG_FILE, "port": mgr.port,
                  "llama_port": getattr(mgr, "llama_port", paths.DEFAULT_LLAMA_PORT),
                  "exe": paths.LLAMA_EXE, "frozen": paths.FROZEN},
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
                mini = bool(body.get("mini"))
                top = bool(body.get("topmost"))
                r = _window_opener(mini, top)
                return self._json({"ok": bool(r), "mini": mini, "topmost": top})

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
