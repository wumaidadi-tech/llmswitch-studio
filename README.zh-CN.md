# LLMSwitch Studio

给 [llama.cpp](https://github.com/ggml-org/llama.cpp) 用的桌面控制台：切模型、算显存、调启动参数、
看实时吞吐，界面是液态玻璃风格。

![概览](docs/shot-dashboard.png)

<sub>[English](README.md) · 简体中文</sub>

---

## 它解决什么

`llama-server` 本身很完整，但有用的信息散在命令行参数、日志文件和几个 HTTP 接口里。
这个程序把日常真正在做的三件事放进同一个窗口：

| 你想… | 界面上有 |
|---|---|
| **看清**现在跑的是什么 | 直接从 GGUF 头读出来的元数据（架构、层数、专家数、原生上下文、量化） |
| **安全地改**配置 | 显存预算条：预计占用 vs 实际可用，超了直接告诉你该降到多少 K |
| **知道**它快不快 | 解码 / prefill 速度、`/slots` 的上下文占用、`/metrics` 的请求数与投机接受率，外加 3 分钟历史曲线 |

全部本地运行：没有遥测、不需要账号、不联网。

## 功能

- **模型库**：递归扫描 `models/`，读真实 GGUF 元数据，识别 `mmproj` 与 MTP 头，一键载入。
  量化按**张量字节占比**统计（多数 GGUF 是混合的，例如 `Q6_K 39% · IQ4_XS 36% · Q5_K 23%`），
  不再只看 `general.file_type` 那个 "MOSTLY_…" 标记；模型名直接用文件名，视觉投影（mmproj）单独列出并可开关。
- **启动 / 切换 / 重启**：一个状态机跑完（`已停止 → 启动中 → 运行中 → 停止中`），
  起来后自动发一个极短请求**预热**，让第一次真实对话不用再等建图。
- **运行与显存**：`-ngl`、`-ot`、`-fa`、`-fit`、`-t`、`-b`、`-ub`、`--cache-ram`、`--keep`、
  `--no-kv-offload`、`--moe-cache-mib`、投机解码、KV 精度——全部可改，旁边就是显存预算条。
- **采样参数**：18 项，滑条 + 步进器；内置四档风格（通用 / 代码 / 写提示词 / 严谨）；
  支持**另存为预设**，把自己调好的一整套参数存下来复用。
- **悬浮窗**：400×244 的置顶小窗，和主窗口共用同一个后端，主窗口挡视线时用它。
- **日志**：`llama-server` 输出尾部实时刷新，错误行高亮。
- **导出**：复制当前实际使用的命令行，或导出 JSON / `.bat` / `.sh`。
- **本地 API**：所有动作都是 `127.0.0.1` 上的 JSON 调用，带一次性随机 token。

| 模型 | 运行 |
|---|---|
| ![模型](docs/shot-models.png) | ![运行](docs/shot-runtime.png) |

| 采样 | 悬浮窗 |
|---|---|
| ![采样](docs/shot-sampling.png) | ![悬浮窗](docs/shot-mini.png) |

## 环境要求

- **Windows 10 / 11（x64）**
- **Microsoft Edge**（Win11 自带）：界面由 Edge 以 `--app` 模式渲染，
  折射效果依赖 Chromium 的 `backdrop-filter: url()`。
- **一份带 `llama-server.exe` 的 llama.cpp**，模型放在它旁边的 `models/` 里。
- Python 3.8+ **仅在从源码运行时需要**（发行版自带解释器）。

> 暂时只支持 Windows：窗口句柄、置顶、显存查询都是 Win32 实现。

## 快速开始（发行版）

1. 从 **Releases** 下载 `LLMSwitchStudio-win64.zip`，解压到任意位置。
2. 双击 `LLMSwitchStudio.exe`。
3. 如果没识别到模型，界面顶部会出现一条提示，把**含 `llama-server.exe` 的文件夹**粘进去，点保存。

窗口没有系统标题栏：拖顶栏移动，右上角是自绘的 `— ▢ ✕`。**只有悬浮窗会置顶**，主窗口就是普通窗口。
如果你的环境不喜欢无边框（比如某些窗口管理器），启动时加 `--framed` 恢复原生标题栏。

程序会在自身目录及最多五级父目录里找 `llama-server.exe`，所以把它放进（或紧挨着）llama.cpp 目录即可免配置。
也可以设置环境变量 `LLAMA_SWITCH_DIR`。

## 从源码运行

```
python app.py                 # 正常窗口
python app.py --mini          # 只开悬浮窗
python app.py --serve         # 只跑后端，不开窗口
python app.py --browser       # 用默认浏览器打开（不用 Edge app 窗口）
python app.py --llama-dir D:\llama.cpp --port 8770 --llama-port 8080
```

`run.bat` / `run-mini.bat` 是找 Python 的包装脚本：每个候选解释器都会被**真正执行一次**并询问版本，
不只依赖 `where py`。

## 打包 exe

```
pip install pyinstaller
python build_exe.py                  # 目录版 → dist/LLMSwitchStudio/
python build_exe.py --onefile        # 单文件 → dist/LLMSwitchStudio.exe
python build_exe.py --out D:\apps    # 自定义输出目录
```

构建会先在临时目录跑 PyInstaller，再把产物拷回来——某些杀软会破坏"直接写进特定目录"的 exe。
如果双击没反应、或报 `Could not load PyInstaller's embedded PKG archive`，换个输出目录，
或者直接用源码跑。

## 实现方式

```
┌──────────────────────────────┐         ┌────────────────────────────────────┐
│  界面（web/，无框架）          │  HTTP   │  Python 后端（core/，纯标准库）      │
│  · 液态玻璃 = backdrop-filter │◄───────►│  api.py      JSON API + 静态资源    │
│    + SVG 位移滤镜             │  1 秒   │  server.py   进程与命令行           │
│  · canvas 曲线 / 表单控件      │  轮询   │  monitor.py  /health /props        │
└──────────────────────────────┘  token  │              /slots /metrics        │
┌──────────────────────────────┐         │  gguf.py     GGUF 头解析            │
│  Edge --app 窗口              │◄────────┤  config.py   schema / 预设          │
└──────────────────────────────┘         └────────────────────────────────────┘
```

两个端口，故意分开：

| 端口 | 谁 | 为什么 |
|---|---|---|
| `8080`（可改） | `llama-server` | OpenAI 兼容客户端连的是这个 |
| `8770`（可改） | 本程序的接口 | 给界面用、接收命令 |

两者**不能相同**：Windows 的 `SO_REUSEADDR` 允许两个进程绑同一端口，`llama-server`
会悄悄抢走控制台端口，界面就永远卡在"启动中"。

## 配置与数据

状态默认放在程序旁边（便携），目录不可写时退回 `%LOCALAPPDATA%\LLMSwitch`：

```
.workbuddy/
  state.json        # 每模型参数、预设、窗口位置
  app.log           # 程序自己的操作日志
  llama-server.log  # llama.cpp 输出
  edge-profile/     # 窗口专用的 Edge 配置目录
```

`state.json` 就是普通 JSON，可以直接备份或搬到别的机器。

## HTTP 接口

只绑定 `127.0.0.1`，所有 `/api/*` 需要 `.workbuddy/runtime.json` 里的 token
（`X-Token` 头或 `?t=`）。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/snapshot` | 一次拿全：状态、模型、配置、实时指标、历史 |
| GET | `/api/logs?n=400` | 日志尾部 |
| GET | `/api/export?format=bat\|json\|sh` | 导出等效命令行 / 配置 |
| POST | `/api/server` | `{"action":"start\|stop\|restart\|swap","model":"<id>"}` |
| POST | `/api/select` | `{"id":"<模型 id>"}` |
| POST | `/api/config` | `{"id":"…","cfg":{"launch":{…},"sampling":{…}}}` |
| POST | `/api/style` | `{"style":"gen\|code\|prompt\|strict"}` |
| POST | `/api/presets` | `{"action":"save\|delete\|get","name":"…","cfg":{…}}` |
| POST | `/api/window` | `{"mini":true,"topmost":true}`：打开悬浮窗 |
| POST | `/api/settings` | `{"llama_dir":"D:\\llama.cpp"}` |

模型 id 是相对 llama.cpp 目录的路径，例如
`models\my-model\Qwen3-35B-A3B-Q4_K_M.gguf`。

## 排障

**卡片显示"启动失败"并带原因。** 概览页给短原因（显存不足 / 参数被拒 / 模型读不到 / 端口占用 /
权限 / CUDA 异常），日志页有原始那行。进程秒退时原因取自 `llama-server.log` 尾部，
因为这种情况 `Popen` 本身是成功的。

**显存不够。** KV 是**启动时一次性预留**的，不是用多少占多少。降上下文、把 KV 改成 `q8_0`，
或关掉别的显存占用者（浏览器和图像工具关了窗口往往还占着）。预算条会在你真的撞墙之前变黄。

**端口被占。** 用 `--port` / `--llama-port` 改，注意两者要保持不同。

**没有窗口。** 某些安全策略会拦 Edge 的 `--app` 模式，程序会自动退回默认浏览器打开（见 `app.log`）。
也可以直接用 `--browser`。

**杀软报毒。** 发行版是未签名的 PyInstaller 打包，个别引擎会按启发式报毒。可以核对 Release 里的哈希、
把目录加入白名单，或者直接用源码跑。程序不联网，唯一的网络活动是 `127.0.0.1`。

**双击 exe 没反应。** 多半是杀软的沙箱或受保护目录。换个目录（比如放到 `llama-server.exe` 旁边），
看看 exe 旁边的 `crash.log`，或者直接用源码运行。

**AMD / Intel 显卡。** 显存数字来自 `nvidia-smi`，取不到时界面显示"未检测到"，
规划器退回公式估算；启动服务本身不受影响。

**界面是中文。** 文案集中在 `core/config.py`（参数名与说明）和 `web/js/app.js`，
欢迎 PR 做多语言。

## 许可

[MIT](LICENSE)。

## 参考

- [llama.cpp](https://github.com/ggml-org/llama.cpp)——服务端、接口与全部参数说明见其 `tools/server/README.md`。
- Apple《Meet Liquid Glass》(WWDC25)——本界面遵循的材质 / 透镜 / 镜面高光模型。
- [1ar labs《How Liquid Glass works》](https://1arlabs.com/updates/how-liquid-glass-works)——位移图与 2D 折射。
- [webtricks.dev《Liquid glass in CSS》](https://webtricks.dev/blog/liquid-glass-css) 与
  [sohumsuthar/liquid-glass](https://github.com/sohumsuthar/liquid-glass)——`feDisplacementMap`
  滤镜图、RGB 位移编码、`color-interpolation-filters="sRGB"`，以及"折射只放在少量主表面上"的性能原则。
- [LogRocket《How to create Liquid Glass effects with CSS and SVG》](https://blog.logrocket.com/how-create-liquid-glass-effects-css-and-svg)——
  镜面高光边缘合成。
