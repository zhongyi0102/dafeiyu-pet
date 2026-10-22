# 更新日志

本项目的所有重要变更都会记录在此文件。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

分类说明：

- **新增（Added）** — 新功能
- **变更（Changed）** — 现有功能的调整
- **废弃（Deprecated）** — 即将移除的功能
- **移除（Removed）** — 已删除的功能
- **修复（Fixed）** — 缺陷修复
- **安全（Security）** — 安全相关

---

## [1.1.0] — 2026-10-21

### 新增（Added）

- **视觉模型独立 API 配置**：设置 →「聊天」→「看看屏幕」新增三个输入框
  - **视觉模型** — 留空则用内置默认 `deepseek-v4-flash-vision-exp`
  - **接口地址** — 留空则用 `https://api.deepseek.com/v1`
  - **视觉 Key** — 留空则复用聊天 Key（密码模式显示）
- **「测试」按钮**：造一张纯色图发给配置的模型，实测能否读图。
  结果用颜色标注：绿色 ✓ 成功 / 橙色 ⚠ 有读图能力但 token 不足 / 红色 ✗ 失败
- **「恢复默认」按钮**：一键清空三项配置，回到内置默认



- **「看看屏幕」改为用户框选截图**（不再自动截全屏）
  - 点击后先隐藏桌宠，等 150ms 再截底图（保证拍不到桌宠）
  - 全屏无边框遮罩：底图压暗 + 十字光标 + 选区保持原亮度 + 显示尺寸
  - 左键拖拽框选，松开确认；Esc / 右键取消；选区任一边 < 20px 视为取消
  - 兼容多显示器与高 DPI：按鼠标所在屏幕截图，选区坐标 × 设备像素比
  - 新增 `petcore/region_select.py`（`RegionSelectOverlay`）
- **两步流程**：视觉模型先描述选区 → 描述以「[屏幕观察] …」交给聊天模型按人设回复
- 视觉请求用「描述风格」的长度句与视觉「单次上限」；等待期间气泡显示「看看中…」


- **视觉解析结果写进聊天**：框选后聊天里依次出现三条
  - `[屏幕解析] …`（视觉模型看到的内容，让你知道它到底看到了什么）
  - `[看看屏幕] (框选)`
  - 宠物按人设的回复
  无论聊天窗口开没开都会写入（不开则落 `chats.json`，下次打开可见）。


- **余额查询支持智谱 GLM**（原来只有 DeepSeek）
  - 端点：`GET https://open.bigmodel.cn/api/biz/account/query-customer-account-report`
  - 返回 `data.availableBalance`，附带 `rechargeAmount` / `giveAmount` / `totalSpendAmount`
  - 播报格式：`智谱余额 ¥4.97（充值 5.00 / 已花 0.03）`
  - `BalanceWorker` 增加 `provider` 参数，拆出 `_query_deepseek()` / `_query_zhipu()`
  - 设置 →「余额」页新增「余额来源」下拉：自动 / DeepSeek / 智谱 GLM
  - `auto` 模式下：有智谱 Key 就用智谱，否则用 DeepSeek
  - 配置字段 `balance.provider`（默认 `auto`），带校验


- **视觉请求限制可视化并可调**（设置 →「聊天」→「看看屏幕」，与描述风格/单次上限同表）
  - **图片最长边**：滑块+数字框，512~2048px，步长 64（默认 1024）
    替换原先写死的 1024
  - **图片质量**：滑块+数字框，60~95（默认 70），用于 JPEG 压缩
  - **请求超时**：数字框，10~120 秒（默认 60），替换写死的 120
  - 每项下方灰色小字说明作用；全部写入配置 `look_screen.max_side/quality/timeout`
  - 「清空」按钮只清 Key/地址/模型，不重置这三项
  - 测试成功后状态行显示实际数据：`✓ 识别完成。试拍输出 48 字（耗时 4.3s，输入 42 / 输出 161 tokens）`


- **视觉模型有默认值且可手填**
  - 新增 `VISION_DEFAULT_MODELS`（智谱 `glm-5.3-flash` / DeepSeek
    `deepseek-v4-flash-vision-exp`）与兜底 `VISION_DEFAULT_MODEL_FALLBACK`
  - 默认配置 `look_screen.vision_model` 不再是空串
  - 下拉框设为可编辑：既能从识别结果选，也能直接手输模型名
  - 手输后立即保存（`editTextChanged`）
  - 下方灰字显示「默认模型：xxx」或「自定义模型：xxx（默认是 yyy）」
  - `_vision_cfg()` 在模型为空时按地址回落对应服务商的默认模型
