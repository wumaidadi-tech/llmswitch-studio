# LLMSwitch Studio · 设计说明（现状评估 / 方案 / 阶段计划）

> 本文是 `LLMSwitch/v2` 的设计底稿。旧版 `LLMSwitch/LLM开关.py` 与
> `LLM自定义参数.py` **原样保留**，可随时回滚。

---

## 一、现状评估

### 1.1 代码规模与结构

| 文件 | 行数 | 角色 |
|---|---|---|
| `LLM开关.py` | 3353 | 常驻悬浮窗：Win32 自绘 GDI 卡片 + 毛玻璃抓屏 + 自绘右键菜单 + 进程启停 + 状态探测 |
| `LLM自定义参数.py` | 2154 | Tk 参数弹窗：采样 13 项 + ctx/kv/mtp + 风格档 + 运行卡 |

技术栈是**纯 ctypes + Win32 GDI + tkinter**，零第三方依赖（除 PIL 用于抓屏模糊），
这部分做得相当扎实：DPI 感知、多显示器、原子落盘、GDI 句柄生命周期、
日志归因、显存档位自动挑选，全部有注释和实测依据。

### 1.2 真实可用的部分（不能丢）

- **启动参数是踩坑换来的**：`-ngl 999 -ot exps=CPU -fa on`（8GB 卡跑 35B MoE 的前提）、
  **绝不传 `-np`**（显式给值会关掉 `kv-unified` 与 `cache-idle-slots`，交错请求互相冲 KV，
  实测 45K token 全量重算 / prefill 142s）、`-c` 是启动时一次性预留、
  ctx<128K 时关掉思考链保留、`dry_n` 不接受负数。
- **参数档位**：思考预算 512（实测 2048 与 512 答案质量无差、速度差很多）、
  采样 13 项的具体值、风格四档。
- **探测经验**：`/slots` 空闲槽位不返回占用字段（只有 `is_processing=true` 时才有
  `n_prompt_tokens`）、必须绕开系统代理、`--log-file` 是预分配 + NUL 填充所以不能解析日志。
- **状态机**：`stopped/starting/running/stopping` + 秒退归因（Popen 成功但进程秒退时，从日志尾部挑失败原因）。

### 1.3 问题清单（本次要解决的）

| # | 问题 | 证据 / 影响 |
|---|---|---|
| 1 | **视觉表现力封顶** | GDI 只能做「抓屏→高斯模糊→叠雾」，做不出 Apple Liquid Glass 的**边缘折射（lensing）**、镜面高光随指针移动、色散。抓屏线程还要按帧率跑，费 CPU |
| 2 | **信息密度极低** | 悬浮窗只有 3 行文字；想看「现在跑的是哪套参数」「上下文用了多少」必须右键翻菜单或开参数弹窗 |
| 3 | **没有指标面** | 只有 tok/s 与上下文占用两项。缺 `/metrics`（请求数、prefill 吞吐、投机接受率、busy slots）、缺历史曲线、缺显存趋势 |
| 4 | **CUDA/显存配置几乎不可见** | 旧版把这些**硬编码在代码里**（`MODEL_OVERRIDES`、`pick_ctx()` 阈值、`-ot exps=CPU`），用户无法调 `-ngl / -ot / -b / -ub / --cache-ram / --moe-cache-mib / --no-kv-offload` |
| 5 | **模型库信息来自文件名** | 不知道真实层数/专家数/原生上下文/量化类型；MTP 靠文件名猜（`_gguf_has_mtp` 只回答有/没有） |
| 6 | **孤儿配置** | 模型被删后 `llm_switch.json` 里仍留着配置。本机实测：状态文件有 3 条模型配置，`models/` 只剩 2 个（`heretic-i-compact` 已被删），而 `model` 字段还指着 Quality |
| 7 | **无法导出/复现** | 没有「复制等效命令行」「导出 .bat / JSON」，出问题只能靠日志反推 |
| 8 | **窗口位置/尺寸不记忆** | 只存 x/y，不存宽高；`/slots` 占用、`ctx_cap` 全靠猜 |
| 9 | **两个 Python 进程两套状态** | 参数弹窗是独立进程，改完要等悬浮窗轮询；两边默认值必须手工保持同步（注释里明确警告过） |
| 10 | **日志必须开文件** | UI 内没有日志视图，排障要 `notepad llama_ui.log` |

### 1.4 同类工具的能力基线（调研结论）

