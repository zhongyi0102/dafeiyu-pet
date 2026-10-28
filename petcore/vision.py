# -*- coding: utf-8 -*-
"""看看屏幕 —— 视觉 Key 自动识别。

流程（全部在后台线程跑，不阻塞界面）：
  1. 按 Key 前缀匹配服务商；分不出来就依次试候选地址的 GET {base}/models
  2. 拿到该 Key 能访问的全部模型
  3. 剔除明显不是对话的（embed/rerank/tts/whisper...）
  4. 名字带 vision/vl/4v/omni/gemini/claude/qwen 的排前面
  5. 真实验证：发一张 32×32 PNG + 文字 "ok"，返回 200 才算可用
  6. 验证通过一个就立刻回调（边验证边出现）

本模块只依赖标准库 + requests + Pillow，不依赖 UI。
"""
import re
import logging
import base64
import io
import threading

import requests

# 32×32 纯色 PNG（代码生成，不读磁盘）
_TEST_PNG_CACHE = {}


def make_test_png(size=32):
    """生成一张 32×32 PNG（base64 data URL 用），带缓存。"""
    key = int(size)
    if key in _TEST_PNG_CACHE:
        return _TEST_PNG_CACHE[key]
    from PIL import Image
    im = Image.new("RGB", (key, key), (200, 40, 60))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    data = buf.getvalue()
    _TEST_PNG_CACHE[key] = data
    return data


def _headers(key):
    return {"Authorization": "Bearer " + key, "Content-Type": "application/json"}


def guess_base_by_prefix(key):
    """按 Key 格式/前缀猜服务商地址；猜不出返回 None。

    优先精确格式（如智谱的 32位hex.16~48位），再退回字符串前缀。
    """
    from petcore.consts import VISION_KEY_PREFIXES, VISION_KEY_PATTERNS
    k = (key or "").strip()
    if not k:
        return None
    # 1) 精确格式（正则）
    for pat, base in VISION_KEY_PATTERNS:
        try:
            if pat.match(k):
                return base
        except Exception:
            continue
    # 2) 普通字符串前缀
    for prefix, base in VISION_KEY_PREFIXES:
        if k.startswith(prefix):
            return base
    return None

def list_models(base, key, timeout=None):
    """GET {base}/models。返回 (ok, [模型名], 错误说明)。"""
    from petcore.consts import VISION_LIST_TIMEOUT, vision_join
    if timeout is None:
        timeout = VISION_LIST_TIMEOUT
    url = vision_join(base, "models")
    try:
        r = requests.get(url, headers=_headers(key), timeout=timeout)
    except Exception as ex:
        return False, [], "连不上 %s（%s）" % (url, str(ex)[:60])
    if r.status_code == 401:
        return False, [], "Key 无效（401）"
    if r.status_code != 200:
        return False, [], "HTTP %s" % r.status_code
    try:
        data = r.json()
    except Exception:
        return False, [], "返回不是 JSON"
    items = data.get("data")
    if not isinstance(items, list):
        # 有些服务商直接返回 {"models": [...]}
        items = data.get("models") if isinstance(data.get("models"), list) else []
    names = []
    for it in items:
        if isinstance(it, dict):
            n = it.get("id") or it.get("name")
            if n:
                names.append(str(n))
        elif isinstance(it, str):
            names.append(it)
    return True, names, ""


def verify_key_auth(base, name, key, timeout=None):
    """对「/models 是公开端点」的服务商做二次确认。

    返回 True=Key 有效，False=无效，None=无需确认（/models 已足够）。
    """
    from petcore.consts import VISION_PROVIDER_AUTH_PATH, vision_join
    path = VISION_PROVIDER_AUTH_PATH.get(name)
    if not path:
        return None
    from petcore.consts import VISION_PROBE_TIMEOUT
    try:
        r = requests.get(vision_join(base, path),
                         headers=_headers(key),
                         timeout=timeout or VISION_PROBE_TIMEOUT)
        return r.status_code == 200
    except Exception:
        return False



def _probe_once(name, base, key, timeout):
    """探测一个候选地址：返回 (base, name, models, err)。"""
    ok, models, err = list_models(base, key, timeout=timeout)
    if not ok:
        return base, name, [], err
    # 部分服务商 /models 是公开端点（假 Key 也 200），需二次确认
    auth = verify_key_auth(base, name, key, timeout=timeout)
    if auth is False:
        return base, name, [], "401（Key 无效）"
    return base, name, models, ""

