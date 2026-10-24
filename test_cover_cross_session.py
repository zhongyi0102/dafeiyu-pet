# -*- coding: utf-8 -*-
"""翻唱跨会话验证：切换会话后，翻唱结果写回发起时的会话"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import importlib.util

FORMAL = os.path.normcase(os.path.abspath(r"D:\Software\Portable\应用程序\dafeiyu-pet"))
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-cx")
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

print("=" * 72)
print("翻唱期间切换会话：结果写回发起会话")
print("=" * 72)

c = m.PetApp(); app.processEvents()
c._open_chat(); app.processEvents()
cw = c.chat_window

# 会话 A：发起翻唱
cw.append_external("user", "唱一首我不难过")
origin_sid = cw.current_id
print("  发起会话 A:", origin_sid)

# 发起翻唱（mock：make_cover 延迟返回）
import cover as CV
fake_result = {}

def slow_cover(q, progress=None, timeout=1800):
    time.sleep(1.0)   # 给用户时间切换会话
    return fake_result.get("path", ""), fake_result.get("name", ""), \
        fake_result.get("artist", ""), fake_result.get("err", "")

CV.make_cover = slow_cover
sys.modules["cover"] = CV

cw.busy = True
cw._turn_user_text = "唱一首我不难过"
cw._sing_query = "我不难过"
cw._pending_voice_text = "（歌词：我不难过）"
cw._gen_voice_bubble("（歌词：我不难过）", sing_query="我不难过")
pump(0.5)
ck("1", "_cover_session_id 已记录", getattr(cw, "_cover_session_id", None) == origin_sid,
   getattr(cw, "_cover_session_id", None))

# 用户切换到新会话 B（busy=True 时 _new_session 直接返回，先临时解除）
was_busy = cw.busy
cw.busy = False
cw._new_session()
cw.busy = was_busy
user_sid = cw.current_id
print("  切换到会话 B:", user_sid)
ck("1", "确实切走了", user_sid != origin_sid)
cw.append_external("user", "你好")
pump(0.5)

# 翻唱完成：结果应写回会话 A
fake_result = {"path": os.path.join(TESTDIR, "fake_cover.wav"),
               "name": "我不难过", "artist": "孙燕姿", "err": ""}
# 造假 wav
import wave
with wave.open(fake_result["path"], "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
    w.writeframes(b"\x00\x00" * 22050 * 3)

cw._on_cover_ready(fake_result["path"], "我不难过", "孙燕姿", "")
pump(1.0)

# 会话 A 应有语音条 + 歌词
sess_a = cw._find_session(origin_sid)
msgs_a = sess_a.get("messages") or []
va = [x for x in msgs_a if x.get("voice")]
ck("2", "会话 A 有语音条消息", len(va) >= 1,
   [(x.get("content", "")[:16], bool(x.get("voice"))) for x in msgs_a])
if va:
    ck("2", "voice 指向成品", "fake_cover" in va[0]["voice"], va[0]["voice"])
    vt = [x.get("voice_text") for x in va if x.get("voice_text")]
    ck("2", "voice_text 已存", bool(vt) and "我不难过" in vt[0], vt)
ta = [x for x in msgs_a if "歌词" in (x.get("content") or "")]
ck("2", "会话 A 有歌词文字", len(ta) >= 1)

# 用户当前仍在会话 B，且 B 里没有翻唱结果
ck("2", "用户仍在会话 B", cw.current_id == user_sid, cw.current_id)
sess_b = cw._find_session(user_sid)
msgs_b = sess_b.get("messages") or []
vb = [x for x in msgs_b if x.get("voice")]
ck("2", "会话 B 没有翻唱结果", len(vb) == 0,
   [x.get("content", "")[:16] for x in msgs_b])

# UI：先把界面切回 B（此刻渲染的还是 A 完成后的界面）
cw._switch_session(user_sid)
pump(0.5)
from petcore.chat import ChatBubble
def _live_voice_bubbles(cwv):
    out = []
    lay = cwv.msg_layout
    for i in range(lay.count()):
        it = lay.itemAt(i)
        w = it.widget() if it else None
        if w is None:
            continue
        for b in w.findChildren(ChatBubble):
            if getattr(b, "voice_btn", None) is not None:
                out.append(b)
        if isinstance(w, ChatBubble) and getattr(w, "voice_btn", None):
            out.append(w)
    return out
bubbles_b = _live_voice_bubbles(cw)
ck("2", "切回 B 界面无 A 的语音条", len(bubbles_b) == 0, len(bubbles_b))

# 切回会话 A：语音条出现
cw._switch_session(origin_sid)
pump(0.6)
bubbles_a = [b for b in cw.findChildren(ChatBubble)
             if getattr(b, "voice_btn", None) is not None]
ck("3", "切回 A 语音条出现", len(bubbles_a) >= 1, len(bubbles_a))

# 失败场景：切走后失败，失败文字也写回 A
cw._switch_session(user_sid)
pump(0.4)
cw.busy = True
cw._turn_user_text = "唱一首歌"
cw._sing_query = "歌"
cw._pending_voice_text = "（歌词2）"
cw._gen_voice_bubble("（歌词2）", sing_query="歌")
pump(0.4)
sid2 = cw.current_id
cw._new_session()
sid3 = cw.current_id
pump(0.3)
cw._on_cover_ready("", "", "", "下载失败：搜不到歌")
pump(0.8)
sess_2 = cw._find_session(sid2)
msgs_2 = sess_2.get("messages") or []
ck("4", "失败文字写回发起会话", any("歌词2" in (x.get("content") or "")
                                    for x in msgs_2),
   [x.get("content", "")[:14] for x in msgs_2])
ck("4", "用户仍在最后会话", cw.current_id == sid3)

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
