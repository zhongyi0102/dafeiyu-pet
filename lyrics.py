# -*- coding: utf-8 -*-
"""lyrics.py — 联网搜索歌词（唱歌用）

数据源：网易云音乐公开接口
  1. 搜索歌曲 -> 拿到 song_id
  2. 用 song_id 取 LRC 歌词
  3. 清理时间轴/元信息，只留可唱的歌词行

失败时返回空，由调用方降级（让模型自己写）。
"""
import logging
import re

_logger = logging.getLogger("dafeiyu")


def log(msg, level=logging.INFO):
    """与主程序一致的 log(text, level) 函数式接口。"""
    try:
        _logger.log(level, msg)
    except Exception:
        pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://music.163.com/"}

# LRC 时间轴：[00:12.34]
_LRC_RE = re.compile(r"\[[\d:.]+\]")
# 元信息行（作词/作曲/编曲等）
_META_RE = re.compile(
    r"^\s*(作词|作曲|编曲|制作人|出品|发行|监制|混音|录音|吉他|贝斯|鼓|"
    r"和声|母带|OP|SP|ISRC|词|曲|Produced|Composed|Arranged|Mixed|"
    r"Recorded|Mastered|Written|Lyrics|Music)\s*[:：]", re.I)


def _clean_lrc(lrc_text):
    """LRC -> 纯歌词行（去时间轴、去元信息、去空行）。"""
    if not lrc_text:
        return []
    out = []
    for raw in lrc_text.splitlines():
        line = _LRC_RE.sub("", raw).strip()
        if not line:
            continue
        if _META_RE.match(line):
            continue
        # 制作人员行：含冒号且不是歌词（冒号后是英文名/编号等）
        if ":" in line or "：" in line:
            head = re.split(r"[:：]", line, 1)[0].strip()
            # 冒号前是常见制作术语 -> 丢弃
            if re.search(r"(Engineer|Studio|Arrangement|Vocals|Vocals|"
                         r"Producer|Guitar|Bass|Drum|Piano|Strings|"
                         r"ISRC|OP|SP|Recording|Mixing|Mastering|"
                         r"作词|作曲|编曲|制作|监制|混音|录音|出品|发行|"
                         r"和声|母带|吉他|贝斯|鼓|词|曲)", head, re.I):
                continue
        # 纯英文行（没有中文）且含冒号/斜杠 -> 大概率是元信息
        if re.match(r"^[A-Za-z0-9\s/&:().,'\-]+$", line) and not re.search(r"[\u4e00-\u9fff]", line):
            continue
        # 版权/声明行
        if re.match(r"^[（(]?未经许可", line) or "不得翻唱" in line:
            continue
        out.append(line)
    return out


def search(keyword, limit=5, timeout=12):
    """搜索歌曲。返回 [{"id","name","artist"}, ...]。"""
    import requests
    try:
        s = requests.Session()
        s.trust_env = True       # 走系统代理（网易云在国内，通常直连）
        r = s.post(
            "https://music.163.com/api/search/get/",
            data={"s": keyword, "type": 1, "limit": limit, "offset": 0},
            headers=HEADERS, timeout=timeout)
        data = r.json()
    except Exception as ex:
        log("[歌词] 搜索失败: %s: %s" % (type(ex).__name__, ex))
        return []
    songs = (data.get("result") or {}).get("songs") or []
    out = []
    for x in songs:
        artists = "/".join(a.get("name", "") for a in (x.get("artists") or []))
        out.append({"id": x.get("id"), "name": x.get("name") or "",
                    "artist": artists})
    return out


def fetch(song_id, timeout=12):
    """取歌词原文（LRC）。失败返回空串。"""
    import requests
    try:
        s = requests.Session()
        s.trust_env = True
        r = s.get(
            "https://music.163.com/api/song/lyric",
            params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
            headers=HEADERS, timeout=timeout)
        return (r.json().get("lrc") or {}).get("lyric") or ""
    except Exception as ex:
        log("[歌词] 取歌词失败: %s: %s" % (type(ex).__name__, ex))
        return ""


def get_lyrics(query, max_lines=16, timeout=12):
    """按查询词搜歌词。返回 (song_name, artist, [歌词行])。

    找不到返回 ("", "", [])。
    """
    query = (query or "").strip()
    if not query:
        return "", "", []
    songs = search(query, timeout=timeout)
    # 搜不到就拆词重试（"周杰伦 稻香" -> "稻香"）
    if not songs:
        parts = re.split(r"[\s,，、/]+", query)
        for part in sorted(parts, key=len, reverse=True):
            if len(part) >= 2:
                songs = search(part, timeout=timeout)
                if songs:
                    log("[歌词] 拆词重搜：%r" % part)
                    break
    if not songs:
        return "", "", []
    # 遍历候选：取第一个有「有效歌词」的
    # （很多翻唱版没有歌词，或歌词是别的歌）
    best_order = sorted(
        songs,
        key=lambda s: (0 if (s["name"] and s["name"] in query) else 1,
                       len(s["name"] or "")))
    for s in best_order:
        lrc = fetch(s["id"], timeout=timeout)
        lines = _clean_lrc(lrc)
        # 有效歌词：至少 4 行，且平均行长合理（排除乱码/纯元信息）
        if len(lines) < 4:
            continue
        avg = sum(len(x) for x in lines) / float(len(lines))
        if avg < 4:
            continue
        log("[歌词] 命中：%s - %s（%d 行）"
            % (s["name"], s["artist"], len(lines)))
        return s["name"], s["artist"], lines[:max_lines]
    return "", "", []


def build_sing_text(query, max_lines=16):
    """把歌词拼成适合合成的文本（一句一行）。

    返回 (text, song_name, artist)；失败返回 ("", "", "")。
    """
    name, artist, lines = get_lyrics(query, max_lines=max_lines)
    if not lines:
        return "", "", ""
    return "\n".join(lines), name, artist
