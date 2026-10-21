# -*- coding: utf-8 -*-
"""启动失败诊断：把日志尾部 + 当前配置翻译成"原因 + 怎么改"。

设计原则：
  * 只读，不改任何东西 —— 用户点「诊断」不应该有副作用；
  * 先看日志里的硬证据，再补配置层面的风险提示（可能还没失败，但大概率会）；
  * 每条都给出**具体数值**和**可点的操作**，不要只说 "out of memory"。
"""
import os
import re
import socket

from . import config, monitor, paths

# (正则, 标题, 说明, [修法])  —— 顺序 = 优先级，命中即收集
_LOG_RULES = [
    (r"(cuda|ggml|cublas).*(out of memory|failed to allocate|alloc_buffer|oom)",
     "显存不足（CUDA OOM）",
     "llama-server 向显卡申请显存被拒绝，模型或 KV 缓存放不下。",
     ["把上下文降到更小的档位（8GB 卡建议 32K–64K）",
      "KV 缓存精度改成 q8_0（约为 f16 的一半）",
      "把「微批大小」从 512 降到 256 或 128",
      "关掉其它占显存的程序（ComfyUI、浏览器硬件加速）后再启动"]),
    (r"unknown argument|invalid argument|unrecognized argument|error while handling argument",
     "启动参数被 llama-server 拒绝",
     "某个参数名或值这个构建不认识，进程直接退出。",
     ["检查「进阶项」里手改过的值，先恢复默认再逐项加回",
      "老版本 llama.cpp 不支持 MTP / --lora-scaled 等新参数，换新构建",
      "把该行日志贴出来即可定位具体是哪个参数"]),
    (r"failed to load model|no such file|unable to open|model file.*not found|invalid model",
     "模型文件读不到或格式不对",
     "路径不存在、文件被移动，或不是合法的 GGUF。",
     ["到「模型」页点「重新扫描」确认路径",
      "确认 .gguf 没有被下载工具占用/截断（对比文件大小）"]),
    (r"failed to load lora|error loading lora|lora.*not supported",
     "LoRA 适配器加载失败",
     "适配器和主模型不匹配（不同基座/不同量化体系），或不是 GGUF 格式。",
     ["在「LoRA」卡片里关掉这个适配器",
      "确认适配器与基座模型同源（同架构、同 hidden size）"]),
    (r"address already in use|bind.*failed|only one usage of each socket",
     "端口被占用",
     "另一个 llama-server（或别的程序）已经占着这个端口。",
     ["在「运行」页换一个 llama 端口", "或在任务管理器结束残留的 llama-server.exe"]),
    (r"access is denied|permission denied|errno 13",
     "没有权限",
     "文件被杀毒软件/权限设置锁住，或目录不可写。",
     ["把 llama.cpp 与模型放到普通目录（不要放 Program Files）",
      "把该目录加入杀毒软件白名单"]),
    (r"no kernel image is available|invalid device function|cuda error: 209",
     "CUDA 构建与显卡不匹配",
     "这个 llama.cpp 构建编译的算力架构不包含你的显卡。",
     ["换新的 CUDA 构建（新卡需要较新的 arch）",
      "确认没有误用 CPU-only 包"]),
    (r"unknown model architecture|unsupported model|arch.*not supported",
     "这个构建不认识该模型架构",
     "模型用了比 llama.cpp 更新的结构（例如新的 MoE/MTP 变体）。",
     ["更新 llama.cpp 到最新构建", "或换一个已知兼容的量化版本"]),
    (r"failed to apply chat template|chat template.*error|jinja",
     "对话模板出错",
     "GGUF 里的 Jinja 模板有语法问题，或客户端传了不支持的字段。",
     ["保持 --jinja 开启（本程序默认开）", "换官方模板的模型文件"]),
    (r"0xc000007b|application was unable to start correctly|vcruntime|msvcp",
     "缺少运行库 / DLL 架构不对",
     "Windows 无法启动 llama-server.exe。",
     ["安装 VC++ 运行库（x64）", "确认下载的是 x64 包，且 dll 与 exe 在同一目录"]),
]

_RE_EVIDENCE = re.compile(
    r"(error|failed|invalid|unable|out of memory|oom|cannot|denied|already in use|not supported"
    r"|abort|panic)", re.I)
_RE_SKIP = re.compile(r"(no error|0 errors|ignoring|deprecated)", re.I)