| 能力 | LM Studio | Ollama | Jan | llama-swap | 旧版 LLMSwitch |
|---|---|---|---|---|---|
| 模型库（含元数据） | ✅ HF 目录 + 兼容性评级 | ✅ 注册表 + tags | ✅ | ✅ 配置驱动 | ❌ 仅文件名 |
| 逐模型预设 | ✅ | Modelfile | ✅ | ✅ | ⚠️ 有配置但无 UI 总览 |
| GPU 卸载粒度 | ✅ 滑块（最强） | 自动 | ⚠️ 较少 | ✅ 透传参数 | ❌ 硬编码 |
| 运行指标 | ✅ tok/s、显存 | ⚠️ 终端 | ⚠️ | ✅ Web UI | ❌ 仅 2 项 |
| 热切换 | ❌ 需重载 | ⚠️ 串行换载 | ✅ 下拉 | ✅ 代理按需换 | ⚠️ 需手动两次点击 |
| 无头/服务化 | ⚠️ | ✅ | ✅ | ✅ | ❌ 必须开 GUI |
| 参数可复现（导出） | ⚠️ | ✅ | ⚠️ | ✅ | ❌ |

**结论**：本项目要补的不是"再做一个 LM Studio"，而是把**这台机器上已经验证过的参数**
变成**可见、可调、可复现、可观测**的一等公民。

---

## 二、方案

### 2.1 总体架构：后端不变，换掉表现层

```
┌──────────────────────────────┐        ┌───────────────────────────────┐
│  Chromium 渲染的 UI（web/）    │  HTTP  │  Python 后端（core/，纯标准库） │
│  · 液态玻璃（backdrop-filter） │◄──────►│  api.py     本地 API + 静态资源 │
│  · canvas 曲线 / 表单          │  1s 轮询│  server.py  进程生命周期+命令行 │
│  · 无框架、无构建              │  token │  monitor.py /health /props     │
└──────────────────────────────┘        │             /slots /metrics    │
                                        │  gguf.py    GGUF 头解析         │
┌──────────────────────────────┐        │  config.py  schema/校验/迁移    │
│ Edge --app 窗口（无地址栏）    │◄───────┤  paths.py                       │
└──────────────────────────────┘        └───────────────────────────────┘
```

**为什么换 HTML/CSS**：折射只能靠 `backdrop-filter: url(#svg-filter)` +
`feDisplacementMap`，目前只有 Chromium 系支持；GDI 自己写卷积既慢又不准。
Edge 在本机就是 Chromium（155），用 `--app` 起窗口零依赖、零安装，
窗口没有地址栏/标签栏，观感与桌面应用一致。

**为什么不引入 webview/Electron**：本机 Python 没装 `pywebview`，装它要联网拉绑定；
Electron 是几十 MB 的额外运行时。`--app` 方案一个 `Popen` 就够，
且 **后端保持纯标准库**（`http.server` + `urllib` + `ctypes`），拷到任何 Python 3.8+ 都能跑。

### 2.2 视觉：Liquid Glass 的四层模型

调研 Apple WWDC25「Meet Liquid Glass」与几份 Web 实现后，把它拆成可在浏览器里复现的四层：

| 层 | 作用 | 实现 |
|---|---|---|
| ① 材质 (material) | 半透明 + 模糊 + 饱和 | `background-color: rgba(9,12,19,.34)` + `backdrop-filter: blur(2px) saturate(185%) brightness(1.04)` |
| ② 折射 (lensing) | 边缘"弯折"背景 | 每个玻璃元素**按自身尺寸**生成位移图（R=X 位移, G=Y, 128=中性），只让 rim 一圈沿法线偏移，`feDisplacementMap` 挂在 `backdrop-filter: url(#f)` |
| ③ 镜面高光 (specular) | 顶面 1px 亮边 + 指针跟随光斑 | `inset 0 1px 0 rgba(255,255,255,.55)` + `radial-gradient(420px at var(--mx) var(--my))` |
| ④ 景深 (depth) | 阴影与自适应着色 | 多层 `box-shadow`；亮度由背景决定，深色底自动提亮文字 |

关键工程细节（都是坑）：

- `color-interpolation-filters="sRGB"` **必须写**，否则 128 中性值漂移，整块背景被平移。
- 位移图必须与元素**同尺寸**（按 24px 归档缓存）。用一张固定图拉伸的话，
  1200px 宽面板的 rim 会变成 100px 厚。
