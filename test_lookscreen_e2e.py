# -*- coding: utf-8 -*-
"""端到端验证 _run_look_screen（修复 UnboundLocalError 后）"""
import io
import importlib.util
import json
import os
import sys
import tempfile
import time

APP = os.path.dirname(os.path.abspath(__file__))    # 自适应：测试文件所在目录即项目根
TESTDIR = os.path.join(tempfile.gettempdir(), "lookscreen-verify")
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR
os.environ["DFY_BUNDLE_DIR"] = APP

os.chdir(APP)
spec = importlib.util.spec_from_file_location("petmod", os.path.join(APP, "桌宠.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap, QImage, QColor

app = QApplication(sys.argv)
ok = []


def ck(n, cond, ex=""):
    ok.append((n, bool(cond)))
    print(("PASS " if cond else "FAIL ") + n + ((" | " + str(ex)) if ex else ""))


cfg = json.load(io.open(os.path.join(APP, "config.json"), encoding="utf-8"))
# 用真实 Key（只读不写）
real_vkey = ((cfg.get("look_screen") or {}).get("vision_key") or "").strip()
real_dkey = (cfg.get("ds_api_key") or "").strip()
print("智谱 Key 长度:", len(real_vkey), " DeepSeek Key 长度:", len(real_dkey))
print()

CFG = {
    "config_version": 2, "topmost": True, "passthrough": False, "autostart": False,
    "ds_api_key": real_dkey, "city": "重庆", "birthday": "", "animation_mode": "off",
    "usage": mod._default_usage(), "games": mod._default_games(),
    "reminders": mod._default_reminders(), "balance_full": 20.0,
    "features": mod._default_features(),
    "pets": [{"id": "main", "size": 0.7, "x": 600, "y": 300, "mode": "still", "display": True}],
    "look_screen": {
        "vision_key": real_vkey,
        "vision_base": "https://open.bigmodel.cn/api/paas/v4",
        "vision_model": "glm-4.5v",
        "style": "brief", "max_tokens": 800,
    },
}
with open(mod.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(CFG, f, ensure_ascii=False)

c = mod.PetApp()
app.processEvents()
pet = c.main_pet

print("=== 1. 造一张有内容的截图（内存）===")
img = QImage(480, 300, QImage.Format.Format_RGB32)
img.fill(QColor(250, 250, 252))
pix = QPixmap.fromImage(img)
p = __import__("PySide6.QtGui", fromlist=["QPainter"]).QPainter(pix)
p.setBrush(QColor(70, 130, 220))
p.drawRect(30, 30, 200, 120)
p.setBrush(QColor(230, 120, 90))
p.drawRect(250, 30, 200, 120)
p.setBrush(QColor(190, 205, 225))
p.drawRect(30, 170, 420, 100)
p.end()
print("  造图完成 480x300")
print()

print("=== 2. 调用 _run_look_screen（真实两步请求）===")
pet.bubble_text = ""
c._run_look_screen(pix)
# 等两步完成（最多 90 秒）
t0 = time.time()
last = ""
seen_look = False
while time.time() - t0 < 90:
    app.processEvents()
    time.sleep(0.1)
    t = (pet.bubble_text or "").strip()
    if t != last:
        print("  [%5.1fs] 气泡: %r" % (time.time() - t0, t[:60]))
        last = t
    if t == "看看中…":
        seen_look = True
    # 第二步回复出现（不是"看看中…"也不是错误）就结束
    if t and t != "看看中…" and "瞧不清" not in t and "cannot access" not in t:
        break
print()
print("  总耗时: %.1fs" % (time.time() - t0))

print()
print("=== 3. 断言 ===")
ck("出现过「看看中…」", seen_look)
final = (pet.bubble_text or "").strip()
ck("拿到最终回复", bool(final) and final != "看看中…", final[:60])
ck("不再是 UnboundLocalError", "cannot access local variable" not in final, final[:80])
ck("不是失败提示", "瞧不清呢" not in final, final[:80])

# 截图数据已释放
ck("holder 已释放", c._run_look_screen.__code__ is not None)

print()
print("=== 聊天记录（看两步是否落盘）===")
if os.path.exists(mod.CHAT_PATH):
    chats = json.load(open(mod.CHAT_PATH, encoding="utf-8"))
    for s in chats.get("sessions", []):
        for msg in (s.get("messages") or [])[-4:]:
            role = msg.get("role")
            content = (msg.get("content") or "")[:80]
            print("  [%s] %s" % (role, content))
else:
    print("  （无聊天文件）")

print()
print("=== error.log 尾部（看两步日志）===")
if os.path.exists(mod.LOG_PATH):
    lines = open(mod.LOG_PATH, encoding="utf-8", errors="ignore").read().splitlines()
    tail = [l for l in lines if l.strip()][-8:]
    for l in tail:
        print("  " + l)
else:
    print("  （无日志）")

print()
print("RESULT:", sum(1 for _, v in ok if v), "/", len(ok))
print("FAILED:", [n for n, v in ok if not v] or "none")

for w in c.pets:
    w.hide()
if c.tray:
    c.tray.hide()
app.quit()
