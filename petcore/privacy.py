# -*- coding: utf-8 -*-
r"""privacy.py — 自动记忆的隐私过滤与质量闸门

两道闸：
  1. 敏感信息：密钥、证件号、银行卡、手机、邮箱、密码、验证码、
     长数字串、精确住址、IP —— 命中整条丢弃（不是打码）。
  2. 质量闸门：祈使/指令句式、超长 —— 命中整条丢弃。

丢弃时只写 INFO 日志（规则编号），绝不记录原文。
"""
import logging
import re

_logger = logging.getLogger("dafeiyu")

MAX_LEN = 40          # 单条记忆最大字数

# 规则编号 -> (说明, 正则)。命中任意一条即丢弃。
SENSITIVE_RULES = (
    ("S1", "API Key / Token", re.compile(
        r"sk-[A-Za-z0-9_\-]{8,}"
        r"|AIza[0-9A-Za-z_\-]{10,}"
        r"|\b[0-9a-fA-F]{32}\.[A-Za-z0-9]{16,48}\b"
        r"|Bearer\s+[A-Za-z0-9_\-\.]{8,}", re.I)),
    ("S2", "身份证号", re.compile(
        r"(?<![0-9])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9])")),
    ("S3", "银行卡号", re.compile(
        r"(?<![0-9])(?:\d{4}[ \-]?){3,4}\d{1,4}(?![0-9])")),
    ("S4", "手机号", re.compile(
        r"(?<![0-9])1[3-9]\d{9}(?![0-9])")),
    ("S5", "邮箱", re.compile(
        r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")),
    ("S6", "密码类", re.compile(
        r"(?:密码|口令|password|passwd|pwd)\s*[:：=是为]?\s*\S{1,}", re.I)),
    ("S7", "验证码", re.compile(
        r"(?:验证码|校验码|动态码|code)\s*[:：=是为]?\s*\d{4,8}", re.I)),
    ("S8", "长数字串", re.compile(
        r"(?<![0-9])\d{12,}(?![0-9])")),
    ("S9", "精确住址", re.compile(
        r"[\u4e00-\u9fa5]{1,8}(?:路|街|巷|号|栋|幢|单元|号楼|室|小区)"
        r"\s*\d+")),
    ("S10", "IP 地址", re.compile(
        r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")),
)

# 质量闸门：祈使 / 指令句式（这些不是「关于用户的事实」）
IMPERATIVE_RULES = (
    ("Q1", "要求助手做什么", re.compile(
        r"^(?:以后|今后|从今往后|接下来|之后)\s*[，,]?\s*你"
        r"|你要|你必须|你得|你应该|你不许|你不能|你不要|你别"
        r"|请帮我|帮我|替我|给我|告诉我|回答我|记住要")),
    ("Q2", "命令式开头", re.compile(
        r"^(?:请|别|不要|不许|必须|务必|记得|记住|通知|提醒|设置|打开|关闭|开始|停止)")),
)

# 相对时间：命中说明模型没按提示词换算成具体日期
RELATIVE_TIME_RULES = (
    ("T1", "相对时间未换算", re.compile(
        r"下周|下下周|下个月|下月|明天|后天|大后天|昨天|前天"
        r"|本周|这周|这星期|本周内|月底|月初|年底|年初"
        r"|过几天|过两天|过一阵|最近|刚才|等会儿|待会儿|回头")),
)


def _log_drop(rule, kind):
    try:
        _logger.info("[记忆] 丢弃自动记忆（%s 规则 %s）" % (kind, rule))
    except Exception:
        pass


def check_text(text, now_str=None):
    r"""检查一条候选记忆是否可以入库。

    返回 (ok, rule)：
      ok=True  —— 可以入库
      ok=False —— 应丢弃，rule 为命中的规则编号（用于日志，不含原文）

    参数 now_str 目前未用于判定，保留给将来「日期是否在合理范围」的校验。
    """
    t = (text or "").strip()
    if not t:
        return False, "E0"

    # 1) 长度
    if len(t) > MAX_LEN:
        _log_drop("L1", "长度")
        return False, "L1"

    # 2) 敏感信息
    for rule, _name, pat in SENSITIVE_RULES:
        if pat.search(t):
            _log_drop(rule, "敏感")
            return False, rule

    # 3) 祈使 / 指令句式
    for rule, _name, pat in IMPERATIVE_RULES:
        if pat.search(t):
            _log_drop(rule, "指令")
            return False, rule

    # 4) 相对时间未换算
    for rule, _name, pat in RELATIVE_TIME_RULES:
        if pat.search(t):
            _log_drop(rule, "相对时间")
            return False, rule

    return True, ""


def filter_ops(ops):
    r"""过滤一批提取结果。

    输入 [{"op": "ADD"/"UPDATE"/"SKIP", "text": ..., "id": ...}, ...]
    返回 (kept, dropped_rules)：
      kept          —— 通过闸门的 op 列表
      dropped_rules —— 被丢弃命中的规则编号列表（去重后按出现顺序）
    """
    kept = []
    rules = []
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op") or "").strip().upper()
        if kind == "SKIP":
            continue
        if kind not in ("ADD", "UPDATE"):
            continue
        text = str(op.get("text") or "").strip()
        ok, rule = check_text(text)
        if not ok:
            rules.append(rule)
            continue
        clean = dict(op)
        clean["text"] = text
        clean["op"] = kind
        kept.append(clean)
    # 去重保序
    seen = set()
    uniq = []
    for r in rules:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return kept, uniq
