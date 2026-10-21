# -*- coding: utf-8 -*-
"""LLMSwitch Studio 入口。

启动流程
  1. 读状态 → 定位 llama.cpp 目录（命令行 / 环境变量 / 上次使用 / 常见位置）
  2. 扫模型 → 起本地 HTTP 服务（127.0.0.1 + 随机 token）
  3. 开窗口加载 UI，两种宿主自动选择：
       · pywebview + WebView2（首选）：真正无边框，没有浏览器自绘标题条
       · Edge --app（兜底）：零依赖，但页面顶部会多出约 29px 的 Chromium 栏
     主窗口 = 控制台；`--mini` / 「打开悬浮窗」= 320×112 悬浮窗，可置顶，
     两者共享同一后端。
  4. 所有窗口都关掉后回收进程

UI 需要 Chromium 内核（`backdrop-filter: url()` 是液态玻璃折射的前提）：
WebView2 与 Edge 都是 Chromium，所以两种宿主都能正确渲染。
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
MINI_W, MINI_H = 320, 112
MAIN_W, MAIN_H = 1240, 840

SPI_GETWORKAREA = 0x0030


def dpi_scale():
    """系统 DPI 缩放（150% -> 1.5）。pywebview 的 width/height 是逻辑像素，
    物理像素 = 逻辑 × 该系数，所以超高分屏上直接给 840 会超出屏幕。"""
    try:
        user32.GetDpiForSystem.restype = ctypes.c_uint
        d = int(user32.GetDpiForSystem())
        if d > 0:
            return d / 96.0
    except Exception:
        pass
    return 1.0


def work_area():
    """屏幕可用区域（不含任务栏），返回逻辑像素 (left, top, w, h)。"""
    r = wt.RECT()
    try:
        if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
            return None
        s = dpi_scale()
        return (r.left / s, r.top / s, (r.right - r.left) / s, (r.bottom - r.top) / s)
    except Exception:
        return None


def clamp_window(w, h, min_w=300, min_h=180):
    """把窗口收进屏幕工作区，避免高 DPI 下窗口比屏幕还高。

    下限可调：悬浮窗只有 320×112，不能套主窗口的 180 下限（否则被撑高，
    底下留一条空白 —— 已实测）。
    """
    wa = work_area()
    if not wa:
        return int(w), int(h)
    _x, _y, aw, ah = wa
    return (max(int(min_w), int(min(w, aw - 24))),
            max(int(min_h), int(min(h, ah - 24))))


def center_pos(w, h):
    wa = work_area()
    if not wa:
        return None, None
    x, y, aw, ah = wa
    return int(x + (aw - w) / 2), int(y + (ah - h) / 2)


def _mini_corner():
    """悬浮窗默认贴右下角（工作区，留 24 逻辑像素）。"""
    wa = work_area()
    if not wa:
        return None, None
    x, y, aw, ah = wa
    return int(x + aw - MINI_W - 24), int(y + ah - MINI_H - 24)

WINDOW_URL = [""]
WEBVIEW = {"lib": None, "win": {}, "size": {}, "started": False}   # pywebview 后端
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


def saved_console_port():
    """上一次运行用的网页端口；读不到就用默认值。"""
    try:
        with open(paths.STATE_FILE, encoding="utf-8") as f:
            p = int(json.load(f).get("console_port") or 0)
        return p if 1024 <= p <= 65535 else 0
    except Exception:
        return 0


# ==================== 窗口 ====================
def _MATCH_CLASS(cls):
    """窗口类名白名单：Edge --app 与 pywebview(WebView2) 两种宿主都认。"""
    return (EDGE_CLASS_HINT in cls) or (WV_CLASS_HINT in cls)


def find_window(title, pid=None):
    """按 标题 + 窗口类 + 面积 找窗口。

    只用 FindWindowW(标题) 不行：Windows 会为应用窗口生成一个同名的
    `Shell.TabProxyWindow`（约 135×37），命中的是假句柄，置顶/关闭检测全部落空。

    pid 不为 None 时只看该进程的窗口。pywebview 窗口必须带上 os.getpid()：
    同时开着两个实例（或旧实例没关）时，标题完全相同，按面积挑最大会选中
    另一个进程的悬浮窗 —— 尺寸修正就修到了别人身上（实测打包版调试时踩到）。
    Edge --app 的窗口属于 msedge.exe，那条路径仍用全局匹配。
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
                    if pid is not None:
                        wpid = ctypes.c_ulong()
                        user32.GetWindowThreadProcessId(h, ctypes.byref(wpid))
                        if wpid.value != pid:
                            h = user32.GetWindow(h, GW_HWNDNEXT)
                            continue
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
    """w/h 是逻辑像素，SetWindowPos 要物理像素（否则高 DPI 下会缩水）。"""
    try:
        user32.GetDpiForWindow.restype = ctypes.c_uint
        s = (int(user32.GetDpiForWindow(ctypes.c_void_p(hwnd))) or 96) / 96.0
    except Exception:
        s = 1.0
    flags = SWP_NOZORDER | SWP_NOACTIVATE | (SWP_NOMOVE if x is None else 0)
    user32.SetWindowPos(ctypes.c_void_p(hwnd), None, x or 0, y or 0,
                        int(w * s), int(h * s), flags)


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
                # 最小化后再点「打开悬浮窗」：Show() 不会还原，先 SW_RESTORE
                hwnd = find_window(title, os.getpid())
                if hwnd and user32.IsIconic(ctypes.c_void_p(hwnd)):
                    user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
                if win.gui is not None and getattr(win, "hidden", False):
                    win.gui.show(win.uid)     # hidden 窗口不触发 shown，绕过装饰器
                    win.hidden = False
                else:
                    try:
                        win.restore()      # 还原最小化；对正常窗口无副作用
                    except Exception:
                        pass
                    win.show()
                if topmost:
                    win.on_top = True
                return True
            if action == "drag":
                hwnd = find_window(title, os.getpid())
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
                    h = find_window(paths.APP_TITLE, os.getpid())
                    if h:
                        user32.ShowWindow(ctypes.c_void_p(h), SW_RESTORE)
                        user32.SetForegroundWindow(ctypes.c_void_p(h))
                        user32.BringWindowToTop(ctypes.c_void_p(h))
                    win.destroy()
                    WEBVIEW["win"].pop(title, None)
                    return "focus-main"
                ew, eh = clamp_window(MAIN_W, MAIN_H)
                win.resize(ew, eh)
                _fix_webview_size(title, ew, eh)
                win.show()
                return "expanded"
        except Exception as exc:                       # noqa: BLE001
            config.log("webview action %s FAILED %r" % (action, exc))
            return False

    if action == "open":
        if webview_lib():
            dw = MINI_W if mini else MAIN_W
            dh = MINI_H if mini else MAIN_H
            if mini:
                px, py = _mini_corner()
            else:
                px, py = center_pos(*clamp_window(w or dw, h or dh))
            return bool(webview_window(title, WINDOW_URL[0] + ("&mini=1" if mini else ""),
                                       w or dw, h or dh,
                                       mini=mini, topmost=topmost, x=px, y=py))
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


