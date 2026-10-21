## 大肥鱼桌宠 v1.1.0

DeepSeek 鲸鱼娘·大肥鱼的桌面宠物（Python + PySide6）。
完整变更记录见 [CHANGELOG.md](https://github.com/zhongyi0102/dafeiyu-pet/blob/main/CHANGELOG.md)。

---

## 本版重点：看看屏幕（框选 + 智谱视觉）

### 改为用户框选，不再自动截全屏
- 右键「看看屏幕」→ 屏幕变暗 → **拖拽框选** → 松开确认
- 左键拖拽框选，Esc / 右键取消；任一边 < 20px 视为取消
- 兼容多显示器与高 DPI（按鼠标所在屏幕截图，坐标 × 设备像素比）
- 截图全程只在内存（QPixmap / QImage / QBuffer），**不落盘**

### 支持智谱 GLM 视觉模型
- 除 DeepSeek 外，新增**智谱 GLM** 支持（`open.bigmodel.cn`）
- 只填 Key，点「测试」**自动识别服务商 + 可用视觉模型**
- 智谱 Key 按格式秒识别（32 位 hex + `.` + 16~48 位），0 秒出结果
- 视觉模型下拉框排序：`-flash` 结尾最前 → `flashx`/其它 → `turbo`/`thinking`/`pro` 最后

### 视觉请求限制可视化
设置 →「聊天」→「看看屏幕」，与描述风格/单次上限同一表格：

| 项目 | 范围 | 默认 |
|---|---|---|
| 图片最长边 | 512~2048 px（步长 64） | 1024 |
| 图片质量 | 60~95 | 70 |
| 请求超时 | 10~120 秒 | 60 |

每项下方有灰色小字说明作用。测试成功后状态行显示实际数据：
`✓ 识别完成。试拍输出 48 字（耗时 4.3s，输入 42 / 输出 161 tokens）`

### 视觉模型有默认值且可手填
- 默认：智谱 `glm-5.3-flash` / DeepSeek `deepseek-v4-flash-vision-exp`
- 下拉框可编辑：既能从识别结果选，也能**直接手输模型名**
- 灰字提示「默认模型：xxx」/「自定义模型：xxx（默认是 yyy）」

### 反幻觉：视觉模型不再瞎编
看动漫截图时不再编出「牧神记第180集」这类画面里不存在的作品与剧情。
五条硬约束（设置页可见）：
1. 只描述画面里真实可见的内容，一个字都不推测
2. 不说出任何作品名、游戏名、动画名、人物名、门派名、地名
3. 不编造画面外的剧情、前后文、角色关系
4. 画面里有什么字就抄什么字，没有的字不会发明
5. 不确定的部分直接跳过，宁少勿编

### 解析结果写进聊天
框选后聊天里依次出现三条（窗口开没开都写入）：
```
[屏幕解析] 截图显示三个矩形：上排左蓝、右橙，下排浅蓝，布局简洁
[看看屏幕] (框选)
哼，笨蛋主人给我看这个干嘛~ 三个方块有什么好看的…
```
可继续追问（解析结果已在会话历史里）。

---

## 余额：两家同时查

- **DeepSeek + 智谱 GLM 并行查询**，一次播报
- 显示：`DeepSeek ¥8.80 / 智谱 ¥4.97`
- 只填一家 Key 时自动只查那家
- 智谱余额端点：`GET https://open.bigmodel.cn/api/biz/account/query-customer-account-report`
- 设置 →「余额」→「查询范围」：两家同时查（推荐）/ 只查 DeepSeek / 只查智谱

---

## 修复 Fixed

### 致命
- **「测试」永久卡在识别中**：worker 线程里用 `QTimer.singleShot` 回主线程更新 UI，
  但该线程没有 Qt 事件循环 → 回调永不执行。实测 81.7 秒仍无结果。
  改用 Signal 跨线程 emit，修复后 **2.2 秒**完成
- **`UnboundLocalError: cannot access local variable 'jpg'`**：
  `finally: jpg = None` 让变量变成局部，遮蔽闭包值。改用字典承载
- **`_look_screen_to_chat` / `_append_to_chat_file` 方法丢失**：视觉成功后同步聊天直接 AttributeError

### 视觉
- **服务商识别被 401 中断**：遇到第一个 401 就中止，导致智谱 Key 永远认不出
- **DeepSeek Key 被误判**：移除过宽的 `sk-` 正则
- **智谱 `/models` 不列出视觉模型**：新增已知模型兜底
- **模型排序未生效**：并行验证到达顺序随机，现在验证完成后统一重排
- **试拍结果被丢弃**：`_reset_vision_test_ui()` 把 token +1，试拍 `stale()` 立刻为真

### 聊天同步
- **跨线程操作 UI**：worker 线程里直接 `append_external()` 创建 Qt 控件，
  报 `setParent different thread`，界面不刷新。改用 Signal 回主线程
- **首次使用 `chats.json` 不存在导致写入失败**：现在自动创建

### 其他
- 聊天 Key 失效时整条流程报错 → 新增降级，用人设口吻显示视觉描述
- 设置页 `_vision_tokens_row` 改名后 3 处未同步引用
- `QFormLayout` 未导入

---

## 下载

### 方式 A：只要 exe（推荐）

下载 **`dafeiyu-pet.exe`**（**已内置全部素材**）

1. 双击运行 —— 首次启动约 8 秒解压素材，之后 0.3 秒
2. 无需安装 Python，无需另外下载素材
3. 右键宠物 →「设置」→ 填入 API Key

> exe 未做代码签名，Windows 可能提示「未知发布者」。
> 点「更多信息」→「仍要运行」即可。

### 方式 B：源码运行

下载 **`dafeiyu-sprites.zip`**（素材包）

1. 下载本仓库源码（`Code` → `Download ZIP`，或 `git clone`）
2. 把 `dafeiyu-sprites.zip` 解压到项目根目录（得到 `sprites/anim/`）
3. `pip install -r requirements.txt`
4. 运行 `启动桌宠.bat`（或先跑 `安装.bat` 一键搞定）

---

## 配置 API Key

**聊天（DeepSeek）**
右键宠物 →「设置 Key」，或在 设置 →「聊天」填写。

**视觉（智谱 GLM）**
设置 →「聊天」→「看看屏幕」→ 填视觉 Key → 点「测试」自动识别。

两个 Key 互相独立，可以不填视觉 Key（那样「看看屏幕」不可用）。

---

## 素材说明

动画素材（86 个动作）来自 [dsh-pet](https://github.com/PC2005-cloud/dsh-pet)，
遵循其授权：**允许开源使用、禁止商用**。详见仓库 [LICENSE](https://github.com/zhongyi0102/dafeiyu-pet/blob/main/LICENSE)。
