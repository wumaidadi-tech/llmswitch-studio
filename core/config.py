# -*- coding: utf-8 -*-
"""配置 schema、默认值、校验、预设、持久化、旧版迁移。

设计原则：
  * 采样与启动参数**每模型一份**，换模型不该带着别人的温度跑。
  * 非法值必须回退默认，而不是原样喂给 llama-server：服务端拒绝参数时
    Popen 仍然"成功"，用户只会看到"已停止"而看不到原因。
  * 写入用 临时文件 + os.replace 原子替换，崩溃不会留下半截 JSON。
  * 界面文案面向所有用户，不写任何"本机/某张显卡"的特定数值。
"""
import json
import os
import threading
import time

from . import paths

SCHEMA_VERSION = 2

# ==================== 采样参数 ====================
# kind: float / int / enum / bool / text
# 前端完全按这张表渲染，新增参数只要在这里加一行。
PARAM_SPECS = [
    dict(key="temp", label="温度", group="sample", kind="float", lo=0.0, hi=3.0, step=0.05, default="0.7",
         hint="越高越发散。对话与写作常用 0.6–0.8"),
    dict(key="top_p", label="Top-P", group="sample", kind="float", lo=0.0, hi=1.0, step=0.01, default="0.95",
         hint="核采样：只保留累计概率前 P 的候选"),
    dict(key="top_k", label="Top-K", group="sample", kind="int", lo=0, hi=200, step=1, default="20",
         hint="每步只在概率最高的 K 个里挑，0 = 不限"),
    dict(key="min_p", label="Min-P", group="sample", kind="float", lo=0.0, hi=1.0, step=0.01, default="0.05",
         hint="低于「最大概率 × Min-P」的候选直接丢弃，通常比 Top-P 更稳"),
    dict(key="presence", label="存在惩罚", group="sample", kind="float", lo=-2.0, hi=2.0, step=0.05, default="0.5",
         hint="对已出现过的词追加惩罚，抑制复读"),
    dict(key="repeat", label="重复惩罚", group="sample", kind="float", lo=1.0, hi=2.0, step=0.01, default="1.08",
         hint="> 1 才生效；越大越强，过高会让措辞变得别扭"),
    dict(key="repeat_n", label="惩罚窗口", group="sample", kind="int", lo=1, hi=8192, step=64, default="256",
         hint="回看多少个 token；太小治不住长距离重复"),
    dict(key="dry_mult", label="DRY 倍率", group="sample", kind="float", lo=0.0, hi=4.0, step=0.05, default="0.8",
         hint="DRY 专门抑制成句复读，普通惩罚抓不住这种循环。0 = 关闭"),
    dict(key="dry_base", label="DRY base", group="sample", kind="float", lo=1.0, hi=3.0, step=0.05, default="1.75",
         hint="惩罚随重复长度指数增长，这是它的底数"),
    dict(key="dry_len", label="DRY 允许长度", group="sample", kind="int", lo=1, hi=16, step=1, default="2",
         hint="不超过该长度的重复视为正常（代码里的换行与缩进需要 3）"),
    dict(key="dry_n", label="DRY 窗口", group="sample", kind="int", lo=0, hi=8192, step=64, default="512",
         hint="DRY 回看窗口；不能为负"),
    dict(key="xtc_prob", label="XTC 概率", group="sample", kind="float", lo=0.0, hi=1.0, step=0.05, default="0.0",
         hint="XTC 主动避开最高概率 token，减少「换着词说同一件事」。0 = 关闭"),
    dict(key="xtc_thresh", label="XTC 阈值", group="sample", kind="float", lo=0.0, hi=1.0, step=0.01, default="0.1",
         hint="只对概率低于该值的候选生效"),
    dict(key="typical_p", label="Typical-P", group="sample", kind="float", lo=0.0, hi=1.0, step=0.01, default="1.0",
         hint="局部典型性采样；1.0 = 关闭"),
    # ---- 思考 ----
    dict(key="budget", label="思考预算", group="think", kind="int", lo=0, hi=8192, step=1, default="512",
         hint="思考链的 token 上限；0 = 不限。越大越慢，一般 256–1024 足够"),
    dict(key="seed", label="随机种子", group="think", kind="int", lo=-1, hi=2147483647, step=1, default="-1",
         hint="-1 = 每次随机；固定值可用于复现"),
    # ---- 采样链 ----
    dict(key="sampler_order", label="采样器顺序", group="order", kind="enum", default="rec",
         options=[["rec", "推荐"], ["def", "llama 默认"]],
         hint="推荐：把惩罚与 DRY 放到温度之后，量化模型更不容易陷循环"),
    dict(key="sty", label="采样风格", group="order", kind="enum", default="gen",
         options=[["gen", "通用"], ["code", "代码"], ["prompt", "写提示词"], ["strict", "严谨"]],
         hint="一次套用一组采样值，套完仍可手动微调"),
]

