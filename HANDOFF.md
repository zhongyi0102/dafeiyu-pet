# 大肥鱼桌宠 — 会话交接文档

> 生成时间：2026-10-26 14:50（Asia/Shanghai）
> 用途：上下文过长需换新会话，供后续 Agent 接手。
> **接手第一件事：读「三、未完成的待办」，不要重做已完成的部分。**

---

## 一、项目基本信息

| 项 | 值 |
|---|---|
| 项目路径 | `D:\Software\Portable\应用程序\dafeiyu-pet` |
| 技术栈 | Python 3.13 + PySide6 6.12（`.venv`） |
| GitHub | https://github.com/zhongyi0102/dafeiyu-pet （public，main） |
| 入口 | `桌宠.py`（5060 行，`PetApp`） |
| 打包 | PyInstaller，`桌宠.spec` |
| 运行方式 | 用户跑**源码版**（快捷方式指向 `.venv\Scripts\pythonw.exe 桌宠.py`） |

### 当前 git 状态

```
本地 HEAD:     ae1e968   （领先 origin/main 26 个提交，全部本地未推送）
origin/main:   6fae258   （= v1.1.4 已发布）
工作区:         仅剩 HANDOFF.md 未跟踪（本文档）
已有 tag:       v1.1.0 / v1.1.1 / v1.1.2 / v1.1.3 / v1.1.4
备份分支:       origin/backup/auto-memory → cde1d22
```

### dist 里的 exe（**已重打，对应最新代码**）

```
大肥鱼桌宠.exe   457.43 MB  2026/10/26 22:29:06
SHA256: A1EEECF49DB7A8D501503ECD25CB29E7C7760EA2230E90DD9A514133F49BC5F1
```

对应 `ae1e968`，含全部 26 个提交的改动。35 秒烟测通过，无 ERROR/Traceback。

---

## 二、已完成的工作（26 个提交，全部本地未推送）

### A. 自动记忆 + 遗忘曲线（6 提交，`62ef0fa` → `cde1d22`）

| 提交 | 内容 |
|---|---|
| `62ef0fa` | `MemoryStore` 扩展：`source`/`strength`/`last_access`/`mention_count`/`origin_session`/`origin_time`；老数据兼容；衰减计算 |
| `c6e7b2a` | `block()` 按保留率注入 + 同日缓存（同一天内输出恒定） |
| `ecc3381` | `petcore/privacy.py`：14 条规则（S1-S10 敏感信息、L1 超长、Q1/Q2 指令句式、T1 相对时间） |
| `1eddee6` | `petcore/memory_extract.py`：提取器，`ADD`/`UPDATE`/`SKIP`，`UPDATE` 只能指 `source=auto` |
| `181dbe0` | `MemoryDialog` 显示 `[手动]`/`[自动]`/`(淡化中)` + `FEATURES.auto_memory` 开关（默认关） |
| `cde1d22` | 接入 `ChatWindow`：8 条消息或空闲 10 分钟触发，`mem_extracted_upto` 索引，后台线程 |

**关键参数**：半衰期 `7 × (1 + 0.5 × mention_count)` 天，上限 28 天；阈值 `R ≥ 0.15`；最多注入 15 条；自动记忆上限 200 条。

**用户已手动测试通过** ✅

### B. 视觉工具箱（4 提交，`ae6e4bd` → `3eab19b`）

对应 `dsh-vision-toolkit` 的 6 个缺失能力，**Python 本地实现，不依赖 Node**。

| 提交 | 内容 |
|---|---|
| `ae6e4bd` | `petcore/vision_tools.py`：`dominant_colors`（主色）、`pixel_diff`（像素对比+热力图） |
| `21369d8` | 同文件：`long_screenshot_ocr`（长图分块 OCR）、`extract_foreground`（前景抠图） |
| `7f74b9e` | `petcore/vision_locate.py`：`ground_element`（元素定位）、`detect_elements`（元素清单） |
| `3eab19b` | `petcore/vision_intent.py`（关键词意图识别）+ 桌宠 `run_vision_tool` + 聊天接入 |

**触发方式**：聊天自然语言，无新界面。
```
「帮我找提交按钮在哪」→ 元素定位（视觉模型）
「这个页面有哪些按钮」→ 元素清单（视觉模型）
「这个页面用了什么颜色」→ 主色提取（纯本地 0 token）
「这两张图哪里不一样」→ 像素对比（纯本地）
「帮我读这个长截图」→ 长截图 OCR（纯本地）
「帮我抠图」→ 前景抠图（纯本地）
```

