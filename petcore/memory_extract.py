# -*- coding: utf-8 -*-
r"""memory_extract.py — 自动记忆提取

从会话中未处理过的用户消息里提取值得长期记住的个人信息。

设计要点（对齐翻唱的教训）：
  - 提取在后台 QThread 里跑，启动时绑定 origin_session + 消息区间
  - 完成信号带回 (session_id, upto, ops)，写记忆是全局的，与会话无关
  - 只有写盘成功后才推进 mem_extracted_upto
  - 任何失败只写日志，不影响聊天
"""
import json
import logging
import time

from PySide6.QtCore import QThread, Signal

_logger = logging.getLogger("dafeiyu")

MAX_OPS = 3           # 一次最多输出几条
MAX_TOKENS = 500      # 输出上限


def _log(msg, level=logging.INFO):
    try:
        _logger.log(level, msg)
    except Exception:
        pass


# ===== 提示词 =====

SYSTEM_PROMPT = """你是信息提取器。从「用户消息片段」中提取值得长期记住的用户个人信息。

【只记这些】
- 用户自己的偏好（喜欢/讨厌什么）
- 个人事实（名字、职业、养宠物、所在地级市）
- 近期计划（要做的事、约定）
- 重要事件（生日、纪念日、考试、面试）

【绝对不记】
- 闲聊寒暄、情绪发泄
- 点歌、歌词、翻唱相关
- 助手自己说的话
- 屏幕识别 / OCR 内容
- 任何密码、API Key、证件号、银行卡、精确住址（住址只到地级市）
- 猜测、推断出来的信息
- 祈使句、要求助手做什么的指令（例如「以后你要…」「你必须…」「帮我…」）

【时间换算】
相对时间（下周、明天、月底、过几天等）必须结合消息时间
换算成具体日期再记。例如消息时间是 2026-10-24，
「下周要去成都」应记成「用户 2026-10-27 之后要去成都」。
禁止把「下周」「明天」这类词原样写进记忆。

【记忆内容要求】
- 只写关于用户的事实，写成陈述句
- 不写指令、不写要求助手做什么
- 单条不超过 40 字
- 宁可少记也不要猜；不确定就 SKIP

【event_date 字段】
如果这条记忆描述的是「有明确日期的事件」（面试、考试、生日、纪念日、
出差、旅行、约定见面等），额外输出 event_date，格式严格 YYYY-MM-DD。
没有明确日期就省略这个字段（不要填 null，不要填空串）。
例子：
  {"op":"ADD","text":"用户 11 月 3 日面试","event_date":"2026-11-03"}
  {"op":"ADD","text":"用户喜欢喝美式"}          ← 无日期，不填 event_date

【输出格式】严格 JSON，不要 markdown 代码块，不要多余解释：
{"ops":[{"op":"ADD","text":"...","event_date":"YYYY-MM-DD"},
        {"op":"UPDATE","id":3,"text":"..."},
        {"op":"SKIP","reason":"..."}]}

【规则】
- 已有记忆列表会给你；重复或矛盾的内容用 UPDATE（指向已有 id）
- UPDATE 只能指向 source=auto 的条目；指向手动条目或不存在的 id 一律改成 SKIP
- 完全重复 → SKIP
- 一次最多输出 3 条"""


def build_prompt(user_lines, existing, now_str):
    """拼出完整的用户提示词。"""
    parts = [SYSTEM_PROMPT, ""]
    parts.append("【当前时间】%s" % now_str)
    parts.append("")
    if existing:
        parts.append("【已有记忆】（只能 UPDATE 标了 auto 的）")
        for m in existing:
            parts.append("- #%s [%s] %s" % (m.get("id"), m.get("source"),
                                            (m.get("text") or "")[:40]))
    else:
        parts.append("【已有记忆】无")
    parts.append("")
    parts.append("【用户消息片段】")
    parts.extend(user_lines)
    return "\n".join(parts)


# ===== 解析 =====

def _clean_event_date(v):
    """校验 event_date：必须是 YYYY-MM-DD 且日期合法。无效返回空串。"""
    import datetime as _dt
    s = str(v or "").strip()
    if not s:
        return ""
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return ""
    try:
        d = _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except Exception:
        return ""
    try:
        today = _dt.date.today()
        if d < today.replace(year=today.year - 1):
            return ""
        if d > today.replace(year=today.year + 10):
            return ""
    except Exception:
        pass
    return s