def _fix_webview_size(title, w, h, timeout=4.0, settle=1.4):
    """按物理像素把窗口设成请求的 CSS 尺寸。

    pywebview/WinForms 建窗时会吃掉一圈不可见边框（本机 150% 缩放下
    1280×860 实测只有 1266×823，悬浮窗 320×112 只有 306×75），
    不修的话紧凑布局会被裁掉。

    而且不能只设一次：Form 先按带边框的尺寸建出来，再把 FormBorderStyle
    改成 None；WinForms 改边框时**保持 client 不变、把外框缩回 client 尺寸**，
    会把我们刚设的外框吃掉（打包版首次开悬浮窗实测 480×168 → 458×144，
    CSS 只拿到 306×96）。这里在 settle 时间内反复校准，连续 0.3 秒稳定
    在目标尺寸才收手。
    """
    end = time.time() + timeout
    hwnd = None
    while time.time() < end:
        hwnd = find_window(title, os.getpid())
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        config.log("size fix %r: window not found" % title)
        return False
    try:
        user32.GetDpiForWindow.restype = ctypes.c_uint
        dpi = int(user32.GetDpiForWindow(ctypes.c_void_p(hwnd))) or 96
    except Exception:
        dpi = 96
    s = dpi / 96.0
    tw, th = int(round(w * s)), int(round(h * s))
    last, stable_since = (0, 0), None
    stop = time.time() + settle
    while True:
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, tw, th,
                            SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
        time.sleep(0.1)
        r = wt.RECT()
        user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
        last = (r.right - r.left, r.bottom - r.top)
        if last == (tw, th):
            stable_since = stable_since or time.time()
            if time.time() - stable_since >= 0.3:
                break
        else:
            stable_since = None
        if time.time() >= stop:
            break
    ok = last == (tw, th)
    config.log("window %s: size %dx%d -> %dx%d%s"
               % (title, last[0], last[1], tw, th, "" if ok else " (MISMATCH)"))
    return ok


