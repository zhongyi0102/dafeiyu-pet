# -*- coding: utf-8 -*-
r"""persona_lines.py — 按人设生成固定台词池

流程：
  1. 保存人设时调用 generate_all()，分批请求模型生成全部池
  2. 校验每池（句数、长度、无括号动作、占位符完整）
  3. 通过后写 persona_lines_cache.json（以人设哈希为 key）
  4. 运行时 get_pool() 从缓存取，缓存缺失/校验失败回落中性池

关键约束：
  - 生成失败绝不清掉旧缓存
  - 新人设生成完成前旧缓存保持可用
  - 运行时零模型调用
"""
import hashlib
import json
import logging
import os
import re

_logger = logging.getLogger("dafeiyu")

# 新缓存文件名（旧 persona_lines_cache.json 不再使用，启动时自动忽略）
CACHE_NAME = "persona_lines.json"
LINES_PER_POOL = 8          # 每池目标句数（6~10 之间）
LINES_MIN_PER_POOL = 6      # 低于这个句数算该池失败
LINES_MAX_PER_POOL = 10
BATCH_POOLS = 10            # 每批生成几个池（池多时拆成 2~4 次请求）
MIN_LEN = 4                 # 单句最短
MAX_LEN = 20                # 单句最长（本次要求 ≤20 字）
REQUEST_TIMEOUT = 20        # 单次请求超时（秒）
REQUEST_RETRY = 1           # 失败重试次数

# 需要生成的池及占位符要求（与 consts 的池名一致）
POOL_SPECS = {
    "WATER_LINES": (),
    "STAND_LINES": (),
    "EYES_LINES": (),
    "TODO_LINES": ("{content}",),
    "REVISIT_TODAY_LINES": ("{what}",),
    "REVISIT_YESTERDAY_LINES": ("{what}",),
    "COVER_DONE_LINES": ("{song}",),
    "COVER_FAIL_LINES": (),
    "COVER_BUSY_LINES": (),
    "INNER_LINES": (),
    "POKE_LINES": (),
    "STROKE_LINES": (),
    "PAT_LINES": (),
    "DRAG_LINES": (),
    "BIRTHDAY_LINES": (),
    "SLEEP_ZZZ_LINES": (),
    "REMIND_SNOOZE_LINES": (),
    "RPS_WIN_LINES": (),
    "RPS_LOSE_LINES": (),
    "RPS_TIE_LINES": (),
    "CATCH_SCORE_LINES": ("{n}",),
    "LINES": (),
    "CHAT_EMPTY_GREETINGS": (),
    # 新增两个兜底池：模型请求失败/超时时用（搭话 / 回来打招呼）
    "PROACTIVE_FALLBACK_LINES": (),
    "WELCOME_FALLBACK_LINES": (),
    # 视觉工具结果卡片上方的开场白
    "TOOL_RESULT_OPENERS": (),
    # 好感度三档 × 三类 = 9 池
    "AFFECTION_COLD_REACT": (),
    "AFFECTION_COLD_POKE": (),
    "AFFECTION_COLD_STROKE": (),
    "AFFECTION_NORMAL_REACT": (),
    "AFFECTION_NORMAL_POKE": (),
    "AFFECTION_NORMAL_STROKE": (),
    "AFFECTION_CLINGY_REACT": (),
    "AFFECTION_CLINGY_POKE": (),
    "AFFECTION_CLINGY_STROKE": (),
}