- **设置页展示反幻觉规则**：`LOOK_SCREEN_ANTI_HALLUCINATION_HELP`
  五条规则以灰底小字显示在「看看屏幕」区域顶部，让用户知道识别时会遵守什么

### 变更（Changed）

- **设置页「聊天」页界面微调**
  - 「看看屏幕」五项（视觉 Key / 接口地址 / 视觉模型 / 描述风格 / 单次上限）
    收进同一个 `QFormLayout`：标签列右对齐同宽，控件列左对齐
  - 聊天区「单次输出上限」改成滑块 + 数字框，范围 100~2000
  - `system` 摘要移到「人设」区下方，文案改为「聊天 system = …」
  - 删除重复的「填入视觉 Key 后点测试自动识别」提示（只留顶部一句）
  - 去掉「聊天」标题下多余的分隔线
  - 删除视觉 Key 旁的「粘贴」按钮及相关代码
- **「看看屏幕」不再自动截全屏**：内容由用户框选决定，
  因此移除「窗口黑名单确认」与「只截活动窗口」的设计


- **余额改为两家同时查询、同时显示**
  - `query_balance()` 并行启动 DeepSeek 与智谱两个 `BalanceWorker`
  - 两边的结果都回来后合并成**一条**播报，只显示「模型 + 金额」：
    `DeepSeek ¥8.80 / 智谱 ¥4.97`
  - 只填了一家的 Key 时自动只查那家（不报错）
  - 档位动画按**合计**金额判断
  - 新增 `_on_one_balance()`（单家结果暂存）与 `_report_balances()`（合并播报）
  - `self.balance_worker` → `self.balance_workers`（列表）+ `_balance_results` / `_balance_pending`
  - 设置 →「余额」的「余额来源」改为「查询范围」：
    两家同时查（推荐）/ 只查 DeepSeek / 只查智谱 GLM
  - 提示文字显示两家 Key 的就绪状态

### 移除（Removed）

- `LOOK_SCREEN_MAX_SIDE`（全屏截图长边限制，框选后不再需要）
- `LOOK_SCREEN_MAX_TOKENS` / `LOOK_SCREEN_TOKEN_RETRY`（改用视觉「单次上限」配置）
- `SettingsWindow._paste_vision_key()`（粘贴按钮已删）

### 修复（Fixed）

**看看屏幕无法使用**（功能从上线起就未真正可用）

- **默认模型名错误**：原默认值为 `deepseek-vl2`（DeepSeek 无此模型）。
  正确名称为 `deepseek-v4-flash-vision-exp`——**必须全小写**，
  写成 `DeepSeek-V4-Flash-Vision-Exp` 会被 API 拒绝
  （`The supported API model names are ...`）
- **推理模型 token 不足导致空回复**：视觉模型属推理模型，
  思考过程（`reasoning_content`）会吃掉 token。
  原 `max_tokens=200` 常被推理耗尽，`content` 返回空串。
  现在默认 `1200`，并在 `finish_reason == "length"` 且 `content` 为空时
  自动用 `2000` 重试一次
- **仍为空时不再静默失败**：退回 `reasoning_content` 摘要，
  并提示"可在设置里换个视觉模型"，而不是只说"没看出啥"
- **模型名可留空**：留空时回落内置默认，不再报"未配置视觉模型"


- **服务商识别被 401 中断**：原逻辑遇到第一个 401 就中止，
  导致智谱 Key 在 DeepSeek 那步就被判死，永远认不出智谱
- **智谱 Key 认不出**：新增 `VISION_KEY_PATTERNS`，
  按「32 位 hex + . + 16~48 位」精确识别智谱（0 秒出结果）
- **DeepSeek Key 被误判**：移除过宽的 `sk-` 正则
  （会把 DeepSeek 认成 SiliconFlow），改交给并行探测
- **探测慢**：顺序试探最坏约 120 秒 → 改并行（2 家）+ 格式优先（0 秒）
- **智谱 /models 不列出视觉模型**：新增 `VISION_KNOWN_MODELS` 兜底，
  用已知模型（glm-4.6v 等）继续验证
- 修复设置页 `_vision_tokens_row` 改名后 3 处未同步的引用
- 修复 `QFormLayout` 未导入

---


- **「测试」永久卡在识别中**（致命）：worker 线程里用
  `QTimer.singleShot(0, ...)` 回主线程更新 UI，但该线程没有 Qt 事件循环，
  **回调永不执行** → UI 一直停在「识别中…」，地址与模型永远填不上。
  实测 81.7 秒仍无结果。
  改用 `_VisionBus(QObject)` 的 Signal 跨线程 emit（Qt 自动排队到主线程），
  9 处 `QTimer.singleShot` 全部替换。修复后 **2.2 秒**完成识别。
