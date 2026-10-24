# -*- coding: utf-8 -*-
"""语音暂停 / 继续 / 停止 验证"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import importlib.util

FORMAL = os.path.normcase(os.path.abspath(r"D:\Software\Portable\应用程序\dafeiyu-pet"))
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-pause")
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


print("=" * 72)
print("一、VoicePlayer 新增接口")
print("=" * 72)
for fn in ("state", "is_playing", "is_paused", "pause", "resume", "stop",
           "position_ms", "seek_ms"):
    ck("1", "方法 %s" % fn, hasattr(V.VoicePlayer, fn))

vp = V.VoicePlayer(lambda: {"enabled": True, "volume": 80,
                            "engine": "edge", "url": "http://127.0.0.1:9880",
                            "vox_url": "", "vox_control": "", "vox_steps": 24,
                            "timeout": 120})
st0 = vp.state()
ck("1", "初始状态可读", st0 in ("stopped", "unavailable"), st0)
ck("1", "position_ms 可读", isinstance(vp.position_ms(), int), vp.position_ms())
print()

print("=" * 72)
print("二、真实播放 + 暂停 + 继续")
print("=" * 72)
# 造一个 6 秒的 wav（静音），直接喂给 _play_bytes
import wave, struct
wav_path = os.path.join(TESTDIR, "test6s.wav")
with wave.open(wav_path, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(22050)
    n = 22050 * 6
    w.writeframes(b"\x00\x00" * n)
data = open(wav_path, "rb").read()
print("  测试音频: 6 秒, %d 字节" % len(data))

vp._play_bytes(data, 80)
pump(1.0)
st = vp.state()
print("  播放中状态:", st)
ck("2", "开始播放", st == "playing", st)

# 暂停
ok = vp.pause()
pump(0.3)
st2 = vp.state()
print("  pause() 返回:", ok, " 状态:", st2)
ck("2", "pause 返回 True", ok is True)
ck("2", "状态变 paused", st2 == "paused", st2)
pos_paused = vp.position_ms()
print("  暂停位置: %d ms" % pos_paused)

# 暂停期间位置应基本不变
pump(1.0)
pos2 = vp.position_ms()
print("  1 秒后位置: %d ms（应基本不变）" % pos2)
ck("2", "暂停后位置不再前进", abs(pos2 - pos_paused) < 400,
   "%d → %d" % (pos_paused, pos2))

# 继续
ok2 = vp.resume()
pump(0.5)
st3 = vp.state()
print("  resume() 返回:", ok2, " 状态:", st3)
ck("2", "resume 返回 True", ok2 is True)
ck("2", "状态变 playing", st3 == "playing", st3)
pos3 = vp.position_ms()
ck("2", "继续后位置前进", pos3 > pos_paused, "%d → %d" % (pos_paused, pos3))

# 停止
ok3 = vp.stop()
pump(0.3)
st4 = vp.state()
ck("2", "stop 后状态 stopped", st4 == "stopped", st4)

# seek
vp._play_bytes(data, 80)
pump(0.6)
vp.seek_ms(4000)
pump(0.4)
pos_seek = vp.position_ms()
print("  seek 到 4000ms 后位置:", pos_seek)
ck("2", "seek 生效", pos_seek >= 3500, pos_seek)
vp.stop()
print()

print("=" * 72)
print("三、语音条 UI 三态")
print("=" * 72)
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

c = m.PetApp(); app.processEvents()
c._open_chat(); app.processEvents()
cw = c.chat_window

msg = cw.append_external("assistant", "🎵 测试语音",
                         voice_path=wav_path, voice_sec=6.0)
pump(0.5)
# 找那条气泡
from petcore.chat import ChatBubble
bubbles = [b for b in cw.findChildren(ChatBubble)
           if getattr(b, "voice_btn", None) is not None]
ck("3", "找到语音条气泡", len(bubbles) >= 1, len(bubbles))
if bubbles:
    b = bubbles[-1]
    ck("3", "有暂停按钮", getattr(b, "voice_pause_btn", None) is not None)
    ck("3", "有停止按钮", getattr(b, "voice_stop_btn", None) is not None)
    ck("3", "控制条默认隐藏", not b.voice_ctrl.isVisible())
    # 播放
    b._play_voice()
    pump(0.8)
    ck("3", "播放后控制条显示", b.voice_ctrl.isVisible())
    ck("3", "播放中按钮显示暂停图标", b.voice_pause_btn.text() == "⏸",
       b.voice_pause_btn.text())
    ck("3", "语音条显示播放中", "播放中" in b.voice_btn.text(),
       b.voice_btn.text())
    # 再点 → 暂停
    b._play_voice()
    pump(0.5)
    ck("3", "暂停后语音条显示已暂停", "已暂停" in b.voice_btn.text(),
       b.voice_btn.text())
    ck("3", "暂停后按钮变继续图标", b.voice_pause_btn.text() == "▶",
       b.voice_pause_btn.text())
    # 再点 → 继续
    b._play_voice()
    pump(0.6)
    ck("3", "继续后显示播放中", "播放中" in b.voice_btn.text(),
       b.voice_btn.text())
    ck("3", "继续后按钮变暂停图标", b.voice_pause_btn.text() == "⏸",
       b.voice_pause_btn.text())
    # 停止
    b._stop_voice()
    pump(0.4)
    ck("3", "停止后控制条隐藏", not b.voice_ctrl.isVisible())
    ck("3", "停止后语音条复位", "\"" in b.voice_btn.text(), b.voice_btn.text())

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