- **玻璃不叠玻璃**：内层卡片只用低强度磨砂（普通 `backdrop-filter`），
  否则祖先成为 Backdrop Root，子元素的 `backdrop-filter` 静默失效。
- 先写 blur 再写 `url()`，Safari/Firefox 丢掉带 `url()` 的整条声明时仍保留磨砂。
- 性能预算：20+ 玻璃元素会拖垮合成器 → 只有侧栏/顶栏/弹窗走折射，
  卡片走磨砂；色散（chromatic aberration）直接不做。

### 2.3 功能：补齐的六块

1. **模型库**：递归扫 `models/`，**读 GGUF 头部**拿 `arch / n_layers / n_expert /
   n_ctx_train / file_type`，自动配对同目录 mmproj 与独立 draft 头。
   本机实测：`qwen35moe / 41 层 / 256 专家 / 262144 原生上下文`，
   Quality 是 `Q6_K`、UD 是 `Q4_K_M`（旧版这两个只能显示成"21.9GB"）。
2. **加载与切换**：启动 / 停止 / 重启 / 换模型统一走一个状态机；
   换模型 = 停 → 等端口释放 → 起（`swap`）。起来后自动发一个 1-token 请求**预热**，
   把首次请求要建计算图的那一下提前吃掉。
3. **CUDA 与显存**：把原来硬编码的项全部暴露成表单
   （`-ngl / -ot / -fa / -fit / -fitt / -t / -b / -ub / --cache-ram / --keep /
   --no-kv-offload / --moe-cache-mib / --spec-type / --spec-draft-n-max`），
   外加**显存预算条**：按 `2680 + ctx/1024×30(f16)|15(q8_0)` 估算，
   和当前可用显存比较，超了直接给"建议降到 32K"的具体动作。
4. **运行监控**：`/health`（状态机）、`/props`（build_info、n_ctx、槽位数）、
   `/slots`（上下文占用，拿不到时保留上次值 —— 空闲槽位不返回字段）、
   `/metrics`（请求数、prefill 吞吐、投机草稿/接受数 → 接受率）、
   `nvidia-smi`（显存/利用率），拼成 1 秒一次的快照 + 4 分钟历史曲线。
5. **参数管理**：schema 驱动（`PARAM_SPECS`/`LAUNCH_SPECS`），前端按表渲染，
   新增参数只改一处；每模型独立配置；风格四档；导出 JSON / .bat / .sh；
   非法值在写入前收敛（`dry_n` 负数、`np` 非 auto 都要拦）。
6. **共存与迁移**：首启动只读旧 `llm_switch.json` 导入（保留手工调过的 `ctx/kv`），
   随后写自己的 `v2/LLMSwitchData/state.json`；磁盘上消失的模型配置自动清理。

### 2.4 安全与稳定性

- 只绑 `127.0.0.1`；所有 `/api/*` 要 token（`X-Token` 头或 `Cookie`），
  首页由服务端注入，防止浏览器里任意网页 POST 到本机 API。
- 单实例：端口被占 → 不再起第二个，只把窗口叫出来。
- 关窗口 = 退出（检测窗口消失 6 秒后回收）；`--serve` 则常驻无窗口。

---

## 三、分阶段实施计划

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0 骨架** | 路径/状态/GGUF 解析/进程管理/API/静态服务打通；GGUF 元数据在真机上验证 | ✅ 完成 |
| **P1 视觉** | 液态玻璃四层 + 极光背景 + 四页布局（概览/模型/运行/采样/日志）+ 无头截图迭代 | ✅ 完成 |
| **P2 功能补齐** | 模型库卡片、显存预算、指标曲线、日志流、导出、风格档、快捷键 | ✅ 完成 |
| **P3 端到端验证** | 真实启停（32K/q8_0）：13.2s 到 `running`、`/slots` 回报 `n_ctx=32768`、4 槽、warmup 226ms、停止后 `stopped` | ✅ 完成 |
| **P4 真机窗口** | Edge `--app` 窗口 + 置顶 + 关窗退出；当前构建环境的桌面看不到该窗口（受执行环境限制，已确认窗口句柄/标题正常），已加"找不到窗口就退回默认浏览器"兜底 | ⚠️ 需你在本机双击 `run.bat` 确认一次 |
| **P5 待办** | ① 用真实长请求校验 tok/s 与接受率显示；② 加"开机自启"开关；③ 模型库支持拖入 .gguf 自动登记；④ 把 `--log-file` 换成 stdout 管道以便实时日志更细 | ⏳ 建议下一步 |

---