POOL_HINTS = {
    "WATER_LINES": "喝水提醒",
    "STAND_LINES": "起身活动提醒",
    "EYES_LINES": "护眼提醒",
    "TODO_LINES": "待办提醒",
    "REVISIT_TODAY_LINES": "记忆回访-事件当天",
    "REVISIT_YESTERDAY_LINES": "记忆回访-事件次日",
    "COVER_DONE_LINES": "翻唱完成",
    "COVER_FAIL_LINES": "翻唱失败",
    "COVER_BUSY_LINES": "翻唱进行中又来请求",
    "INNER_LINES": "待机碎碎念",
    "POKE_LINES": "被戳时的反应",
    "STROKE_LINES": "被摸头时的反应",
    "PAT_LINES": "被拍时的反应",
    "DRAG_LINES": "被拖拽时的反应",
    "BIRTHDAY_LINES": "生日祝福",
    "SLEEP_ZZZ_LINES": "睡着时的拟声",
    "REMIND_SNOOZE_LINES": "点稍后再提醒后的回话",
    "RPS_WIN_LINES": "猜拳赢了",
    "RPS_LOSE_LINES": "猜拳输了",
    "RPS_TIE_LINES": "猜拳平局",
    "CATCH_SCORE_LINES": "接食物小游戏得分",
    "LINES": "待机/开场白",
    "CHAT_EMPTY_GREETINGS": "空聊天窗口的问候语",
    "AFFECTION_COLD_REACT": "好感度低-单击回应（冷淡）",
    "AFFECTION_COLD_POKE": "好感度低-被戳（冷淡）",
    "AFFECTION_COLD_STROKE": "好感度低-被摸头（冷淡）",
    "AFFECTION_NORMAL_REACT": "好感度中-单击回应",
    "AFFECTION_NORMAL_POKE": "好感度中-被戳",
    "AFFECTION_NORMAL_STROKE": "好感度中-被摸头",
    "AFFECTION_CLINGY_REACT": "好感度高-单击回应（亲近）",
    "AFFECTION_CLINGY_POKE": "好感度高-被戳（亲近）",
    "AFFECTION_CLINGY_STROKE": "好感度高-被摸头（亲近）",
    # 兜底池：模型请求失败/超时时用，同样受人设控制
    "PROACTIVE_FALLBACK_LINES": "主动搭话失败时的兜底一句",
    "WELCOME_FALLBACK_LINES": "回来打招呼失败时的兜底一句",
    # 视觉工具结果卡片上方的开场白
    "TOOL_RESULT_OPENERS": "视觉工具结果卡片上方的开场一句",
}

GEN_PROMPT = (
    "你是台词写手。根据下面的人设，为桌宠的固定场景写台词。\n"
    "\n"
    "【人设】\n"
    "{persona}\n"
    "\n"
    "【输出要求】\n"
    "严格输出 JSON，不要 markdown 代码块，不要多余解释：\n"
    '{{"pools": {{"池名": ["台词1", "台词2"], ...}}}}\n'
    "\n"
    "【每句要求】\n"
    "- 数量：每个池恰好 {n} 句\n"
    "- 长度：{min_len}~{max_len} 字\n"
    "- 口语化，符合人设语气\n"
    "- 不要括号动作（不要用圆括号包动作描写）\n"
    "- 不要颜文字、不要 emoji、不要换行\n"
    "- 称呼用户的方式以人设为准；人设没写就用「你」\n"
    "- 必须原样保留占位符，不要改写\n"
    "\n"
    "【本次要生成的池】\n"
    "{pool_specs}\n"
    "\n"
    "【注意】\n"
    "- 完全按人设说话：语气、称呼、用词、态度都以人设为准\n"
    "- 台词要能独立使用，不依赖上下文\n"
    "- 同一池内的句子不要重复、不要高度相似\n"
    "- 只输出 JSON，不解释、不加任何说明文字"
)


def persona_hash(persona):
    """人设文本的稳定哈希（用于缓存 key）。"""
    p = (persona or "").strip()
    return hashlib.sha256(p.encode("utf-8")).hexdigest()[:16]


def cache_path(app_dir):
    return os.path.join(app_dir, CACHE_NAME)


def load_cache(app_dir):
    """读整个缓存文件；损坏返回空结构。"""
    try:
        p = cache_path(app_dir)
        if not os.path.exists(p):
            return {"version": 1, "personas": {}}
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("personas"), dict):
            return {"version": 1, "personas": {}}
        return d
    except Exception as ex:
        _logger.warning("[台词] 读缓存失败: %s: %s", type(ex).__name__, ex)
        return {"version": 1, "personas": {}}


def save_cache(app_dir, data):
    """原子写缓存。"""
    try:
        p = cache_path(app_dir)
        d = os.path.dirname(os.path.abspath(p))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except Exception as ex:
        _logger.error("[台词] 写缓存失败: %s: %s", type(ex).__name__, ex)
        return False