def detect_provider(key, log=None):
    """确定服务商地址。返回 (base, provider_name, err)。

    策略（关键修复）：
      1. 先按 Key 格式精确识别（智谱 32hex.xx / Gemini AIza / OpenRouter sk-or-）
         → 命中就直接用，0 秒出结果
      2. 格式识别不出（多数 sk- 开头的 Key）：**并行**探测全部候选地址
      3. **401 不再中止循环** —— 各服务商对陌生 Key 都回 401，
         不能用它判断「Key 无效」；只有全部失败才算失败
    """
    from petcore.consts import (VISION_PROVIDERS, 
                                VISION_PROBE_TIMEOUT)

    def _log(m):
        if log:
            log(m)

    # ---- 1) 格式精确识别 ----
    base = guess_base_by_prefix(key)
    if base:
        _log("按 Key 格式识别到服务商：%s" % base)
        return base, _provider_name(base), ""

    # ---- 2) 并行探测（VISION_PROVIDERS 是 (名称, 地址)）----
    _log("格式识别不出，并行探测 %d 个候选服务商…" % len(VISION_PROVIDERS))
    results = []
    lock = threading.Lock()

    def _one(name, base):
        r = _probe_once(name, base, key, VISION_PROBE_TIMEOUT)
        with lock:
            results.append(r)

    threads = []
    for name, base in VISION_PROVIDERS:
        t = threading.Thread(target=_one, args=(name, base), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    # 谁成功就用谁（保持候选表顺序，结果可预期）
    ok_list = [r for r in results if not r[3]]
    if ok_list:
        # 按 VISION_PROVIDERS 的顺序挑第一个成功的
        for name, base in VISION_PROVIDERS:
            for b, n, models, err in ok_list:
                if b == base:
                    _log("探测成功：%s（%s）" % (n, b))
                    return b, n, ""

    # ---- 3) 全部失败 ----
    errs = [(r[1], r[3]) for r in results if r[3]]
    if errs and all("401" in e for _n, e in errs):
        return "", "", ("所有候选服务商都返回 401：Key 无效，"
                        "或这是一家不在候选表里的服务商")
    if errs:
        return "", "", errs[0][1]
    return "", "", "无法识别这个 Key 属于哪家服务商"

def _provider_name(base):
    from petcore.consts import VISION_PROVIDERS
    for name, cand in VISION_PROVIDERS:
        if cand.rstrip("/") == (base or "").rstrip("/"):
            return name
    return base


def suggest_models(provider_name, listed_models):
    """给某服务商挑候选模型。

    优先：/models 里筛出来的（按 filter_and_rank 排序）；
    兜底：该服务商的已知视觉模型（/models 常不列出）。
    已知模型并入后**统一按同一规则重排**，保证默认选到最合适的。
    返回 (候选列表, 是否来自兜底)。
    """
    from petcore.consts import VISION_KNOWN_MODELS
    listed = list(listed_models or [])
    known = list(VISION_KNOWN_MODELS.get(provider_name or "", ()))
    if not listed and not known:
        # 未知服务商（中转站等）且 /models 不可用：
        # 把所有已知模型都当候选，逐个真实验证，能用哪个算哪个。
        # 注意：这会多发几个请求，但只在「地址填了 + /models 挂掉」时才走到。
        all_known = []
        for _v in VISION_KNOWN_MODELS.values():
            for _m in _v:
                if _m not in all_known:
                    all_known.append(_m)
        if all_known:
            return filter_and_rank(all_known), True
        return [], True
    if not listed:
        # 只有已知列表：也按规则排一遍
        return filter_and_rank(known), True
    # 合并后统一排序（去重）
    merged = []
    for m in known + listed:
        if m and m not in merged:
            merged.append(m)
    return filter_and_rank(merged), False


def _version_key(name):
    """从模型名提取版本号元组，用于「同档内从新到旧」排序。

    例：glm-5.3-flash → (5, 3)
        glm-4.6v-flash → (4, 6)
        glm-4v-flash   → (4, 0)
    取不到版本号的返回 (0, 0)。
    """
    m = re.search(r"(\d+)(?:\.(\d+))?", name or "")
    if not m:
        return (0, 0)
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else 0
    return (major, minor)



def filter_and_rank(models):
    """剔除明显非对话模型，并按「档位 → 版本号从新到旧」排序。

    剔除：
      1) 命中 VISION_EXCLUDE_WORDS（含 ocr / autoglm）
      2) 纯文本 GLM（glm-<数字>[.<数字>]，无 v、无 flash）

    排序（升序）：
      档位 0：含 "-flash" 且其后不是 "x"（含 glm-5.3-flash-0901）
      档位 1：含 "flashx"
      档位 2：其它
      档位 3：含 turbo / thinking / pro（无论有没有 flash，一律最后）
    同档内：版本号从新到旧 → 偏好分 → 名字
    """
    from petcore.consts import (VISION_EXCLUDE_WORDS, VISION_PREFER_WORDS,
                                VISION_RANK_LAST_WORDS, _VISION_PLAINTEXT_GLM)
    out = []
    seen = set()
    for n in models or []:
        low = n.lower()
        if any(w in low for w in VISION_EXCLUDE_WORDS):
            continue
        if _VISION_PLAINTEXT_GLM.match(low) and "v" not in low and "flash" not in low:
            continue
        if n in seen:
            continue
        seen.add(n)
        # ---- 主排序档位（thinking/turbo/pro 优先判断）----
        if any(w in low for w in VISION_RANK_LAST_WORDS):
            tier = 3
        elif "-flash" in low and not low.split("-flash", 1)[1].startswith("x"):
            tier = 0
        elif "flashx" in low:
            tier = 1
        else:
            tier = 2
        # ---- 偏好分 ----
        score = 0
        for i, w in enumerate(VISION_PREFER_WORDS):
            if w in low:
                score = max(score, len(VISION_PREFER_WORDS) - i)
        out.append((tier, _version_key(n), score, n))

    # 档位 → 版本号（从新到旧）→ 偏好分（高到低）→ 名字
    out.sort(key=lambda x: (x[0], -x[1][0], -x[1][1], -x[2], x[3]))
    return [n for _t, _v, _s, n in out]


def verify_model(base, key, model, timeout=None):
    """发一张小图，返回 (可用?, 说明)。200 才算可用。"""
    from petcore.consts import VISION_VERIFY_TIMEOUT, vision_join
    if timeout is None:
        timeout = VISION_VERIFY_TIMEOUT
    try:
        b64 = base64.b64encode(make_test_png(32)).decode("ascii")
    except Exception as ex:
        return False, "造测试图失败：%s" % ex
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "ok"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + b64}},
            ],
        }],
        "max_tokens": 16,
    }
    try:
        r = requests.post(vision_join(base, "chat/completions"),
                          headers=_headers(key), json=payload, timeout=timeout)
    except Exception as ex:
        return False, str(ex)[:60]
    if r.status_code == 200:
        return True, ""
    if r.status_code == 401:
        return False, "401"
    return False, "HTTP %s" % r.status_code


def verify_all(base, key, models, on_found=None, should_stop=None, log=None):
    """并行验证一批模型，每通过一个就立刻回调 on_found(model)。

    用固定数量的 worker 消费队列，保证全部模型都被验证到。
    返回 (可用模型列表, 是否被中断)。
    """
    from petcore.consts import VISION_VERIFY_WORKERS
    found = []
    lock = threading.Lock()
    queue = list(models or [])
    qlock = threading.Lock()

    def _next():
        with qlock:
            return queue.pop(0) if queue else None

    def _worker():
        while True:
            if should_stop and should_stop():
                return
            m = _next()
            if m is None:
                return
            ok, why = verify_model(base, key, m)
            if ok:
                with lock:
                    found.append(m)
                if log:
                    log("验证通过：%s" % m)
                if on_found:
                    try:
                        on_found(m)
                    except Exception as ex:
                        log("[%s] %s 失败: %s: %s"
                            % (__name__, "_worker", type(ex).__name__, ex),
                            logging.WARNING)
            elif log:
                log("跳过 %s（%s）" % (m, why))

    n = max(1, min(int(VISION_VERIFY_WORKERS), len(queue) or 1))
    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return found, bool(should_stop and should_stop())
