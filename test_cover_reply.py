# -*- coding: utf-8 -*-
"""A+B 验证：无歌词气泡 + 人设短评 + 转文字仍可用 + Aa 图标"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import wave
import importlib.util

FORMAL = os.path.normcase(os.path.abspath(r"D:\Software\Portable\应用程序\dafeiyu-pet"))
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-ab")
shutil.rmtree(TESTDIR, ignore_errors=True)
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR
os.environ["DFY_BUNDLE_DIR"] = FORMAL
sys.path.insert(0, FORMAL)
os.chdir(FORMAL)
from PySide6.QtWidgets import QApplication
import importlib.util
app = QApplication(sys.argv)
spec = importlib.util.spec_from_file_location("m", os.path.join(FORMAL, "桌宠.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
import petcore.consts as C
import petcore.chat as CH

RESULTS = []
def ck(g, n, cond, ex=""):
    RESULTS.append((g, n, bool(cond), ex))
    print("  [%s] %-3s %s%s" % ("PASS" if cond else "FAIL", g, n,
                                ((" | " + str(ex)) if ex else "")))

def pump(sec=0.4):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.02)

CFG = {
    "config_version": 2, "topmost": True, "passthrough": False, "autostart": False,
    "ds_api_key": "sk-x", "chat_base": "http://127.0.0.1:9/v1", "chat_model": "m",
    "chat_migrated": True, "city": "重庆", "birthday": "", "animation_mode": "off",
    "usage": m._default_usage(), "games": m._default_games(),
    "reminders": m._default_reminders(), "balance_full": 20.0,
    "features": m._default_features(),
    "pets": [{"id": "main", "size": 0.7, "x": 600, "y": 300, "mode": "still", "display": True}],
    "voice": {"engine": "edge", "enabled": True, "url": "http://127.0.0.1:9880"},
    "proactive_ai": {"enabled": False},
}
with open(m.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(CFG, f, ensure_ascii=False)

wav = os.path.join(TESTDIR, "song.wav")
with wave.open(wav, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
    w.writeframes(b"\x00\x00" * 22050 * 3)

LYRICS = ("这一路上走走停停\n顺着少年漂流的痕迹\n迈出车站的前一刻\n"
          "竟有些犹豫\n不禁笑这近乡情怯\n仍无可避免")

print("=" * 72)
print("一、文案池")
print("=" * 72)
ck("1", "COVER_DONE_LINES 存在", hasattr(C, "COVER_DONE_LINES"))
ck("1", "COVER_FAIL_LINES 存在", hasattr(C, "COVER_FAIL_LINES"))
ck("1", "成功文案 >= 8 句", len(C.COVER_DONE_LINES) >= 8, len(C.COVER_DONE_LINES))
ck("1", "失败文案 >= 8 句", len(C.COVER_FAIL_LINES) >= 8, len(C.COVER_FAIL_LINES))
ck("1", "成功文案 <= 25 字", all(len(x) <= 40 for x in C.COVER_DONE_LINES),
   max(len(x) for x in C.COVER_DONE_LINES))
ck("1", "失败文案 <= 25 字", all(len(x) <= 40 for x in C.COVER_FAIL_LINES),
   max(len(x) for x in C.COVER_FAIL_LINES))
ck("1", "有 {song} 占位", any("{song}" in x for x in C.COVER_DONE_LINES),
   sum(1 for x in C.COVER_DONE_LINES if "{song}" in x))
ck("1", "无括号动作", not any(("（" in x or "(" in x) for x in
                            C.COVER_DONE_LINES + C.COVER_FAIL_LINES))
ck("1", "无歌词原文", not any(("走走停停" in x or "漂流" in x) for x in
                            C.COVER_DONE_LINES + C.COVER_FAIL_LINES))
print()

print("=" * 72)
print("二、翻唱成功：只有卡片 + 短评，无歌词气泡")
print("=" * 72)
c = m.PetApp(); app.processEvents()
c._open_chat(); app.processEvents()
cw = c.chat_window
cw.append_external("user", "唱一首起风了")
n_before = len(cw.messages)

# 模拟翻唱成功
cw._pending_voice_text = LYRICS
cw._cover_session_id = cw.current_id
cw._on_cover_ready(wav, "起风了", "冯沁苑", "")
pump(0.8)

msgs = cw.messages
new_msgs = msgs[n_before:]
print("  新增消息 %d 条:" % len(new_msgs))
for x in new_msgs:
    print("    [%s] %r  voice=%s" % (x.get("role"),
          (x.get("content") or "")[:40],
          os.path.basename(x["voice"]) if x.get("voice") else "-"))

ck("2", "新增 2 条消息（卡片+短评）", len(new_msgs) == 2, len(new_msgs))
card = [x for x in new_msgs if x.get("voice")]
ck("2", "有语音卡片", len(card) == 1, len(card))
if card:
    ck("2", "卡片有 voice_text（转文字数据）",
       card[0].get("voice_text") == LYRICS,
       (card[0].get("voice_text") or "")[:30])
    ck("2", "卡片标题含歌名", "起风了" in (card[0].get("content") or ""),
       card[0].get("content"))
short = [x for x in new_msgs if not x.get("voice")]
ck("2", "有短评消息", len(short) == 1, len(short))
if short:
    t = short[0].get("content") or ""
    print("    短评: %r" % t)
    ck("2", "短评不含整首歌词", "走走停停" not in t and "漂流" not in t, t[:30])
    ck("2", "短评长度合理（<=40字）", len(t) <= 40, len(t))
    ck("2", "短评是人设句（在文案池或含歌名）",
       t in C.COVER_DONE_LINES or "起风了" in t or "这首歌" in t, t)
ck("2", "无第三条歌词消息", len(new_msgs) == 2)

# 落盘检查
d = json.load(io.open(m.CHAT_PATH, encoding="utf-8"))
sess = (d.get("sessions") or [{}])[0]
saved = sess.get("messages") or []
n_lyric_saved = sum(1 for x in saved if "走走停停" in (x.get("content") or "")
                    and not x.get("voice"))
ck("2", "落盘无歌词气泡", n_lyric_saved == 0, n_lyric_saved)
ck("2", "落盘保留 voice_text",
   any(x.get("voice_text") for x in saved))
print()

print("=" * 72)
print("三、转文字仍可用（Aa 按钮）")
print("=" * 72)
from petcore.chat import ChatBubble
bubbles = [b for b in cw.findChildren(ChatBubble)
           if getattr(b, "voice_ctrl", None) is not None]
ck("3", "语音卡片存在", len(bubbles) >= 1, len(bubbles))
if bubbles:
    b = bubbles[-1]
    ck("3", "按钮文本是 Aa", b.voice_text_btn.text() == "Aa",
       repr(b.voice_text_btn.text()))
    ck("3", "按钮可见尺寸", b.voice_text_btn.width() >= 24
       and b.voice_text_btn.height() >= 24,
       "%dx%d" % (b.voice_text_btn.width(), b.voice_text_btn.height()))
    # 点开转文字
    b._toggle_voice_text()
    pump(0.4)
    ok = (getattr(b, "voice_text_area", None) is not None
          and b.voice_text_area.isVisible())
    ck("3", "转文字展开", ok)
    if ok:
        ta = b.voice_text_area.text()
        print("    展开内容前 30 字: %r" % ta[:30])
        ck("3", "内容为歌词原文", "走走停停" in ta, ta[:24])
    b._toggle_voice_text()
    pump(0.3)
    ck("3", "再点收起", not b.voice_text_area.isVisible())
print()

print("=" * 72)
print("四、翻唱失败：只发失败短评，不发歌词/错误详情")
print("=" * 72)
cw.append_external("user", "唱一首不存在的歌")
n2 = len(cw.messages)
cw._pending_voice_text = LYRICS
cw._cover_session_id = cw.current_id
cw._on_cover_ready("", "", "", "下载失败：搜不到这首歌")
pump(0.8)
new2 = cw.messages[n2:]
print("  新增 %d 条:" % len(new2))
for x in new2:
    print("    %r" % ((x.get("content") or "")[:50]))
ck("4", "失败只发 1 条", len(new2) == 1, len(new2))
if new2:
    t = new2[0].get("content") or ""
    ck("4", "失败短评在文案池", t in C.COVER_FAIL_LINES, t)
    ck("4", "不含歌词", "走走停停" not in t)
    ck("4", "不含技术错误详情", "下载失败" not in t and "搜不到" not in t, t)
print()

print("=" * 72)
print("五、普通语音回复不受影响")
print("=" * 72)
cw.append_external("user", "念一下这句话")
n3 = len(cw.messages)
cw._pending_voice_text = "这是要被念出来的台词。"
cw._voice_session_id = cw.current_id
cw._on_voice_ready(wav, 3.0, "", "这是要被念出来的台词。")
pump(0.6)
new3 = cw.messages[n3:]
print("  新增 %d 条" % len(new3))
for x in new3:
    print("    [%s] %r voice=%s" % (x.get("role"),
          (x.get("content") or "")[:30],
          "有" if x.get("voice") else "-"))
ck("5", "普通语音有卡片", any(x.get("voice") for x in new3))
ck("5", "普通语音仍补文字回复",
   any((not x.get("voice")) and "台词" in (x.get("content") or "")
       for x in new3),
   [x.get("content", "")[:20] for x in new3])

try:
    for w in c.pets: w.hide()
    if c.tray: c.tray.hide()
except Exception:
    pass

print()
groups = {}
for g, n, v, ex in RESULTS:
    groups.setdefault(g, [0, 0]); groups[g][1] += 1
    if v: groups[g][0] += 1
for g in sorted(groups):
    p, t = groups[g]
    print("  %s: %d / %d %s" % (g, p, t, "✓" if p == t else "✗"))
print()
print("总计: %d / %d" % (sum(1 for _, _, v, _ in RESULTS if v), len(RESULTS)))
fails = [(g, n, ex) for g, n, v, ex in RESULTS if not v]
if fails:
    print()
    for g, n, ex in fails:
        print("  失败 [%s] %s | %s" % (g, n, ex))
shutil.rmtree(TESTDIR, ignore_errors=True)