## 四、关键取舍

1. **不重写启动参数逻辑**，而是搬过来并注释保留原因 —— 这些值是实测出来的资产。
2. **不引入前端框架**：一个页面 5 个视图，原生 DOM 足够；换来零构建、可直接改。
3. **端口拆开**：UI 的 API 端口（8770）与 llama-server 端口（8080）必须分开。
   实测教训：Windows 的 `SO_REUSEADDR` 允许两个进程绑同一端口，
   一旦混用，llama-server 会悄悄抢走 API 端口，`/health` 永远拿不到、界面卡在"启动中"。
4. **窗口用 `Chrome_WidgetWin_*` + 面积筛选**，不用 `FindWindow(title)`：
   Windows 会为应用窗口生成同名的 `Shell.TabProxyWindow`（约 135×37），
   按标题查找会命中这个假窗口，导致置顶与关闭检测全部失效。
5. **KV「自动」= 128K 及以上 q8_0、以下 f16**。旧版代码写的是 f16 上限，
   但 `MODEL_OVERRIDES` 给每个现役模型都配了 `kv_128k: q8_0` ——
   实际跑的本来就是 q8_0。8GB 卡上 128K/f16 要 ~6.1GB，桌面基线 1.1GB，
   开个浏览器就 OOM，所以默认值直接对齐"实际行为"而不是"纸面值"。
6. **不信任 `where py`，每个候选都真跑一遍**。本机 `py -0p` 指向
   `D:\kaifa\python.exe`（已不存在），`py -3` 以 exit 101 失败；
   只看"命令存在"就把 `PY` 定下来的写法，会把这种情况误报成"版本太旧"。
   `run.bat` 现在逐个执行 `-c "import sys;..."` 比对版本，并带 `--check` 自检。

---

## 五、参考来源

**液态玻璃 / 视觉**
- Apple, *Meet Liquid Glass* (WWDC25, session 219) — https://developer.apple.com/videos/play/wwdc2025/219/
- Apple, *Platforms State of the Union* (WWDC25, session 102) — 层次/协调/一致三原则
- 1ar labs, *How Liquid Glass works* — 位移图、2D 折射、镜面高光与陀螺仪 — https://1arlabs.com/updates/how-liquid-glass-works
- webtricks.dev, *Liquid glass in CSS — frost, rim, sheen and real refraction* — https://webtricks.dev/blog/liquid-glass-css
- sohumsuthar/liquid-glass（GitHub）— 滤镜图、RGB 位移编码、scale 换算、性能门控 — https://github.com/sohumsuthar/liquid-glass/
- deepika-builds/liquid-glass（GitHub）— 单文件零依赖实现、`color-interpolation-filters` 陷阱
- LogRocket, *How to create Liquid Glass effects with CSS and SVG* — https://blog.logrocket.com/how-create-liquid-glass-effects-css-and-svg
- CodeFronts, *SVG Displacement Liquid Glass* — `url()` 在 `backdrop-filter` 中的浏览器支持与降级

**llama.cpp 服务端契约**
- ggml-org/llama.cpp, `tools/server/README.md` — `/health` `/props` `/slots` `/metrics` `/v1/models` 字段与全部启动参数默认值
- 本机 `llama-server.exe --help`（b10775）逐项核对：`-ngl/-ot/-fa/-fit/-fitt/-ctk/-ctv/-b/-ub/-t/-np/-cram/-kvo/-cmoe/-ncmoe/-sm/-mg/-ts/-dev/-rea/-md/-spec-*`
- 本机实测日志：`n_slots = 4, n_ctx_slot = 32768, kv_unified = true`（未传 `-np` 时）

**同类工具能力参考**
- ModelsLab, *llama-swap vs Ollama vs LM Studio* — 热切换、无头化的差异 — https://modelslab.com/blog/api/llama-swap-vs-ollama-vs-lm-studio-which-local-llm-tool
- *Local AI Stack Face-Off: Ollama vs LM Studio vs Jan* — GPU 卸载滑块、逐模型预设
- *Ollama vs LM Studio for Local LLMs* — 模型管理/注册表 vs 文件系统优先

**本机既有实测结论**（项目记忆与日志，非网上来源）
- 思考预算 512 vs 1024/2048/放开 的对照实验
- `-np` 显式传值导致 KV 互冲、长请求全量重算
- `/slots` 空闲槽位无占用字段
- KV 显存拟合式 `2680 + ctx/1024×30(f16)/15(q8_0)`

---