SAMPLING_KEYS = [p["key"] for p in PARAM_SPECS]
SPEC_BY_KEY = {p["key"]: p for p in PARAM_SPECS}
INT_KEYS = {p["key"] for p in PARAM_SPECS if p["kind"] == "int"}

# 内置采样风格（采样参数子集，套用时只覆盖这里出现的键）
STYLES = {
    "gen":    {"temp": "0.7", "top_p": "0.95", "top_k": "20", "min_p": "0.05",
               "presence": "0.5", "repeat": "1.08", "dry_mult": "0.8"},
    "code":   {"temp": "0.6", "top_p": "0.95", "top_k": "20", "min_p": "0.05",
               "presence": "0.3", "repeat": "1.05", "dry_mult": "0.5"},
    "prompt": {"temp": "1.0", "top_p": "0.97", "top_k": "40", "min_p": "0.03",
               "presence": "0.4", "repeat": "1.06", "dry_mult": "0.8"},
    "strict": {"temp": "0.6", "top_p": "0.80", "top_k": "20", "min_p": "0.05",
               "presence": "0.3", "repeat": "1.05", "dry_mult": "0.5"},
}
STYLE_LABELS = {"gen": "通用", "code": "代码", "prompt": "写提示词", "strict": "严谨"}

# ==================== 启动参数 ====================
LAUNCH_SPECS = [
    dict(key="ctx", label="上下文长度", kind="enum", default="128",
         options=[["auto", "自动"], ["8", "8K"], ["16", "16K"], ["32", "32K"],
                  ["64", "64K"], ["128", "128K"], ["256", "256K"]],
         hint="启动时一次性预留 KV 显存，放不下会直接 OOM；显存紧张时先降这一项"),
    dict(key="kv", label="KV 缓存精度", kind="enum", default="auto",
         options=[["auto", "自动"], ["f16", "f16"], ["q8_0", "q8_0"], ["bf16", "bf16"]],
         hint="自动：≥128K 用 q8_0，以下用 f16。q8_0 约占一半显存"),
    dict(key="ngl", label="GPU 层数", kind="int", lo=0, hi=1000, step=1, default="999",
         hint="卸载到 GPU 的层数；999 = 全部。MoE 模型可配合下面的张量覆盖把专家留在内存"),
    dict(key="ot", label="张量覆盖", kind="text", default="exps=CPU",
         placeholder="例如 exps=CPU",
         hint="按张量名覆盖存放位置，如 exps=CPU 把 MoE 专家放到内存；留空 = 不覆盖"),
    dict(key="fa", label="Flash Attention", kind="enum", default="on",
         options=[["on", "开"], ["auto", "自动"], ["off", "关"]],
         hint="开启可省显存、加速长上下文；个别老卡或驱动上不稳定时改自动"),
    dict(key="fit", label="显存自动适配", kind="enum", default="off",
         options=[["off", "关"], ["on", "开"]],
         hint="开启后 llama.cpp 会自动下调未指定的参数以塞进显存（可能覆盖你的设置）"),
    dict(key="fitt", label="适配保留余量", kind="int", lo=0, hi=8192, step=256, default="1024",
         hint="自动适配时留出多少 MiB 不碰"),
    dict(key="threads", label="CPU 线程", kind="int", lo=1, hi=128, step=1, default="16",
         hint="CPU 侧推理线程数，一般取物理核心数"),
    dict(key="batch", label="批大小", kind="int", lo=64, hi=8192, step=64, default="2048",
         hint="逻辑批，影响 prompt 处理速度与显存峰值"),
    dict(key="ubatch", label="微批大小", kind="int", lo=32, hi=2048, step=32, default="512",
         hint="物理批；显存不足时优先降它"),
    dict(key="cache_ram", label="缓存内存", kind="int", lo=0, hi=65536, step=512, default="8192",
         hint="prompt 缓存上限（MiB）；越大越省重复 prefill"),
    dict(key="keep", label="保留起始 token", kind="int", lo=0, hi=32768, step=512, default="4096",
         hint="截断历史时保留开头多少 token，避免系统提示词被切掉"),
    dict(key="kv_offload", label="KV 放显存", kind="bool", default=True,
         hint="关闭后 KV 放到内存：省显存，但长上下文会明显变慢"),
    dict(key="moe_cache", label="专家显存缓存", kind="int", lo=0, hi=8192, step=128, default="0",
         hint="给放在内存的专家权重开 GPU 侧缓存（MiB），0 = 关闭"),
    dict(key="spec", label="投机解码", kind="enum", default="draft-mtp",
         options=[["draft-mtp", "MTP 自投机"], ["none", "关闭"]],
         hint="模型自带 MTP / EAGLE 头时才有收益；其余情况请关闭"),
    dict(key="spec_n_max", label="投机草稿数", kind="int", lo=1, hi=8, step=1, default="2",
         hint="每次预猜的 token 数，过大反而降低有效速度"),
    dict(key="mmproj", label="多模态投影", kind="bool", default=True,
         hint="模型目录里有 mmproj 时挂载，支持图片输入，会额外占用显存/内存"),
    dict(key="metrics", label="暴露监控指标", kind="bool", default=True,
         hint="开启 /metrics（Prometheus 格式），监控页的请求数与接受率依赖它"),
    dict(key="alias", label="模型别名", kind="text", default="qwen-coder",
         placeholder="qwen-coder",
         hint="OpenAI 接口里 model 字段的名字，客户端配置要填一致"),
    dict(key="np", label="并发槽位", kind="enum", default="auto",
         options=[["auto", "自动"], ["1", "1"], ["2", "2"], ["4", "4"]],
         hint="建议保持自动：显式设置会改变 KV 共享与空闲槽缓存的行为，长请求可能被反复重算"),
]

