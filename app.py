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

GWL_STYLE = -16
WS_CAPTION = 0x00C00000
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x10, 0x20
SW_SHOWMINIMIZED, SW_MAXIMIZE, SW_RESTORE = 6, 3, 9
WM_CLOSE, WM_NCLBUTTONDOWN, HTCAPTION = 0x0010, 0x00A1, 2

user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
user32.SendMessageW.restype = ctypes.c_long
user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.IsZoomed.argtypes = [ctypes.c_void_p]
user32.ReleaseCapture.argtypes = []

# 拖动用：GetAsyncKeyState(VK_LBUTTON) 判断左键是否还按着
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.ShowWindow.restype = ctypes.c_bool

VK_LBUTTON = 0x01

# 窗口类名：Edge --app 是 Chrome_WidgetWin_*，pywebview(WebView2) 是 WindowsForms10.Window.*
EDGE_CLASS_HINT = "Chrome_WidgetWin"
WV_CLASS_HINT = "WindowsForms10.Window"

# 逻辑像素尺寸（pywebview 按 DPI 缩放；Edge --window-size 同样按 DPI）
MINI_W, MINI_H = 320, 196
MAIN_W, MAIN_H = 1240, 840

WINDOW_URL = [""]
WEBVIEW = {"lib": None, "win": {}}     # pywebview 后端（有就用，没有退回 Edge）
FRAMELESS = True          # --framed 或 LLMSWITCH_FRAMED=1 可关掉无边框

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
def _MATCH_CLASS(cls):
    """窗口类名白名单：Edge --app 与 pywebview(WebView2) 两种宿主都认。"""
    return (EDGE_CLASS_HINT in cls) or (WV_CLASS_HINT in cls)


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
                if _MATCH_CLASS(cls.value):
                    n = user32.GetWindowTextLengthW(h)
                    if n:
                        buf = ctypes.create_unicode_buffer(n + 1)
                        user32.GetWindowTextW(h, buf, n + 1)
                        # 用前缀匹配：悬浮窗的标题会变成 "LLMSwitch Mini · 运行中"
                        if buf.value.strip().startswith(title):
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


def make_frameless(hwnd):
    """去掉系统标题栏，只留网页内容（窗口仍可从边缘缩放）。"""
    if not FRAMELESS:
        return False
    try:
        style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE)
        if style & WS_CAPTION:
            user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE, style & ~WS_CAPTION)
            user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        return True
    except Exception as exc:                          # noqa: BLE001
        config.log("make_frameless FAILED %r" % (exc,))
        return False


def resize_window(hwnd, w, h, x=None, y=None):
    flags = SWP_NOZORDER | SWP_NOACTIVATE | (SWP_NOMOVE if x is None else 0)
    user32.SetWindowPos(ctypes.c_void_p(hwnd), None, x or 0, y or 0, int(w), int(h), flags)


def prepare_window(title, topmost=False, w=None, h=None, timeout=20):
    """窗口出现后统一处理：去标题栏 → 设尺寸 → 置顶。

    Edge 有时会把样式改回去，所以多试两次。
    """
    end = time.time() + timeout
    hwnd = None
    while time.time() < end:
        hwnd = find_window(title)
        if hwnd:
            break
        time.sleep(0.4)
    if not hwnd:
        return None
    if w and h:
        resize_window(hwnd, w, h)
    for _ in range(3):
        make_frameless(hwnd)
        time.sleep(0.35)
    if topmost:
        set_topmost(hwnd, True)
    elif title == paths.APP_TITLE:
        # 主窗口绝不置顶：只有悬浮窗需要钉在最前面
        set_topmost(hwnd, False)
    return hwnd