def get_persona_pools(app_dir, persona):
    """取该人设的缓存池；没有返回空 dict。"""
    h = persona_hash(persona)
    d = load_cache(app_dir)
    ent = (d.get("personas") or {}).get(h)
    if not isinstance(ent, dict):
        return {}
    pools = ent.get("pools")
    return pools if isinstance(pools, dict) else {}


def save_persona_pools(app_dir, persona, pools, failed_pools=None,
                       total=0, done=0):
    """把某个人设的池写进缓存（保留其他人设的条目）。

    新结构：
      {"version": 2,
       "persona_hash": "...", "generated_at": "...",
       "pools": {...},
       "last_result": {"status","time","reason","failed_pools"}}
    """
    h = persona_hash(persona)
    d = load_cache(app_dir)
    d["version"] = 2
    d.setdefault("personas", {})
    ent = d["personas"].get(h)
    if not isinstance(ent, dict):
        ent = {}
    merged = dict(ent.get("pools") or {})
    merged.update(pools or {})
    failed = list(failed_pools or [])
    # 成功生成的池：从失败列表里移除（fail_count 视为清零）
    ok_names = set((pools or {}).keys())
    if ok_names:
        failed = [f for f in failed
                  if not (isinstance(f, dict) and f.get("pool") in ok_names)]
    if failed and merged:
        status = "partial"
    elif merged:
        status = "ok"
    else:
        status = "fail"
    ent.update({
        "persona": (persona or "")[:500],
        "persona_hash": h,
        "generated_at": _now(),
        "time": _now(),
        "pools": merged,
        "last_result": {
            "status": status,
            "time": _now(),
            "reason": ("%d 个池使用默认台词" % len(failed)) if failed else "",
            "failed_pools": failed,
            "total": int(total or 0),
            "done": int(done or 0),
        },
    })
    d["personas"][h] = ent
    # 顶层冗余字段（便于直接读，不必先算哈希）
    d["persona_hash"] = h
    d["generated_at"] = ent["generated_at"]
    d["pools"] = merged
    d["last_result"] = ent["last_result"]
    return save_cache(app_dir, d)


def load_last_result(app_dir, persona):
    """读该人设的最近一次生成结果（重新打开设置窗口时恢复状态行）。"""
    h = persona_hash(persona)
    d = load_cache(app_dir)
    ent = (d.get("personas") or {}).get(h)
    if not isinstance(ent, dict):
        return None
    lr = ent.get("last_result")
    return lr if isinstance(lr, dict) else None


def save_pool_manually(app_dir, persona, name, lines):
    """手动为某个池保存台词：视为已生成，fail_count 清零、从失败列表移除。"""
    try:
        h = persona_hash(persona)
        d = load_cache(app_dir)
        ent = (d.get("personas") or {}).get(h)
        if not isinstance(ent, dict):
            ent = {"persona": (persona or "")[:500], "pools": {},
                   "last_result": {}}
        pools = dict(ent.get("pools") or {})
        pools[name] = list(lines)
        lr = dict(ent.get("last_result") or {})
        failed = [f for f in (lr.get("failed_pools") or [])
                  if not (isinstance(f, dict) and f.get("pool") == name)]
        lr["failed_pools"] = failed
        lr["status"] = "partial" if failed else "ok"
        lr["reason"] = ("%d 个池使用默认台词" % len(failed)) if failed else ""
        lr["time"] = _now()
        lr["done"] = len(pools)
        lr.setdefault("total", len(pools) + len(failed))
        ent["pools"] = pools
        ent["last_result"] = lr
        d["personas"] = d.get("personas") or {}
        d["personas"][h] = ent
        d["persona_hash"] = h
        d["pools"] = pools
        d["last_result"] = lr
        return save_cache(app_dir, d)
    except Exception as ex:
        _logger.warning("[台词] 手动保存池失败: %s: %s",
                        type(ex).__name__, ex)
        return False