- **`_on_vision_test_done` 引用未定义变量**：`self._vision_cfg()` 是
  `PetApp` 的方法，`SettingsWindow` 上没有 → 改为 `self.app._vision_cfg()`


- **`_run_look_screen` 报 `UnboundLocalError: cannot access local variable 'jpg'`**：
  `run()` 内 `finally: jpg = None` 让 `jpg` 变成局部变量，
  遮蔽了外层闭包值。改用 `holder = {"jpg": None}` 字典承载，
  并在 `run()` 定义**之前**创建（原先在之后创建，闭包捕获不到）。
- **`_look_screen_to_chat` / `_append_to_chat_file` 两个方法丢失**：
  之前的大段替换把它们挤掉了，导致视觉成功后同步聊天时报
  `'PetApp' object has no attribute '_look_screen_to_chat'`。已重新实现。
- **聊天 Key 失效时整条流程报错**：新增 `_look_fallback()` 降级——
  聊天模型不可用（未配置/401/网络错误）时，把视觉描述用人设口吻
  「唔……我看到了：…」直接显示，而不是抛「API Key 无效」。


- **跨线程操作 UI 导致气泡不显示**：worker 线程里直接调
  `cw.append_external()` 创建 Qt 控件，报
  `QObject::setParent: Cannot set parent, new parent is in a different thread`，
  界面不刷新。新增 `PetApp.look_to_chat` Signal，worker 只 emit，
  主线程 `_on_look_to_chat()` 真正写界面与文件。
- **首次使用时 `chats.json` 不存在导致写入失败**：
  `_append_pairs_to_chat_file()` 现在会自动创建目录结构与会话，
  不再直接返回 False。


- **视觉模型瞎编内容**（幻觉）：看动漫截图时编出「牧神记第180集」
  这类画面里根本不存在的作品与剧情。
  - 新增 `LOOK_SCREEN_ANTI_HALLUCINATION`：五条硬约束
    （只描述可见内容 / 禁止猜作品名 / 禁止编剧情 / 有字抄字 / 不确定就跳过）
  - 视觉 prompt 重写为客观描述任务，
    去掉旧版「用大肥鱼口吻…」的说法（那是聊天模型的事）
  - 聊天请求的 user 消息加防编造提醒
    （「仅此而已，不是剧情、不是某部作品」）
  - 实测：编造性解读消失，画面里真实存在的文字正常转述


- **视觉模型排序未生效**：并行验证的到达顺序是随机的，
  导致 `glm-5.3-flash` 排在 `glm-4.5v` 之后。
  现在验证完成后统一 `filter_and_rank()` 重排：
  `-flash` 结尾最前 → `flashx`/其它 → 含 `turbo`/`thinking`/`pro` 最后
- **`suggest_models` 把已知模型无脑排前**：改为合并后统一按规则重排
- **试拍结果被丢弃**：`_on_vision_test_done` 里 `_reset_vision_test_ui()`
  会把 `_vision_token` +1，导致紧接着的试拍 `stale()` 立刻为真。
  试拍改用独立 `_vision_rtoken`

### 变更（Changed）

- **设置页新增「视觉」分页**：左侧导航「聊天」之后
  - 「看看屏幕」整块（视觉 Key / 接口地址 / 视觉模型 / 描述风格 /
    单次上限 / 图片最长边 / 图片质量 / 请求超时 / 反幻觉规则 /
    保存·测试·取消·清空按钮 / 状态行）全部移过去
  - **聊天页只保留**：回复风格、人设、单次输出上限、system 提示
  - 新增 `_build_visual_page()`，`PAGES` 加 `("visual", "视觉")`
  - 视觉页顶部标题改为「视觉」，不再重复「看看屏幕」小标题与分隔线

### 变更（Changed）

- **「识别规则」改为可编辑**（设置 →「视觉」页顶部）
  - 多行文本框，内容是视觉请求 **system** 的固定部分（约 6 行高，可点击标题折叠）
  - 新增常量 `VISION_SYSTEM_DEFAULT`（屏幕观察员 + 五条反幻觉规则）
  - 界面与请求**共用同一份配置** `look_screen.vision_system`，不各写一份
  - 按钮「保存规则」「恢复默认」，旁边小字显示「默认 / 已修改（未保存）/ 已保存」
  - 「恢复默认」有二次确认，避免误覆盖
  - 描述风格的长度句仍由程序**自动追加在 system 最末尾**，不进文本框
  - 下方灰字：「这段会作为视觉请求的 system，长度句自动追加在末尾」
  - 「清空」按钮不清规则
