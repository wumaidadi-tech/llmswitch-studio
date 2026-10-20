# -*- coding: utf-8 -*-
"""打包成 Windows 可执行程序。

用法（在装有 pyinstaller 的 Python 里跑）：
    python build_exe.py                     # 目录版（推荐），产物 dist/LLMSwitchStudio/
    python build_exe.py --onefile           # 单文件版，产物 dist/LLMSwitchStudio.exe
    python build_exe.py --out D://somewhere  # 自定义输出目录

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
ONEFILE = "--onefile" in sys.argv


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
    "--distpath", TMPD,
    "--workpath", TMPW,
    "--specpath", TMPW,
    "--add-data", os.path.join(HERE, "web") + os.pathsep + "web",
    # 只排真正用不到的东西。
    # ⚠️ 不要排 email / xml / asyncio：http.server 内部依赖 email.utils，
    #    排掉后 exe 会在启动瞬间静默退出（--noconsole 下看不到任何报错）。
    "--exclude-module", "tkinter",
    "--exclude-module", "unittest",
    "--exclude-module", "pydoc",
    "--exclude-module", "pydoc_data",
    "--exclude-module", "distutils",
    "--paths", HERE,
    os.path.join(HERE, "app.py"),
]

print(" ".join(args))
if subprocess.call(args, cwd=HERE) != 0:
    sys.exit(1)


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


src = os.path.join(TMPD, NAME + (".exe" if ONEFILE else ""))
dst = os.path.join(DIST, NAME + (".exe" if ONEFILE else ""))
os.makedirs(DIST, exist_ok=True)
if os.path.exists(dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    else:
        os.remove(dst)
if os.path.isdir(src):
    shutil.copytree(src, dst)
else:
    shutil.copy2(src, dst)

exe_src = src if ONEFILE else os.path.join(src, NAME + ".exe")
exe_dst = dst if ONEFILE else os.path.join(dst, NAME + ".exe")
ok = sha(exe_src) == sha(exe_dst)
print("built:", dst)
print("verify:", "OK" if ok else "MISMATCH（目标目录写入被拦截，请直接用临时目录里的产物）")
if not ok:
    print("       临时产物：", exe_src)