### C. 主动关心（6 阶段，`8c9fa52` → `edf76e0`）

| 阶段 | 提交 | 内容 |
|---|---|---|
| ① 触发框架 | `8c9fa52` + `795db9d` | `petcore/care.py` 引擎；旧三类提醒改由引擎调度 |
| ② 健康关心 | `7caacac` | 催睡（23:30-03:00，优先级 90）+ 饭点（双窗口，优先级 70） |
| ③ 反馈降频 | `4134229` | 三按钮（知道了/稍后再提醒/别提这个）+ 降频 + 「别吵我」 |
| ⑤ 主动搭话 | `7296ee1` | 复用 `proactive.py`，计数以引擎为准 |
| ④ 记忆回访 | `d738544` + `681142b` | `event_date` 字段 + 模板回访 |
| ⑥ 好感度 | `edf76e0` | 三档台词（cold/normal/clingy），只影响台词不影响功能 |

**核心机制**：
- `care_state.json`（原子写、损坏自愈）
- **只有气泡真显示才计数**（`mark_fired`）；被拦时 `mark_deferred` 60 秒重试
- 启动保护 5 分钟；全局互斥 8 秒
- 静默规则：锁屏/空闲/全屏/勿扰/翻唱/语音/输入中
- **待办/日程不受每日上限、降频影响**，忙时只延后不丢弃

### D. 风格统一（4 提交，`32b060c` → `3f3df63`）

目标：**设置页「聊天 → 人设」是风格的唯一入口**。

| 提交 | 内容 |
|---|---|
| `32b060c` | 提示词层解耦：`DEFAULT_PERSONA=""`、`EXAMPLE_PERSONA`、`PERSONA_FUNC_RULES`、`persona_from_cfg` 不回落；`proactive`/`screen_vision`/`chat` 全部去「傲娇」「主人」 |
| `ce0e106` | 中性池（23 个）+ `petcore/persona_lines.py`（按人设生成 32 池缓存） |
| `ee6b39f` | 补全 `LINES`（待机）+ `CHAT_EMPTY_GREETINGS`（空聊天）→ 25 池 102 句 |
| `3f3df63` | 实时生成开关 `care_live_generate`（默认关，30 次/天，3 秒超时回落） |

**关键设计**：
- 空人设 → system 只有功能规则 + 长度句，**零风格词**
- 中性池 25 个共 102 句，称呼「你」，无括号动作/颜文字/网络梗
- `REACT_LINES` 只在人设 == `EXAMPLE_PERSONA` 时启用
- 生成缓存 `persona_lines.json`，**人设哈希为 key，失败绝不清旧缓存**
- 生成 35 个池（26 普通 + 9 好感度三档×三类）
- 待办/日程核心信息**由程序拼接，不经过模型**

### E. 台词池系统强化（`b13442f`）

| 项 | 内容 |
|---|---|
| 失败结构 | `failed_pools` = `[{pool, reason_code, detail, fail_count}]` |
| 7 种原因 | `not_returned` / `batch_parse_failed` / `too_few` / `placeholder_broken` / `too_long` / `timeout` / `auth_rejected` |
| 降级重试 | 整批解析失败 → **逐池单独请求**，一个坏响应不再连坐整批 |
| fail_count | 连续失败累计，成功或手写保存后清零；≥2 追加「建议手动编辑」 |
| 日志脱敏 | `_mask_secrets` 屏蔽 sk- / 智谱 / Bearer 三种 Key |
| 新增池 | `PROACTIVE_FALLBACK_LINES`（搭话兜底）、`WELCOME_FALLBACK_LINES`（欢迎兜底）、`TOOL_RESULT_OPENERS`（工具开场） |
| 提示词 | 搭话/欢迎默认提示词只保留场景规则；旧默认自动升级，自定义不覆盖 |

### F. 聊天窗口（`99a19ad`）

| 项 | 内容 |
|---|---|
| 主动搭话 | 占位用户气泡不渲染，宠物气泡加小灰标签「主动搭话」 |
| 主色卡片 | `ColorCard` / `ColorSwatchRow`，点击复制色号，上方开场白 0 token |
| 用量行 | 无数据时整行隐藏 |
| 时间分隔 | 今天 `HH:MM` / 昨天 `昨天 HH:MM` / 更早 `M月D日 HH:MM` |
| 标题 | 第一条真实用户消息前 12 字；纯图片「图片对话」；全主动「主动搭话」 |
| 附件 | `petcore/chat_attach.py`：回形针 / Ctrl+V / 拖拽；最多 4 图、文本 8000 字 |
| 图片流程 | 先判本地工具（命中不调模型）→ 否则视觉描述 → 聊天模型 |
| 存储 | `chats.json` 只存缩略图路径 + 描述，**不存 base64** |
| 清理 | 删除对话时删 `chat_images/<sid>/` |
| 删除按钮 | 输入区常驻「删除对话」，空对话时禁用 |

