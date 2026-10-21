# -*- coding: utf-8 -*-
"""把当前 llama-server 的接入信息写进各个客户端。

参考 cc-switch 的做法（github.com/farion1231/cc-switch）：
  · 只改"必须改的那几个键"，文件里其它内容（注释、排版、别的 provider、MCP、权限）原样保留；
  · 每次写入前留带时间戳的备份；
  · 原子替换（先写 .tmp 再 os.replace），崩溃不会留下半个文件。

支持：
  WorkBuddy   ~/.workbuddy/models.json    JSON 数组，按 id 合并
  Claude Code ~/.claude/settings.json     env.ANTHROPIC_BASE_URL / AUTH_TOKEN
  Codex CLI   ~/.codex/config.toml        [model_providers.llama-local] + 顶层 model / provider
  通用        <数据目录>/client.env、client.json

Claude Code 走 Anthropic Messages 协议；llama.cpp b10775 已经提供 /v1/messages，
所以本地服务可以直接被 Claude Code 使用（更老的构建不支持，会连不上）。
"""
import json
import os
import re
import time

from . import paths

PROVIDER = "llama-local"


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def _backup(path):
    if not os.path.isfile(path):
        return ""
    bak = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    try:
        with open(path, "rb") as src, open(bak, "wb") as dst:
            dst.write(src.read())
        return bak
    except Exception:
        return ""


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _root(base_url):
    """去掉末尾的 /v1 —— Claude Code 自己会拼 /v1/messages。"""
    b = (base_url or "").rstrip("/")
    return b[:-3] if b.endswith("/v1") else b


def _quote(s):
    """TOML 基本字符串：JSON 的转义规则对 ASCII 是兼容的。"""
    return json.dumps(str(s), ensure_ascii=False)


def _json_read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _wb_state(path, model, base_url):
    data = _json_read(path, None)
    if not isinstance(data, list):
        return False, "还没写过"
    hit = next((it for it in data if isinstance(it, dict) and it.get("id") == model), None)
    if not hit:
        return False, "配置里没有「%s」" % model
    cur = str(hit.get("url") or "")
    return cur.startswith(base_url.rstrip("/")), cur


def _claude_state(path, base_url):
    data = _json_read(path, None)
    if not isinstance(data, dict):
        return False, "还没写过"
    cur = str(((data.get("env") or {}) if isinstance(data.get("env"), dict) else {})
              .get("ANTHROPIC_BASE_URL") or "")
    return (bool(cur) and cur.rstrip("/") == _root(base_url)), cur


def _codex_state(path, base_url):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return False, "还没写过"
    top = re.search(r'^model_provider\s*=\s*"([^"]+)"', text, re.M)
    base = re.search(r"\[model_providers\.%s\][\s\S]*?base_url\s*=\s*\"([^\"]+)\"" %
                     re.escape(PROVIDER), text)
    if not top or top.group(1) != PROVIDER:
        return False, "当前 provider 不是 %s" % PROVIDER
    cur = base.group(1) if base else ""
    return (bool(cur) and cur.startswith(base_url.rstrip("/"))), cur


def _file_state(path, base_url):
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return False, "还没生成"
    return base_url.rstrip("/").split("//")[-1].split("/")[0] in text, "已生成"


def targets(model="", base_url=""):
    wb = _home(".workbuddy", "models.json")
    claude = _home(".claude", "settings.json")
    codex = _home(".codex", "config.toml")
    env_path = os.path.join(paths.WORK_DIR, "client.env")
    json_path = os.path.join(paths.WORK_DIR, "client.json")
    rows = [
        {"id": "workbuddy", "name": "WorkBuddy", "path": wb, "writable": True,
         "installed": os.path.isfile(wb),
         "gain": "WorkBuddy 的自定义模型里直接出现「%s」" % (model or "当前模型"),
         "note": "按模型 id 合并 models.json；不认识的字段原样保留"},
        {"id": "claude", "name": "Claude Code", "path": claude, "writable": True,
         "installed": os.path.isfile(claude),
         "gain": "终端里敲 claude 就直连本地（走 /v1/messages，不花 API 钱）",
         "note": "只改 settings.json 的 env：BASE_URL / AUTH_TOKEN / 模型名"},
        {"id": "codex", "name": "Codex CLI", "path": codex, "writable": True,
         "installed": os.path.isfile(codex),
         "gain": "codex 默认改用本地模型（wire_api=chat）",
         "note": "写 [model_providers.llama-local] 并把当前 provider 指过去；cc-switch 切供应商会覆盖回来"},
        {"id": "env", "name": "通用 .env", "path": env_path, "writable": True,
         "installed": True,
         "gain": "任何脚本 source 一下就能用（OPENAI_* 与 ANTHROPIC_* 都有）",
         "note": "写在数据目录，不影响系统环境变量"},
        {"id": "json", "name": "通用 client.json", "path": json_path, "writable": True,
         "installed": True,
         "gain": "给读 JSON 配置的 agent / 自己写的脚本",
         "note": "写在数据目录"},
    ]
    if base_url:
        detect = {"workbuddy": lambda p: _wb_state(p, model, base_url),
                  "claude": lambda p: _claude_state(p, base_url),
                  "codex": lambda p: _codex_state(p, base_url),
                  "env": lambda p: _file_state(p, base_url),
                  "json": lambda p: _file_state(p, base_url)}
        for r in rows:
            ok, cur = detect[r["id"]](r["path"])
            r["configured"] = bool(ok)
            r["current"] = cur
    else:
        for r in rows:
            r["configured"] = False
            r["current"] = ""
    return rows


