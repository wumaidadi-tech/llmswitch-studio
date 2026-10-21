# -*- coding: utf-8 -*-
"""打包成 Windows 可执行程序。

用法（在装有 pyinstaller + pywebview 的 Python 里跑）：
    python build_exe.py                    # 单文件版（默认），产物 dist/LLMSwitchStudio.exe
    python build_exe.py --onedir           # 目录版，产物 dist/LLMSwitchStudio/
    python build_exe.py --out D://somewhere  # 自定义输出目录

单文件版把 Python 运行时、网页资源、pywebview + WebView2 互操作 DLL 全部塞进
一个 exe，双击即可运行（首次启动会解包到 %TEMP%，约 1~3 秒）。
运行时仍需要系统里有 **Microsoft Edge WebView2 Runtime**（Win10/11 基本自带；
没有时程序会自动退回 Edge --app 模式，不会打不开）。

提示：个别安全软件会拦截在"某些目录"下新生成的 exe（双击无反应、
或报 Could not load PyInstaller's embedded PKG archive）。
遇到就换一个输出目录，例如放到 llama.cpp 根目录下。

为什么要"先在临时目录构建、再拷回来"：
    某些环境（杀软实时扫描 / 目录级写入拦截）会让 PyInstaller 直接写在目标
    目录里的 exe 尾部失效，症状是启动即报
        Could not load PyInstaller's embedded PKG archive
    构建到临时目录再复制，产物哈希正确、可直接运行。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "LLMSwitchStudio"
ONEFILE = "--onedir" not in sys.argv          # 默认单文件


def _arg(flag):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""


DIST = os.path.abspath(_arg("--out") or os.path.join(HERE, "dist"))
TMPD = os.path.join(tempfile.gettempdir(), "llmswitch-build")
TMPW = os.path.join(tempfile.gettempdir(), "llmswitch-build-work")

args = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--onefile" if ONEFILE else "--onedir",
    "--noconsole",
    "--name", NAME,
    "--icon", os.path.join(HERE, "assets", "app.ico"),
    "--distpath", TMPD,
    "--workpath", TMPW,
    "--specpath", TMPW,
    "--add-data", os.path.join(HERE, "web") + os.pathsep + "web",
    "--add-data", os.path.join(HERE, "assets") + os.pathsep + "assets",
    # pywebview 的 WebView2 互操作 DLL 在 webview/lib 下，作为 **数据文件** 打包；
    # pywebview 在 frozen 状态会按 webview.__file__/lib 去找，所以相对路径必须保留。
    "--collect-all", "webview",
    # pythonnet（clr）是 pywebview 的 .NET 桥；Python.Runtime.dll 必须按二进制收集。
    "--collect-all", "pythonnet",
    "--collect-all", "clr_loader",
    "--hidden-import", "clr",
    # GUI 后端只用 WinForms + WebView2，Qt/GTK 之类全部排掉省体积。
    "--exclude-module", "PyQt5",
    "--exclude-module", "PyQt6",
    "--exclude-module", "PySide2",
    "--exclude-module", "PySide6",
    "--exclude-module", "gi",
    # 只排真正用不到的东西。
    # ⚠️ 不要排 email / xml / asyncio：http.server 内部依赖 email.utils，
    #    排掉后 exe 会在启动瞬间静默退出（--noconsole 下看不到任何报错）。
    "--exclude-module", "tkinter",
    "--exclude-module", "unittest",
    "--exclude-module", "pydoc",
    "--exclude-module", "pydoc_data",
    "--paths", HERE,
    os.path.join(HERE, "app.py"),
]

print(" ".join(args))
if subprocess.call(args, cwd=HERE) != 0:
    sys.exit(1)


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _rm(p):
    if os.path.isdir(p):
        shutil.rmtree(p)
    elif os.path.exists(p):
        os.remove(p)


suffix = ".exe" if ONEFILE else ""
src = os.path.join(TMPD, NAME + suffix)
dst = os.path.join(DIST, NAME + suffix)
os.makedirs(DIST, exist_ok=True)
_rm(dst)
if os.path.isdir(src):
    shutil.copytree(src, dst)
else:
    shutil.copy2(src, dst)

exe_src = src if ONEFILE else os.path.join(src, NAME + ".exe")
exe_dst = dst if ONEFILE else os.path.join(dst, NAME + ".exe")
ok = sha(exe_src) == sha(exe_dst)
print("built:", dst)
print("size :", "%.1f MB" % (os.path.getsize(exe_dst) / 1048576.0))
print("verify:", "OK" if ok else "MISMATCH（目标目录写入被拦截，请直接用临时目录里的产物）")
if not ok:
    print("       临时产物：", exe_src)