def _port_busy(port):
    s = socket.socket()
    # ⚠️ 不要开 SO_REUSEADDR：Windows 上它允许"抢占"已占用的端口，
    # bind 会成功，于是永远报"端口空闲"。
    try:
        s.bind((paths.HOST, int(port)))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _item(level, title, detail, fixes):
    return {"level": level, "title": title, "detail": detail,
            "fix": [f for f in (fixes or []) if f]}


def _checks(entry, cfg, st, free, need, log_lines):
    """固定检查项 —— 即使没有任何问题也要有东西可看，
    不然"开始诊断"点下去像没反应。"""
    lau = (cfg.get("launch") or {})
    sam = (cfg.get("sampling") or {})
    out = []

    def add(name, ok, detail):
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    ok_dir = paths.looks_like_llama(paths.LLAMA_DIR)
    add("llama.cpp", ok_dir, paths.LLAMA_DIR or "未设置 llama-server.exe 所在目录")
    add("模型文件", bool(entry),
        entry["label"] if entry else "models/ 下没有 .gguf")
    if entry:
        meta = entry.get("meta") or {}
        add("元数据", not meta.get("error"),
            "架构 %s" % (meta.get("arch") or "?") if not meta.get("error") else meta.get("error"))
    add("视觉投影", not (lau.get("mmproj") and entry and not entry.get("mmproj")),
        "有 mmproj" if (entry and entry.get("mmproj")) else "没有 mmproj（不影响纯文本）")
    can_mtp = bool(entry and (entry.get("mtp") or entry.get("has_nextn")))
    add("投机解码", not (lau.get("spec") == "draft-mtp" and not can_mtp),
        "MTP 可用" if can_mtp else "模型无 MTP 头，已按关闭处理")
    port = st.get("llama_port") or paths.DEFAULT_LLAMA_PORT
    if st.get("stage") in ("running", "starting"):
        add("端口", True, "%s 正在被本程序使用" % port)
    else:
        busy = _port_busy(port)
        add("端口", not busy, "%s %s" % (port, "被其它程序占用" if busy else "空闲"))
    active = st.get("stage") in ("running", "starting")
    if free and active:
        add("显存预算", True, "运行中：当前可用 %d MiB（已含本服务占用）" % free)
    elif free:
        add("显存预算", need <= free, "需要 %d / 可用 %d MiB" % (need, free))
    else:
        add("显存预算", True, "读不到显卡信息，跳过（需要 %d MiB）" % need)
    missing = [lo for lo in (lau.get("loras") or [])
               if lo.get("on") and not os.path.isfile(lo.get("path") or "")]
    add("LoRA", not missing, "缺失 %d 个" % len(missing) if missing else
        ("%d 个已启用" % sum(1 for x in (lau.get("loras") or []) if x.get("on"))))
    errs = [ln for ln in log_lines
            if re.search(r"\bE\b|out of memory|cudaMalloc failed|failed to load model",
                         ln, re.I)]
    add("日志错误", not errs, ("最近日志里 %d 条错误行" % len(errs)) if errs else
        "最近 %d 行无错误关键词" % len(log_lines))
    return out


