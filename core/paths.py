# -*- coding: utf-8 -*-
"""路径、端口与"源码/打包"两种运行形态。

两种形态：
  · 源码运行：BASE = LLMSwitch/v2，WEB_DIR = BASE/web
  · 打包 exe ：BASE = exe 所在目录（便携），WEB_DIR = PyInstaller 解包目录里的 web/

用户数据（状态、日志、Edge 配置）优先放在 BASE/.workbuddy（便携版拷走即用），
目录不可写时退回 %LOCALAPPDATA%\\LLMSwitch。
llama.cpp 目录在运行期由 resolve_llama_dir() 决定，不再写死任何本机路径。
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE = getattr(sys, "_MEIPASS", "") if FROZEN else ""

if FROZEN:
    BASE = os.path.dirname(os.path.abspath(sys.executable))
    SWITCH = BASE
    WEB_DIR = os.path.join(BUNDLE or BASE, "web")
else:
    CORE = os.path.dirname(os.path.abspath(__file__))
    BASE = os.path.dirname(CORE)                 # LLMSwitch/v2
    SWITCH = os.path.dirname(BASE)               # LLMSwitch
    WEB_DIR = os.path.join(BASE, "web")


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def _pick_work_dir():
    local = os.path.join(BASE, ".workbuddy")
    if _writable(local):
        return local
    alt = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "LLMSwitch")
    os.makedirs(alt, exist_ok=True)
    return alt


WORK_DIR = _pick_work_dir()
STATE_FILE = os.path.join(WORK_DIR, "state.json")
LOG_FILE = os.path.join(WORK_DIR, "llama-server.log")     # llama-server --log-file
APP_LOG = os.path.join(WORK_DIR, "app.log")               # 本程序操作日志
RUNTIME_FILE = os.path.join(WORK_DIR, "runtime.json")     # 端口/token，供外部脚本读取
EDGE_PROFILE = os.path.join(WORK_DIR, "edge-profile")     # Edge app 模式的独立配置目录

# 旧版（v1 悬浮窗）状态文件可能在这些位置，迁移时逐个试
LEGACY_STATES = [
    os.path.join(SWITCH, ".workbuddy", "llm_switch.json"),
    os.path.join(BASE, ".workbuddy", "llm_switch.json"),
    os.path.join(BASE, "llm_switch.json"),
]

# ---- 运行参数 ----
DEFAULT_PORT = 8770          # 控制台 HTTP API 端口（只给 UI 用）
DEFAULT_LLAMA_PORT = 8080    # llama-server 的端口（客户端 / OpenAI SDK 认的是这个）
HOST = "127.0.0.1"
APP_TITLE = "LLMSwitch Studio"
MINI_TITLE = "LLMSwitch Mini"

# Windows 常量
CNW = 0x08000000                     # CREATE_NO_WINDOW
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

# ---- llama.cpp 目录（运行期确定，见 resolve_llama_dir）----
LLAMA_DIR = ""
LLAMA_EXE = ""
MODELS_DIR = ""


def looks_like_llama(d):
    return bool(d) and os.path.isfile(os.path.join(d, "llama-server.exe"))


def resolve_llama_dir(saved="", cli=""):
    """按 命令行 > 环境变量 > 上次使用 > 常见位置 的顺序找 llama.cpp 目录。

    返回 (路径, 来源说明)；找不到返回 ("", "")，由 UI 引导用户手动指定。
    """
    cands = []
    if cli:
        cands.append(("命令行 --llama-dir", cli))
    env = os.environ.get("LLAMA_SWITCH_DIR") or os.environ.get("LLAMA_CPP_DIR")
    if env:
        cands.append(("环境变量", env))
    if saved:
        cands.append(("上次使用", saved))
    grand = os.path.dirname(os.path.dirname(BASE))
    cands += [
        ("程序所在目录", BASE),
        ("当前目录", os.getcwd()),
        ("程序目录/llama.cpp", os.path.join(BASE, "llama.cpp")),
        ("程序目录/llama", os.path.join(BASE, "llama")),
    ]
    # 逐级向上找（exe 可能放在 build/bin、子目录或仓库任意位置）
    up = BASE
    for i in range(1, 6):
        up = os.path.dirname(up)
        if not up:
            break
        cands.append(("上 %d 级目录" % i, up))
    for label, d in cands:
        if d and looks_like_llama(os.path.abspath(d)):
            return os.path.abspath(d), label
    # 最后在上级目录里扫一层 llama-*/ 之类的构建目录
    for base in (os.path.dirname(BASE), grand):
        try:
            for name in sorted(os.listdir(base)):
                d = os.path.join(base, name)
                if os.path.isdir(d) and looks_like_llama(d):
                    return d, "目录扫描"
        except Exception:
            pass
    return "", ""


def set_llama_dir(d):
    """设置并校验 llama.cpp 目录。成功返回 True。"""
    global LLAMA_DIR, LLAMA_EXE, MODELS_DIR
    d = os.path.abspath(d or "")
    if not looks_like_llama(d):
        return False
    LLAMA_DIR = d
    LLAMA_EXE = os.path.join(d, "llama-server.exe")
    MODELS_DIR = os.path.join(d, "models")
    return True


def ensure_dirs():
    os.makedirs(WORK_DIR, exist_ok=True)
