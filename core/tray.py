# -*- coding: utf-8 -*-
"""系统托盘图标（纯 ctypes，不引第三方库）。

为什么自己做：pywebview 没有托盘 API，而"关窗口后继续跑 + 空闲休眠"必须有个
一直存在的入口，否则用户只能靠任务管理器。

设计：
  · 独立线程 + 消息专用窗口（HWND_MESSAGE），不抢主线程的消息循环；
  · 菜单四项：打开主界面 / 悬浮窗 / 启动·停止 / 退出程序；
  · 任何一步失败都只写日志、不影响主程序（tray 是可选功能）。
"""
import ctypes
import threading
import time
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_APP = 0x8000
WM_TRAY = WM_APP + 20
WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x100, 0x2
HWND_MESSAGE = -3

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT),
        ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID), ("hBalloonIcon", wt.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.TrackPopupMenu.restype = wt.BOOL
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                              ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE
shell32.ExtractIconExW.argtypes = [wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.HICON),
                                   ctypes.POINTER(wt.HICON), wt.UINT]
shell32.ExtractIconExW.restype = wt.UINT


class Tray:
    """commands: open / mini / toggle / quit 四个回调"""

    def __init__(self, exe_path, tooltip, commands, log=None):
        self.exe_path = exe_path
        self.tooltip = tooltip[:120]
        self.commands = commands
        self.log = log or (lambda *a: None)
        self.hwnd = None
        self.nid = None
        self._nid_ptr = None
        self._wndproc = None
        self._thread = None
        self._tid = 0
        self._icons = None
        self._menu_ids = [1, 2, 3, 5]
        self._labels = ["打开主界面", "悬浮窗", "启动 / 停止", "退出程序"]

    # ---------- 对外 ----------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        for _ in range(30):
            if self.hwnd:
                return True
            time.sleep(0.1)
        return False

    def stop(self):
        if self.hwnd:
            try:
                user32.PostMessageW(self.hwnd, WM_DESTROY, 0, 0)
            except Exception:
                pass

    def set_tooltip(self, text):
        if not self.nid:
            return
        try:
            self.nid.szTip = text[:120]
            shell32.Shell_NotifyIconW(NIM_MODIFY, self._nid_ptr)
        except Exception:
            pass

    # ---------- 内部 ----------
    def _run(self):
        try:
            self._tid = kernel32.GetCurrentThreadId()
            self._wndproc = WNDPROC(self._on_message)
            hinst = kernel32.GetModuleHandleW(None)
            cls = WNDCLASSW()
            cls.lpfnWndProc = self._wndproc
            cls.hInstance = hinst
            cls.lpszClassName = "LLMSwitchTrayW"
            if not user32.RegisterClassW(ctypes.byref(cls)):
                err = ctypes.get_last_error()
                if err not in (0, 1410):           # 1410 = 类已注册
                    self.log("tray: RegisterClass failed %s" % err)
                    return
            self.hwnd = user32.CreateWindowExW(0, "LLMSwitchTrayW", "LLMSwitchTray", 0,
                                               0, 0, 0, 0, wt.HWND(HWND_MESSAGE), None,
                                               hinst, None)
            if not self.hwnd:
                self.log("tray: CreateWindow failed")
                return
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._load_icon()
            nid.szTip = self.tooltip
            self.nid = nid
            self._nid_ptr = ctypes.byref(nid)
            if not shell32.Shell_NotifyIconW(NIM_ADD, self._nid_ptr):
                self.log("tray: Shell_NotifyIcon failed")
                return
            self.log("tray: icon added")
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:                   # noqa: BLE001
            self.log("tray crashed %r" % (exc,))
        finally:
            try:
                if self._nid_ptr:
                    shell32.Shell_NotifyIconW(NIM_DELETE, self._nid_ptr)
            except Exception:
                pass
            self.hwnd = None

    def _load_icon(self):
        """优先从 .ico 文件加载（源码运行时也能显示自己的图标），
        其次从 exe 里取第一个图标，最后退回系统默认图标。"""
        path = self.exe_path or ""
        if path.lower().endswith(".ico"):
            try:
                user32.LoadImageW.restype = wt.HANDLE
                h = user32.LoadImageW(None, path, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON|LOADFROMFILE|DEFAULTSIZE
                if h:
                    return h
            except Exception:
                pass
        try:
            big = wt.HICON()
            small = wt.HICON()
            got = shell32.ExtractIconExW(path, 0, ctypes.byref(big),
                                         ctypes.byref(small), 1)
            if got:
                self._icons = (big, small)
                return small if small.value else big
        except Exception:
            pass
        try:
            return user32.LoadIconW(None, wt.LPCWSTR(32512))   # IDI_APPLICATION
        except Exception:
            return None

    def _on_message(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                low = int(lparam) & 0xFFFF
                if low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._fire("open")
                elif low == WM_RBUTTONUP:
                    self._popup(hwnd)
                return 0
            if msg == WM_COMMAND:
                cmd = int(wparam) & 0xFFFF
                name = {1: "open", 2: "mini", 3: "toggle", 5: "quit"}.get(cmd)
                if name:
                    self._fire(name)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as exc:                   # noqa: BLE001
            self.log("tray msg error %r" % (exc,))
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup(self, hwnd):
        menu = user32.CreatePopupMenu()
        for i, (mid, label) in enumerate(zip(self._menu_ids, self._labels)):
            if i == 3:
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, MF_STRING, mid, label)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)
        cmd = user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                    pt.x, pt.y, 0, hwnd, None)
        user32.DestroyMenu(menu)
        name = {1: "open", 2: "mini", 3: "toggle", 5: "quit"}.get(int(cmd or 0))
        if name:
            self._fire(name)

    def _fire(self, name):
        fn = self.commands.get(name)
        if not fn:
            return
        try:
            fn()
        except Exception as exc:                   # noqa: BLE001
            self.log("tray command %s failed %r" % (name, exc))
