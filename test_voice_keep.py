# -*- coding: utf-8 -*-
"""翻唱语音保留与找回 验证"""
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
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-relink")
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

def make_wav(path, sec=2.0):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * int(22050 * sec))

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
print("一、翻唱成品永不清理")
print("=" * 72)
# 在 cover_cache 造两个成品
cc = os.path.join(TESTDIR, "cover_cache")
os.makedirs(cc, exist_ok=True)
w1 = os.path.join(cc, "童话_20261024_100000")
os.makedirs(w1, exist_ok=True)
f1 = os.path.join(w1, "童话_翻唱.wav")
make_wav(f1, 3.0)
time.sleep(0.1)
w2 = os.path.join(cc, "指纹_20261024_110000")
os.makedirs(w2, exist_ok=True)
f2 = os.path.join(w2, "指纹_翻唱.wav")
make_wav(f2, 4.0)
ck("1", "两个成品已造好", os.path.exists(f1) and os.path.exists(f2))

# 检查没有任何清理逻辑会删它们（grep 源码）
src = ""
for fn in ("cover.py", "pet_voice.py", "桌宠.py", "petcore\\chat.py"):
    src += open(os.path.join(FORMAL, fn), encoding="utf-8").read()
has_remove = "remove" in src and "cover_cache" in src
# 确认 cover.py 里没有对成品目录的清理调用
import re
cov = open(os.path.join(FORMAL, "cover.py"), encoding="utf-8").read()
cleans = re.findall(r"(remove|rmtree|unlink)\([^)]*cover", cov)
ck("1", "cover.py 无成品清理调用", not cleans, cleans)

print()
print("=" * 72)
print("二、voice 引用丢失 → 打开聊天自动找回")
print("=" * 72)
# 构造一个「丢失」场景：历史里有 🎵 标题消息但没有 voice 字段
c = m.PetApp(); app.processEvents()
c._open_chat(); app.processEvents()
cw = c.chat_window
# 直接往当前会话塞两条消息（模拟旧版本写入）
sess = cw._current_session()
sess.setdefault("messages", []).append(
    {"role": "assistant", "content": "🎵 指纹 - 杜宣达", "ts": time.time()})
sess["messages"].append(
    {"role": "assistant", "content": "我们 变成一对 差点缘分", "ts": time.time()})
cw.save_history()
cw._render_current_session()
pump(0.4)
# 打开窗口时 __init__ 已跑过一次 relink（正式 cover_cache 的成品
# 可能已被挂到别的消息上）。我们造的测试成品（童话/指纹）此刻应仍未引用。
# 检查：指纹成品是否已被自动挂到某处（可能挂到了测试会话的 🎵 消息上）
n_voice_before = sum(1 for x in sess["messages"] if x.get("voice"))
print("  自动找回后本会话 voice 数:", n_voice_before)
# 手动再跑一次找回（此时剩余未引用的应该只有我们造的测试成品中
# 名字能匹配上 🎵 消息的）
n = cw._relink_cover_songs()
pump(0.5)
print("  第二次找回数量:", n)
ck("2", "重复执行不重复挂", n == 0, n)
n_voice_after = sum(1 for x in sess["messages"] if x.get("voice"))
ck("2", "voice 引用已补上", n_voice_after >= 1, n_voice_after)
vm_list = [x for x in sess["messages"] if x.get("voice")]
if vm_list:
    vm = vm_list[0]
    ck("2", "挂到了正确的消息", "指纹" in (vm.get("content") or ""),
       vm.get("content"))
    ck("2", "voice 指向真实文件", os.path.exists(vm.get("voice", "")),
       vm.get("voice"))
    ck("2", "voice_sec 正常", vm.get("voice_sec", 0) >= 1.0, vm.get("voice_sec"))
# 语音条在 UI 里出现
from petcore.chat import ChatBubble
bubbles = [b for b in cw.findChildren(ChatBubble)
           if getattr(b, "voice_btn", None) is not None]
ck("2", "语音条在 UI 里出现", len(bubbles) >= 1, len(bubbles))

# 再跑一次：不应重复挂
n2 = cw._relink_cover_songs()
ck("2", "重复执行不重复挂", n2 == 0, n2)
n_voice_again = sum(1 for x in sess["messages"] if x.get("voice"))
ck("2", "voice 引用不重复增长", n_voice_again == n_voice_after,
   "%d -> %d" % (n_voice_after, n_voice_again))
close_ok = True
try:
    for w in c.pets: w.hide()
    if c.tray: c.tray.hide()
except Exception:
    pass
print()

print("=" * 72)
print("三、没有宿主消息的成品不乱挂")
print("=" * 72)
w3 = os.path.join(cc, "孤勇者_20261024_120000")
os.makedirs(w3, exist_ok=True)
f3 = os.path.join(w3, "孤勇者_翻唱.wav")
make_wav(f3, 2.0)
n3 = cw._relink_cover_songs()
ck("3", "无宿主不挂", n3 == 0, n3)
print()

print("=" * 72)
print("四、重开聊天窗口语音条仍在")
print("=" * 72)
try:
    if c.settings_window: c.settings_window.hide()
    if c.chat_window: c.chat_window.hide()
except Exception:
    pass
app.processEvents()
c._open_chat(); app.processEvents()
pump(0.5)
cw2 = c.chat_window
n_voice_reopen = 0
for s in cw2.sessions:
    for x in (s.get("messages") or []):
        if x.get("voice"):
            n_voice_reopen += 1
ck("4", "重开后 voice 引用保留", n_voice_reopen >= 1, n_voice_reopen)
from petcore.chat import ChatBubble
bubbles2 = [b for b in cw2.findChildren(ChatBubble)
            if getattr(b, "voice_btn", None) is not None]
ck("4", "语音条仍在 UI", len(bubbles2) >= 1, len(bubbles2))
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