- **视觉请求改用 system + user 两条消息**
  - system = 识别规则（可编辑）+ 自动追加的长度句
  - user = 固定指令 `VISION_USER_PROMPT`（描述这张屏幕截图）
  - 原来规则全塞在 user 里，现在语义更正确
- **四行灰字说明改为悬停提示**：默认模型 / 最长边 / JPEG 质量 / 超时

### 新增（Added）

- **可收缩区块控件 `CollapsibleSection`**（设置页复用）
  - 标题行：箭头（展开 ▾ / 收起 ▸）+ 标题 + 右侧状态小字，整行可点
  - 收起只隐藏内容区，不影响内部控件读写与配置保存
  - 展开状态持久化到 `ui_sections`（只影响显示）

### 变更（Changed）

- **「人设」（聊天页）与「识别规则」（视觉页）改用同一可收缩控件**
  - 样式与行为完全一致，标题色统一为 `#2f3542`（去掉识别规则原来的绿色）
  - 收起时隐藏：说明文字、文本框、保存/恢复默认按钮；只留标题行
  - 标题行右侧显示状态：`默认` / `已保存` / `未保存`
  - 有未保存修改时收起，文本框内容保留、状态显示「未保存」
  - 首次默认：**人设展开、识别规则收起**
  - 聊天页底部 `system = …` 那行不收缩，始终显示

### 修复（Fixed）

- `_refresh_chat_hint()` 里引用了已删除的 `_persona_state`，
  异常被 `except` 吞掉，导致 `system = …` 那行一直空白

### 新增（Added）

- **视觉页「高级设置」折叠区**（默认收起）
  - 四项收进去：单次上限 / 图片最长边 / 图片质量 / 请求超时
  - 收起时标题行右侧显示摘要：`上限 800 · 最长边 704 · 质量 68 · 超时 60 秒`
  - 改任一值摘要实时更新
  - 展开状态持久化到 `ui_sections.vision_advanced`

### 变更（Changed）

- **视觉页布局重排**
  - 基础区始终可见：视觉 Key / 接口地址 / 视觉模型 / 描述风格
  - 「描述风格」灰字（当前追加句）紧贴控件下方，不再放页面底部
  - 四项各带一行灰字说明（样式与聊天页一致，随折叠区显示/隐藏）
  - 四项放进**同一个 QFormLayout**：标签列同宽，滑块长度与数字框左边缘自动对齐
  - 请求超时数字框宽度改为 96px，与其他数字框一致
- `CollapsibleSection` 新增 `set_summary()`：收起时显示摘要、展开时显示状态

## [1.0.0] — 2026-10-20

首个公开版本。从单文件 10777 行重构为模块化结构，并做到「下载一个 exe 就能用」。

### 新增（Added）

**核心功能**

- 三视图透明桌面宠物（正面 / 侧面 / 背面），无边框、置顶、可拖拽
- 拖拽甩抛物理：惯性、重力、反弹、摩擦、跨屏
- 多宠物：右键「生小肥鱼」孵化新桌宠，配置各自独立
- 86 个画师原画帧动画，含待机、点击、拖拽、吃东西、时节特效等

**AI 对话**

- 多会话聊天窗口，会话按宠物隔离
- 前缀缓存优化（人设 + 长度句固定不变，降低 token 消耗）
- 长期记忆（`memory.json`），超长上下文自动总结
- 可编辑人设，支持「简短 / 正常 / 详细」三档回复长度
- SSE 流式输出、停止生成、失败重试

**提醒系统**

- 5 种提醒（喝水 / 站起来 / 护眼 / 待办 / 定时）
- 提醒时宠物走到屏幕中间，大气泡 + 图标 + 按钮
- 待办面板（增删改）、定时提醒、免打扰时段
- 提前提醒、错过宽限静默处理

**主动陪伴**

- 宠物静默够久时主动说话，4 种情境：深夜 / 饭点 / 久坐 / 孤独
- 18 条傲娇风台词，阈值全部可调（静默时长 / 同应用时长 / 冷却 / 每日上限）
- 睡觉、免打扰、小游戏、聊天中自动不打扰

**看看屏幕**

- 右键截图 → 压缩 JPEG（最长边 768、质量 70，仅内存处理不落盘）
- 附前台窗口「程序名 | 标题」→ 发视觉模型 → 用人设回应
- 4 秒冷却防连点，请求跑后台线程

**其它功能**

- 时节特效 8 种（落叶、雪、花瓣、烟花、月亮、南瓜等）
- 小游戏：猜拳、接食物（30 秒）
- DeepSeek 余额查询，支持自动刷新
- 节日整点台词、待机碎碎念、系统监控播报
- 统一设置窗口，10 个分页