def window_action(action, mini=False, topmost=False, w=None, h=None):
    """给 UI 用的窗口控制：open / drag / minimize / maximize / close / expand

    action 别名要兼容前端按钮：min / max / min / maxi / minify。
    """
    action = {"min": "minimize", "max": "maximize", "maxi": "maximize",
              "minimize": "minimize", "maximize": "maximize",
              "close": "close", "expand": "expand", "open": "open",
              "drag": "drag"}.get(action, action)
    title = paths.MINI_TITLE if mini else paths.APP_TITLE
    # pywebview 分支优先：窗口已存在就直接 show，不再新开一个
    win = WEBVIEW["win"].get(title)
    if win is not None:
        try:
            if action == "open":
                win.show()
                if topmost:
                    win.on_top = True
                return True
            if action == "drag":
                hwnd = find_window(title)
                if not hwnd:
                    return False
                return drag_window(hwnd)
            if action == "minimize":
                win.minimize()
                return True
            if action == "maximize":
                if getattr(win, "maximized", False):
                    win.restore()
                else:
                    win.maximize()
                return True
            if action == "close":
                win.destroy()
                WEBVIEW["win"].pop(title, None)
                return True
            if action == "expand":
                other = WEBVIEW["win"].get(paths.APP_TITLE)
                if other is not None and other is not win:
                    # 主窗口已在：关掉悬浮窗，把主窗口带到前台（不是"另开一个页面"）
                    try:
                        other.show()
                        other.restore()
                    except Exception:
                        pass
                    h = find_window(paths.APP_TITLE)
                    if h:
                        user32.ShowWindow(ctypes.c_void_p(h), SW_RESTORE)
                        user32.SetForegroundWindow(ctypes.c_void_p(h))
                        user32.BringWindowToTop(ctypes.c_void_p(h))
                    win.destroy()
                    WEBVIEW["win"].pop(title, None)
                    return "focus-main"
                win.resize(MAIN_W, MAIN_H)
                win.show()
                return "expanded"
        except Exception as exc:                       # noqa: BLE001
            config.log("webview action %s FAILED %r" % (action, exc))
            return False

    if action == "open":
        if webview_lib():
            dw = MINI_W if mini else MAIN_W
            dh = MINI_H if mini else MAIN_H
            return bool(webview_window(title, WINDOW_URL[0] + ("&mini=1" if mini else ""),
                                       w or dw, h or dh,
                                       mini=mini, topmost=topmost))
        return bool(open_window(WINDOW_URL[0] + ("&mini=1" if mini else ""), mini, topmost,
                                w or (MINI_W if mini else MAIN_W),
                                h or (MINI_H if mini else MAIN_H)))

    hwnd = find_window(title)
    if not hwnd:
        return False
    if action == "drag":
        return drag_window(hwnd)
    if action == "minimize":
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWMINIMIZED)
        return True
    if action == "maximize":
        if user32.IsZoomed(ctypes.c_void_p(hwnd)):
            user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
        else:
            user32.ShowWindow(ctypes.c_void_p(hwnd), SW_MAXIMIZE)
        return True
    if action == "close":
        user32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, None, None)
        return True
    if action == "expand":
        # 悬浮窗原地变大 + 由页面跳到主界面（不另开窗口）
        other = find_window(paths.APP_TITLE)
        if other and other != hwnd:
            user32.PostMessageW(ctypes.c_void_p(hwnd), WM_CLOSE, None, None)
            return "focus-main"
        resize_window(hwnd, w or MAIN_W, h or MAIN_H)
        make_frameless(hwnd)
        return "expanded"
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


def webview_lib():
    """有 pywebview + WebView2 时优先用它：窗口真正无边框、可置顶。

    Edge 的 --app 窗口会在页面区域里自己画一条 29px 标题栏（client 268 → innerHeight 239，
    已实测），WS_CAPTION 去掉也没用 —— 只有换宿主才能彻底去掉黑条。
    """
    if WEBVIEW["lib"] is None:
        try:
            import webview as _wv
            WEBVIEW["lib"] = _wv
        except Exception as exc:                       # noqa: BLE001
            WEBVIEW["lib"] = False
            config.log("pywebview unavailable -> edge fallback: %r" % (exc,))
    return WEBVIEW["lib"]