### G. 设置页重构（`e8076c6`）

| 项 | 内容 |
|---|---|
| 页面合并 | 视觉+记忆并入聊天页四折叠组；余额并入聊天模型组底部后删页 |
| 宠物页 | 重组为四组：宠物管理 / 选中的宠物 / 物理 / 窗口 |
| 通用页 | 只剩：设置生日 / 设置城市 / 开机自启 / 检查更新 |
| 删除 | 「关于」页、「打开程序目录」按钮、`_open_app_dir` 函数 |
| 生日编辑 | 月/日 84px、保存取消与数字框等高、上下留白 8px |
| dev_mode | `consts.is_dev_mode()`：源码 True；打包版看 `dev_mode` 或 `--dev` |
| 提醒测试按钮 | 三行「测试」改用 `is_dev_mode()` 控制显隐 |
| 余额简化 | 删除自动查询 / 间隔 / 满额基准 / 六档 / 档位动作 |

---

## 三、未完成的待办（接手后从这里开始）

### 待办 1：等用户手动测试 ⏳

exe 已重打（`ae1e968`，2026-10-26 22:29:06），等用户手测确认。

**重打命令**：
```powershell
$dir = 'D:\Software\Portable\应用程序\dafeiyu-pet'
Set-Location $dir
Get-Process pythonw,python,'大肥鱼桌宠' -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Remove-Item (Join-Path $dir 'build') -Recurse -Force -ErrorAction SilentlyContinue
& (Join-Path $dir '.venv\Scripts\python.exe') -m PyInstaller 桌宠.spec --noconfirm --clean
# 报告体积与 SHA256 给用户
```

**⚠️ 打包注意**：先杀掉所有 `pythonw`/`大肥鱼桌宠` 进程，否则报 `PermissionError: [WinError 5]`。

### 待办 2：等用户手测后发布 v1.1.5 ⏳

发布流程（**必须等用户明确说「可以发布」**）：
1. push main
2. 新建 tag `v1.1.5`（**不要动已有 tag**）
3. 创建 Release，上传 exe + `dafeiyu-sprites.zip`（可复用 v1.1.4 的）
4. 核验本地/远程大小与 SHA256 一致

**Release 说明应包含**：
- 自动记忆 + 遗忘曲线
- 视觉工具箱（6 个能力）
- 主动关心（6 阶段）
- 风格统一（人设为空 = 无风格）

### 待办 3：可选后续（用户未确认）

- 台词池进一步中性化（当前已有中性池，但 `REACT_LINES` 网络梗保留给示例人设）
- 好感度台词的人工调优
- 视觉工具（找不同 / 长截图 OCR / 抠图）结果也改成卡片形式
- `chat_image_privacy_ok` 目前是独立配置字段，如需收敛可并入其他结构

---

## 四、关键约束（用户反复强调，必须遵守）

### 输出与操作规范

1. **所有输出里 Key 一律用 `***`**（日志脱敏已实现 `mask_secrets`）
2. **测试文件不保留**，测试后自动消除（用 `_run_tests_tmp.py`）
3. **测试时不要弹窗**（用 `DFY_TEST_MODE=1`）
4. **不打包发布、不 push、不打 tag** — 除非用户明确要求
5. **每阶段结束暂停等确认**，不要连续做完多个阶段
6. **分小提交**，每个提交后跑回归

### 技术红线