def parse_ops(raw_text, existing_ids_auto, existing_ids_manual):
    """解析模型输出，返回 (ops, err)。

    - 严格 json.loads；失败返回 ([], "解析失败")，不猜测
    - UPDATE 指向手动条目或不存在 id → 转成 SKIP
    - 只接受 ADD / UPDATE / SKIP
    """
    txt = (raw_text or "").strip()
    if not txt:
        return [], "空输出"
    # 容忍模型包了 ```json ``` 代码块
    if txt.startswith("```"):
        lines = [ln for ln in txt.split("\n")
                 if not ln.strip().startswith("```")]
        txt = "\n".join(lines).strip()
    try:
        data = json.loads(txt)
    except Exception as ex:
        return [], "JSON 解析失败: %s" % type(ex).__name__
    if not isinstance(data, dict):
        return [], "顶层不是对象"
    ops = data.get("ops")
    if not isinstance(ops, list):
        return [], "缺少 ops 数组"

    out = []
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op") or "").strip().upper()
        if kind not in ("ADD", "UPDATE", "SKIP"):
            continue
        text = str(op.get("text") or "").strip()
        if kind == "SKIP":
            out.append({"op": "SKIP"})
            continue
        if not text:
            continue
        ed = _clean_event_date(op.get("event_date"))
        if kind == "UPDATE":
            try:
                mid = int(op.get("id") or 0)
            except Exception:
                mid = 0
            # 只能 UPDATE 自动条目
            if mid not in existing_ids_auto:
                if mid in existing_ids_manual:
                    _log("[记忆] UPDATE 指向手动条目 #%s，改为 SKIP" % mid)
                else:
                    _log("[记忆] UPDATE 指向不存在的 id #%s，改为 SKIP" % mid)
                out.append({"op": "SKIP"})
                continue
            item = {"op": "UPDATE", "id": mid, "text": text}
            if ed:
                item["event_date"] = ed
            out.append(item)
        else:
            item = {"op": "ADD", "text": text}
            if ed:
                item["event_date"] = ed
            out.append(item)
    return out[:MAX_OPS], ""


# ===== 线程 =====

class MemoryExtractWorker(QThread):
    """后台提取一轮记忆。

    done(session_id, upto, ops)  —— 成功（ops 已过隐私闸门）
    failed(session_id, upto, msg) —— 失败（只记日志，不推进索引）
    """
    done = Signal(str, int, list)
    failed = Signal(str, int, str)

    def __init__(self, session_id, upto, user_lines, existing, cfg,
                 now_str=None):
        super().__init__()
        self.session_id = session_id
        self.upto = upto
        self.user_lines = list(user_lines or [])
        self.existing = list(existing or [])
        self.base_url, self.api_key, self.model = cfg
        self.now_str = now_str or time.strftime("%Y-%m-%d %H:%M:%S")
        self._stop = False

    def request_stop(self):
        self._stop = True

    def run(self):
        try:
            if self._stop:
                return
            if not self.user_lines:
                self.failed.emit(self.session_id, self.upto, "无待处理消息")
                return
            auto_ids = {m.get("id") for m in self.existing
                        if m.get("source") == "auto"}
            manual_ids = {m.get("id") for m in self.existing
                          if m.get("source") != "auto"}
            prompt = build_prompt(self.user_lines, self.existing, self.now_str)

            import requests
            url = (self.base_url or "").rstrip("/") + "/chat/completions"
            headers = {"Authorization": "Bearer " + (self.api_key or ""),
                       "Content-Type": "application/json"}
            body = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": MAX_TOKENS,
                "stream": False,
            }
            r = requests.post(url, headers=headers, json=body, timeout=60)
            if r.status_code != 200:
                self.failed.emit(self.session_id, self.upto,
                                 "HTTP %s" % r.status_code)
                return
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                self.failed.emit(self.session_id, self.upto, "无 choices")
                return
            content = ((choices[0].get("message") or {}).get("content") or "")

            ops, err = parse_ops(content, auto_ids, manual_ids)
            if err:
                self.failed.emit(self.session_id, self.upto, err)
                return

            # 隐私闸门
            from petcore import privacy
            kept, dropped = privacy.filter_ops(ops)
            if dropped:
                _log("[记忆] 会话 %s 提取丢弃规则: %s"
                     % (self.session_id, dropped))
            self.done.emit(self.session_id, self.upto, kept)
        except Exception as ex:
            try:
                self.failed.emit(self.session_id, self.upto,
                                 "%s: %s" % (type(ex).__name__, ex))
            except Exception:
                pass