def webview_window(title, url, w, h, mini=False, topmost=False, show=True):
    wv = webview_lib()
    if not wv:
        return None
    try:
        win = wv.create_window(
            title, url,
            width=int(w), height=int(h),
            frameless=bool(FRAMELESS), easy_drag=False,
            on_top=bool(topmost), background_color="#070910",
            hidden=not show,
            min_size=(260, 150) if mini else (760, 560),
        )
        WEBVIEW["win"][title] = win
        return win
    except Exception as exc:                           # noqa: BLE001
        config.log("create_window FAILED %r" % (exc,))
        return None


def drag_window(hwnd):
    """无边框窗口的拖动：按住左键期间跟随光标移动窗口。

    WM_NCLBUTTONDOWN/HTCAPTION 对 frameless 的 WinForms/Chromium 窗口都不生效，
    所以自己轮询光标位置来移动。
    """
    def run():
        r = wt.RECT()
        user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
        p0 = wt.POINT()
        user32.GetCursorPos(ctypes.byref(p0))
        ox, oy = p0.x - r.left, p0.y - r.top
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        moved = False
        while True:
            st = user32.GetAsyncKeyState(VK_LBUTTON)
            if not (st & 0x8000):
                break
            p = wt.POINT()
            user32.GetCursorPos(ctypes.byref(p))
            nx, ny = p.x - ox, p.y - oy
            if not moved and (nx, ny) == (r.left, r.top):
                time.sleep(0.01)
                continue
            moved = True
            user32.SetWindowPos(ctypes.c_void_p(hwnd), None, int(nx), int(ny), 0, 0,
                                SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
            time.sleep(0.015)
        if moved:
            user32.BringWindowToTop(ctypes.c_void_p(hwnd))
    threading.Thread(target=run, daemon=True).start()
    return True


def open_window(url, mini=False, topmost=False, w=1280, h=880, pos=None):
    exe = find_edge()
    if not exe:
        return None
    sw, sh = (w, h) if not mini else (MINI_W, MINI_H)
    if webview_lib():
        win = webview_window(paths.MINI_TITLE if mini else paths.APP_TITLE,
                             url, sw, sh, mini=mini, topmost=topmost)
        if win:
            return "webview"
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
    threading.Thread(target=prepare_window,
                     args=(paths.MINI_TITLE if mini else paths.APP_TITLE, topmost, sw, sh),
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
    ap.add_argument("--framed", action="store_true", help="保留系统标题栏（无边框出问题时用）")
    args = ap.parse_args()

    global FRAMELESS
    if args.framed or os.environ.get("LLMSWITCH_FRAMED") == "1":
        FRAMELESS = False
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

    WINDOW_URL[0] = base_url

    def _win(action, mini, topmost, w, h):
        return window_action(action, mini, topmost, w or win.get("w") or 1280,
                             h or win.get("h") or 880)

    api.set_window_opener(_win)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=api.probe_loop, args=(mgr, stop_evt), daemon=True).start()
    config.log("http listening on %s:%d" % (paths.HOST, args.port))

    url = base_url + ("&mini=1" if args.mini else "")
    use_wv = False
    if not args.serve:
        if not args.browser and webview_lib():
            use_wv = True
            if args.mini:
                webview_window(paths.MINI_TITLE, url, MINI_W, MINI_H, mini=True, topmost=True)
            else:
                webview_window(paths.APP_TITLE, base_url,
                               win.get("w") or MAIN_W, win.get("h") or MAIN_H)
                # 悬浮窗先建好再藏着：点"打开悬浮窗"直接 show，避免二次开窗口卡顿
                webview_window(paths.MINI_TITLE, base_url + "&mini=1", MINI_W, MINI_H,
                               mini=True, topmost=True, show=False)
            config.log("ui backend: pywebview (frameless=%s)" % FRAMELESS)
        elif args.browser or not find_edge():
            os.startfile(url)                          # noqa: S606
        else:
            open_window(url, args.mini, args.topmost,
                        win.get("w") or MAIN_W, win.get("h") or MAIN_H,
                        (win.get("x"), win.get("y")))
            threading.Thread(target=watch_windows, args=(stop_evt,), daemon=True).start()

    try:
        if use_wv:
            WEBVIEW["lib"].start()                     # 阻塞到所有窗口关闭
            stop_evt.set()
        else:
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