def clear_persona_pools(app_dir, persona):
    """清掉某人设的台词缓存（「恢复默认台词」用，回落中性池）。"""
    h = persona_hash(persona)
    d = load_cache(app_dir)
    ent = (d.get("personas") or {}).get(h)
    if isinstance(ent, dict):
        ent["pools"] = {}
        ent["last_result"] = {
            "status": "cleared", "time": _now(),
            "reason": "已恢复默认台词", "failed_pools": []}
        d["personas"][h] = ent
        d["last_result"] = ent["last_result"]
        d["pools"] = {}
    return save_cache(app_dir, d)


def _now():
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S")


# ===== 校验 =====

_BRACKET_RE = re.compile(u"[\uff08(][^\uff09)]{0,20}[\uff09)]")
_EMOJI_RE = re.compile(
    u"[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF]")


def validate_line(line, placeholders=()):
    """校验单句。返回 (ok, 原因)。"""
    s = str(line or "").strip()
    if not s:
        return False, "空"
    if "\n" in s or "\r" in s:
        return False, "含换行"
    if len(s) < MIN_LEN:
        return False, "太短"
    if len(s) > MAX_LEN:
        return False, "太长"
    if _BRACKET_RE.search(s):
        return False, "含括号动作"
    if _EMOJI_RE.search(s):
        return False, "含 emoji"
    for ph in placeholders:
        if ph not in s:
            return False, "缺占位符 %s" % ph
    return True, ""


def validate_pool(name, lines, min_count=None):
    """校验一个池。返回 (ok, 原因, 干净列表)。"""
    need = min_count if min_count is not None else 1
    if not isinstance(lines, (list, tuple)):
        return False, "不是数组", []
    phs = POOL_SPECS.get(name, ())
    clean = []
    seen = set()
    for ln in lines:
        ok, _why = validate_line(ln, phs)
        if not ok:
            continue
        s = str(ln).strip()
        if s in seen:
            continue
        seen.add(s)
        clean.append(s)
    if len(clean) < need:
        return False, "有效句数 %d < %d" % (len(clean), need), clean
    return True, "", clean


# ===== 生成 =====

def _build_pool_specs(names):
    parts = []
    for n in names:
        hint = POOL_HINTS.get(n, n)
        phs = POOL_SPECS.get(n, ())
        ph_txt = ("  占位符必须包含：" + " ".join(phs)) if phs else ""
        parts.append("- %s：%s%s" % (n, hint, ph_txt))
    return "\n".join(parts)


# ===== 失败原因 =====

REASON_TEXT = {
    "not_returned": "模型没有返回这个池",
    "batch_parse_failed": "模型返回格式不对（该批次 {n} 个池都受影响）",
    "too_few": "句数不足（只有 {got} 句，至少需要 {need} 句）",
    "placeholder_broken": "占位符 {ph} 被模型改动或丢失",
    "too_long": "句子太长（超过 {max} 字的已丢弃，剩 {left} 句）",
    "timeout": "请求超时（该批次）",
    "auth_rejected": "模型服务拒绝请求，请检查 Key 或余额",
}

# fail_count 达到这个值时追加手动编辑建议
FAIL_HINT_THRESHOLD = 2


def pool_label(name):
    """池的中文名（与「查看/编辑台词」窗口一致）。"""
    return POOL_HINTS.get(name, name)


def reason_text(code, detail=None):
    """把 reason_code + detail 拼成界面文案。"""
    tpl = REASON_TEXT.get(code or "", "生成失败")
    d = detail if isinstance(detail, dict) else {}
    try:
        return tpl.format(**d)
    except Exception:
        return tpl


def format_failure(f):
    """一个失败项的完整文案：原因 + （连续失败建议）。"""
    if not isinstance(f, dict):
        return str(f or "")
    txt = reason_text(f.get("reason_code"), f.get("detail"))
    if int(f.get("fail_count") or 0) >= FAIL_HINT_THRESHOLD:
        txt += "；建议手动编辑，或简化人设后重新生成"
    return txt


