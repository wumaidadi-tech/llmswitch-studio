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
    # ---- 采样 ----
    dict(key="temp", label="温度", group="sample", kind="float", lo=0.0, hi=3.0, step=0.05, default="0.7",
         hint="随机性。0.2 很稳、0.7 均衡、1.0 以上开始发散（适合创意写作）"),
    dict(key="top_p", label="Top-P", group="sample", kind="float", lo=0.0, hi=1.0, step=0.01, default="0.95",
         hint="只从累计概率前 P 的词里挑。0.9 砍掉长尾，1.0 = 不过滤"),
    dict(key="top_k", label="Top-K", group="sample", kind="int", lo=0, hi=200, step=1, default="20",
         hint="每步最多考虑 K 个候选词。0 = 不限，20–40 最常用"),
    dict(key="min_p", label="Min-P", group="sample", kind="float", lo=0.0, hi=1.0, step=0.01, default="0.05",
         hint="低于「最高概率 × Min-P」的词直接丢弃。0.05 比 Top-P 更温和"),
    dict(key="presence", label="存在惩罚", group="sample", kind="float", lo=-2.0, hi=2.0, step=0.05, default="0.5",
         hint="对出现过的词加罚，减少复读。0 = 关闭，0.5 适中"),
    dict(key="repeat", label="重复惩罚", group="sample", kind="float", lo=1.0, hi=2.0, step=0.01, default="1.08",
         hint="1.0 = 关闭；1.1 左右合适，1.3 以上用词会变怪"),
    dict(key="repeat_n", label="惩罚窗口", group="sample", kind="int", lo=1, hi=8192, step=64, default="256",
         hint="往回看多少 token 才算重复。256 够日常，长文可加到 512–1024"),
    dict(key="dry_mult", label="DRY 倍率", group="sample", kind="float", lo=0.0, hi=4.0, step=0.05, default="0.8",
         hint="专治整句重复。0 = 关；0.8 起效，调太高文字会变生硬"),
    dict(key="dry_base", label="DRY base", group="sample", kind="float", lo=1.0, hi=3.0, step=0.05, default="1.75",
         hint="DRY 惩罚随重复长度增长的速度。越大越激进，1.75 常用"),
    dict(key="dry_len", label="DRY 允许长度", group="sample", kind="int", lo=1, hi=16, step=1, default="2",
         hint="不超过这个长度的重复算正常。写代码建议 3，避免误伤缩进"),
    dict(key="dry_n", label="DRY 窗口", group="sample", kind="int", lo=0, hi=8192, step=64, default="512",
         hint="DRY 往回看多少 token。0 = 不看，等于关闭"),
    dict(key="xtc_prob", label="XTC 概率", group="sample", kind="float", lo=0.0, hi=1.0, step=0.05, default="0.0",
         hint="主动避开最高概率的词，减少「换个说法说同一句」。0 = 关"),
    dict(key="xtc_thresh", label="XTC 阈值", group="sample", kind="float", lo=0.0, hi=1.0, step=0.01, default="0.1",
         hint="只替换概率低于该值的候选。0.1 常用"),
    dict(key="typical_p", label="Typical-P", group="sample", kind="float", lo=0.0, hi=1.0, step=0.01, default="1.0",
         hint="局部典型性采样。1.0 = 关；0.9 左右更稳但可能呆板"),
    # ---- 思考 ----
    dict(key="budget", label="思考预算", group="think", kind="int", lo=0, hi=8192, step=1, default="512",
         hint="思考链最多花多少 token。0 = 不限；一般 256–1024，越大越慢"),
    dict(key="seed", label="随机种子", group="think", kind="int", lo=-1, hi=2147483647, step=1, default="-1",
         hint="-1 = 每次随机；填固定值可以复现同样的输出"),
    # ---- 采样链 ----
    dict(key="sampler_order", label="采样器顺序", group="order", kind="enum", default="rec",
         options=[["rec", "推荐"], ["def", "llama 默认"]],
         hint="只决定各个采样器的执行顺序，不会套用任何参数值。采样风格请用上方「预设」"),
    # sty 只用于记录最后套用的风格（server 会据此调整代码场景的 DRY 长度），
    # 界面上不再重复显示 —— 上方「预设」里的内置风格才是唯一的入口。
    dict(key="sty", label="采样风格", group="order", kind="enum", default="gen", hidden=True,
         options=[["gen", "通用"], ["code", "代码"], ["prompt", "写提示词"], ["strict", "严谨"]],
         hint="由上方「预设」控制"),
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
         hint="一次对话能记住多少内容，128K 约等于 20 万字。KV 在启动时就按这个值占满显存"),
    dict(key="kv", label="KV 缓存精度", kind="enum", default="auto",
         options=[["auto", "自动"], ["f16", "f16"], ["bf16", "bf16"], ["q8_0", "q8_0"],
                  ["q5_1", "q5_1"], ["q5_0", "q5_0"], ["q4_1", "q4_1"],
                  ["iq4_nl", "iq4_nl"], ["q4_0", "q4_0"], ["f32", "f32"]],
         hint="KV 的存储精度。自动 = 128K 及以上用 q8_0、以下用 f16；q8_0 约为 f16 一半显存、几乎不掉质量；"
              "q5/q4 系列更省但长文更容易串味；f32 只用于测精度，显存翻倍"),
    dict(key="ngl", label="GPU 层数", kind="int", lo=0, hi=1000, step=1, default="999",
         hint="多少层放到显卡。999 = 全部；显存不够就往下调，调到 0 就全在 CPU 上（很慢）"),
    dict(key="ot", label="张量覆盖", kind="text", default="exps=CPU",
         placeholder="例如 exps=CPU",
         hint="把指定权重放到别处。exps=CPU 让 MoE 专家留在内存、只把注意力放显卡；留空则不改"),
    dict(key="fa", label="Flash Attention", kind="enum", default="on",
         options=[["on", "开"], ["auto", "自动"], ["off", "关"]],
         hint="开启更省显存、长上下文更快。个别老卡/驱动上不稳定时改自动"),
    dict(key="fit", label="显存自动适配", kind="enum", default="off",
         options=[["off", "关"], ["on", "开"]],
         hint="让 llama.cpp 自己下调没填的参数来塞进显存。开了它可能悄悄改掉你设的值"),
    dict(key="fitt", label="适配保留余量", kind="int", lo=0, hi=8192, step=256, default="1024",
         hint="自动适配时给系统留多少空闲显存（MiB）。留太少容易 OOM"),
    dict(key="threads", label="CPU 线程", kind="int", lo=1, hi=128, step=1, default="16",
         hint="CPU 侧推理线程数。一般取物理核心数，开超线程通常没有帮助"),
    dict(key="batch", label="批大小", kind="int", lo=64, hi=8192, step=64, default="2048",
         hint="一次处理多少 token。越大 prompt 处理越快，显存峰值也越高"),
    dict(key="ubatch", label="微批大小", kind="int", lo=32, hi=2048, step=32, default="512",
         hint="单次计算块的大小。爆显存时先降它，比降批大小更有效"),
    dict(key="cache_ram", label="缓存内存", kind="int", lo=0, hi=65536, step=512, default="8192",
         hint="prompt 缓存占多少内存（MiB）。同样开头重发时可以省掉整段 prefill"),
    dict(key="keep", label="保留起始 token", kind="int", lo=0, hi=32768, step=512, default="4096",
         hint="历史被截断时保留开头多少 token，避免系统提示词被切掉"),
    dict(key="kv_offload", label="KV 放显存", kind="bool", default=True,
         hint="关掉 = KV 放内存：省显存，但长上下文明显变慢"),
    dict(key="moe_cache", label="专家显存缓存", kind="int", lo=0, hi=8192, step=128, default="0",
         hint="给留在内存里的专家权重开一块 GPU 缓存（MiB）。0 = 关闭"),
    dict(key="spec", label="投机解码", kind="enum", default="draft-mtp",
         options=[["draft-mtp", "MTP 自投机"], ["none", "关闭"]],
         hint="模型自带 MTP / EAGLE 头时才会更快；其他模型请选关闭"),
    dict(key="spec_n_max", label="投机草稿数", kind="int", lo=1, hi=8, step=1, default="2",
         hint="每次预猜几个 token。太大浪费算力，2–3 通常最好"),
    dict(key="mmproj", label="多模态投影", kind="bool", default=True,
         hint="模型目录里有 mmproj 时挂上，可输入图片；会额外占用显存/内存"),
    dict(key="metrics", label="暴露监控指标", kind="bool", default=True,
         hint="开放 /metrics（Prometheus）。关掉后监控页只能读日志里的速度"),
    dict(key="alias", label="模型别名", kind="text", default="qwen-coder",
         placeholder="local-model",
         hint="OpenAI 接口里 model 字段的名字，客户端配置要填一致"),
    dict(key="np", label="并发槽位", kind="enum", default="auto",
         options=[["auto", "自动"], ["1", "1"], ["2", "2"], ["4", "4"]],
         hint="建议保持自动：显式设置会改变 KV 共享与空闲槽缓存，长请求可能被反复重算"),
]

