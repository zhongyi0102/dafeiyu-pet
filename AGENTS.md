# AGENTS.md — 大肥鱼桌宠 项目工作约定

> 本文件对 `E:\dafeiyu-pet` 项目的所有会话生效。
> 全局约定见 `~/.dsh/AGENTS.md`，本文件是项目级补充，冲突时以本文件为准。

---

## 一、遇到不熟悉的功能：先调研开源实现

**不要立即从零实现。** 按下面顺序执行：

### 1. 先检索

在 GitHub 搜索同类活跃项目，用这些关键词组合：

```
<功能名> python
<功能名> pyside6 / pyqt
<功能名> desktop app
```

按以下三项筛选：

| 检查项 | 门槛 |
|---|---|
| **活跃度** | 最近 3-6 个月有 commit |
| **License** | MIT / Apache-2.0 / BSD 等明确许可 |
| **Issues** | 可访问，能看到已知问题 |

### 2. 读实现

看这几个地方，理解设计取舍：

- `README` — 功能边界、支持的平台、明确不做什么
- 目录结构 — 模块怎么切分
- 关键文件 — 核心算法/流程的实现
- `requirements.txt` / `pyproject.toml` — 依赖有多重
- `Issues` 里带 `bug` / `limitation` / `wontfix` 标签的 — 已知坑

### 3. 比较后报告

动手前先给用户一份对比，至少包含：

- 查了哪几个项目（名称 + 链接 + star 数 + 最近提交时间）
- 各自的实现思路差异
- 各自的依赖代价
- 已知问题
- **我准备借鉴哪个、为什么**

### 4. 借鉴设计，不复制代码

- **吸收思路**：架构、算法、边界处理方式
- **自己写实现**：适配本项目的代码风格、命名、结构
- **不搬来源不明的代码**：License 不清晰、无出处、明显是逆向产物的一律不动
- 确需引用片段时：注明来源项目 + License + 原文链接

### 5. 例外情况（可以不调研直接做）

- 项目内已有同类实现（先 `grep` 确认）
- 明确的 bug 修复（不是新功能）
- 用户已经指定了实现方式
- 纯 UI 文案/样式调整

---

## 二、设计任务：给选项，让用户决策

做**设计方案**时不要只给一个方案。

**适用范围**：UI 设计、配色、布局、主题、架构选型、技术栈选择、命名风格、文档结构。

**要求**：

1. **先给选项表**：2-3 个可选方向，每个写清：风格特征、适用场景、代价/风险
2. **标明推荐**：可以推荐，但最终由用户拍板
3. **用户选定后再执行**：不要一边列选项一边已经动手
4. **不要擅自「优化」**：用户选定的方案就是方案，执行时不要偷偷改

---

## 三、输出与操作规范

### 硬性约束

| 约束 | 说明 |
|---|---|
| **Key 脱敏** | 所有输出里 Key 一律用 `***` |
| **测试文件不保留** | 测试完自动删除临时脚本 |
| **测试不弹窗** | 用 `DFY_TEST_MODE=1` |
| **不擅自打包发布** | 打包 / push / tag / Release 必须用户明确要求 |
| **每阶段暂停等确认** | 不要连续做完多个阶段 |
| **分小提交** | 每个提交后跑回归并汇报 |

### 技术红线

| 红线 | 说明 |
|---|---|
| 不改翻唱算法 | 除非明确要求 |
| 不加新功能 | 除非明确要求 |
| 不改 UI | 除非明确要求 |
| 不碰 `E:\AI\` 下的其他项目 | 除本项目自身 |
| 待办/日程核心信息程序拼接 | 不能让模型改写时间、内容 |
| 实时生成只替换「说什么」 | 不能绕过冷却、每日上限、静默规则、启动保护 |
| 前缀缓存稳定 | 新增内容不得让 system 提示词前缀变动 |

---

## 四、输出风格

- **简体中文**
- **短句、表格优先**，少用长段落
- **结论先行**，细节在后
- **文件路径给完整路径**
- **不确定的地方直说**，不要编

---

## 五、项目环境

| 项 | 值 |
|---|---|
| 项目根目录 | `E:\dafeiyu-pet` |
| Python | 3.13.13（`.venv`） |
| 系统 Python | `C:\Users\32532\AppData\Local\Programs\Python\Python313\python.exe` |
| 启动方式 | `.venv\Scripts\pythonw.exe "桌宠.py"` |
| 快捷方式 | 开始菜单 + Startup（均已指向 E 盘） |
| Git | main 分支，远程 origin/main |

### 常用命令

```powershell
$dir = 'E:\dafeiyu-pet'
$py  = Join-Path $dir '.venv\Scripts\python.exe'

# 语法检查
& $py -m py_compile (Join-Path $dir '桌宠.py')

# 跑测试
& $py -u (Join-Path $dir 'test_physics.py')

# 杀进程（测试/打包前必须）
Get-Process pythonw,python,'大肥鱼桌宠' -ErrorAction SilentlyContinue | Stop-Process -Force

# 重启（改代码后）
# 右键托盘 → 重启
```

### 备份位置

```
E:\dafeiyu-backup\20261027_131814\   569.1 MB
```

---

## 六、测试隔离（必须）

所有测试用临时目录，**绝不碰真实数据**：

```python
import os, tempfile, shutil
TESTDIR = os.path.join(tempfile.gettempdir(), "dfy-xxx")
shutil.rmtree(TESTDIR, ignore_errors=True)
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR        # 配置/聊天/记忆全在这里
os.environ["DFY_BUNDLE_DIR"] = BASE        # 素材和代码从正式目录读
os.environ["DFY_TEST_MODE"] = "1"          # 不弹窗
```

**同时在测试开头断言** `m.APP_DIR == TESTDIR`，确保没碰真实数据。

### 回归基线

| 文件 | 用例数 |
|---|---|
| `test_physics.py` | 45 |
| `test_region_select.py` | 29 |
| `test_vision24b.py` | 65 |
| `test_vision_style.py` | 56 |
| **合计** | **195/195** |

`test_lookscreen_e2e.py`（5 项）依赖外部视觉 API，网络波动会偶发 4/5，不计入基线。
