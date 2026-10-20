# -*- coding: utf-8 -*-
"""GGUF 读取与模型库扫描。

只读文件头 —— 不加载权重。用途：
  1. 把文件名里的信息换成**文件里的真实元数据**（层数、专家数、原生上下文、量化类型）；
  2. 判断主模型是否内嵌 MTP(nextn) 头，决定能否 `--spec-type draft-mtp`；
  3. 给每个主模型配对同目录的 mmproj / 独立 draft 头。

GGUF v2/v3 二进制布局（都是小端）：
    magic "GGUF" | version u32 | tensor_count u64 | kv_count u64
    kv: key(string) value_type u32 value
    tensor_info: name(string) n_dims u32 dims[n_dims]u64 type u32 offset u64
    string = u64 长度 + UTF-8 字节
"""
import os
import re
import struct

from . import paths

# ---- 值类型 ----
_STR, _ARR = 8, 9
_FIX = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}

# general.file_type -> 量化名（llama.cpp 的枚举，仅收录常见项）
_FTYPE = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1",
    10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
    14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
    19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS",
    24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
    29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
}

# GGML 张量类型（llama.cpp ggml_type 枚举里常用的部分）
TTYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1", 8: "Q8_0",
    9: "Q8_1", 10: "Q2_K", 11: "Q3_K", 12: "Q4_K", 13: "Q5_K", 14: "Q6_K",
    15: "Q8_K", 16: "IQ2_XXS", 17: "IQ2_XS", 18: "IQ3_XXS", 19: "IQ1_S",
    20: "IQ4_NL", 21: "IQ3_S", 22: "IQ2_S", 23: "IQ4_XS", 24: "I8", 25: "I16",
    26: "I32", 27: "I64", 28: "F64", 29: "IQ1_M", 30: "BF16", 34: "TQ1_0",
    35: "TQ2_0", 39: "MXFP4",
}

# 元数据里值得取出来的键（其余一律 seek 跳过，不读内容）
_KEEP = {
    "general.architecture": "arch",
    "general.name": "name",
    "general.file_type": "file_type",
    "general.quantization_version": "quant_version",
    "tokenizer.ggml.model": "tokenizer",
    "split.count": "split_count",
}
_KEEP_SUFFIX = {
    "block_count": "n_layers",
    "context_length": "n_ctx_train",
    "embedding_length": "n_embd",
    "expert_count": "n_expert",
    "expert_used_count": "n_expert_used",
    "attention.head_count": "n_head",
    "attention.head_count_kv": "n_head_kv",
}

_QUANT_RE = re.compile(
    r"(IQ\d[_A-Z0-9]*|Q\d[_A-Z0-9]*|BF16|F16|FP16|F32)", re.I)


class _Reader:
    def __init__(self, f):
        self.f = f
        self.n = 0                       # 已读字节数（用于限额）

    def take(self, k):
        b = self.f.read(k)
        if len(b) != k:
            raise EOFError
        self.n += k
        return b

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.take(8))[0]

    def string(self):
        n = self.u64()
        if n > 64 * 1024 * 1024:         # 防御：损坏文件里的天文数字
            raise ValueError("bad string len")
        return self.take(n).decode("utf-8", "replace")

    def value(self, vt, want=False):
        """读一个值；want=False 时只跳过（数组用 seek，不逐元素读）。"""
        if vt == _STR:
            return self.string() if want else (self.f.seek(self.u64(), 1) or None)
        if vt == _ARR:
            at = self.u32()
            cnt = self.u64()
            if not want:
                if at in _FIX:
                    self.f.seek(_FIX[at] * cnt, 1)
                    self.n += _FIX[at] * cnt
                else:                     # 字符串/嵌套数组只能逐个走
                    for _ in range(cnt):
                        self.value(at, False)
                return None
            out = []
            for _ in range(min(cnt, 4096)):
                out.append(self.value(at, True))
            return out
        sz = _FIX.get(vt)
        if sz is None:
            raise ValueError("bad value type %d" % vt)
        b = self.take(sz)
        if not want:
            return None
        if vt == 7:                       # bool
            return b[0] != 0
        if vt == 6:
            return struct.unpack("<f", b)[0]
        if vt == 12:
            return struct.unpack("<d", b)[0]
        return int.from_bytes(b, "little", signed=(vt in (1, 3, 5, 11)))