LAUNCH_KEYS = [p["key"] for p in LAUNCH_SPECS]
LAUNCH_BY_KEY = {p["key"]: p for p in LAUNCH_SPECS}
LAUNCH_INT = {p["key"] for p in LAUNCH_SPECS if p["kind"] == "int"}

CTX_CHOICES = {"auto", "8", "16", "32", "64", "128", "256"}
# 与 llama-server --cache-type-k 的 allowed values 保持一致
# （b10775: f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1）
KV_CHOICES = {"auto", "f32", "f16", "bf16", "q8_0", "q5_1", "q5_0", "q4_1", "iq4_nl", "q4_0"}
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
    "console_port": 0,     # 网页/API 端口（0 = 用默认值）
    "llama_port": 0,       # llama-server 端口（0 = 用默认值）
    "ui": {"tab": "dashboard", "mini": False, "topmost": False},
    "window": {"x": None, "y": None, "w": 1280, "h": 880},
    # 生命周期：空闲休眠 / 启动时恢复上次状态。全局一份，不跟模型走。
    "lifecycle": {
        "idle_sleep": False,      # 空闲后让 llama-server 自己睡（llama.cpp 原生）
        "idle_minutes": 15,       # 空闲多少分钟后睡
        "restore_launch": True,   # 下次启动本程序时恢复上次的运行状态
        "tray": False,            # 关窗口后驻留托盘（需重启程序生效）
        "last_running": False,    # 上次退出时服务是否在运行
        "last_model": "",
        "last_at": 0,
    },
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


