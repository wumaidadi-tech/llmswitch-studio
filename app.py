# -*- coding: utf-8 -*-
"""LLMSwitch Studio 入口。

启动流程
  1. 读状态 → 定位 llama.cpp 目录（命令行 / 环境变量 / 上次使用 / 常见位置）
  2. 扫模型 → 起本地 HTTP 服务（127.0.0.1 + 随机 token）
  3. 用 **Edge 的 app 模式**开窗口加载 UI：
       · 主窗口（1280×880）= 入口页/控制台
       · 紧凑窗口（400×300，`--mini`）= 悬浮窗，可置顶，与主窗口共享同一后端
  4. 所有窗口都关掉后回收进程

为什么不用 pywebview / Electron：Edge 本身就是 Chromium（支持
`backdrop-filter: url()`，这是做出折射的前提），`--app` 起窗口零依赖、零安装。
"""
import argparse
import ctypes
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt

# PyInstaller --noconsole 时 sys.stdout/stderr 是 None，print 会炸
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _crash_log_path():
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                            else __file__))
    return os.path.join(base, "crash.log")


def _excepthook(tp, val, tb):
    """打包成 --noconsole 后看不到任何报错，至少留个 crash.log。"""
    try:
        import traceback
        with open(_crash_log_path(), "a", encoding="utf-8") as f:
            f.write("\n==== %s ====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            traceback.print_exception(tp, val, tb, file=f)
    except Exception:
        pass


sys.excepthook = _excepthook
try:
    import faulthandler
    _fh = open(_crash_log_path(), "a", encoding="utf-8")
    faulthandler.enable(_fh)
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import api, config, gguf, monitor, paths, server   # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 64 位下必须声明原型，否则 HWND 会被截断（窗口一个都找不到）
user32.GetTopWindow.restype = ctypes.c_void_p
user32.GetTopWindow.argtypes = [ctypes.c_void_p]
user32.GetWindow.restype = ctypes.c_void_p
user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.RECT)]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]

GW_HWNDNEXT = 2
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    import shutil
    return shutil.which("msedge") or ""


def hide_console():
    try:
        h = kernel32.GetConsoleWindow()
        if h:
            user32.ShowWindow(h, 0)
    except Exception:
        pass


