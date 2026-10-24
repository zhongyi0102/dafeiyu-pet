# -*- coding: utf-8 -*-
"""翻唱状态气泡验证"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import importlib.util

FORMAL = os.path.normcase(os.path.abspath(r"D:\Software\Portable\应用程序\dafeiyu-pet"))
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-cvs")
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

RESULTS = []
def ck(g, n, cond, ex=""):
    RESULTS.append((g, n, bool(cond), ex))
    print("  [%s] %-3s %s%s" % ("PASS" if cond else "FAIL", g, n,
                                ((" | " + str(ex)) if ex else "")))

def pump(sec=0.3):
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
print("翻唱状态气泡")
print("=" * 72)

c = m.PetApp(); app.processEvents()
c._open_chat(); app.processEvents()
cw = c.chat_window
pet = c.main_pet

# 找状态气泡的方法
ck("1", "_update_cover_status 方法", hasattr(cw, "_update_cover_status"))
ck("1", "_on_cover_tick 方法", hasattr(cw, "_on_cover_tick"))
ck("1", "_close_cover_status 方法", hasattr(cw, "_close_cover_status"))

# 发起一个假的翻唱（make_cover 延迟 6 秒返回成品）
import cover as CV
fake_path = ""
_cc = os.path.join(FORMAL, "cover_cache")
if os.path.isdir(_cc):
    cands = []
    for _root, _dirs, _files in os.walk(_cc):
        for _f in _files:
            if _f.endswith("_翻唱.wav"):
                cands.append(os.path.join(_root, _f))
    if cands:
        fake_path = max(cands, key=os.path.getmtime)
print("成品文件:", os.path.basename(fake_path) if fake_path else "(无)",
      "存在:", os.path.exists(fake_path) if fake_path else False)

def slow_cover(q, progress=None, timeout=1800):
    for i, msg in enumerate(("正在搜索并下载…", "正在分离人声和伴奏…",
                             "正在换成大肥鱼的声音…", "正在混音…")):
        time.sleep(1.5)
        if progress:
            progress(msg)
    return fake_path, "童话", "光良", ""

CV.make_cover = slow_cover
# 让 chat.py 里的 import cover 拿到打补丁的模块
sys.modules["cover"] = CV

cw.busy = True
cw._turn_user_text = "唱一首童话"
cw._sing_query = "童话"
cw.current_bubble = None
cw._pending_voice_text = "（歌词）"
cw._gen_voice_bubble("（歌词）", sing_query="童话")

pump(0.6)
# 状态气泡应该出现
has_status = getattr(cw, "_cover_status_bubble", None) is not None
ck("2", "状态气泡已出现", has_status)
if has_status:
    txt = cw._cover_status_bubble.label.text()
    print("  初始文字: %r" % txt[:60])
    ck("2", "含「正在翻唱」", "正在翻唱" in txt)
    ck("2", "含歌名", "童话" in txt)

# 等 3 秒，tick 应该刷新秒数
pump(3.0)
if has_status:
    txt2 = cw._cover_status_bubble.label.text()
    print("  3 秒后: %r" % txt2[:80])
    ck("2", "显示已用秒数", ("秒" in txt2), txt2[:60])

# 进度文案更新
cw._on_cover_progress("正在分离人声和伴奏…")
pump(0.3)
if has_status:
    txt3 = cw._cover_status_bubble.label.text()
    ck("2", "进度文案更新", "分离" in txt3, txt3[:60])

# 完成：状态气泡移除，语音条出现
cw._on_cover_ready(fake_path, "童话", "光良", "")
pump(0.5)
ck("3", "状态气泡已移除", getattr(cw, "_cover_status_bubble", None) is None)
ck("3", "计时器已停",
   not getattr(cw, "_cover_timer", None).isActive()
   if getattr(cw, "_cover_timer", None) else True)
# 语音条在最后一条 assistant 消息里
msgs = cw.messages
last = msgs[-1] if msgs else {}
has_voice = any(x.get("voice") for x in msgs)
ck("3", "历史带 voice 字段", has_voice,
   [x.get("voice") for x in msgs if x.get("voice")][:1])
ck("3", "文字歌词也补上", any("歌词" in (x.get("content") or "") for x in msgs))

# 失败场景：状态气泡也要移除
cw.busy = True
cw._turn_user_text = "唱一首歌"
cw._sing_query = "歌"
cw._pending_voice_text = "（歌词2）"
cw._gen_voice_bubble("（歌词2）", sing_query="歌")
pump(0.5)
ck("4", "第二个状态气泡出现",
   getattr(cw, "_cover_status_bubble", None) is not None)
cw._on_cover_ready("", "", "", "下载失败：找不到歌")
pump(0.5)
ck("4", "失败后状态气泡移除",
   getattr(cw, "_cover_status_bubble", None) is None)
ck("4", "失败时补文字", any("歌词2" in (x.get("content") or "")
                            for x in cw.messages))

# 桌宠气泡也显示进度
ck("5", "_on_cover_progress 更新 token_label",
   cw.token_label.text() != "", cw.token_label.text()[:40])

try:
    for w in c.pets: w.hide()
    if c.tray: c.tray.hide()
except Exception: pass

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
    for g, n, ex in fails:
        print("  失败 [%s] %s | %s" % (g, n, ex))
shutil.rmtree(TESTDIR, ignore_errors=True)