def _fix_all_sizes():
    time.sleep(0.4)
    for title, (w, h) in list(WEBVIEW["size"].items()):
        _fix_webview_size(title, w, h, timeout=2.0)


def webview_window(title, url, w, h, mini=False, topmost=False, show=True,
                   x=None, y=None):
    wv = webview_lib()
    if not wv:
        return None
    w, h = clamp_window(int(w), int(h), 240, 96) if mini else clamp_window(int(w), int(h))
    try:
        extra = {}
        if x is not None:
            extra["x"] = int(x)
        if y is not None:
            extra["y"] = int(y)
        win = wv.create_window(
            title, url,
            width=w, height=h,
            frameless=bool(FRAMELESS), easy_drag=False,
            on_top=bool(topmost), background_color="#070910",
            hidden=not show,
            min_size=(240, 96) if mini else (760, 560),
            **extra,
        )
        WEBVIEW["win"][title] = win
        WEBVIEW["size"][title] = (w, h)
        config.log("window %s: request %dx%d initial %sx%s uid=%s"
                   % (title, w, h, win.initial_width, win.initial_height, win.uid))
        # start() 之前 GUI 循环还没跑，窗口尚未真正创建；那时调用会白等。
        # 另外窗口在 Show 的那一刻还会被 WinForms 重新按 client 定一次外框，
        # 所以 shown 之后再补一次；再加一个定时兜底，防止事件注册晚于 Show。
        if show and WEBVIEW["started"]:
            _fix_webview_size(title, w, h, settle=0.4)

            def _refix():
                _fix_webview_size(title, w, h, timeout=2.0, settle=0.8)

            try:
                win.events.shown += lambda *_: threading.Thread(
                    target=_refix, daemon=True).start()
            except Exception:
                pass
            timer = threading.Timer(1.0, _refix)
            timer.daemon = True
            timer.start()
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
    sw, sh = (MINI_W, MINI_H) if mini else clamp_window(int(w), int(h))
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
    ap.add_argument("--port", type=int, default=None,
                    help="控制台 API 端口（默认用上次保存的端口，否则 %d）" % paths.DEFAULT_PORT)
    ap.add_argument("--llama-port", type=int, default=None,
                    help="llama-server 端口（默认用上次保存的端口，否则 %d）" % paths.DEFAULT_LLAMA_PORT)
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

    port = args.port or saved_console_port() or paths.DEFAULT_PORT
    if not 1024 <= port <= 65535:
        port = paths.DEFAULT_PORT

    # 单实例：端口已占 → 只把窗口叫出来。若占用者不是本程序（runtime.json
    # 对不上），就顺延找一个空闲端口，避免把别的服务当成自己的 UI 打开。
    if not port_free(port):
        rt = {}
        rt_ok = False
        try:
            with open(paths.RUNTIME_FILE, encoding="utf-8") as f:
                rt = json.load(f)
            rt_ok = int(rt.get("port") or 0) == port
        except Exception:
            rt = {}
        if rt_ok:
            url = "http://127.0.0.1:%d/?t=%s" % (port, rt.get("token", ""))
            config.log("another instance on port %d, opening window only" % port)
            open_window(url, args.mini, args.topmost)
            return 0
        for p in range(port + 1, min(port + 30, 65536)):
            if port_free(p):
                config.log("port %d is used by another app; using %d" % (port, p))
                port = p
                break
        else:
            config.log("no free console port near %d" % port)
            return 1

    boot_data(args.llama_dir)

    llama_port = args.llama_port or config.STATE.get("llama_port") or paths.DEFAULT_LLAMA_PORT
    try:
        llama_port = int(llama_port)
    except Exception:
        llama_port = paths.DEFAULT_LLAMA_PORT
    if not 1024 <= llama_port <= 65535:
        llama_port = paths.DEFAULT_LLAMA_PORT

    mgr = server.Manager(port, llama_port)
    monitor.set_port(llama_port)
    # 关窗口不会杀掉 llama-server（它是独立进程）。重开程序时如果端口上已经有
    # 一个健康的服务，就认领它 —— 否则界面显示"已停止"但显存被占着，再点启动还会撞端口。
    try:
        if monitor.health() == "ok":
            pr = monitor.props() or {}
            alias = (config.get_cfg().get("launch") or {}).get("alias")
            if not alias or (pr.get("model_alias") or alias) == alias:
                nc = int(((pr.get("default_generation_settings") or {}).get("n_ctx") or 0))
                mgr._set("running", pid=None, model_id=config.STATE.get("model"),
                         ctx=("%dK" % (nc // 1024)) if nc else "", kv="")
                config.log("adopted existing llama-server on port %d" % llama_port)
    except Exception as exc:                           # noqa: BLE001
        config.log("adopt check failed %r" % (exc,))
    token = secrets.token_urlsafe(18)
    try:
        with open(paths.RUNTIME_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": port, "token": token, "pid": os.getpid()}, f)
    except Exception:
        pass

    win = config.STATE.get("window") or {}
    base_url = "http://127.0.0.1:%d/?t=%s" % (port, token)

    stop_evt = threading.Event()
    httpd = api.make_server(port, token, mgr)

    WINDOW_URL[0] = base_url

    # 先读一次「上次是否在跑」，再启动探测线程（探测线程会写这个字段）
    lc = config.lifecycle()

    def _win(action, mini, topmost, w, h):
        # 主窗口的保存尺寸只用于主窗口；悬浮窗必须用自己的 320×112，
        # 否则会被主窗口的 w/h 覆盖成整屏大小（已实测）。
        if mini:
            return window_action(action, mini, topmost, w, h)
        return window_action(action, mini, topmost,
                             w or win.get("w") or MAIN_W,
                             h or win.get("h") or MAIN_H)

    api.set_window_opener(_win)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=api.probe_loop, args=(mgr, stop_evt), daemon=True).start()
    config.log("http listening on %s:%d" % (paths.HOST, port))

    # 恢复上次的运行状态（LM Studio 的做法：退出时在跑，下次打开自动拉起）
    if lc.get("restore_launch") and lc.get("last_running"):
        def _restore_last():
            time.sleep(1.5)
            if mgr.stage in ("running", "starting"):
                return
            if monitor.health() == "ok":       # 上次的进程还活着，不用再拉一个
                config.log("restore: server already healthy, skip")
                return
            config.log("restore last session -> start %s" % config.STATE.get("model"))
            mgr.start(config.STATE.get("model"))
            config.set_last_running(True, config.STATE.get("model"))
        threading.Thread(target=_restore_last, daemon=True).start()

    url = base_url + ("&mini=1" if args.mini else "")
    use_wv = False
    if not args.serve:
        if not args.browser and webview_lib():
            use_wv = True
            if args.mini:
                mini_pos = _mini_corner()
                webview_window(paths.MINI_TITLE, url, MINI_W, MINI_H, mini=True,
                               topmost=True, x=mini_pos[0], y=mini_pos[1])
            else:
                mw, mh = clamp_window(int(win.get("w") or MAIN_W),
                                      int(win.get("h") or MAIN_H))
                mx, my = center_pos(mw, mh)
                webview_window(paths.APP_TITLE, base_url, mw, mh, x=mx, y=my)
                # 悬浮窗按需创建。不要预建 hidden 窗口：pywebview 的 shown 事件
                # 对 hidden 窗口永远不触发，之后 show() 会白等 20 秒（已实测）。
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
            WEBVIEW["started"] = True
            # start(func)：GUI 起来后在子线程执行，补正 pre-start 建窗时的尺寸损耗
            WEBVIEW["lib"].start(_fix_all_sizes)        # 阻塞到所有窗口关闭
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
            httpd.server_close()
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
