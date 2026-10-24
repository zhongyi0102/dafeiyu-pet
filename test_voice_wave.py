# -*- coding: utf-8 -*-
"""语音条升级验证：波形 / 进度拖动 / 倍速 / 转文字"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import wave
import struct
import importlib.util

FORMAL = os.path.normcase(os.path.abspath(r"D:\Software\Portable\应用程序\dafeiyu-pet"))
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-wave")
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
import pet_voice as V

RESULTS = []
def ck(g, n, cond, ex=""):
    RESULTS.append((g, n, bool(cond), ex))
    print("  [%s] %-3s %s%s" % ("PASS" if cond else "FAIL", g, n,
                                ((" | " + str(ex)) if ex else "")))

def pump(sec=0.3):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.02)

# 造 6 秒带起伏的 wav
wav_path = os.path.join(TESTDIR, "wave6s.wav")
with wave.open(wav_path, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(22050)
    import math
    n = 22050 * 6
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(i * 0.02) *
                              (0.5 + 0.5 * math.sin(i * 0.001))))
        for i in range(n))
    w.writeframes(frames)

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
print("一、WaveBar 组件")
print("=" * 72)
from petcore.chat import WaveBar, ChatBubble
wb = WaveBar(wav_path, 6.0)
ck("1", "波形采样非全零", any(p > 0.05 for p in wb._peaks), wb._peaks[:6])
ck("1", "波形条数", len(wb._peaks) == 36, len(wb._peaks))
ck("1", "set_progress", (wb.set_progress(0.5), True)[1])
ck("1", "set_duration", (wb.set_duration(10.0), True)[1])
# 模拟拖动
wb.resize(300, 26)
wb._seek_to(150)
pump(0.2)
ck("1", "拖动到中点触发 seek 信号", abs(wb._progress - 0.5) < 0.1, wb._progress)
wb._seek_to(280)
ck("1", "拖动到末尾", wb._progress > 0.85, wb._progress)
print()

print("=" * 72)
print("二、VoicePlayer 倍速")
print("=" * 72)
vp = V.VoicePlayer(lambda: {"enabled": True, "volume": 80})
ck("2", "默认倍速 1.0", abs(vp.rate() - 1.0) < 0.01, vp.rate())
ck("2", "set_rate(1.5)", vp.set_rate(1.5))
ck("2", "倍速生效", abs(vp.rate() - 1.5) < 0.01, vp.rate())
ck("2", "set_rate(2.0)", vp.set_rate(2.0))
ck("2", "倍速 2.0 生效", abs(vp.rate() - 2.0) < 0.01, vp.rate())
ck("2", "set_rate(0.75)", vp.set_rate(0.75))
ck("2", "倍速 0.75 生效", abs(vp.rate() - 0.75) < 0.01, vp.rate())
ck("2", "越界夹紧", vp.set_rate(99) and abs(vp.rate() - 3.0) < 0.01, vp.rate())
vp.set_rate(1.0)
print()

print("=" * 72)
print("三、语音条 UI 完整功能")
print("=" * 72)
CFG["ds_api_key"] = "sk-x"
with open(m.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(CFG, f, ensure_ascii=False)
c = m.PetApp(); app.processEvents()
c._open_chat(); app.processEvents()
cw = c.chat_window
# 带原文
msg = cw.append_external("assistant", "🎵 测试歌曲",
                         voice_path=wav_path, voice_sec=6.0,
                         voice_text="这是这首歌的歌词内容，用来测试转文字功能。")
pump(0.5)
from petcore.chat import ChatBubble
bubbles = [b for b in cw.findChildren(ChatBubble)
           if getattr(b, "voice_btn", None) is not None]
ck("3", "语音条气泡", len(bubbles) >= 1)
b = bubbles[-1]
ck("3", "有波形条", getattr(b, "wave_bar", None) is not None)
ck("3", "波形已采样", len(b.wave_bar._peaks) == 36)
ck("3", "有时间显示", getattr(b, "voice_time", None) is not None,
   b.voice_time.text() if getattr(b, "voice_time", None) else "")
ck("3", "有倍速按钮", getattr(b, "voice_rate_btn", None) is not None)
ck("3", "倍速按钮初始 1.0x", b.voice_rate_btn.text() == "1.0x",
   b.voice_rate_btn.text())
ck("3", "有转文字按钮", getattr(b, "voice_text_btn", None) is not None)
ck("3", "原文已存", getattr(b, "_voice_text", "") != "",
   getattr(b, "_voice_text", "")[:20])

ck("3", "播放条常驻显示", b.voice_ctrl.isVisible())
# 播放 → 按钮变 ⏸，角标显示剩余秒数
b._play_voice()
pump(1.0)
ck("3", "播放钮变 ⏸", b.voice_pause_btn.text() == "⏸",
   b.voice_pause_btn.text())
ck("3", "时间格式为 进度/总时长",
   b.voice_time.text() == "00:00 / 00:06", b.voice_time.text())

# 倍速循环
b._cycle_voice_rate()
pump(0.3)
ck("3", "倍速切到 1.25x", b.voice_rate_btn.text() == "1.25x",
   b.voice_rate_btn.text())
b._cycle_voice_rate()
pump(0.3)
ck("3", "倍速切到 1.5x", b.voice_rate_btn.text() == "1.5x",
   b.voice_rate_btn.text())
b._cycle_voice_rate()   # 2.0x
pump(0.2)
ck("3", "倍速切到 2.0x", b.voice_rate_btn.text() == "2.0x",
   b.voice_rate_btn.text())
b._cycle_voice_rate()   # 0.75x
pump(0.2)
ck("3", "倍速切到 0.75x", b.voice_rate_btn.text() == "0.75x",
   b.voice_rate_btn.text())
b._cycle_voice_rate()   # 回到 1.0x
pump(0.2)
ck("3", "循环回到 1.0x", b.voice_rate_btn.text() == "1.0x",
   b.voice_rate_btn.text())

# 转文字
b._toggle_voice_text()
pump(0.3)
ck("3", "转文字展开", getattr(b, "voice_text_area", None) is not None
   and b.voice_text_area.isVisible())
if getattr(b, "voice_text_area", None) is not None:
    ta = b.voice_text_area.text()
    print("  展开的文字: %r" % ta[:50])
    ck("3", "转文字内容正确", "歌词内容" in ta, ta[:40])
b._toggle_voice_text()
pump(0.3)
ck("3", "再点收起", not b.voice_text_area.isVisible())

# 暂停 / 停止回归
b._toggle_pause_voice()
pump(0.4)
ck("3", "暂停后按钮变 ▶", b.voice_pause_btn.text() == "▶",
   b.voice_pause_btn.text())
b._stop_voice()
pump(0.4)
ck("3", "停止后复位为 00:00 / 总时长",
   b.voice_time.text() == "00:00 / 00:06", b.voice_time.text())
try:
    for w in c.pets: w.hide()
    if c.tray: c.tray.hide()
except Exception:
    pass
print()

print("=" * 72)
print("四、历史重开后保留全部控件")
print("=" * 72)
c._open_chat(); app.processEvents()
pump(0.5)
cw2 = c.chat_window
bubbles2 = [b for b in cw2.findChildren(ChatBubble)
            if getattr(b, "voice_btn", None) is not None]
ck("4", "重开后语音条在", len(bubbles2) >= 1, len(bubbles2))
if bubbles2:
    b2 = bubbles2[-1]
    ck("4", "波形仍在", getattr(b2, "wave_bar", None) is not None)
    ck("4", "倍速仍在", getattr(b2, "voice_rate_btn", None) is not None)
    ck("4", "转文字按钮仍在", getattr(b2, "voice_text_btn", None) is not None)
    ck("4", "原文保留", getattr(b2, "_voice_text", "") != "",
       getattr(b2, "_voice_text", "")[:20])
    # 点转文字还能展开
    b2._toggle_voice_text()
    pump(0.3)
    ck("4", "转文字可用",
       getattr(b2, "voice_text_area", None) is not None
       and b2.voice_text_area.isVisible())
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