def _parse_pools(text):
    """从模型输出解析 pools。失败返回 {}。"""
    t = (text or "").strip()
    if not t:
        return {}
    if "```" in t:
        for part in t.split("```"):
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    try:
        d = json.loads(t)
    except Exception:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            return {}
        try:
            d = json.loads(t[i:j + 1])
        except Exception:
            return {}
    if not isinstance(d, dict):
        return {}
    pools = d.get("pools")
    return pools if isinstance(pools, dict) else {}


def generate_all(persona, chat_cfg, app_dir, on_progress=None,
                 only_pools=None, cancel_check=None):
    """按人设生成台词池，成功后写缓存。失败绝不清旧缓存。

    persona     : 人设文本
    chat_cfg    : (base_url, api_key, model)
    on_progress : callable(done, total, message) 可选
    only_pools  : 只生成这些池（「重试失败项」用）；None = 全部
    cancel_check: callable() -> bool，返回 True 表示已被取消（人设又保存了）

    返回 dict:
      {"ok": bool, "status": "ok"|"partial"|"fail"|"empty"|"nokey",
       "reason": str, "total": N, "done": x, "failed_pools": [...],
       "pools": {name: [lines]}}
    """
    p = (persona or "").strip()
    if not p:
        return {"ok": False, "status": "empty", "reason": "人设为空",
                "total": 0, "done": 0, "failed_pools": [], "pools": {}}
    base, key, model = chat_cfg or ("", "", "")
    if not (base and key and model):
        return {"ok": False, "status": "nokey",
                "reason": "未配置聊天模型 Key，请先到「聊天模型」填写",
                "total": 0, "done": 0, "failed_pools": [], "pools": {}}

    import requests
    names = list(only_pools) if only_pools else list(POOL_SPECS.keys())
    names = [n for n in names if n in POOL_SPECS]
    total = len(names)
    if not total:
        return {"ok": False, "status": "fail", "reason": "没有要生成的池",
                "total": 0, "done": 0, "failed_pools": [], "pools": {}}

    batches = [names[i:i + BATCH_POOLS] for i in range(0, total, BATCH_POOLS)]
    got = {}
    failed = []          # [{pool, reason_code, detail, fail_count}]
    failed_map = {}      # pool -> 失败项（便于累加 fail_count）
    done = 0
    last_err = ""

    def _cancelled():
        try:
            return bool(cancel_check and cancel_check())
        except Exception:
            return False

    def _mark_fail(pool, code, detail=None):
        """记录一个池的失败（同一池多次失败只保留最后一条）。"""
        failed_map[pool] = {"pool": pool, "reason_code": code,
                            "detail": detail or {}}

    def _request(batch):
        """发一次请求，返回 (text, err_code, err_detail)。失败重试 1 次。"""
        prompt = GEN_PROMPT.format(
            persona=p[:2000], n=LINES_PER_POOL,
            min_len=MIN_LEN, max_len=MAX_LEN,
            pool_specs=_build_pool_specs(batch))
        last_code, last_detail = "timeout", {}
        for attempt in range(REQUEST_RETRY + 1):
            try:
                r = requests.post(
                    (base or "").rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + key,
                             "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.8, "max_tokens": 2000},
                    timeout=REQUEST_TIMEOUT)
                if r.status_code != 200:
                    if r.status_code in (401, 402, 403):
                        return "", "auth_rejected", {}
                    last_code = "batch_parse_failed"
                    last_detail = {"n": len(batch), "http": r.status_code}
                    if 400 <= r.status_code < 500:
                        return "", last_code, last_detail
                    continue
                text = ((r.json().get("choices") or [{}])[0]
                        .get("message", {}).get("content") or "")
                return text, "", {}
            except Exception as ex:
                nm = type(ex).__name__.lower()
                if "timeout" in nm:
                    last_code, last_detail = "timeout", {"batch": len(batch)}
                else:
                    last_code = "batch_parse_failed"
                    last_detail = {"n": len(batch),
                                   "err": type(ex).__name__}
                continue
        return "", last_code, last_detail

    for bi, batch in enumerate(batches):
        if _cancelled():
            return {"ok": False, "status": "fail", "reason": "已被新的保存取消",
                    "total": total, "done": done, "failed_pools": failed,
                    "pools": got}
        if on_progress:
            try:
                on_progress(done, total, "正在生成第 %d/%d 批…"
                            % (bi + 1, len(batches)))
            except Exception:
                pass
        text, err_code, err_detail = _request(batch)
        if err_code:
            last_err = err_code
            for name in batch:
                _mark_fail(name, err_code, err_detail)
            continue

        pools = _parse_pools(text)
        if not pools:
            # 整批解析失败 ≠ 整批都该死。降级为逐池单独请求，
            # 谁能救回来就救谁，救不回的才标 batch_parse_failed。
            raw_head = re.sub(r"\s+", " ", (text or ""))[:200]
            _log_pool_failures(batch, "batch_parse_failed",
                               {"n": len(batch)}, raw_head)
            if on_progress:
                try:
                    on_progress(done, total, "整批解析失败，改为逐个重试…")
                except Exception:
                    pass
            for name in batch:
                if _cancelled():
                    _mark_fail(name, "batch_parse_failed", {"n": 1})
                    continue
                single = [name]
                stext, serr, _sd = _request(single)
                if serr or not stext:
                    _mark_fail(name, serr or "batch_parse_failed", {"n": 1})
                    continue
                spools = _parse_pools(stext)
                raw_lines = spools.get(name)
                if not raw_lines:
                    _mark_fail(name, "not_returned", {})
                    continue
                code, detail, clean = _diagnose_pool(name, raw_lines)
                if code:
                    _mark_fail(name, code, detail)
                    _log_pool_failures([name], code, detail, "")
                else:
                    got[name] = clean
                    done += 1
                if on_progress:
                    try:
                        on_progress(done, total, "已生成 %d/%d 个池"
                                    % (done, total))
                    except Exception:
                        pass
            continue

        raw_head = re.sub(r"\s+", " ", (text or ""))[:200]
        for name in batch:
            raw_lines = pools.get(name)
            if not raw_lines:
                _mark_fail(name, "not_returned", {})
                _log_pool_failures([name], "not_returned", {}, raw_head)
                continue
            code, detail, clean = _diagnose_pool(name, raw_lines)
            if code:
                _mark_fail(name, code, detail)
                _log_pool_failures([name], code, detail, raw_head)
            else:
                got[name] = clean
                done += 1
        if on_progress:
            try:
                on_progress(done, total, "已生成 %d/%d 个池" % (done, total))
            except Exception:
                pass

    # 累计 fail_count（连续失败次数）
    prev = _load_fail_counts(app_dir, p)
    for name, f in failed_map.items():
        f["fail_count"] = int(prev.get(name) or 0) + 1
    failed = [failed_map[k] for k in failed_map]

    if not got:
        # 全部失败：不清旧缓存
        status = "nokey" if last_err == "auth_rejected" else "fail"
        reason = (reason_text("auth_rejected") if last_err == "auth_rejected"
                  else _reason_text(last_err))
        # 仍要把失败明细持久化，便于「查看原因」
        save_persona_pools(app_dir, p, {}, failed_pools=failed,
                           total=total, done=0)
        return {"ok": False, "status": status, "reason": reason,
                "total": total, "done": 0, "failed_pools": failed,
                "pools": {}}

    # 合并进该人设的缓存（只覆盖这次成功的池，其余保留）
    save_persona_pools(app_dir, p, got, failed_pools=failed,
                       total=total, done=done)

    if failed:
        return {"ok": True, "status": "partial",
                "reason": "%d 个池使用默认台词" % len(failed),
                "total": total, "done": done, "failed_pools": failed,
                "pools": got}
    return {"ok": True, "status": "ok", "reason": "",
            "total": total, "done": done, "failed_pools": [], "pools": got}