## 六、v2.1 增补（面向所有人 + 可执行文件）

### 6.1 从"某台机器"到"任何人"

第一版把几处实测结论直接写进了界面文案（"8GB 卡跑 35B 的关键"之类），这对其他用户无意义。
本轮把所有用户可见文案改成**只描述参数本身的作用**，判定依据来自 llama.cpp 的语义而不是某次实验：

| 位置 | 改法 |
|---|---|
| `config.PARAM_SPECS / LAUNCH_SPECS` 的 `hint` | 只说明"这一项影响什么、调大调小会怎样" |
| 概览/运行页副标题 | "决定模型怎么放进显存"这类中性描述 |
| 显存估算 | 保留公式（2680 + ctx/1024×30 或 15），但标注为"估算，用于启动前预警" |
| README | 面向陌生用户：环境要求、发行版、源码、打包、API、排障 |

同时删掉了 `paths.py` 里写死的 `D://Edge//...` 兜底，改成 `resolve_llama_dir()`：
命令行 `--llama-dir` → 环境变量 `LLAMA_SWITCH_DIR` → 上次使用 → 自身目录与最多 5 级父目录 → 目录扫描。
找不到时不再假设，界面顶部出现一条"指定 llama-server.exe 所在文件夹"的输入条。

### 6.2 入口页 + 悬浮窗

- 主窗口就是入口页（状态、模型、运行、采样、日志五个页签）。
- 主窗口/侧栏/品牌菜单里的"打开悬浮窗"调用 `POST /api/window {mini:true, topmost:true}`，
  后端用同一个 token 再起一个 400×220 的 Edge app 窗口，加载 `?mini=1`。
- 两个窗口共用一个后端与一份状态，不存在同步问题：谁改都一样。
- 悬浮窗 `document.title = "LLMSwitch Mini"`，后端靠"窗口类 + 面积 + 标题"把它挑出来置顶。

### 6.3 控件重做（针对具体反馈）

| 反馈 | 处理 |
|---|---|
| 下拉弹层和周围完全不和 | 弃用原生 `<select>`（弹层由系统绘制，无法与玻璃统一），改为自绘 `.sel`：按钮 + `position: fixed` 浮层，自动上下翻转、点外部/滚动即关 |
| 悬浮时出现的加/减按钮很丑 | 隐藏 `input[type=number]` 的原生上下箭头，改为自绘 `− / +` 步进器（`.step`），与滑条同高、同边框色 |
| 滑条不好 | 重画轨道：5px 渐变填充（`--p` 百分比）+ 15px 球形滑块 + focus/hover 光晕 |
| 参数页太稀 | 采样页 3 列网格（`minmax(300px,1fr)`），带滑条的项在窄卡片里才占整行 |
| 左右两块一长一短 | 运行页改为：**整行显存规划** + 两张等高参数卡 + 整行进阶卡；`align-items: start` 避免拉伸出空洞 |
| 有的能输入有的是下拉 | 统一规则：数字=滑条+步进器、枚举=自绘下拉、布尔=开关、只有 `-ot`/`alias` 这类自由文本用输入框 |
| 左上角图标点不动 | 改成 `<button>`，点开一个玻璃 popover（概览 / 悬浮窗 / 日志 / 数据目录 / 关于） |
| 不能保存为新预设吗 | 新增用户预设：`state.presets` + `POST /api/presets`，可命名、套用整套（采样+启动）、删除；内置四档风格仍在 |

### 6.4 打包 exe

- `build_exe.py` 调 PyInstaller，默认 **onedir**（启动快、无需解包），`--onefile` 可选。
- 冻结后在 `paths.py` 里用 `sys.frozen` 切换：`WEB_DIR = sys._MEIPASS/web`，
  数据目录跟随 exe（便携），不可写才退回 `%LOCALAPPDATA%\LLMSwitch`。
- `--noconsole` 下 `sys.stdout/stderr` 为 `None`：启动时替换为 `devnull` 并安装
  `sys.excepthook` + `faulthandler`，异常写 `crash.log`（否则用户只会看到"双击没反应"）。