def port_free(port):
    s = socket.socket()
    try:
        s.bind((paths.HOST, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ==================== 窗口 ====================
def find_window(title):
    """按 标题 + 窗口类 + 面积 找窗口。

    只用 FindWindowW(标题) 不行：Windows 会为应用窗口生成一个同名的
    `Shell.TabProxyWindow`（约 135×37），命中的是假句柄，置顶/关闭检测全部落空。
    """
    best, best_area = None, 0
    h = user32.GetTopWindow(None)
    guard = 0
    while h and guard < 4000:
        guard += 1
        try:
            if user32.IsWindowVisible(h):
                cls = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(h, cls, 128)
                if "Chrome_WidgetWin" in cls.value:
                    n = user32.GetWindowTextLengthW(h)
                    if n:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        user32.GetWindowTextW(h, buf, n + 1)
                        if buf.value.strip() == title:
                            r = wt.RECT()
                            user32.GetWindowRect(h, ctypes.byref(r))
                            area = (r.right - r.left) * (r.bottom - r.top)
                            if area > best_area:
                                best, best_area = h, area
        except Exception:
            pass
        h = user32.GetWindow(h, GW_HWNDNEXT)
    return best if best_area > 260 * 180 else None


def set_topmost(hwnd, on=True):
    HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
    SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x0002, 0x0001, 0x0010
    try:
        user32.SetWindowPos(ctypes.c_void_p(hwnd),
                            ctypes.c_void_p(HWND_TOPMOST if on else HWND_NOTOPMOST),
                            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return True
    except Exception:
        return False


def pin_window(title, timeout=25):
    """等某个窗口出现后钉在最上层（新开的悬浮窗用）。"""
    end = time.time() + timeout
    while time.time() < end:
        h = find_window(title)
        if h:
            set_topmost(h, True)
            return True
        time.sleep(0.5)
    return False


def open_window(url, mini=False, topmost=False, w=1280, h=880, pos=None):
    exe = find_edge()
    if not exe:
        return None
    sw, sh = (w, h) if not mini else (400, 220)
    pos = pos or (None, None)
    args = [exe,
            "--app=" + url,
            "--user-data-dir=" + paths.EDGE_PROFILE,
            "--window-size=%d,%d" % (sw, sh),
            "--no-first-run", "--no-default-browser-check",
            "--disable-features=msEdgeSidebarV2,msHubApps",
            "--disable-sync", "--disable-extensions",
            "--autoplay-policy=no-user-gesture-required"]
    if pos[0] is not None:
        args.append("--window-position=%d,%d" % pos)
    try:
        subprocess.Popen(args, creationflags=paths.CNW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:                          # noqa: BLE001
        config.log("open_window FAILED %r" % (exc,))
        return None
    if topmost:
        threading.Thread(target=pin_window,
                         args=(paths.MINI_TITLE if mini else paths.APP_TITLE,),
                         daemon=True).start()
    return "edge"


def watch_windows(stop_evt):
    """所有窗口都消失（约 6s）后退出。"""
    seen = False
    deadline = time.time() + 20
    while not stop_evt.is_set() and time.time() < deadline:
        if find_window(paths.APP_TITLE) or find_window(paths.MINI_TITLE):
            seen = True
            break
        time.sleep(0.5)
    if not seen:
        config.log("no app window detected; backend keeps running")
        return
    gone = 0
    while not stop_evt.is_set():
        if find_window(paths.APP_TITLE) or find_window(paths.MINI_TITLE):
            gone = 0
        else:
            gone += 1
            if gone >= 3:
                config.log("all windows closed -> shutdown")
                stop_evt.set()
                break
        stop_evt.wait(2.0)


# ==================== 启动 ====================
def boot_data(cli_dir=""):
    """读状态 → 定位 llama.cpp → 扫模型 → 对齐配置"""
    config.load()
    d, src = paths.resolve_llama_dir(config.STATE.get("llama_dir", ""), cli_dir)
    if d:
        paths.set_llama_dir(d)
        config.STATE["llama_dir"] = d
        config.log("llama.cpp: %s (%s)" % (d, src))
    else:
        config.log("llama.cpp not found: waiting for user to pick a folder")
    config.MODELS = gguf.scan_models() if paths.LLAMA_DIR else []
    config.reconcile()
    config.save()
    return bool(d)


def main():
    ap = argparse.ArgumentParser(description="LLMSwitch Studio")
    ap.add_argument("--port", type=int, default=paths.DEFAULT_PORT, help="控制台 API 端口")
    ap.add_argument("--llama-port", type=int, default=paths.DEFAULT_LLAMA_PORT,
                    help="llama-server 端口（客户端连的就是它）")
    ap.add_argument("--llama-dir", default="", help="llama.cpp 目录（含 llama-server.exe）")
    ap.add_argument("--serve", action="store_true", help="只起后端，不开窗口")
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开")
    ap.add_argument("--mini", action="store_true", help="只开紧凑悬浮窗")
    ap.add_argument("--topmost", action="store_true", help="窗口置顶")
    ap.add_argument("--console", action="store_true", help="保留控制台（调试）")
    args = ap.parse_args()

    paths.ensure_dirs()
    if not args.console and not paths.FROZEN:
        hide_console()

    # 单实例：端口已占 → 只把窗口叫出来
    if not port_free(args.port):
        try:
            with open(paths.RUNTIME_FILE, encoding="utf-8") as f:
                rt = json.load(f)
            url = "http://127.0.0.1:%d/?t=%s" % (args.port, rt.get("token", ""))
        except Exception:
            url = "http://127.0.0.1:%d/" % args.port
        config.log("another instance on port %d, opening window only" % args.port)
        open_window(url, args.mini, args.topmost)
        return 0

    boot_data(args.llama_dir)

    mgr = server.Manager(args.port, args.llama_port)
    monitor.set_port(args.llama_port)
    token = secrets.token_urlsafe(18)
    try:
        with open(paths.RUNTIME_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": args.port, "token": token, "pid": os.getpid()}, f)
    except Exception:
        pass

    win = config.STATE.get("window") or {}
    base_url = "http://127.0.0.1:%d/?t=%s" % (args.port, token)

    stop_evt = threading.Event()
    httpd = api.make_server(args.port, token, mgr)

    def _open(mini, topmost):
        return open_window(base_url + ("&mini=1" if mini else ""), mini, topmost,
                           win.get("w") or 1280, win.get("h") or 880,
                           (win.get("x"), win.get("y")))

    api.set_window_opener(_open)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=api.probe_loop, args=(mgr, stop_evt), daemon=True).start()
    config.log("http listening on %s:%d" % (paths.HOST, args.port))

    url = base_url + ("&mini=1" if args.mini else "")
    if not args.serve:
        if args.browser or not find_edge():
            os.startfile(url)                          # noqa: S606
        else:
            open_window(url, args.mini, args.topmost,
                        win.get("w") or 1280, win.get("h") or 880,
                        (win.get("x"), win.get("y")))
            threading.Thread(target=watch_windows, args=(stop_evt,), daemon=True).start()

    try:
        while not stop_evt.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        try:
            httpd.shutdown()
        except Exception:
            pass
        config.log("bye")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        _excepthook(*sys.exc_info())
        traceback.print_exc()
        sys.exit(1)