**体验细节**

- 播放速率 0.5x ~ 4.0x（8 档）
- 气泡文字大小 50% ~ 150%，气泡区随缩放变高
- 撞飞彩蛋：高速撞边缘时头部按速度方向旋转，5 秒后回正
- 隐藏时暂停动画解码，CPU 接近 0
- 截屏时自动隐藏（可关）

**安装与分发**

- 单文件 exe（351 MB，内置全部素材），双击即用，无需 Python
- `安装.bat` 一键脚本：检查 Python → 建 venv → 装依赖 → 下载素材 → 启动
- `桌宠.spec` PyInstaller 打包配置
- 素材走 GitHub Release 附件，仓库保持轻量（1.2 MB）

### 变更（Changed）

- **架构**：`桌宠.py` 从 10777 行拆分为 5 个模块（-73%）
  - `桌宠.py`（2944 行）— 入口 + `PetApp`
  - `petcore/consts.py`（2466 行）— 常量、配置、校验、工具
  - `petcore/pet_window.py`（2493 行）— 宠物窗口
  - `petcore/chat.py`（1640 行）— 聊天窗口与后台线程
  - `petcore/settings.py`（1466 行）— 设置窗口
  - 依赖单向：子模块只依赖 `consts`，通过 `self.app` 访问应用级功能
- **exe 模式**：素材从「exe 旁边」改为「exe 内置 + 首次解压到 `%LOCALAPPDATA%\DaFeiYuPet`」
  - 首次启动 8 秒，之后 0.3 秒
- **素材位置**：从仓库移除（310 MB），改走 Release 附件
- **动画播放**：播放速率改为运行时倍速，不再需要重新转帧
- **气泡布局**：气泡区高度随文字缩放动态调整，避免压到宠物头顶

### 移除（Removed）

- 4 个提醒功能：点击关闭提醒、系统通知、提醒提示音、可调稍后时间
- `DS_PERSONA` 冗余常量（改用 `DEFAULT_PERSONA`）
- `turn_left/right/up/down` 代码动作（转向改为纯功能性 `TURN_DIRS`）
- 备份目录 `sprites/_bak_anims`（51 MB）、`sprites/anim/_bak_bal_*`（9 MB）
- `tools/preview`（814 MB 审计用 GIF 预览图）

### 修复（Fixed）

- **时节特效不可见**（长期存在）：特效矩形是全屏尺寸，但绘制在 220×310 的宠物窗口内；
  且 `paintEvent` 里 `p.translate` 符号反了
- **月亮/南瓜从不显示**：定位在窗口外，改为窗口相对坐标
- **37 个动作脚底漂移 8~68px**：`make_frames.py` 强制逐帧底部对齐，
  把镜头运动变成了上下跳；改为 `dy = 0` 并重新转换
- **余额动画漂移 27px**：20-21 帧（镜头拉远）未对齐，按中位底部平移
- **提醒气泡不渲染**：`_remind_rects` 引用未定义的 `need_w`，异常被 `except` 吞掉
- **提醒气泡按钮重叠**：218px 窗口放不下两个全宽按钮，改为窄模式 + 数量文字让位
- **提醒动作不播放**：`animation_mode = "off"` 时 `animate=False` 杀掉了整个动作
- **悬停暂停失效**：`remind_until` 不推进，改为累积悬停时长
- **拖拽无法打断提醒走动**：补上 `dragging` 检查
- **`remind_dnd` 双存储不同步**：勾选框写 `remind_extras`，读取却查 `features`
- **`fx_xmas` / `fx_firework` 无法播放**：未注册进 `ACTIONS`
- **拆分后 `_FRAME_CACHE` 未定义**：`import *` 不导入下划线名，需显式 import 20 个
- **拆分后 `APP_DIR` 指向 `petcore/`**：配置写到错误目录，改为取父目录
- **`启动桌宠.bat` 中文文件名损坏**：原文件是 `????.py`，根本启动不了

### 安全（Security）

- 隐私清理：提交历史中的 QQ 邮箱改为 GitHub 匿名邮箱
- 确认无 API Key、无本地路径、无运行时数据文件进入仓库
- `.gitignore` 排除 `config.json` / `chats.json` / `memory.json` / `error.log`

---

## 版本规划

| 版本 | 计划 |
|---|---|
| 1.0.x | 缺陷修复 |
| 1.1.0 | 待定（可能是点击音效 / 边缘吸附） |

---

[1.0.0]: https://github.com/zhongyi0102/dafeiyu-pet/releases/tag/v1.0.0