- **踩到的坑 1**：`--exclude-module email` 会让 exe 启动即静默退出——`http.server` 依赖 `email.utils`。
- **踩到的坑 2**：构建脚本改成"先在临时目录构建，再拷回目标目录并校验 sha256"。
  某些安全软件会破坏直接写入特定目录的 exe（症状：`Could not load PyInstaller's embedded PKG archive`）。
  实测同一份字节，放在 `...\LLMSwitch\` 下启动失败、放在上层目录正常，说明是目录级拦截而非文件损坏。

### 6.5 已知限制

- 仅 Windows；依赖 Edge（Chromium）渲染，Safari/Firefox 会退化成普通磨砂。
- 显存估算用固定系数（每 1K token：f16≈30MiB、q8_0≈15MiB）加上固定开销，
  不同模型层数会有偏差，只作预警。
- 界面文案仍是中文（README 双语），i18n 未做。

### 6.6 v2.2 修复（来自实测反馈）

| 反馈 | 根因 | 处理 |
|---|---|---|
| 下拉框收不回去 | 弹层 append 到 `body`，`closeSel()` 用 `openSel.querySelector` 找不到它 | 记住 `sel._pop` 引用再移除；同时修掉 `onDocDown` 里先置空再取值的空引用 |
| 选项点了没反应 | 弹层在 `#content` 之外，委托的 click 收不到 | 选项点击改挂到 `document` |
| 上下文长度不能键盘输入 | 枚举只有按钮，没有输入框 | 改成组合框：输入框 + 箭头，打字「64」回车即 64K，↑↓ 选择、Esc 还原 |
| 加减按钮没反应 | `stepBtn.closest('[data-ns]')` 返回 null（`data-ns` 在 input 上） | 从 `.step` 里取 input，再读 ns/k |
| 参数说明不明确 | hint 写的是结论不是用法 | 全部重写：这一项影响什么、调大调小怎样、常用值 |
| 保存预设弹窗太空 | 同一个弹窗模板，宽 820px 但只有一个输入框 | `askName` 加 `.compact`（440px），标题/输入/按钮紧凑排列 |
| 主窗口也置顶了 | 窗口标题被 JS 改成「LLMSwitch Mini · 运行中」，精确匹配失败，加上主/副窗口判断混乱 | 标题匹配改**前缀匹配**；主窗口显式 `set_topmost(False)`，只有悬浮窗置顶 |
| 点「展开」又开一个窗口 | 展开实现是 `openFloat()` | 改 `action:"expand"`：原地 SetWindowPos 放大 + 页面 `location.replace` 到主界面；若已有主窗口则关闭悬浮窗 |
| 悬浮窗一打开就很大 | Edge 按 profile 恢复上次窗口尺寸，忽略 `--window-size` | 窗口出现后用 `SetWindowPos` 强制尺寸；顶栏黑框用 `SetWindowLong` 去掉 `WS_CAPTION`（`--framed` 可回退） |
| 量化显示 Q6_K 不对 | 只读了 `general.file_type`（"MOSTLY_Q6_K"） | 改为按张量 offset 差值累计**每种类型的字节数**：Quality 实际是 Q6_K 39.1% + IQ4_XS 36.4% + Q5_K 23.5%，无单一类型 ≥60% 时显示「混合」 |
| 模型名是猜的 | 早期用规则拼短名 | 直接用文件名，超长时做**中间省略**（保留结尾的 Quality / Q4_K_M） |
| 看不到视觉模型 | mmproj 只在标签里出现一个 Vision | 模型卡新增「视觉」行：文件名 + 体积 + 开关（写回 `launch.mmproj`，重启生效） |

另外：临时 UI 测试脚本（CDP 抓页 + 起浏览器）被安全软件判定为可疑文件，已删除；
这类脚本只在开发时用完即弃，不进仓库。

---

## 七、v2.3：WebView2 宿主、单文件发布、紧凑悬浮窗

本节取代 6.2 的 400×220 悬浮窗和 6.4 的默认 onedir 打包。

### 7.1 黑条：Edge `--app` 去不掉的标题栏

| 现象 | 根因 | 处理 |
|---|---|---|
| 主窗口和悬浮窗顶部都有一条约 29px 的黑条，右上角按钮与黑条错位 | `--app` 仍是浏览器窗口，Chromium 自绘标题栏；`SetWindowLong` 去掉 `WS_CAPTION` 只能去掉外框，去不掉页面内的这条 | 改用 **pywebview + WebView2**：窗口就是客户区，真正无边框；WebView2 仍是 Chromium，`backdrop-filter: url()` 的折射照常工作 |
| 某些机器没有 WebView2 / pywebview | — | 保留 Edge `--app` 兜底；README 说明兜底会多一条浏览器栏，`--browser` 可强制用默认浏览器 |

