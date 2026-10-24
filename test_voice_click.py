# -*- coding: utf-8 -*-
"""语音卡片按钮「真实点击」测试

重要：之前的测试直接调 b._play_voice()，绕过了按钮信号，
导致「播放键连错函数」这个 bug 没被发现。
本测试一律用 button.click() 触发，走真实信号链路。
"""
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
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-click")
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

def pump(sec=0.5):
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
    w.writeframes(b"\x00\x00" * 22050 * 8)

print("=" * 72)
print("一、播放键真实点击（关键回归）")
print("=" * 72)
c = m.PetApp(); app.processEvents()
c._open_chat(); app.processEvents()
cw = c.chat_window
cw.show()
pump(0.5)
cw.append_external("assistant", "🎵 测试歌曲",
                   voice_path=wav, voice_sec=8.0, voice_text="歌词")
pump(0.8)

from petcore.chat import ChatBubble
bs = [b for b in cw.findChildren(ChatBubble)
      if getattr(b, "voice_ctrl", None) is not None]
ck("1", "卡片已创建", len(bs) >= 1, len(bs))
b = bs[-1]
pl = b._voice_player()

# 初始状态
ck("1", "初始未播放", not b._playing_now and not b._paused_now,
   "playing=%s paused=%s" % (b._playing_now, b._paused_now))
ck("1", "播放键可用", b.voice_pause_btn.isEnabled())
ck("1", "播放键显示 ▶", b.voice_pause_btn.text() == "▶",
   b.voice_pause_btn.text())

# ★ 关键：用 button.click() 走真实信号
b.voice_pause_btn.click()
pump(1.0)
ck("1", "点击后开始播放", pl is not None and pl.state() == "playing",
   pl.state() if pl else None)
ck("1", "按钮变为 ⏸", b.voice_pause_btn.text() == "⏸",
   b.voice_pause_btn.text())
ck("1", "_playing_now 为 True", b._playing_now)

print()
print("=" * 72)
print("二、再次点击 = 暂停（三态正确）")
print("=" * 72)
pos1 = pl.position_ms()
pump(0.8)
b.voice_pause_btn.click()
pump(0.6)
ck("2", "暂停后状态 paused", pl.state() == "paused", pl.state())
ck("2", "按钮变回 ▶", b.voice_pause_btn.text() == "▶",
   b.voice_pause_btn.text())
ck("2", "_paused_now 为 True", b._paused_now)
pos2 = pl.position_ms()
pump(0.8)
ck("2", "暂停后位置不动", abs(pl.position_ms() - pos2) < 300,
   "%d -> %d" % (pos2, pl.position_ms()))

print()
print("=" * 72)
print("三、第三次点击 = 继续")
print("=" * 72)
b.voice_pause_btn.click()
pump(0.8)
ck("3", "继续后 playing", pl.state() == "playing", pl.state())
ck("3", "位置继续前进", pl.position_ms() > pos2,
   "%d -> %d" % (pos2, pl.position_ms()))
ck("3", "按钮变 ⏸", b.voice_pause_btn.text() == "⏸",
   b.voice_pause_btn.text())

print()
print("=" * 72)
print("四、倍速按钮真实点击（不该触发播放）")
print("=" * 72)
b._stop_voice()
pump(0.5)
ck("4", "停止后状态", pl.state() == "stopped", pl.state())
rate0 = b.voice_rate_btn.text()
b.voice_rate_btn.click()
pump(0.5)
rate1 = b.voice_rate_btn.text()
ck("4", "倍速按钮改变倍速", rate0 != rate1, "%s -> %s" % (rate0, rate1))
ck("4", "倍速点击不影响播放状态", pl.state() == "stopped", pl.state())

print()
print("=" * 72)
print("五、Aa 按钮真实点击")
print("=" * 72)
b.voice_text_btn.click()
pump(0.5)
ck("5", "Aa 展开文字", getattr(b, "voice_text_area", None) is not None
   and b.voice_text_area.isVisible())
if getattr(b, "voice_text_area", None) is not None:
    ck("5", "内容正确", "歌词" in b.voice_text_area.text(),
       b.voice_text_area.text()[:20])
b.voice_text_btn.click()
pump(0.4)
ck("5", "再点收起", not b.voice_text_area.isVisible())

print()
print("=" * 72)
print("六、波形点击 = seek（不该触发播放）")
print("=" * 72)
b._stop_voice()
pump(0.4)
b.wave_bar._seek_to(50)   # 点到中间
pump(0.5)
ck("6", "波形点击不启动播放", pl.state() == "stopped", pl.state())

print()
print("=" * 72)
print("七、文件丢失时点击 → 提示 + 置灰")
print("=" * 72)
cw.append_external("assistant", "🎵 丢失的",
                   voice_path=os.path.join(TESTDIR, "nope.wav"),
                   voice_sec=5.0, voice_text="x")
pump(0.8)
bs2 = [x for x in cw.findChildren(ChatBubble)
       if getattr(x, "voice_ctrl", None) is not None]
b2 = bs2[-1]
b2.voice_pause_btn.click()   # 真实点击
pump(0.6)
ck("7", "点击后置灰", not b2.voice_pause_btn.isEnabled())
lab = getattr(b2, "voice_song_label", None)
ck("7", "显示「音频文件已丢失」",
   lab is not None and lab.text() == "音频文件已丢失",
   lab.text() if lab is not None else None)

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