def read_meta(path, max_kv=200000):
    """读 GGUF 头部元数据 → dict。失败返回 {"error": ...}，绝不抛给调用方。"""
    out = {}
    try:
        with open(path, "rb") as f:
            r = _Reader(f)
            if r.take(4) != b"GGUF":
                return {"error": "not gguf"}
            ver = r.u32()
            if ver not in (2, 3):
                return {"error": "unsupported gguf v%d" % ver}
            tcnt = r.u64()
            kcnt = r.u64()
            if kcnt > max_kv:
                return {"error": "kv_count too large"}
            arch = None
            for _ in range(kcnt):
                key = r.string()
                vt = r.u32()
                alias = _KEEP.get(key)
                if alias is None and "." in key:
                    suffix = key.rsplit(".", 1)[1]
                    if suffix in _KEEP_SUFFIX:
                        alias = _KEEP_SUFFIX[suffix]
                if key == "general.architecture":
                    arch = None
                val = r.value(vt, want=alias is not None)
                if alias and val is not None:
                    out[alias] = val
                if key == "general.architecture" and isinstance(val, str):
                    arch = val
            # 张量表：找 nextn；同时按 offset 差值算出每种类型的真实字节数
            has_nextn = False
            tensors = []
            for _ in range(tcnt):
                nm = r.string()
                low = nm.lower()
                if "nextn" in low:
                    has_nextn = True
                nd = r.u32()
                f.seek(8 * nd, 1)
                ty = r.u32()
                off = r.u64()
                tensors.append((ty, off))
            tensors.sort(key=lambda x: x[1])
            try:
                file_size = os.path.getsize(path)
            except OSError:
                file_size = 0
            by_type = {}
            for i, (ty, off) in enumerate(tensors):
                end = tensors[i + 1][1] if i + 1 < len(tensors) else file_size
                if end > off:
                    by_type[ty] = by_type.get(ty, 0) + (end - off)
            total = sum(by_type.values()) or 1
            mix = sorted(({"type": TTYPES.get(t, "T%d" % t), "bytes": b,
                           "pct": round(100.0 * b / total, 1)}
                          for t, b in by_type.items()),
                         key=lambda x: -x["bytes"])
            out["quant_mix"] = mix[:4]
        out.setdefault("arch", arch or "")
        out["has_nextn"] = has_nextn
        out["gguf_version"] = ver
    except Exception as exc:                       # noqa: BLE001 —— 读不了就当没有
        return {"error": "%s: %s" % (type(exc).__name__, exc)}
    return out


# ==================== 命名与配对 ====================
_LABEL_NOISE = {"uncensored", "native", "mtp", "preserved", "abliterated",
                "apex", "miniplus", "v2", "v21", "i", "gguf", "instruct"}
_APEX_RE = re.compile(r"APEX[-_]?I[-_]?([A-Za-z]+)", re.I)
_APEX_TIERS = ("quality", "balanced", "compact", "mini")
_SIZE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)B(?![a-zA-Z])")
_ACTIVE_RE = re.compile(r"A(\d+(?:\.\d+)?)B", re.I)


def apex_tier(name):
    m = _APEX_RE.search(name)
    if m and m.group(1).lower() in _APEX_TIERS:
        return m.group(1).lower()
    return None


def size_tag(name):
    """从文件名取 "35B-A3B" 这类规格标签（取不到返回空）"""
    m = _SIZE_RE.search(name)
    if not m:
        return ""
    total = m.group(1) + "B"
    a = _ACTIVE_RE.search(name)
    return total + ("-A" + a.group(1) + "B" if a else "")


def _stem(path):
    """去掉 .gguf 后缀。

    ⚠️ 不能用 os.path.splitext：模型名里带 "Qwen3.6"，splitext 会把
       ".6-35B-A3B-UD-Q4_K_M" 当成扩展名，标签直接退化成 "Qwen3"。
    """
    base = os.path.basename(path)
    return base[:-5] if base.lower().endswith(".gguf") else base