def _diagnose_pool(name, raw_lines):
    """诊断一个池为什么不合格。返回 (reason_code, detail, clean)。

    code 为空表示合格。
    """
    if not isinstance(raw_lines, (list, tuple)):
        return "not_returned", {}, []
    phs = POOL_SPECS.get(name, ())
    clean = []
    seen = set()
    dropped_long = 0
    dropped_ph = []
    for ln in raw_lines:
        s = str(ln or "").strip()
        if not s:
            continue
        # 占位符被改动/丢失
        missing = [p for p in phs if p not in s]
        if missing:
            dropped_ph.extend(missing)
            continue
        if len(s) > MAX_LEN:
            dropped_long += 1
            continue
        ok, _why = validate_line(s, phs)
        if not ok:
            continue
        if s in seen:
            continue
        seen.add(s)
        clean.append(s)

    if len(clean) >= LINES_MIN_PER_POOL:
        return "", {}, clean
    # 优先级：占位符 > 超长 > 句数不足
    if dropped_ph:
        return ("placeholder_broken",
                {"ph": dropped_ph[0]}, clean)
    if dropped_long:
        return ("too_long",
                {"max": MAX_LEN, "left": len(clean)}, clean)
    return ("too_few",
            {"got": len(clean), "need": LINES_MIN_PER_POOL}, clean)


