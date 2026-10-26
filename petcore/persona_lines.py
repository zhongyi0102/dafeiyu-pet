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

CACHE_NAME = "persona_lines_cache.json"
LINES_PER_POOL = 8          # 生成时每池固定 8 句
BATCH_POOLS = 6             # 每批生成几个池
MIN_LEN = 4                 # 单句最短
MAX_LEN = 40                # 单句最长

# 需要生成的池及占位符要求（与 consts 的池名一致）
POOL_SPECS = {
    "WATER_LINES": (),
    "STAND_LINES": (),
    "EYES_LINES": (),
    "SLEEP_LINES": (),
    "MEAL_LINES": (),
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
    "SLEEP_LINES": "深夜催睡",
    "MEAL_LINES": "饭点提醒",
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
    "AFFECTION_COLD_REACT": "好感度低-单击回应（冷淡）",
    "AFFECTION_COLD_POKE": "好感度低-被戳（冷淡）",
    "AFFECTION_COLD_STROKE": "好感度低-被摸头（冷淡）",
    "AFFECTION_NORMAL_REACT": "好感度中-单击回应",
    "AFFECTION_NORMAL_POKE": "好感度中-被戳",
    "AFFECTION_NORMAL_STROKE": "好感度中-被摸头",
    "AFFECTION_CLINGY_REACT": "好感度高-单击回应（亲近）",
    "AFFECTION_CLINGY_POKE": "好感度高-被戳（亲近）",
    "AFFECTION_CLINGY_STROKE": "好感度高-被摸头（亲近）",
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
    "- 台词要能独立使用，不依赖上下文\n"
    "- 同一池内的句子不要重复、不要高度相似\n"
    "- 语气贴合人设，不要超出人设范围自行发挥"
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


def save_persona_pools(app_dir, persona, pools):
    """把某个人设的池写进缓存（保留其他人设的条目）。"""
    h = persona_hash(persona)
    d = load_cache(app_dir)
    d.setdefault("personas", {})
    d["personas"][h] = {
        "persona": (persona or "")[:500],
        "pools": pools,
        "time": _now(),
    }
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


def generate_all(persona, chat_cfg, app_dir, on_progress=None):
    """按人设生成全部池，成功后写缓存。

    chat_cfg: (base_url, api_key, model)
    on_progress: callable(done, total, message) 可选

    返回 (ok, 消息, 池数量)。失败不清旧缓存。
    """
    p = (persona or "").strip()
    if not p:
        return False, "人设为空，无需生成", 0
    base, key, model = chat_cfg or ("", "", "")
    if not (base and key and model):
        return False, "未配置聊天 Key/模型，无法生成", 0

    import requests
    names = list(POOL_SPECS.keys())
    total = len(names)
    batches = [names[i:i + BATCH_POOLS] for i in range(0, total, BATCH_POOLS)]
    got = {}
    done = 0

    for bi, batch in enumerate(batches):
        if on_progress:
            try:
                on_progress(done, total, "正在生成第 %d/%d 批…"
                            % (bi + 1, len(batches)))
            except Exception:
                pass
        prompt = GEN_PROMPT.format(
            persona=p[:2000], n=LINES_PER_POOL,
            min_len=MIN_LEN, max_len=MAX_LEN,
            pool_specs=_build_pool_specs(batch))
        try:
            r = requests.post(
                (base or "").rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json"},
                json={"model": model,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.8, "max_tokens": 2000},
                timeout=90)
            if r.status_code != 200:
                return False, "第 %d 批 HTTP %s" % (bi + 1, r.status_code), len(got)
            text = ((r.json().get("choices") or [{}])[0]
                    .get("message", {}).get("content") or "")
        except Exception as ex:
            return False, "第 %d 批请求失败: %s" % (bi + 1, ex), len(got)

        pools = _parse_pools(text)
        if not pools:
            return False, "第 %d 批输出无法解析" % (bi + 1), len(got)

        for name in batch:
            raw_lines = pools.get(name)
            if not raw_lines:
                continue
            ok, _why, clean = validate_pool(name, raw_lines,
                                            min_count=LINES_PER_POOL)
            if ok:
                got[name] = clean
                done += 1
        if on_progress:
            try:
                on_progress(done, total, "已生成 %d/%d 个池" % (done, total))
            except Exception:
                pass

    if not got:
        return False, "没有生成出任何合格台词池", 0

    # 只保存合格的池；旧缓存整体保留
    save_persona_pools(app_dir, p, got)
    return True, "已按人设生成 %d 个台词池" % len(got), len(got)


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
