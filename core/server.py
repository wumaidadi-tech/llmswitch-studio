# -*- coding: utf-8 -*-
"""llama-server 进程生命周期与启动命令行生成。

这里是"血泪经验"的集中地，改动前先读注释：
  * **不要传 -np**（含 -np 1）：显式给值会让 kv-unified / cache-idle-slots
    这两项"槽位=auto 才启用"的默认机制失效，客户端交错发来的短请求会把长
    请求的 KV 冲掉，导致每轮全量重算（实测 45K token / prefill 142s）。
  * `-ot exps=CPU` 是 8GB 卡跑 35B MoE 的前提：专家权重留在内存，
    只把注意力层放显存。
  * `-c` 是启动时一次性预留 KV，开大不会"用多少占多少"。
  * 思考链保留：ctx < 128K 时关掉 preserve，否则几轮就填满上下文。
"""
import ctypes
import os
import subprocess
import threading
import time

from . import config, gguf, monitor, paths

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SAMPLER_SEQ = "penalties;dry;top_k;typ_p;top_p;min_p;xtc;temp"


def pid_alive(pid):
    if not pid:
        return False
    h = kernel32.OpenProcess(paths.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == paths.STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(h)


def _rotate(path, limit=8 << 20):
    """llama-server 的 --log-file 只追加，启动前轮转一次防无限长大"""
    try:
        if os.path.exists(path) and os.path.getsize(path) > limit:
            os.replace(path, path + ".old")
    except Exception:
        pass


def esc_bat(arg):
    a = str(arg)
    if a == "":
        return '""'
    if any(c in a for c in ' \t&()[]{}^=;!\'"+,`~'):
        # cmd 里转义引号是双写 ""，不是反斜杠（反斜杠是给 C 运行时的）
        return '"%s"' % a.replace('"', '""')
    return a


class Manager:
    def __init__(self, port=None, llama_port=None):
        self.port = port or paths.DEFAULT_PORT                 # UI/API 端口
        self.llama_port = int(llama_port or paths.DEFAULT_LLAMA_PORT)   # llama-server 端口
        self.lock = threading.RLock()
        self.stage = "stopped"        # stopped / starting / running / stopping / error
        self.pid = None
        self.model_id = None
        self.ctx = ""
        self.kv = ""
        self.cmd = []
        self.started_at = 0.0
        self.ready_at = 0.0
        self.stage_at = time.time()
        self.error = ""
        self.warmup_ms = 0
        self.pending_model = None
        self.thread = None
        self.last_activity = 0.0     # 最近一次检测到请求的时间（悬停显示 + 空闲休眠）
        self.auto_unloaded = False   # 是否因为空闲被自动卸载/休眠

    # ---------- 对外快照 ----------
    def status(self):
        with self.lock:
            idle = (time.time() - self.last_activity) if self.last_activity else 0
            return {
                "stage": self.stage,
                "pid": self.pid,
                "model": self.model_id,
                "ctx": self.ctx,
                "kv": self.kv,
                "started_at": self.started_at,
                "ready_at": self.ready_at,
                "stage_at": self.stage_at,
                "error": self.error,
                "warmup_ms": self.warmup_ms,
                "cmd": list(self.cmd),
                "cmdline": " ".join(esc_bat(a) for a in self.cmd),
                "port": self.port,
                "llama_port": self.llama_port,
                "last_activity": int(self.last_activity),
                "idle_secs": int(idle),
                "auto_unloaded": self.auto_unloaded,
            }

    def touch_activity(self):
        with self.lock:
            self.last_activity = time.time()
            if self.auto_unloaded:
                self.auto_unloaded = False

    def _set(self, stage, **kw):
        with self.lock:
            self.stage = stage
            self.stage_at = time.time()
            for k, v in kw.items():
                setattr(self, k, v)

    # ---------- 命令行 ----------
    def build_cmd(self, entry, cfg, free_mib=None):
        m = entry["meta"] if isinstance(entry.get("meta"), dict) else {}
        extra = 0
        if entry.get("mtp"):
            try:
                extra = int(os.path.getsize(entry["mtp"]) / (1024 * 1024) * 0.25)
            except Exception:
                extra = 0
        ctx, kv = config.resolve_ctx(cfg, free_mib, extra)
        sam, lau = cfg["sampling"], cfg["launch"]

        cmd = [paths.LLAMA_EXE, "-m", entry["model"]]
        if lau["mmproj"] and entry.get("mmproj"):
            cmd += ["--mmproj", entry["mmproj"]]
        cmd += ["--alias", lau["alias"] or "qwen-coder",
                "-c", ctx,
                "-ngl", str(lau["ngl"] or "999"),
                "-fa", lau["fa"],
                "-fit", lau["fit"]]
        if lau["fit"] == "on":
            cmd += ["-fitt", str(lau["fitt"])]
        if str(lau["ot"]).strip():
            cmd += ["-ot", str(lau["ot"]).strip()]
        cmd += ["-t", str(lau["threads"]),
                "-b", str(lau["batch"]),
                "-ub", str(lau["ubatch"]),
                "--cache-type-k", kv, "--cache-type-v", kv,
                "--keep", str(lau["keep"]),
                "--cache-ram", str(lau["cache_ram"])]
        if not lau["kv_offload"]:
            cmd += ["--no-kv-offload"]
        if int(lau["moe_cache"]) > 0:
            cmd += ["--moe-cache-mib", str(lau["moe_cache"])]
        if lau["np"] and str(lau["np"]).strip().lower() != "auto":
            # 允许但危险：见文件头注释。前端会把这个输入框标红。
            cmd += ["-np", str(lau["np"]).strip()]

        # 投机解码：只在模型确实带 MTP 头时才敢开
        can_mtp = bool(entry.get("mtp")) or bool(m.get("has_nextn")) or entry.get("has_nextn")
        if lau["spec"] == "draft-mtp" and can_mtp:
            cmd += ["--spec-type", "draft-mtp", "--spec-draft-n-max", str(lau["spec_n_max"])]
            if entry.get("mtp"):
                cmd += ["-md", entry["mtp"]]

        # 思考链
        cmd += ["--reasoning", "on"]
        if int(ctx) < 131072:
            cmd += ["--no-reasoning-preserve"]
        try:
            budget = int(float(sam.get("budget", "512")))
        except Exception:
            budget = 512
        if budget > 0:
            cmd += ["--reasoning-budget", str(budget)]

        # 采样
        cmd += ["--temp", sam["temp"], "--top-p", sam["top_p"], "--top-k", sam["top_k"],
                "--min-p", sam["min_p"],
                "--presence-penalty", sam["presence"],
                "--repeat-penalty", sam["repeat"], "--repeat-last-n", sam["repeat_n"]]
        dlen = sam["dry_len"]
        if sam["sty"] == "code" and str(dlen) == "2":
            dlen = "3"      # 代码里换行+缩进是正常重复，2 会被 DRY 误罚
        if float(sam["dry_mult"]) > 0:
            cmd += ["--dry-multiplier", sam["dry_mult"], "--dry-base", sam["dry_base"],
                    "--dry-allowed-length", str(dlen), "--dry-penalty-last-n", sam["dry_n"]]
        if float(sam["xtc_prob"]) > 0:
            cmd += ["--xtc-probability", sam["xtc_prob"], "--xtc-threshold", sam["xtc_thresh"]]
        if float(sam["typical_p"]) < 1.0:
            cmd += ["--typical-p", sam["typical_p"]]
        if sam.get("sampler_order") == "rec":
            cmd += ["--samplers", SAMPLER_SEQ]
        if str(sam.get("seed", "-1")) not in ("", "-1"):
            cmd += ["--seed", str(sam["seed"])]

        # LoRA / 适配器：路径用相对 llama.cpp 目录的形式，避免 Windows 盘符里的冒号
        # 与 --lora-scaled 的 "FNAME:SCALE" 分隔符冲突。
        for lo in (lau.get("loras") or []):
            if not lo.get("on"):
                continue
            p = str(lo.get("path") or "")
            if not p or not os.path.isfile(p):
                config.log("lora skipped (missing): %s" % p)
                continue
            try:
                relp = os.path.relpath(p, paths.LLAMA_DIR)
            except Exception:
                relp = p
            if ":" in relp:                     # 跨盘符才退回绝对路径
                relp = p
            try:
                sc = float(lo.get("scale") or 1)
            except Exception:
                sc = 1.0
            if abs(sc - 1.0) < 1e-6:
                cmd += ["--lora", relp]
            else:
                cmd += ["--lora-scaled", "%s:%s" % (relp, ("%.2f" % sc).rstrip("0").rstrip("."))]

        # 空闲休眠：llama.cpp 原生能力，醒来时按需重新加载模型，客户端无感
        lc = config.STATE.get("lifecycle") or {}
        if lc.get("idle_sleep"):
            try:
                secs = int(float(lc.get("idle_minutes") or 15) * 60)
            except Exception:
                secs = 900
            if secs >= 30:
                cmd += ["--sleep-idle-seconds", str(secs)]

        cmd += ["--jinja", "--host", paths.HOST, "--port", str(self.llama_port)]
        if entry.get("mmproj") and lau["mmproj"]:
            cmd += ["--image-min-tokens", "1024"]
        if lau["metrics"]:
            cmd += ["--metrics"]
        cmd += ["--log-file", paths.LOG_FILE]
        return cmd, ctx, kv

    # ---------- 启动 ----------
    def start(self, model_id=None, block=False):
        with self.lock:
            if self.stage in ("starting", "stopping"):
                return False, "busy"
            if self.stage == "running":
                return False, "already running"
            self.stage = "starting"
            self.stage_at = time.time()
            self.error = ""
            self.warmup_ms = 0
            self.auto_unloaded = False
            self.last_activity = time.time()
            self.pending_model = model_id or config.STATE.get("model")
        t = threading.Thread(target=self._boot, args=(self.pending_model,), daemon=True)
        t.start()
        self.thread = t
        if block:
            t.join(240)
        return True, "starting"

    def _boot(self, model_id):
        if not paths.looks_like_llama(paths.LLAMA_DIR):
            self._set("error", error="还没有指定 llama.cpp 目录（设置里填 llama-server.exe 所在文件夹）")
            return
        entry = config.model_entry(model_id)
        if entry is None:
            self._set("error", error="没有检测到模型（models\\ 下没有 .gguf）")
            return
        cfg = config.get_cfg(entry["id"])
        free = monitor.vram_free()
        try:
            cmd, ctx, kv = self.build_cmd(entry, cfg, free)
        except Exception as exc:                      # noqa: BLE001
            self._set("error", error="生成启动参数失败：%s" % exc)
            config.log("build_cmd FAILED %r" % (exc,))
            return
        with self.lock:
            self.cmd = cmd
            self.ctx = "%dK" % (int(ctx) // 1024)
            self.kv = kv
            self.model_id = entry["id"]
            self.started_at = time.time()
        config.log("start %s ctx=%s kv=%s ngl=%s ot=%s fit=%s spec=%s budget=%s"
                   % (entry["label"], ctx, kv, cfg["launch"]["ngl"], cfg["launch"]["ot"],
                      cfg["launch"]["fit"], cfg["launch"]["spec"],
                      cfg["sampling"].get("budget")))
        paths.ensure_dirs()
        _rotate(paths.LOG_FILE)
        try:
            with open(paths.LOG_FILE, "ab") as out:
                out.write(("\n===== %s  %s =====\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                                       entry["label"])).encode("utf-8"))
                out.flush()
                proc = subprocess.Popen(cmd, cwd=paths.LLAMA_DIR, creationflags=paths.CNW,
                                        stdout=out, stderr=subprocess.STDOUT)
            with self.lock:
                self.pid = proc.pid
        except Exception as exc:                      # noqa: BLE001
            self._set("error", error="拉起进程失败：%s" % str(exc)[:90])
            config.log("Popen FAILED %r" % (exc,))
            return

        # 等 /health 变 ok。加载 23GB 模型要几十秒，给 300s。
        deadline = time.time() + 300
        while time.time() < deadline:
            with self.lock:
                if self.stage != "starting":          # 用户点了停止
                    return
            if not pid_alive(proc.pid):
                raw = monitor.log_error()
                self._set("error", error="%s（进程已退出）" % (monitor.error_summary(raw) or "启动失败"))
                config.log("server died during load: %s" % raw)
                return
            st = monitor.health()
            if st == "ok":
                break
            time.sleep(0.7)
        else:
            self._set("error", error="加载超时（300s），请查看日志")
            return

        with self.lock:
            self.ready_at = time.time()
            self.stage = "running"
            self.stage_at = time.time()
            self.last_activity = time.time()
        config.log("running pid=%s load=%.1fs" % (proc.pid, self.ready_at - self.started_at))
        self._warmup()

    def _warmup(self):
        """起来后先发一个极短请求：第一次请求要建计算图，冷启动 ~25s。
        主动吃掉这一下，用户第一次真用就是热的。"""
        t0 = time.time()
        ok, _ = monitor.http_post("/completion",
                                  {"prompt": "hi", "n_predict": 1, "cache_prompt": True,
                                   "stream": False}, timeout=180, port=self.llama_port)
        if ok:
            with self.lock:
                self.warmup_ms = int((time.time() - t0) * 1000)
            config.log("warmup %dms" % self.warmup_ms)

    # ---------- 停止 ----------
    def stop(self, block=False):
        with self.lock:
            pid = self.pid
            if self.stage == "stopped":
                return False, "already stopped"
            self.stage = "stopping"
            self.stage_at = time.time()
        t = threading.Thread(target=self._kill, args=(pid,), daemon=True)
        t.start()
        if block:
            t.join(30)
        return True, "stopping"

    def _kill(self, pid):
        try:
            if pid_alive(pid):
                r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, creationflags=paths.CNW, timeout=20)
                config.log("taskkill pid=%s rc=%s" % (pid, r.returncode))
            else:
                subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"],
                               capture_output=True, creationflags=paths.CNW, timeout=20)
                config.log("taskkill by image name")
        except Exception as exc:                      # noqa: BLE001
            config.log("stop FAILED %r" % (exc,))
        for _ in range(40):
            if not pid_alive(pid):
                break
            time.sleep(0.25)
        with self.lock:
            self.pid = None
            self.stage = "stopped"
            self.stage_at = time.time()
            self.ctx = ""
            self.kv = ""
            self.cmd = []
            self.error = ""
        config.log("stopped")

    # ---------- 重启 / 换模型 ----------
    def restart(self, model_id=None):
        with self.lock:
            if self.stage == "stopping":
                return False, "busy"
        threading.Thread(target=self._restart, args=(model_id,), daemon=True).start()
        return True, "restarting"

    def _restart(self, model_id):
        if self.stage == "running" or pid_alive(self.pid) or self.stage == "error":
            self.stop(block=True)
        for _ in range(60):
            if self.stage == "stopped":
                break
            time.sleep(0.25)
        time.sleep(0.4)          # 给端口释放留一点时间
        if model_id:
            with config._lock:
                config.STATE["model"] = model_id
            config.save()
        self.start(model_id)

    def swap(self, model_id):
        """切换模型：同一个模型就只重启（应用新参数），不同模型走完整重启。"""
        ent = config.model_entry(model_id)
        if ent is None:
            return False, "model not found"
        return self.restart(model_id)