1. **不改翻唱算法、不加新功能、不改 UI**（除非明确要求）
2. **不碰 `E:\AI\` 下的任何文件**
3. **待办/日程的核心信息（时间、内容）必须由程序拼接**，不能让模型改写
4. **实时生成只替换「说什么」**，不能绕过冷却、每日上限、静默规则、启动保护
5. **前缀缓存**：任何新增内容不得让 system 提示词前缀变动

---

## 五、测试基础设施

### 测试隔离（**必须**）

```python
import os, tempfile, shutil
TESTDIR = os.path.join(tempfile.gettempdir(), "dfy-xxx")
shutil.rmtree(TESTDIR, ignore_errors=True)
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR        # 配置/聊天记录/记忆全在这里
os.environ["DFY_BUNDLE_DIR"] = BASE        # 素材和代码从正式目录读
os.environ["DFY_TEST_MODE"] = "1"          # 不弹窗
```

**同时**在测试开头断言 `m.APP_DIR == TESTDIR`，确保没碰真实数据。

### 现有测试文件（可保留的 5 个）

| 文件 | 行数 | 结果 |
|---|---|---|
| `test_physics.py` | 224 | 45/45 |
| `test_region_select.py` | 197 | 29/29 |
| `test_vision24b.py` | 227 | 65/65 |
| `test_vision_style.py` | 266 | 56/56 |
| `test_lookscreen_e2e.py` | 140 | 5/5（**依赖外部视觉 API，网络波动会偶发 4/5，非代码问题**） |

**基线合计：195/195**（不含 lookscreen 的 5 项）

### 临时测试运行器 `_run_tests_tmp.py`

- 跑所有 `test_*.py`（除 keep-list）
- **无结果输出 = 失败**（不要误判通过后删脚本）
- 全通过才删除临时脚本

### 常用模式

**启动 PetApp 的最小配置**：
```python
CFG = {"config_version": 2, "topmost": True, "passthrough": False,
    "autostart": False, "ds_api_key": "sk-x",
    "chat_base": "http://127.0.0.1:9/v1", "chat_model": "m",
    "chat_migrated": True, "city": "重庆", "birthday": "",
    "animation_mode": "off",
    "usage": m._default_usage(), "games": m._default_games(),
    "reminders": dict(C.DEFAULT_REMINDERS),
    "remind_extras": m._default_remind_extras(),
    "features": m._default_features(),
    "pets": [{"id": "main", "size": 0.7, "x": 600, "y": 300,
              "mode": "still", "display": True}],
    "proactive_ai": {"enabled": False}}
```

**假时间注入**：衰减/跨天测试直接改 `last_access`、`cs.day`、`cs.started_at` 等字段。

**mock 视觉调用**：`c._vision_tool_call = lambda: fake_func`

---

## 六、踩过的坑（避免重复）

| 坑 | 现象 | 解决 |
|---|---|---|
| **PowerShell here-string 吃掉 `\n`** | Python 源码里字符串被拆成两行 → SyntaxError | 用 `[System.IO.File]::WriteAllText` 写脚本文件，或不在字符串里写 `\n` |
| **`write` 工具报「file no longer exists」** | 临时脚本刚建就被清理 | 用 pwsh 直接执行内联 Python，或换文件名 |
| **`@staticmethod` 位置被插错** | `TypeError: takes 1 argument but 2 were given` | 插入方法时检查装饰器归属 |
| **CRLF/LF 混用** | 锚点 `count=0` | 修改前先检测 `NL = "\r\n" if "\r\n" in raw else "\n"` |
| **`_voice_player` 是属性不是方法** | `callable()` 判断失败 | 直接 `getattr(self, "_voice_player", None)` 后判 `isinstance` |
| **打包时 exe 被占用** | `PermissionError: [WinError 5]` | 先杀进程 |
| **测试误删脚本** | 运行器把「无结果」当通过 | 已修为失败，不删 |
| **网络抖动** | GitHub API / 视觉 API 超时 | 重试 3-5 次，间隔 4-8 秒 |
| **`git show > file` 产出 UTF-16** | Python 报 `Non-UTF-8 code` | 用 `subprocess` 取原始 bytes 写入 |

---

## 七、常用命令速查

```powershell
$dir = 'D:\Software\Portable\应用程序\dafeiyu-pet'
$py  = Join-Path $dir '.venv\Scripts\python.exe'

# 语法检查
& $py -m py_compile (Join-Path $dir '桌宠.py')

# 跑单个测试
& $py -u (Join-Path $dir 'test_physics.py')

# 杀掉运行中的实例（测试/打包前必须）
Get-Process pythonw,python,'大肥鱼桌宠' -ErrorAction SilentlyContinue | Stop-Process -Force

# 打包
Remove-Item (Join-Path $dir 'build') -Recurse -Force -ErrorAction SilentlyContinue
& $py -m PyInstaller 桌宠.spec --noconfirm --clean

# 清理测试残留
Get-ChildItem $env:TEMP -Directory -Filter 'dfy-*' | Remove-Item -Recurse -Force