def _load_fail_counts(app_dir, persona):
    """读该人设上一轮各池的 fail_count。"""
    try:
        h = persona_hash(persona)
        d = load_cache(app_dir)
        ent = (d.get("personas") or {}).get(h)
        if not isinstance(ent, dict):
            return {}
        lr = ent.get("last_result") or {}
        out = {}
        for f in (lr.get("failed_pools") or []):
            if isinstance(f, dict) and f.get("pool"):
                out[f["pool"]] = int(f.get("fail_count") or 0)
        return out
    except Exception:
        return {}


def _log_pool_failures(pools, code, detail, raw_head=""):
    """每个失败池写一行日志（不含 Key）。

    原始返回里可能恰好回显了 Key（模型复述 prompt 时），这里统一脱敏。
    """
    try:
        d = detail if isinstance(detail, dict) else {}
        head = _mask_secrets((raw_head or "")[:200])
        for pn in pools:
            _logger.warning("[台词] 池失败 pool=%s reason=%s detail=%s raw=%r",
                            pn, code, d, head)
    except Exception:
        pass


_KEY_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}"
    r"|[0-9a-fA-F]{32}\.[A-Za-z0-9]{16,48}"
    r"|(?i:bearer)\s+[A-Za-z0-9._\-]{8,})")


def _mask_secrets(text):
    """把疑似 Key 的片段替换成 ***。"""
    try:
        return _KEY_RE.sub("***", str(text or ""))
    except Exception:
        return ""


def _reason_text(err):
    """把底层错误码翻成界面文案。"""
    e = (err or "").lower()
    if not e:
        return "生成失败，已使用默认台词"
    if e in ("timeout",):
        return "网络超时，已使用默认台词，可稍后重试"
    if e in ("auth_rejected",) or any(
            x in e for x in ("401", "402", "403", "unauthorized",
                             "invalid api key", "insufficient")):
        return "模型服务拒绝请求，请检查 Key 或余额"
    if e in ("batch_parse_failed",) or "格式" in e or "解析" in e:
        return "模型返回格式不对，已使用默认台词"
    return "生成失败，已使用默认台词"


# ===== 实时生成一句（阶段③）=====

LIVE_PROMPT = (
    "根据下面的人设，为这个场景写一句话。\n"
    "\n"
    "【人设】\n"
    "{persona}\n"
    "\n"
    "【场景】{scene}\n"
    "{required}"
    "\n"
    "【要求】\n"
    "- 只输出这一句话，不要解释、不要引号、不要换行\n"
    "- 不超过 {max_len} 字\n"
    "- 不要括号动作、不要颜文字、不要 emoji\n"
    "- 称呼用户的方式以人设为准；人设没写就用「你」\n"
    "{placeholder_hint}"
)


def validate_live_line(text, required_info=""):
    """校验实时生成的一句。返回 (ok, 原因, 干净句子)。"""
    s = str(text or "").strip()
    # 去掉可能的引号包裹
    for q in ('"', "'", "\u201c", "\u201d", "\u300c", "\u300d"):
        if s.startswith(q) and s.endswith(q) and len(s) > 2:
            s = s[1:-1].strip()
    if not s:
        return False, "空", ""
    # 只取第一行
    s = s.split("\n")[0].strip()
    from petcore.consts import LIVE_GEN_MIN_LEN as _MIN, LIVE_GEN_MAX_LEN as _MAX
    if len(s) < _MIN:
        return False, "太短", ""
    if len(s) > _MAX:
        return False, "太长", ""
    if _BRACKET_RE.search(s):
        return False, "含括号动作", ""
    if _EMOJI_RE.search(s):
        return False, "含 emoji", ""
    if required_info and required_info not in s:
        return False, "缺必须信息", ""
    return True, "", s