`app.py` 按 pywebview → Edge `--app` → 默认浏览器 的顺序自动选择，日志里会写明实际用的哪一种。

### 7.2 尺寸：四个坑

1. **pywebview 建窗会吃掉一圈不可见边框**：150% 缩放下，请求 320×112 实际只有 306×75 CSS。
   → 窗口出现后用 `GetDpiForWindow` + `SetWindowPos` 按物理像素补正（`_fix_webview_size`）。
   必须在 `webview.start()` 之后做，之前原生窗口还没创建，只会白等 4 秒。
2. **`hidden=True` 的窗口永远不触发 pywebview 的 `shown` 事件**，之后 `show()` 会等 20 秒然后抛错。
   → 悬浮窗改成按需创建，不再预建隐藏窗口。
3. **主窗口的保存尺寸被传给了悬浮窗**：`_win()` 对两种窗口用同一套 `w or saved_w`，悬浮窗一开就是整屏。
   → mini 分支只用 `MINI_W/MINI_H`，不套主窗口的尺寸。
4. **两处"最小尺寸"会把悬浮窗撑高**：
   - `clamp_window()` 原来写死 `max(180, h)` → 112 变 180（底下一条空白）；
   - pywebview 的 `min_size=(260,150)` 是**逻辑像素**，150% 下强制 150 CSS / 225 物理。
   → `clamp_window` 增加 `min_w/min_h` 参数，悬浮窗用 240×96；`min_size=(240,96)`。
5. **同名窗口跨进程串台**：`find_window` 只按标题前缀 + 面积找窗口。调试时同时开着
   源码版和打包版，两个悬浮窗都叫 `LLMSwitch Mini · 已停止`，尺寸修正选中了**面积更大的
   另一个进程**，源码版怎么修都是 306×96。
   → `find_window(title, pid)` 增加进程过滤，pywebview 窗口一律传 `os.getpid()`；
   Edge `--app` 的窗口属于 msedge.exe，那条路径仍用全局匹配。
6. **Show 的那一刻外框会被重置**：Form 先按带边框的尺寸建出来，再把
   `FormBorderStyle` 改成 `None`；WinForms 保持 client 不变、把外框缩回 client，
   于是建窗时设好的 480×168 在窗口真正显示后变成 458×144。
   → 尺寸修正改成"多次校准 + 0.3s 稳定判定"，并在 pywebview 的 `shown` 事件之后
   再补一次，外加 1 秒定时兜底（防止事件注册晚于 Show）。

### 7.3 单文件 exe

- `build_exe.py` 默认 `--onefile`（单文件，约 12 MB），`--onedir` 仍可选。
- PyInstaller 额外收集 `webview` / `pythonnet` / `clr_loader`：WebView2 互操作 DLL 在
  `webview/lib` 下，必须作为**数据文件**保留相对路径；`Python.Runtime.dll` 按二进制收集。
- 仍然先在临时目录构建、再拷回目标目录并校验 sha256（某些杀软会做目录级拦截）。
- `run.bat` 两轮扫描解释器，优先选装了 pywebview 的那个（`:probe_wv`，跳过 WindowsApps 别名）。

### 7.4 悬浮窗：320×268 → 320×112

反馈是"太大，而且启动和展开不是一直用"。旧版悬浮窗只有 300×106，三行、没有整行按钮。
这一轮按同样的密度重做：

```
● 运行中  33.4 tok/s              3.0/8.0G   ⤢  ×
┌ Qwen3-35B-A3…erved-APEX-Quality            ⌄ ┐
│ 上下文 24 / 128K  q8_0 MTP 视觉        剩 104K │
│ ▬▬▬▬▬──────────────────────────────────────── │
```

| 位置 | 交互 |
|---|---|
| 左侧 22px 圆钮 | 启动 / 停止（运行中变绿并显示停止方块；启动中脉冲） |
| 模型行 | 点击就地弹出模型列表；选中即 `select`，运行中自动 `swap` |
| 上下文行右侧 | 3px 进度条，>85% 变黄红 |
| 右上角 `⤢` / `×` | 展开主界面 / 关闭；整窗可拖 |

实现要点：

- 悬浮窗改用独立的 `#hud` 三行结构，不再复用主界面的卡片再隐藏（小窗口里三层玻璃只会又空又重）。
- 标签（`q8_0 / MTP / 视觉`）从模型行移到上下文行，模型名可用宽度从约 195px 增到约 270px；
  `midName` 同时保留头尾，`…APEX-I-Quality` 这种区分度最高的尾巴不会被截掉。