# 推送到 GitHub（网络不稳时）
$env:HTTPS_PROXY = "http://127.0.0.1:3067"
git push origin main
```

---

## 八、目录结构

```
dafeiyu-pet/
├── 桌宠.py              5060 行  PetApp 主程序
├── 桌宠.spec                    打包配置
├── proactive.py          719 行  主动搭话
├── screen_vision.py      528 行  看屏幕
├── lyrics.py             270 行  歌词/下载
├── cover.py              317 行  翻唱流水线
├── pet_voice.py          885 行  语音引擎
├── weather_cn.py         111 行  天气汉化
├── petcore/
│   ├── consts.py        3550 行  常量/台词池/提示词
│   ├── chat.py          4062 行  聊天窗口/气泡/记忆
│   ├── settings.py      4377 行  设置窗口
│   ├── pet_window.py    2554 行  宠物窗口/气泡绘制
│   ├── care.py           587 行  关心引擎（新）
│   ├── persona_lines.py  529 行  台词生成（新）
│   ├── vision_tools.py   407 行  视觉工具箱（新）
│   ├── vision_locate.py  292 行  元素定位（新）
│   ├── vision_intent.py  109 行  意图识别（新）
│   ├── memory_extract.py 267 行  记忆提取（新）
│   ├── privacy.py        146 行  隐私过滤（新）
│   ├── vision.py         365 行  视觉模型配置
│   └── region_select.py  151 行  区域选择
├── tools/                       _separate.py / rvc_convert_cli.py
├── sprites.zip                  317.6 MB 素材
├── cover_cache/                 251.3 MB 翻唱缓存
└── dist/                        打包产物
```

### 新增的文件（本次会话）

| 文件 | 行数 | 用途 |
|---|---|---|
| `petcore/care.py` | 587 | 关心触发引擎 |
| `petcore/persona_lines.py` | ~880 | 按人设生成台词 + 失败原因诊断 |
| `petcore/vision_tools.py` | 407 | 主色/对比/长图OCR/抠图 |
| `petcore/vision_locate.py` | 292 | 元素定位/清单 |
| `petcore/vision_intent.py` | 109 | 视觉意图识别 |
| `petcore/memory_extract.py` | 267 | 自动记忆提取 |
| `petcore/privacy.py` | 146 | 隐私过滤 |
| `petcore/chat_attach.py` | ~230 | 聊天附件采集/压缩/存储/清理 |

### 运行时生成的状态文件（都在 `APP_DIR`，全部已 gitignore）

```
config.json                配置文件（persona 字段 = 人设）
chats.json                 会话历史（含 attachments 字段，不存图片本体）
memory.json                长期记忆
care_state.json            关心引擎状态 + 好感度 + 实时生成计数
persona_lines.json         按人设生成的台词缓存（含 last_result 失败原因）
proactive_state.json       主动搭话状态
screen_memory.json         看屏记忆
chat_images/<sid>/         聊天附件缩略图与发送压缩图
vision_out/                视觉工具输出（找不同热图等）
error.log                  日志（已脱敏）
```

---

## 九、给接手 Agent 的建议

1. **先跑一遍回归**确认基线（195/195），再动手。
2. **不要重做已完成的 26 个提交**，先读提交信息确认。
3. **打包前必须杀进程**，否则必失败。
4. **发布前必须得到用户明确确认**，不要自作主张 push/tag。
5. **改 `consts.py` 的台词池时**，记得同步检查 `persona_lines.POOL_SPECS` 是否需要加池。
6. **改提示词时**，检查 `build_system_prompt` 的前缀稳定性（同日缓存不能破）。
7. **测试永远用 `DFY_APP_DIR` 隔离**，绝不碰真实 `chats.json`。
8. **报错先看 `error.log`**（`dist\error.log` 是 exe 的，项目根 `error.log` 是源码的）。
9. **改台词池相关代码时**，注意「整批解析失败 → 逐池降级」这条路径不能删。

---

## 十、最近一次对话的结尾状态

用户最后确认的工作：

- 本会话新增 4 个提交（`b13442f` / `99a19ad` / `e8076c6` / `ae1e968`）
- 累计 26 个提交，全部本地未推送
- exe 已重打（2026-10-26 22:29:06），等用户手测
- **未发布、未 push、未打 tag**
- 工作区干净（仅本文档待提交）

下一步等用户指令：**手测 exe → 确认后发布 v1.1.5**。

### 本会话 4 个提交的内容

| 提交 | 内容 |
|---|---|
| `b13442f` | 台词池系统：失败原因、兜底池、工具开场池 |
| `99a19ad` | 聊天窗口：主动搭话占位、主色卡片、附件收发 |
| `e8076c6` | 设置页重构：合并页面、通用页精简、dev_mode 门控 |
| `ae1e968` | gitignore：补充运行时产物 |
