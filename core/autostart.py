# -*- coding: utf-8 -*-
"""开机自启（HKCU Run 键，不需要管理员权限）。

只写一个值，卸载/关闭时删掉；不建快捷方式、不改系统设置。
"""
import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "LLMSwitchStudio"


def _command():
    """开机时用什么命令拉起来：默认只开悬浮窗，别一登录就弹大窗口。"""
    if getattr(sys, "frozen", False):
        return '"%s" --mini' % sys.executable
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(pyw):
        exe = pyw
    app = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    return '"%s" "%s" --mini' % (exe, app)


def enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _t = winreg.QueryValueEx(k, VALUE)
            return bool(v)
    except OSError:
        return False


def command():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _t = winreg.QueryValueEx(k, VALUE)
            return v
    except OSError:
        return ""


def set_enabled(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(k, VALUE)
            except FileNotFoundError:
                pass
    return enabled()
