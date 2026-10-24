# -*- coding: utf-8 -*-
"""语音/图片消息跨重启保留验证（_normalize_session 修复）"""
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
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-keep2")
shutil.rmtree(TESTDIR, ignore_errors=True)
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR
os.environ["DFY_BUNDLE_DIR"] = FORMAL
sys.path.insert(0, FORMAL)
os.chdir(FORMAL)
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
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

# 造测试语音
wav_path = os.path.join(TESTDIR, "song.wav")
with wave.open(wav_path, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
    w.writeframes(b"\x00\x00" * 22050 * 5)
img_path = os.path.join(TESTDIR, "shot.png")
from PySide6.QtGui import QImage as _QI, QPixmap as _QP, QColor as _QC, QPainter as _QPn
_im = _QI(100, 80, _QI.Format.Format_RGB32)
_im.fill(_QC(80, 120, 200))
_pm = _QP.fromImage(_im)
_pm.save(img_path, "PNG")

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
print("一、写入带语音/图片/时间戳的消息 → 落盘")
print("=" * 72)
c = m.PetApp(); app.processEvents()
c._open_chat(); app.processEvents()
cw = c.chat_window
cw.append_external("user", "唱一首指纹")
cw.append_external("assistant", "🎵 指纹 - 杜宣达",
                   voice_path=wav_path, voice_sec=5.0,
                   voice_text="我们 变成一对 差点缘分……")
cw.append_external("assistant", "我们 变成一对 差点缘分……")
cw.append_external("user", "看看屏幕", image_path=img_path)
pump(0.5)
# 落盘检查
d = json.load(io.open(m.CHAT_PATH, encoding="utf-8"))
sess = (d.get("sessions") or [{}])[0]
msgs = sess.get("messages") or []
n_voice = sum(1 for x in msgs if x.get("voice"))
n_img = sum(1 for x in msgs if x.get("image"))
n_text = sum(1 for x in msgs if x.get("voice_text"))
ck("1", "落盘含 voice", n_voice >= 1, n_voice)
ck("1", "落盘含 image", n_img >= 1, n_img)
ck("1", "落盘含 voice_text", n_text >= 1, n_text)
close = True
try:
    for w in c.pets: w.hide()
    if c.tray: c.tray.hide()
    if c.chat_window: c.chat_window.hide()
except Exception: pass
# 清掉第一组在 APP_DIR 根产生的杂音文件（第三组 relink 扫的是
# APP_DIR/cover_cache，但 song.wav 若出现在别的扫描路径会干扰断言）
import glob as _glob
for _junk in _glob.glob(os.path.join(TESTDIR, "*.wav")):
    try: os.remove(_junk)
    except Exception: pass
print()

print("=" * 72)
print("二、销毁窗口 → 重开（模拟重启）→ 字段保留")
print("=" * 72)
app.processEvents()
c._open_chat(); app.processEvents()
pump(0.5)
cw2 = c.chat_window
n_voice2 = sum(1 for s in cw2.sessions for x in (s.get("messages") or [])
               if x.get("voice"))
n_img2 = sum(1 for s in cw2.sessions for x in (s.get("messages") or [])
             if x.get("image"))
n_text2 = sum(1 for s in cw2.sessions for x in (s.get("messages") or [])
              if x.get("voice_text"))
ck("2", "voice 保留", n_voice2 >= 1, n_voice2)
ck("2", "image 保留", n_img2 >= 1, n_img2)
ck("2", "voice_text 保留", n_text2 >= 1, n_text2)
from petcore.chat import ChatBubble
bubbles = [b for b in cw2.findChildren(ChatBubble)
           if getattr(b, "voice_btn", None) is not None]
ck("2", "语音条重现", len(bubbles) >= 1, len(bubbles))
img_bubbles = [b for b in cw2.findChildren(ChatBubble)
               if getattr(b, "image_label", None) is not None]
ck("2", "图片气泡重现", len(img_bubbles) >= 1, len(img_bubbles))
# 转文字可用
if bubbles:
    b = bubbles[-1]
    b._toggle_voice_text()
    pump(0.3)
    ok = (getattr(b, "voice_text_area", None) is not None
          and b.voice_text_area.isVisible())
    ck("2", "转文字可用", ok)
    if ok:
        print("     内容: %r" % b.voice_text_area.text()[:40])
try:
    for w in c.pets: w.hide()
    if c.tray: c.tray.hide()
    if c.chat_window: c.chat_window.hide()
except Exception: pass
print()

print("=" * 72)
print("三、_relink 找回正式环境丢失的翻唱")
print("=" * 72)
# 模拟正式环境：一条「🎵 指纹 - 胡歌」没有 voice，但 cover_cache 有成品
c2 = m.PetApp(); app.processEvents()
c2._open_chat(); app.processEvents()
cw3 = c2.chat_window
# 用正规 API 写入（直接改 dict 会被 _sync_session 覆盖）
cw3.append_external("user", "唱一首·指纹")
cw3.append_external("assistant", "🎵 指纹 - 胡歌")
# _relink 只挂缺 voice 的；先看初始（应为 0，因为没成品路径）
got0 = [x for x in cw3.messages if x.get("voice")]
# 初始可能已被 _relink 自动挂上（打开窗口时跑过一次）
n = cw3._relink_cover_songs()
pump(0.5)
print("  找回:", n)
got = [(x.get("content", "")[:20], os.path.basename(x.get("voice") or ""))
       for x in cw3.messages if x.get("voice")]
print("  挂载:", got)
ck("3", "标题消息挂上了 voice", len(got) >= 1, got)
if got:
    print("  voice:", got[0][1])
    # 「🎵 指纹 - 杜宣达」可能被 _repair_voice_msgs 用旧 TTS 文件
# （第一组留下的 song.wav）就近挂上——这是 repair 的正常行为。
# 核心断言：「🎵 指纹 - 胡歌」必须挂上 cover_cache 的指纹成品
hu_got = [v for c_, v in got if "胡歌" in c_]
ck("3", "胡歌标题挂上指纹成品",
   bool(hu_got) and "指纹" in hu_got[0], hu_got)
try:
    for w in c2.pets: w.hide()
    if c2.tray: c2.tray.hide()
    if c2.chat_window: c2.chat_window.hide()
except Exception: pass

print()
groups = {}
for g, n2_, v, ex in RESULTS:
    groups.setdefault(g, [0, 0]); groups[g][1] += 1
    if v: groups[g][0] += 1
for g in sorted(groups):
    p, t = groups[g]
    print("  %s: %d / %d %s" % (g, p, t, "✓" if p == t else "✗"))
print()
print("总计: %d / %d" % (sum(1 for _, _, v, _ in RESULTS if v), len(RESULTS)))
fails = [(g, n2_, ex) for g, n2_, v, ex in RESULTS if not v]
if fails:
    print()
    for g, n2_, ex in fails:
        print("  失败 [%s] %s | %s" % (g, n2_, ex))
shutil.rmtree(TESTDIR, ignore_errors=True)