def quant_of(name, file_type=None, mix=None):
    """量化标识。

    优先看**张量字节占比**：现在的量化多为混合（例如 Q6_K 39% + IQ4_XS 36% + Q5_K 24%），
    GGUF 的 general.file_type 只是 "MOSTLY_xxx" 的粗略标记，单看它会骗人。
    没有任何一种类型占比 ≥ 60% 时返回 "混合"。
    """
    if mix:
        top = mix[0]
        if top["pct"] >= 60:
            return top["type"]
        return "混合"
    m = _QUANT_RE.search(_stem(name))
    if m:
        return m.group(1).upper().replace("-", "_")
    return _FTYPE.get(file_type if isinstance(file_type, int) else -1, "")


def model_label(path):
    """模型显示名：直接用文件名（去掉 .gguf）。

    之前会拼成 "Qwen3.6·APEX·Quality" 这种缩写，猜错时反而误导，
    现在是什么文件就显示什么。
    """
    return _stem(path)


def _old_model_label(path):
    name = _stem(path)
    low = name.lower()
    if "huihui" in low:
        return "Qwen3.6·Huihui"
    m = re.match(r"(Qwen\d+(?:\.\d+)?)", name, re.I)
    family = m.group(1) if m else name[:14]
    rest = name[len(family):] if m else ""
    tier = apex_tier(name)
    if tier:
        return "%s·APEX·%s" % (family, tier.capitalize())
    if "ud-" in low or "_ud_" in low or "ud_q" in low:
        q = quant_of(name)
        return "%s·UD%s" % (family, ("·" + q) if q else "")
    key = ""
    for tok in re.split(r"[-._]", rest.strip("-._ ")):
        t = tok.strip()
        if not t or t.isdigit() or t.lower() in _LABEL_NOISE:
            continue
        if re.match(r"^\d+B$", t, re.I) or re.match(r"^A\d+B$", t, re.I):
            continue
        key = t
        break
    return family + ("·" + key.capitalize() if key else "")


def _is_sidecar(low):
    """附属文件（mmproj 视觉投影 / 独立投机草稿头）。

    ⚠️ 判据不能用"文件名里含 mtp" —— 主模型叫
       "...-Native-MTP-Preserved-APEX-I-Quality.gguf" 也含 "-mtp-"，
       曾因此把主力模型整个扫丢。只认"开头就是 mtp-/draft-"这类真正的前缀。
    """
    return ("mmproj" in low
            or low.startswith(("mtp-", "mtp_", "draft-", "draft_"))
            or "draft" in low)


def scan_models():
    """扫 models/ → 主模型条目列表（递归，按目录分组配对 mmproj / draft 头）。

    返回 [{id, label, model, mmproj, mtp, size, mtime, meta, quant, spec, dir}]
    id 用相对 LLAMA_DIR 的路径（与旧版状态文件一致，迁移时能对上）。
    """
    found = []
    for root, _dirs, files in os.walk(paths.MODELS_DIR):
        for fn in files:
            if not fn.lower().endswith(".gguf"):
                continue
            full = os.path.join(root, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            found.append((full, fn, st.st_size, st.st_mtime))
    mains, sides = [], []
    for full, fn, size, mtime in found:
        (sides if _is_sidecar(fn.lower()) else mains).append((full, fn, size, mtime))

    by_dir = {}
    for item in sides:
        by_dir.setdefault(os.path.dirname(item[0]), []).append(item)

    out = []
    for full, fn, size, mtime in sorted(mains, key=lambda x: x[0].lower()):
        d = os.path.dirname(full)
        peers = by_dir.get(d, [])
        mmproj = ""
        draft = ""
        for pfull, pfn, _ps, _pm in peers:
            low = pfn.lower()
            if "mmproj" in low and not mmproj:
                mmproj = pfull
            elif "mmproj" not in low and not draft:
                draft = pfull
        meta = read_meta(full)
        rel = os.path.relpath(full, paths.LLAMA_DIR)
        out.append({
            "id": rel,
            "label": model_label(full),
            "quant_mix": meta.get("quant_mix") or [],
            "mmproj_name": os.path.basename(mmproj) if mmproj else "",
            "mmproj_size": (os.path.getsize(mmproj) if mmproj else 0),
            "model": full,
            "file": fn,
            "mmproj": mmproj,
            "mtp": draft,
            "spec": size_tag(fn),
            "quant": quant_of(fn, meta.get("file_type"), meta.get("quant_mix")),
            "size": size,
            "mtime": int(mtime),
            "dir": os.path.basename(d),
            "meta": meta,
            "has_nextn": bool(meta.get("has_nextn")),
        })
    return out