LAUNCH_KEYS = [p["key"] for p in LAUNCH_SPECS]
LAUNCH_BY_KEY = {p["key"]: p for p in LAUNCH_SPECS}
LAUNCH_INT = {p["key"] for p in LAUNCH_SPECS if p["kind"] == "int"}

CTX_CHOICES = {"auto", "8", "16", "32", "64", "128", "256"}
KV_CHOICES = {"auto", "f16", "q8_0", "bf16"}
SPEC_CHOICES = {"draft-mtp", "none"}
NP_CHOICES = {"auto", "1", "2", "4"}

_lock = threading.RLock()
_save_lock = threading.Lock()

STATE = {
    "version": SCHEMA_VERSION,
    "model": None,
    "models": {},          # {id: cfg}
    "presets": {},         # {名字: {sampling:…, launch:…}} 用户预设
    "llama_dir": "",       # llama.cpp 目录（打包版必须记下来）
    "ui": {"tab": "dashboard", "mini": False, "topmost": False},
    "window": {"x": None, "y": None, "w": 1280, "h": 880},
}
MODELS = []                # 由 api/app 层填充（gguf.scan_models() 的结果）


# ==================== 规范化 ====================
def _num(spec, raw):
    """按 spec 收敛一个数值。非法/空 → 默认值。"""
    d = spec["default"]
    try:
        v = str(raw).strip()
    except Exception:
        return str(d)
    if v == "":
        return str(d)
    try:
        f = float(v)
    except Exception:
        return str(d)
    lo, hi = spec.get("lo"), spec.get("hi")
    if lo is not None:
        f = max(lo, f)
    if hi is not None:
        f = min(hi, f)
    if spec["kind"] == "int":
        return str(int(round(f)))
    return ("%.4f" % f).rstrip("0").rstrip(".") or "0"