def _norm_loras(raw):
    """LoRA 适配器列表：只保留「路径 + 强度 + 开关」，路径不存在也保留
    （文件可能在别的机器上，启动时再校验，避免一次扫描失败就把用户配置清掉）。"""
    out = []
    if not isinstance(raw, list):
        return out
    seen = set()
    for it in raw[:16]:
        if not isinstance(it, dict):
            continue
        p = str(it.get("path") or "").strip()
        if not p or len(p) > 512 or p in seen:
            continue
        seen.add(p)
        try:
            sc = float(it.get("scale", 1.0))
        except Exception:
            sc = 1.0
        sc = max(0.0, min(4.0, sc))
        out.append({"path": p, "scale": ("%.2f" % sc).rstrip("0").rstrip(".") or "0",
                    "on": bool(it.get("on", True))})
    return out


def norm_launch(raw, base=None):
    m = dict(base or {k: LAUNCH_BY_KEY[k]["default"] for k in LAUNCH_KEYS})
    raw = raw if isinstance(raw, dict) else {}
    # loras 不是标量 schema 项，单独处理（前端有一块专用列表 UI）
    loras = raw.get("loras", (base or {}).get("loras"))
    m["loras"] = _norm_loras(loras)
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


# 每 1K token 的 KV 占用（MiB），按 llama.cpp 的元素位宽折算，误差 ±10%
KV_MIB_PER_K = {
    "f32": 60, "f16": 30, "bf16": 30, "q8_0": 16,
    "q5_1": 12, "q5_0": 11, "q4_1": 9, "iq4_nl": 9, "q4_0": 9,
}


def vram_need(ctx_tokens, kv, extra_mib=0):
    """估算 llama-server 的总占用 MiB。

    经验式：固定开销（CUDA context + 计算缓冲）+ KV。
        KV 每 1K token：f16/bf16 ≈ 30、q8_0 ≈ 16、q5≈11–12、q4≈9、f32 ≈ 60
    这是**估算**，用于启动前预警，不替代实测；不同模型/层数会有偏差。
    """
    per_k = KV_MIB_PER_K.get(kv, 15)
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


# ==================== 生命周期 ====================
def lifecycle():
    with _lock:
        return json.loads(json.dumps(STATE.get("lifecycle") or {}, ensure_ascii=False))


def set_lifecycle(**kw):
    with _lock:
        lc = STATE.setdefault("lifecycle", {})
        for k, v in kw.items():
            if k in ("idle_sleep", "restore_launch", "last_running", "tray"):
                lc[k] = bool(v)
            elif k == "idle_minutes":
                try:
                    lc[k] = int(max(1, min(720, int(float(v)))))
                except Exception:
                    pass
            elif k in ("last_model", "last_at"):
                lc[k] = v
    save()
    return lifecycle()


def set_last_running(running, model_id=None):
    """记录"上次退出时服务在不在跑"。probe 每秒都会调，只在变化时落盘。"""
    with _lock:
        lc = STATE.setdefault("lifecycle", {})
        changed = bool(lc.get("last_running")) != bool(running)
        if model_id and lc.get("last_model") != model_id:
            changed = True
        if not changed:
            return False
        lc["last_running"] = bool(running)
        if model_id:
            lc["last_model"] = model_id
        lc["last_at"] = int(time.time())
    save()
    return True


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
        cp = d.get("console_port")
        if isinstance(cp, (int, float)) and 1024 <= int(cp) <= 65535:
            STATE["console_port"] = int(cp)
        lp = d.get("llama_port")
        if isinstance(lp, (int, float)) and 1024 <= int(lp) <= 65535:
            STATE["llama_port"] = int(lp)
        ui = d.get("ui")
        if isinstance(ui, dict):
            for k in ("tab",):
                if isinstance(ui.get(k), str):
                    STATE["ui"][k] = ui[k]
            for k in ("mini", "topmost"):
                if k in ui:
                    STATE["ui"][k] = bool(ui[k])
        lc = d.get("lifecycle")
        if isinstance(lc, dict):
            for k in ("idle_sleep", "restore_launch", "last_running", "tray"):
                if k in lc:
                    STATE["lifecycle"][k] = bool(lc[k])
            for k in ("idle_minutes",):
                if isinstance(lc.get(k), (int, float)):
                    STATE["lifecycle"][k] = int(max(1, min(720, lc[k])))
            for k in ("last_model",):
                if isinstance(lc.get(k), str):
                    STATE["lifecycle"][k] = lc[k]
            for k in ("last_at",):
                if isinstance(lc.get(k), (int, float)):
                    STATE["lifecycle"][k] = int(lc[k])
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
