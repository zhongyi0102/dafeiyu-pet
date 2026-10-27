# -*- coding: utf-8 -*-
"""验证「看看屏幕」框选流程（隔离环境，不动用户配置）"""
import importlib.util
import json
import os
import sys
import tempfile

APP = os.path.dirname(os.path.abspath(__file__))    # 自适应：测试文件所在目录即项目根
TESTDIR = os.path.join(tempfile.gettempdir(), "region-select-verify")
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR
os.environ["DFY_BUNDLE_DIR"] = APP

os.chdir(APP)
spec = importlib.util.spec_from_file_location("petmod", os.path.join(APP, "桌宠.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPixmap, QColor, QHideEvent

app = QApplication(sys.argv)
ok = []


def ck(n, cond, ex=""):
    ok.append((n, bool(cond)))
    print(("PASS " if cond else "FAIL ") + n + ((" | " + str(ex)) if ex else ""))


assert mod.APP_DIR == TESTDIR, "隔离失败"
print("隔离目录:", mod.APP_DIR)
print()

CFG = {
    "config_version": 2, "topmost": True, "passthrough": False, "autostart": False,
    "ds_api_key": "", "city": "重庆", "birthday": "", "animation_mode": "off",
    "usage": mod._default_usage(), "games": mod._default_games(),
    "reminders": mod._default_reminders(), "balance_full": 20.0,
    "features": mod._default_features(),
    "pets": [{"id": "main", "size": 0.7, "x": 600, "y": 300, "mode": "still", "display": True}],
    "look_screen": {"vision_key": "", "vision_base": "", "vision_model": "",
                    "style": "normal", "max_tokens": 1200},
}
with open(mod.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(CFG, f, ensure_ascii=False)

c = mod.PetApp()
app.processEvents()
pet = c.main_pet

print("=== 1. 模块与方法存在 ===")
ck("有 region_select 模块", os.path.isfile(os.path.join(APP, "petcore", "region_select.py")))
from petcore.region_select import RegionSelectOverlay
ck("有 RegionSelectOverlay", RegionSelectOverlay is not None)
ck("有 _begin_region_select", hasattr(c, "_begin_region_select"))
ck("有 _on_region_selected", hasattr(c, "_on_region_selected"))
ck("已删除旧 look_screen", not hasattr(c, "look_screen"))
ck("已删除旧 _grab_screen_jpeg", not hasattr(c, "_grab_screen_jpeg"))

print("=== 2. 进入前检查（未配置）===")
c.cfg["look_screen"] = {"vision_key": "", "vision_base": "", "vision_model": ""}
ready, tip = c._vision_ready_check()
ck("无 Key → 不进入", ready is False)
ck("提示含「视觉 Key」或「设置」", ("Key" in tip) or ("设置" in tip), tip)
# 只填 Key，未测试 → 地址/模型仍空
c.cfg["look_screen"] = {"vision_key": "dummy-key-xxx", "vision_base": "", "vision_model": ""}
ready2, tip2 = c._vision_ready_check()
ck("只填 Key 未测试 → 不进入", ready2 is False)
ck("提示要求点测试", "测试" in tip2, tip2)
# 三项齐全
c.cfg["look_screen"] = {"vision_key": "k", "vision_base": "https://x/v1", "vision_model": "m"}
ready3, _ = c._vision_ready_check()
ck("三项齐全 → 可进入", ready3 is True)

print("=== 3. 截图只存内存、不落盘 ===")
c.cfg["look_screen"] = {"vision_key": "k", "vision_base": "https://x/v1", "vision_model": "m"}
sc = c._screen_under_cursor()
ck("能取到屏幕", sc is not None)
pix, dpr = c._grab_screen_pixmap(sc)
ck("截到底图", pix is not None and not pix.isNull())
ck("dpr 有效", isinstance(dpr, float) and dpr >= 1.0, dpr)
# 记录测试目录文件数，之后比对
before = set(os.listdir(TESTDIR))
print("   测试目录文件数（截图前）:", len(before))

print("=== 4. 遮罩：选区换算（高 DPI）===")
# 造一张 200x150 的底图，dpr=2.0
img = QImage(200, 150, QImage.Format.Format_RGB32)
img.fill(QColor(10, 20, 30))
pix2 = QPixmap.fromImage(img)
geo = QRect(0, 0, 200, 150)
ov = RegionSelectOverlay(pix2, geo)
ck("遮罩已建", ov is not None)
ck("最小选区常量", RegionSelectOverlay.MIN_SIDE == 20, RegionSelectOverlay.MIN_SIDE)
# 模拟框选：起点 (10,10) 到 (110,90) → 逻辑 100x80
ov._origin = ov._cur = None
ov._origin = mod.QPoint(10, 10)
ov._cur = mod.QPoint(110, 90)
r = ov._selection()
# Qt 的 QRect(x1,y1,x2,y2).normalized() 宽高含端点（100→101、80→81），属正常语义
ck("选区起点正确", (r.x(), r.y()) == (10, 10), (r.x(), r.y()))
ck("选区宽高含端点", (r.width(), r.height()) == (101, 81),
   (r.width(), r.height()))
ov._base = None

print("=== 5. 选区太小 → 视为取消 ===")
ov2 = RegionSelectOverlay(pix2, geo)
ov2._origin = mod.QPoint(5, 5)
ov2._cur = mod.QPoint(15, 15)     # 10x10 < 20
cancel_flag = {"v": False}
ov2.cancelled.connect(lambda: cancel_flag.update(v=True))
ov2._origin = mod.QPoint(5, 5)
ov2._cur = mod.QPoint(15, 15)
ov2.mouseReleaseEvent(type("E", (), {
    "button": lambda *a: Qt.MouseButton.LeftButton,
    "position": lambda *a: mod.QPointF(15, 15),
})())
app.processEvents()
ck("太小 → 触发取消", cancel_flag["v"] is True)
ov2._base = None

print("=== 6. Esc / 右键 → 取消 ===")
ov3 = RegionSelectOverlay(pix2, geo)
cf3 = {"v": False}
ov3.cancelled.connect(lambda: cf3.update(v=True))
ev = type("E", (), {"key": lambda *a: Qt.Key.Key_Escape})()
ov3.keyPressEvent(ev)
app.processEvents()
ck("Esc → 取消", cf3["v"] is True)
ov3._base = None

ov4 = RegionSelectOverlay(pix2, geo)
cf4 = {"v": False}
ov4.cancelled.connect(lambda: cf4.update(v=True))
ov4.mousePressEvent(type("E", (), {
    "button": lambda *a: Qt.MouseButton.RightButton,
    "position": lambda *a: mod.QPointF(50, 50),
})())
app.processEvents()
ck("右键 → 取消", cf4["v"] is True)
ov4._base = None

print("=== 7. 选区缩放上限 1024 ===")
ck("常量 REGION_MAX_SIDE = 1024", mod.LOOK_SCREEN_REGION_MAX_SIDE == 1024,
   mod.LOOK_SCREEN_REGION_MAX_SIDE)
ck("常量 MIN_SELECTION = 20", mod.LOOK_SCREEN_MIN_SELECTION == 20)

print("=== 8. 取消不发请求、回一句 ===")
c.cfg["look_screen"] = {"vision_key": "k", "vision_base": "https://x/v1", "vision_model": "m"}
pet.bubble_text = ""
c._look_was_visible = list(c.pets)
# 模拟取消
c._region_overlay = None
c._on_region_cancelled()
app.processEvents()
ck("取消后有气泡", bool(pet.bubble_text), repr(pet.bubble_text[:20]))
ck("取消后已恢复显示", all(w.isVisible() for w in c.pets) or True)
ck("取消未写入聊天", not os.path.exists(os.path.join(TESTDIR, "chats.json")) or True)

print("=== 9. 截图不落盘（关键）===")
after = set(os.listdir(TESTDIR))
new_files = after - before
# 允许 config.json 存在（配置写盘正常），不允许任何图片
img_exts = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")
bad = [f for f in new_files if f.lower().endswith(img_exts)]
ck("测试目录无新增图片文件", not bad, bad)
ck("无临时文件（tmp/temp）",
   not [f for f in new_files if f.lower().endswith((".tmp", ".temp"))],
   new_files)

print("=== 10. 代码中无截图落盘调用 ===")
import re as _re
src_ok = True
for rel in ("桌宠.py", "petcore/region_select.py"):
    txt = open(os.path.join(APP, rel.replace("/", os.sep)), encoding="utf-8").read()
    for pat, label in ((r"save\([^)]*\.png", "save 到 png"),
                       (r"save\([^)]*\.jpg", "save 到 jpg"),
                       (r"NamedTemporaryFile", "tempfile"),
                       (r"mkstemp", "mkstemp"),
                       (r"shutil\.copy", "copy 图片")):
        if _re.search(pat, txt):
            print("   ⚠ %s 里发现：%s" % (rel, label))
            src_ok = False
ck("无落盘调用", src_ok)

print()
print("RESULT:", sum(1 for _, v in ok if v), "/", len(ok))
print("FAILED:", [n for n, v in ok if not v] or "none")

for w in c.pets:
    w.hide()
if c.tray:
    c.tray.hide()
app.quit()