def norm_sampling(raw, base=None):
    m = dict(base or {k: SPEC_BY_KEY[k]["default"] for k in SAMPLING_KEYS})
    raw = raw if isinstance(raw, dict) else {}
    for spec in PARAM_SPECS:
        k = spec["key"]
        if k not in raw:
            continue
        if spec["kind"] == "enum":
            if raw[k] in [o[0] for o in spec["options"]]:
                m[k] = raw[k]
        else:
            m[k] = _num(spec, raw[k])
    for k in SAMPLING_KEYS:
        m.setdefault(k, SPEC_BY_KEY[k]["default"])
    if float(m["dry_n"]) < 0:      # 老配置可能存过 -1（旧文档说法），llama.cpp 拒绝
        m["dry_n"] = "0"
    return m


def norm_launch(raw, base=None):
    m = dict(base or {k: LAUNCH_BY_KEY[k]["default"] for k in LAUNCH_KEYS})
    raw = raw if isinstance(raw, dict) else {}
    for spec in LAUNCH_SPECS:
        k = spec["key"]
        if k not in raw:
            continue
        v = raw[k]
        if spec["kind"] == "bool":
            m[k] = bool(v) if not isinstance(v, str) else v.strip().lower() in ("1", "true", "on", "yes")
        elif spec["kind"] == "enum":
            m[k] = v if v in [o[0] for o in spec["options"]] else spec["default"]
        elif spec["kind"] == "int":
            m[k] = _num(spec, v)
        else:
            s = str(v).strip()
            m[k] = s if len(s) <= 256 else spec["default"]
    # 老字段 mtp(bool) → spec 枚举
    if "mtp" in raw and "spec" not in raw:
        m["spec"] = "draft-mtp" if raw.get("mtp") else "none"
    m["ctx"] = m["ctx"] if m["ctx"] in CTX_CHOICES else "auto"
    m["kv"] = m["kv"] if m["kv"] in KV_CHOICES else "auto"
    m["spec"] = m["spec"] if m["spec"] in SPEC_CHOICES else "draft-mtp"
    m["np"] = m["np"] if m["np"] in NP_CHOICES else "auto"
    m["fa"] = m["fa"] if m["fa"] in ("on", "off", "auto") else "on"
    m["fit"] = m["fit"] if m["fit"] in ("on", "off") else "off"
    return m


def auto_kv(ctx_tokens):
    """auto 档的 KV 类型：128K 及以上用 q8_0，以下 f16。

    128K/f16 在多数单卡上会吃掉数 GB 且不可回收（KV 是启动时一次性预留的），
    所以大上下文默认走 q8_0。
    """
    return "f16" if int(ctx_tokens) < 131072 else "q8_0"


def norm_cfg(raw, mid=""):
    raw = raw if isinstance(raw, dict) else {}
    cfg = norm_sampling(raw.get("sampling") if isinstance(raw.get("sampling"), dict) else raw)
    launch = norm_launch(raw.get("launch") if isinstance(raw.get("launch"), dict) else raw)
    lc, kv = launch["ctx"], launch["kv"]
    if kv == "auto":
        kv = "f16" if lc in ("8", "16", "32", "64") else "q8_0"
    return {"sampling": cfg, "launch": launch, "kv_effective": kv, "id": mid}


def default_cfg(mid=""):
    sam = dict(STYLES["gen"])
    sam.update({k: SPEC_BY_KEY[k]["default"] for k in SAMPLING_KEYS})
    sam["sty"] = "gen"
    return norm_cfg({"sampling": sam,
                     "launch": {k: LAUNCH_BY_KEY[k]["default"] for k in LAUNCH_KEYS}}, mid)