# ==================== WorkBuddy ====================
def write_workbuddy(model, base_url, api_key, max_ctx, images):
    path = _home(".workbuddy", "models.json")
    data = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:                    # noqa: BLE001
            return False, "读不懂现有 models.json：%s" % exc, path, ""
        if not isinstance(data, list):
            return False, "现有 models.json 不是数组，未改动", path, ""
    want = {
        "id": model, "name": model, "vendor": "Custom",
        "url": base_url.rstrip("/") + "/chat/completions",
        "apiKey": api_key,
        "supportsToolCall": True, "supportsImages": bool(images),
        "supportsReasoning": True, "useCustomProtocol": True,
        "maxInputTokens": int(max_ctx or 131072),
    }
    hit = next((it for it in data if isinstance(it, dict)
                and (it.get("id") == model or it.get("name") == model)), None)
    if hit is None:
        data.append(want)
    else:
        hit.update(want)                 # 合并：未知字段保留
    bak = _backup(path)
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True, ("已更新" if bak else "已新增") + "「%s」" % model, path, bak


# ==================== Claude Code ====================
def write_claude(model, base_url, api_key, max_ctx, images):
    path = _home(".claude", "settings.json")
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:                    # noqa: BLE001
            return False, "读不懂现有 settings.json：%s" % exc, path, ""
        if not isinstance(data, dict):
            return False, "现有 settings.json 不是对象，未改动", path, ""
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    env.update({
        "ANTHROPIC_BASE_URL": _root(base_url),
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
    })
    data["env"] = env
    bak = _backup(path)
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return True, "已写入 Claude Code（重开会话生效）", path, bak


# ==================== Codex CLI ====================
_HEADER_RE = re.compile(r"^\s*\[model_providers\.%s\]\s*$" % re.escape(PROVIDER), re.M)


def _toml_set_top(text, key, value):
    """只改第一个 [section] 之前的顶层键；没有就插在顶部区域末尾。"""
    lines = text.splitlines()
    sec = next((i for i, ln in enumerate(lines) if ln.strip().startswith("[")), len(lines))
    head = [ln for ln in lines[:sec]
            if not re.match(r"^\s*%s\s*=" % re.escape(key), ln)]
    while head and not head[-1].strip():
        head.pop()
    head.append("%s = %s" % (key, _quote(value)))
    return "\n".join(head + [""] + lines[sec:])


def _toml_drop_provider(text):
    """删掉旧的 [model_providers.llama-local] 整段（到下一个 [ 之前），其余不动。"""
    res, skipping = [], False
    header = re.compile(r"^\[model_providers\.%s\]\s*$" % re.escape(PROVIDER))
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("["):
            skipping = bool(header.match(s))
            if skipping:
                continue
        if not skipping:
            res.append(ln)
    return "\n".join(res)


def write_codex(model, base_url, api_key, max_ctx, images):
    path = _home(".codex", "config.toml")
    text = ""
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except Exception as exc:                    # noqa: BLE001
            return False, "读不了 config.toml：%s" % exc, path, ""
    body = _toml_drop_provider(text)
    body = _toml_set_top(body, "model", model)
    body = _toml_set_top(body, "model_provider", PROVIDER)
    block = "\n".join([
        "",
        "[model_providers.%s]" % PROVIDER,
        'name = "llama.cpp (LLMSwitch)"',
        "base_url = %s" % _quote(base_url.rstrip("/")),
        'wire_api = "chat"',
        "experimental_bearer_token = %s" % _quote(api_key),
        "",
    ])
    bak = _backup(path)
    _atomic_write(path, body.rstrip() + "\n" + block)
    return True, "已写入 Codex 配置（重开终端生效）", path, bak


# ==================== 通用文件 ====================
def write_env(model, base_url, api_key, max_ctx, images):
    path = os.path.join(paths.WORK_DIR, "client.env")
    root = base_url.rstrip("/")
    text = "\n".join([
        "# generated by LLMSwitch Studio %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "# 任意 agent / 脚本都可以读这几个变量",
        "OPENAI_BASE_URL=%s" % root,
        "OPENAI_API_KEY=%s" % api_key,
        "OPENAI_MODEL=%s" % model,
        "",
        "OPENAI_API_BASE=%s" % root,
        "ANTHROPIC_BASE_URL=%s" % _root(base_url),
        "ANTHROPIC_AUTH_TOKEN=%s" % api_key,
        "",
    ])
    bak = _backup(path)
    _atomic_write(path, text)
    return True, "已写入 %s" % os.path.basename(path), path, bak


def write_json(model, base_url, api_key, max_ctx, images):
    path = os.path.join(paths.WORK_DIR, "client.json")
    obj = {
        "openai_base_url": base_url.rstrip("/"),
        "anthropic_base_url": _root(base_url),
        "api_key": api_key,
        "model": model,
        "max_input_tokens": int(max_ctx or 131072),
        "supports_images": bool(images),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    bak = _backup(path)
    _atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    return True, "已写入 %s" % os.path.basename(path), path, bak


def write(target, model, base_url, api_key, max_ctx, images):
    fn = {"workbuddy": write_workbuddy, "claude": write_claude, "codex": write_codex,
          "env": write_env, "json": write_json}.get(target)
    if fn is None:
        return False, "不支持该客户端", "", ""
    return fn(model, base_url, api_key, max_ctx, images)