def live_generate_one(kind, persona, chat_cfg, app_dir,
                      scene="", required_info="",
                      placeholder="", timeout=None):
    """现场生成一句台词。

    返回 (ok, 句子, 原因)。失败时句子为空，调用方回落固定台词。
    合格句子会追加进该人设的缓存池。
    """
    import time as _t
    from petcore.consts import (
        LIVE_GEN_TIMEOUT_SEC, LIVE_GEN_MAX_LEN, LIVE_GEN_MIN_LEN)

    p = (persona or "").strip()
    if not p:
        return False, "", "人设为空"
    base, key, model = chat_cfg or ("", "", "")
    if not (base and key and model):
        return False, "", "未配置聊天 Key"

    to = LIVE_GEN_TIMEOUT_SEC if timeout is None else float(timeout)
    prompt = LIVE_PROMPT.format(
        persona=p[:1500],
        scene=scene or (POOL_HINTS.get(kind) or kind),
        required=("【必须包含】%s\n" % required_info) if required_info else "",
        max_len=LIVE_GEN_MAX_LEN,
        placeholder_hint=(
            "【占位符】句子里必须出现 %s\n" % placeholder) if placeholder else "")

    import requests
    t0 = _t.time()
    try:
        r = requests.post(
            (base or "").rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.9, "max_tokens": 120},
            timeout=to)
    except Exception as ex:
        return False, "", "请求失败: %s" % type(ex).__name__
    if _t.time() - t0 > to:
        return False, "", "超时"
    if r.status_code != 200:
        return False, "", "HTTP %s" % r.status_code
    try:
        text = ((r.json().get("choices") or [{}])[0]
                .get("message", {}).get("content") or "")
    except Exception:
        return False, "", "响应解析失败"

    ok, why, clean = validate_live_line(text, required_info)
    if not ok:
        return False, "", why

    # 合格：顺手存进缓存池，下次复用
    try:
        pools = get_persona_pools(app_dir, p)
        pool = list(pools.get(kind) or [])
        if clean not in pool:
            pool.append(clean)
            # 只保留最近 20 句，避免无限增长
            pools[kind] = pool[-20:]
            save_persona_pools(app_dir, p, pools)
    except Exception:
        pass
    return True, clean, ""


# ===== 运行时取池 =====

def get_pool(name, persona, app_dir):
    """运行时取池：人设缓存 → 中性池。

    REACT_LINES 只在人设等于 EXAMPLE_PERSONA 时启用，否则返回空列表。
    """
    from petcore import consts as C

    p = (persona or "").strip()
    if name == "REACT_LINES":
        if p and p == C.EXAMPLE_PERSONA.strip():
            return list(C.REACT_LINES)
        return []

    if not p:
        return C.neutral_pool(name)

    pools = get_persona_pools(app_dir, p)
    lines = pools.get(name)
    if isinstance(lines, list) and lines:
        return list(lines)
    return C.neutral_pool(name)


def get_affection_pool(kind, persona, tier, app_dir):
    """取好感度台词池。

    人设为空 → 中性池（不分档）。
    有人设 → AFFECTION_{TIER}_{KIND} 缓存 → 中性池。
    """
    from petcore import consts as C

    p = (persona or "").strip()
    if not p:
        return list(C.NEUTRAL_AFFECTION_LINES.get(kind) or [])
    key = "AFFECTION_%s_%s" % (str(tier or "normal").upper(),
                               str(kind or "react").upper())
    pools = get_persona_pools(app_dir, p)
    lines = pools.get(key)
    if isinstance(lines, list) and lines:
        return list(lines)
    return list(C.NEUTRAL_AFFECTION_LINES.get(kind) or [])