def apply_style(cfg, style):
    """套用内置采样风格：只覆盖风格里出现的键，其余保留用户手调值。"""
    if style not in STYLES:
        return cfg
    sam = dict(cfg["sampling"])
    sam.update(STYLES[style])
    sam["sty"] = style
    out = dict(cfg)
    out["sampling"] = norm_sampling(sam)
    return out


def vram_need(ctx_tokens, kv, extra_mib=0):
    """估算 llama-server 的总占用 MiB。

    经验式：固定开销（CUDA context + 计算缓冲）+ KV。
        KV 每 1K token：f16 ≈ 30 MiB，q8_0 ≈ 15 MiB
    这是**估算**，用于启动前预警，不替代实测；不同模型/层数会有偏差。
    """
    per_k = 30 if kv in ("f16", "bf16") else 15
    return int(2680 + (int(ctx_tokens) / 1024.0) * per_k + extra_mib)


def pick_ctx(free_mib, extra_mib=0):
    """按可用显存挑最大的档位。检测失败时保守给 32K。"""
    if free_mib is None:
        return "32768"
    for ctx in (131072, 65536, 32768, 16384):
        if free_mib >= 2680 + (ctx // 1024) * 30 + 400 + extra_mib:
            return str(ctx)
    return "8192"


def resolve_ctx(cfg, free_mib=None, extra_mib=0):
    """配置 → 真正要传给 -c 的 token 数（以及 KV 类型）"""
    lc = cfg["launch"]["ctx"]
    ctx = pick_ctx(free_mib, extra_mib) if lc == "auto" else str(int(lc) * 1024)
    kv = cfg["launch"]["kv"]
    if kv == "auto":
        kv = auto_kv(ctx)
    return ctx, kv


# ==================== 用户预设 ====================
def list_presets():
    with _lock:
        return json.loads(json.dumps(STATE.get("presets") or {}, ensure_ascii=False))


def save_preset(name, cfg):
    name = (name or "").strip()[:40]
    if not name:
        return False, "预设名不能为空"
    c = norm_cfg(cfg)
    with _lock:
        STATE.setdefault("presets", {})[name] = {"sampling": c["sampling"], "launch": c["launch"]}
    save()
    return True, name


def delete_preset(name):
    with _lock:
        n = STATE.setdefault("presets", {}).pop(name, None)
    if n is None:
        return False
    save()
    return True


def preset_cfg(name):
    with _lock:
        p = (STATE.get("presets") or {}).get(name)
    return norm_cfg(p) if isinstance(p, dict) else None


# ==================== 持久化 ====================
def _coerce_state(d):
    with _lock:
        if not isinstance(d, dict):
            return
        ms = d.get("models")
        if isinstance(ms, dict):
            for mid, cfg in ms.items():
                if isinstance(cfg, dict):
                    STATE["models"][mid] = norm_cfg(cfg, mid)
        ps = d.get("presets")
        if isinstance(ps, dict):
            for name, cfg in ps.items():
                if isinstance(cfg, dict):
                    STATE["presets"][name] = {
                        "sampling": norm_sampling(cfg.get("sampling") or cfg),
                        "launch": norm_launch(cfg.get("launch") or cfg),
                    }
        m = d.get("model")
        if isinstance(m, str):
            STATE["model"] = m
        ld = d.get("llama_dir")
        if isinstance(ld, str):
            STATE["llama_dir"] = ld
        ui = d.get("ui")
        if isinstance(ui, dict):
            for k in ("tab",):
                if isinstance(ui.get(k), str):
                    STATE["ui"][k] = ui[k]
            for k in ("mini", "topmost"):
                if k in ui:
                    STATE["ui"][k] = bool(ui[k])
        win = d.get("window")
        if isinstance(win, dict):
            for k in ("w", "h"):
                if isinstance(win.get(k), (int, float)) and win[k] > 200:
                    STATE["window"][k] = int(win[k])
            for k in ("x", "y"):
                if isinstance(win.get(k), (int, float)):
                    STATE["window"][k] = int(win[k])


def _migrate_legacy():
    """旧版 llm_switch.json → v2。只读旧文件，成功后写自己的状态。"""
    d = None
    used = ""
    for p in paths.LEGACY_STATES:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            used = p
            break
        except Exception:
            continue
    if not isinstance(d, dict):
        return 0
    n = 0
    raw_models = d.get("models")
    if isinstance(raw_models, dict):
        for mid, old in raw_models.items():
            if not isinstance(old, dict):
                continue
            cfg = norm_cfg(old, mid)
            if old.get("kv") in KV_CHOICES:
                cfg["launch"]["kv"] = old["kv"]
                if old["kv"] != "auto":
                    cfg["kv_effective"] = old["kv"]
            if old.get("ctx") in CTX_CHOICES:
                cfg["launch"]["ctx"] = old["ctx"]
            STATE["models"][mid] = cfg
            n += 1
    if isinstance(d.get("model"), str):
        STATE["model"] = d["model"]
    for k in ("x", "y"):
        if isinstance(d.get(k), (int, float)):
            STATE["window"][k] = int(d[k])
    if n:
        log("migrated %d model configs from %s" % (n, used))
    return n


def load():
    """读状态（不扫描磁盘；reconcile 由调用方在扫描后执行）。"""
    paths.ensure_dirs()
    data = None
    try:
        with open(paths.STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = None
    if data is None:
        _migrate_legacy()
    else:
        _coerce_state(data)


def reconcile():
    """对齐磁盘：模型列表变了就补齐配置，清掉已删除模型的孤儿配置。

    ⚠️ 只有在**确实扫到了模型**时才清理：llama.cpp 目录没配好时扫出空列表，
       这里若无条件清理会把用户调好的全部参数一起删掉。
    """
    with _lock:
        ids = [m["id"] for m in MODELS]
        if not ids:
            return
        if STATE["model"] not in ids:
            STATE["model"] = ids[0]
        for mid in list(STATE["models"].keys()):
            if mid not in ids:
                STATE["models"].pop(mid, None)
        for mid in ids:
            if mid not in STATE["models"]:
                STATE["models"][mid] = default_cfg(mid)


def save():
    with _lock:
        data = json.loads(json.dumps(STATE, ensure_ascii=False))
    with _save_lock:
        try:
            paths.ensure_dirs()
            tmp = paths.STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, paths.STATE_FILE)
        except Exception:
            pass


def get_cfg(mid=None):
    mid = mid or STATE.get("model")
    with _lock:
        if not mid:
            return default_cfg("")
        cfg = STATE["models"].get(mid)
        if cfg is None:
            cfg = default_cfg(mid)
            STATE["models"][mid] = cfg
        return json.loads(json.dumps(cfg))


def put_cfg(mid, raw):
    """合并一份配置（采样 + 启动），规范化后写回"""
    with _lock:
        base = STATE["models"].get(mid) or default_cfg(mid)
        merged = {"sampling": dict(base["sampling"]), "launch": dict(base["launch"])}
        if isinstance(raw, dict):
            if isinstance(raw.get("sampling"), dict):
                merged["sampling"].update(raw["sampling"])
            if isinstance(raw.get("launch"), dict):
                merged["launch"].update(raw["launch"])
            if "sampling" not in raw and "launch" not in raw:
                merged["sampling"].update(raw)     # 兼容扁平提交
        cfg = norm_cfg(merged, mid)
        STATE["models"][mid] = cfg
    save()
    return cfg


def model_entry(mid=None):
    mid = mid or STATE.get("model")
    for m in MODELS:
        if m["id"] == mid:
            return m
    return MODELS[0] if MODELS else None


# ==================== 日志 ====================
def log(msg):
    try:
        paths.ensure_dirs()
        line = "%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg)
        with open(paths.APP_LOG, "a", encoding="utf-8") as f:
            f.write(line)
        if os.path.getsize(paths.APP_LOG) > (1 << 20):
            os.replace(paths.APP_LOG, paths.APP_LOG + ".old")
    except Exception:
        pass