- 模型列表 `max-height` 覆盖模型行以下，滚轮翻页，点外部或 Esc 关闭。
  上一版绑定的 `openMiniModels()` 实际上没有定义（点击会抛 ReferenceError），这一轮补上了。
- 后端尺寸：320×112 逻辑；本机 150% 缩放下窗口为 480×168 物理，贴右下角、置顶。
- 高度是量出来的，不是估的：三行内容底边在 y=98（`.hud` 内边距 8/9、行高 24/28/26、间距 6），
  原来的 132 会在底部留 25px 空白；收到 112 后只剩约 5px 呼吸位，视觉上贴近旧版 300×106。

### 7.5 验证数据（本机 2560×1600 @150%）

- 主窗口：CSS 1280×860，物理 1920×1290，无黑条。
- 悬浮窗：CSS 320×112，物理 480×168，`topmost=True`，位置 (2043,1365)。
- 模型列表 2 项；点选后弹层关闭、无 JS 异常；启停/展开按钮在演示模式下均正确提示。
- 单文件 exe 12.0 MB；从项目外的普通目录启动后 `/healthz` 200，打包版悬浮窗同样是
  CSS 320×112 / 物理 480×168，无裁切。
- 实测坑：产物留在仓库内 `dist/` 时会被安全软件静默拦截（进程退出码 -1、连
  `LLMSwitchData/app.log` 都来不及写）；同一份文件复制到普通目录就能正常运行。这是
  目录级拦截，不是包损坏，发布给用户从 GitHub 下载不受影响。

### 7.6 v2.1.1：数据目录与应用图标

- **数据目录改名**：`.workbuddy/` → `LLMSwitchData/`，不再带工具品牌名。
  `paths.ensure_dirs()` 在 `config.load()` 之前检查新目录有没有 `state.json`；
  没有就从旧的 `.workbuddy/` 复制一份，旧目录不删。日志、Edge 配置也随目录一起换。
- **应用图标**：`web/icon.svg` 是主稿（玻璃开关 + 薄荷色滑块，和界面同一套极光色）。
  Edge 无头渲染成 1024px 透明 PNG，再用 Pillow 生成 16/24/32/48/64/128/256 的
  `assets/app.ico`；`build_exe.py --icon` 嵌进 exe，窗口和任务栏直接读它。
  网页端用同一个 SVG 做 favicon，侧栏 brand 用内联图形保持 16px 下的清晰度。

### 7.7 v2.1.2：采样去重、菜单、端口热切换与更新检查

- **采样页去重**：原来的「采样风格」下拉和上方预设 chips 说的是同一件事，而且下拉只改
  `sty` 字段、不会套用那一组参数。给 spec 加 `hidden=True`，前端渲染时过滤掉；后台仍保留
  `sty`（`server.py` 用它给代码风格调整 DRY 允许长度）。界面只留「采样器顺序」，并在提示里
  写明它只决定执行顺序，不改变任何采样值。
- **侧栏菜单**：去掉概览 / 打开悬浮窗 / 查看日志（都有更直接的入口），改为数据目录、
  服务与端口、检查更新、关于。菜单与弹窗改接近实色的深底（不再叠玻璃折射），小字号和标题
  不会再被背景糊掉；全局 `[hidden]{display:none!important}` 修掉 `.btn` 的 `display` 覆盖
  `hidden` 属性导致按钮该藏没藏的问题。
- **端口热切换**：`console_port` 存进 `state.json`，启动时如果没有显式 `--port` 就用上次的；
  `POST /api/console-port` 先在空闲端口建第二个 HTTP server，更新 `runtime.json`、状态和
  `mgr.port`，主窗口由页面自己 `location` 跳转、悬浮窗由后端 `load_url`，2 秒后关闭旧 server。
  非法端口、被占用端口在服务端拒绝；端口被别的程序占用时启动会顺延找空闲端口。
- **打开网页 / 检查更新**：`/api/open-web` 用默认浏览器打开带 token 的本机页面；
  `/api/update` 拉 GitHub `releases/latest`（10 分钟缓存）并比较版本；`/api/open-url` 只允许
  打开本项目仓库地址，避免变成任意 URL 启动器。
- **实测**：8781 → 8791 → 8781，主窗口和悬浮窗都跟着跳，旧端口关闭，`state`/`runtime`
  持久化，重启沿用；非法端口 80/99999 与陌生 URL 均被 400 拒绝；全程无 JS 异常。