def run(mgr, entry=None, cfg=None, lines=None, free_mib=None):
    st = mgr.status()
    entry = entry or config.model_entry()
    cfg = cfg or config.get_cfg(entry["id"] if entry else None)
    free = free_mib if free_mib is not None else monitor.vram_free()
    log_lines = monitor.log_tail(600) if lines is None else list(lines)
    blob = "\n".join(log_lines[-260:]).lower()

    items = []
    for rx, title, detail, fixes in _LOG_RULES:
        if re.search(rx, blob, re.I):
            items.append(_item("error", title, detail, fixes))

    # ---------- 配置侧风险（还没炸，但大概率炸） ----------
    lau, sam = cfg["launch"], cfg["sampling"]
    try:
        ctx, kv = config.resolve_ctx(cfg, free, int((entry or {}).get("mtp") and 32 or 0))
        need = config.vram_need(int(ctx), kv)
    except Exception:
        ctx, kv, need = 0, "", 0
    if free and need > free and st.get("stage") not in ("running", "starting"):
        items.append(_item(
            "warn", "预计显存不够（配置层面）",
            "按当前配置估算需要 %d MiB，可用只有 %d MiB。" % (need, free),
            ["把上下文降到 %sK" % (_suggest_ctx(free)),
             "KV 改 q8_0" if kv in ("f16", "bf16") else "把上下文再降一档",
             "关掉其它占显存的程序（ComfyUI 关标签页不等于退出）"]))

    if free and lau.get("ctx") == "256" and free < 7000:
        items.append(_item(
            "warn", "256K 上下文在 8GB 级显卡上很难成立",
            "KV 是启动时一次性预留的，256K 会把余量压到几百 MiB。",
            ["降到 128K 或 64K", "确认没有同时跑别的 GPU 任务"]))

    if lau.get("np") not in ("auto", ""):
        items.append(_item(
            "warn", "并发槽位被手动指定",
            "显式 -np 会关掉 kv-unified 与 cache-idle-slots，长请求可能被反复重算。",
            ["把「并发槽位」改回自动"]))

    if lau.get("fit") == "on":
        items.append(_item(
            "info", "开了「显存自动适配」",
            "它会在启动时悄悄下调你没指定的参数，实际值可能和你设的不一样。",
            ["排查异常时先关掉它"]))

    can_mtp = bool(entry and (entry.get("mtp") or entry.get("has_nextn")))
    if lau.get("spec") == "draft-mtp" and not can_mtp:
        items.append(_item(
            "warn", "模型没有 MTP 头却开了投机解码",
            "会多一次无用的加载尝试，甚至启动失败。",
            ["把「投机解码」改成关闭"]))

    if lau.get("mmproj") and entry and not entry.get("mmproj"):
        items.append(_item(
            "info", "开启视觉投影但目录里没有 mmproj",
            "程序会自动跳过；如果客户端的图片输入报错，就是这个原因。",
            ["把 mmproj-*.gguf 放到模型同目录"]))

    missing = [lo for lo in (lau.get("loras") or [])
               if lo.get("on") and not os.path.isfile(lo.get("path") or "")]
    if missing:
        items.append(_item(
            "warn", "LoRA 文件不在了",
            "、".join(os.path.basename(m.get("path") or "") for m in missing[:3]),
            ["在「LoRA」卡片里关掉它，或把文件放回原处"]))

    if st["stage"] == "stopped" and _port_busy(st.get("llama_port") or paths.DEFAULT_LLAMA_PORT):
        items.append(_item(
            "warn", "llama 端口已被占用",
            "端口 %s 现在有进程在监听，启动会失败。" % st.get("llama_port"),
            ["换端口，或结束残留的 llama-server.exe"]))

    if st["stage"] == "error":
        err = (st.get("error") or "").strip()
        if err and not any(it["level"] == "error" for it in items):
            items.append(_item("error", "服务报错", err,
                               ["看下面的日志证据", "改完参数点「保存并重启」"]))
        elif err:
            items.insert(0, _item("error", "服务当前状态：启动失败", err, []))

    # 同标题去重（日志可能命中多条规则但结论一样）
    seen, uniq = set(), []
    for it in items:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        uniq.append(it)

    checks = _checks(entry, cfg, st, free, need, log_lines)
    evidence = [ln for ln in log_lines if _RE_EVIDENCE.search(ln) and not _RE_SKIP.search(ln)][-8:]
    evidence = [re.sub(r"^[\d.:\s]+[IWED]\s+", "", ln)[:300] for ln in evidence]

    if not uniq:
        bad = sum(1 for c in checks if not c["ok"])
        summary = ("%d 项检查全部通过，日志里也没有 OOM / 参数被拒 / 端口占用的痕迹。"
                   % len(checks)) if not bad else ("%d 项检查里有 %d 项没通过，见下。"
                                                   % (len(checks), bad))
    else:
        bad = sum(1 for it in uniq if it["level"] == "error")
        summary = ("发现 %d 个错误、%d 个风险。" % (bad, len(uniq) - bad)) if bad \
            else ("发现 %d 个需要注意的地方，暂时没有硬错误。" % len(uniq))
    return {
        "ok": True,
        "stage": st["stage"],
        "summary": summary,
        "items": uniq,
        "checks": checks,
        "evidence": evidence,
        "meta": {"ctx": ctx, "kv": kv, "need_mib": need, "free_mib": free,
                 "port": st.get("llama_port"), "model": entry["label"] if entry else "",
                 "error": st.get("error") or ""},
    }


def _suggest_ctx(free):
    for c in (128, 64, 32, 16):
        if free >= 2680 + c * 30 + 400:
            return c
    return 8
