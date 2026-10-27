# -*- coding: utf-8 -*-
"""验证：视觉描述风格 + 单次上限（与聊天页独立）"""
import importlib.util
import json
import os
import sys
import tempfile

APP = os.path.dirname(os.path.abspath(__file__))    # 自适应：测试文件所在目录即项目根
TESTDIR = os.path.join(tempfile.gettempdir(), "vision-style-verify")
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR          # ★ 隔离，不动用户配置
os.environ["DFY_BUNDLE_DIR"] = APP

os.chdir(APP)
spec = importlib.util.spec_from_file_location("petmod", os.path.join(APP, "桌宠.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from PySide6.QtWidgets import QApplication, QComboBox
from PySide6.QtCore import Qt

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
    "ds_api_key": "sk-chat-key", "city": "重庆", "birthday": "", "animation_mode": "off",
    "usage": mod._default_usage(), "games": mod._default_games(),
    "reminders": mod._default_reminders(), "balance_full": 20.0,
    "features": mod._default_features(),
    "pets": [{"id": "main", "size": 0.7, "x": 600, "y": 300, "mode": "still", "display": True}],
}
with open(mod.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(CFG, f, ensure_ascii=False)

c = mod.PetApp()
app.processEvents()

print("=== 1. 常量 ===")
ck("有 VISION_STYLE_CHOICES", hasattr(mod, "VISION_STYLE_CHOICES"))
ck("3 档风格", len(mod.VISION_STYLE_CHOICES) == 3, mod.VISION_STYLE_CHOICES)
ck("默认 normal", mod.VISION_STYLE_DEFAULT == "normal")
ck("有 VISION_STYLE_SUFFIXES", hasattr(mod, "VISION_STYLE_SUFFIXES"))
ck("简短句正确", mod.vision_style_suffix("brief") == "用 100 字以内描述。",
   mod.vision_style_suffix("brief"))
ck("适中句正确", mod.vision_style_suffix("normal") == "用 200 字以内描述。")
ck("详细句正确", mod.vision_style_suffix("detail") == "用 400 字以内描述。")
ck("上限范围 100~2000",
   (mod.VISION_TOKENS_MIN, mod.VISION_TOKENS_MAX) == (100, 2000),
   (mod.VISION_TOKENS_MIN, mod.VISION_TOKENS_MAX))
ck("上限默认 1200", mod.VISION_TOKENS_DEFAULT == 1200)

print("=== 2. 配置默认值 ===")
saved = json.load(open(mod.CONFIG_PATH, encoding="utf-8"))
ls0 = saved.get("look_screen") or {}
ck("默认有 style", ls0.get("style") == "normal", ls0.get("style"))
ck("默认有 max_tokens", ls0.get("max_tokens") == 1200, ls0.get("max_tokens"))

print("=== 3. look_screen 用新配置（不是 VISION_TOKENS_DEFAULT）===")
c.cfg["look_screen"]["style"] = "brief"
c.cfg["look_screen"]["max_tokens"] = 555
suf, mt = c._vision_style_cfg()
ck("读到风格句", suf == "用 100 字以内描述。", suf)
ck("读到上限 555", mt == 555, mt)
c.cfg["look_screen"]["max_tokens"] = 99999
_, mt2 = c._vision_style_cfg()
ck("超上限夹到 2000", mt2 == 2000, mt2)
c.cfg["look_screen"]["max_tokens"] = 5
_, mt3 = c._vision_style_cfg()
ck("超下限夹到 100", mt3 == 100, mt3)
c.cfg["look_screen"]["max_tokens"] = 1200
c.cfg["look_screen"]["style"] = "normal"

print("=== 4. 设置页 5 行控件（顺序）===")
c.open_settings("chat")
app.processEvents()
sw = c.settings_window
ck("有 vision_key", hasattr(sw, "vision_key"))
ck("有 vision_base", hasattr(sw, "vision_base"))
ck("有 vision_model", hasattr(sw, "vision_model"))
ck("有 vision_style（下拉）", isinstance(sw.vision_style, QComboBox))
ck("有 _vision_tokens_field", hasattr(sw, "_vision_tokens_field"))
ck("风格 3 项", sw.vision_style.count() == 3, sw.vision_style.count())
ck("风格项文字", [sw.vision_style.itemText(i) for i in range(3)] ==
   ["简短（100 字）", "适中（200 字）", "详细（400 字）"],
   [sw.vision_style.itemText(i) for i in range(3)])


def idx_of(target, layout):
    res = []

    def walk(l, path=""):
        for i in range(l.count()):
            it = l.itemAt(i)
            if it is None:
                continue
            if it.widget() is target:
                res.append(path + str(i))
            sub = it.layout()
            if sub is not None:
                walk(sub, path + str(i) + ".")
    walk(layout)
    return res


lay = sw._vision_section.body_layout   # 「视觉」已并入聊天页第 ④ 组
sw._vision_section.set_expanded(True)  # 展开，保证内部控件 isVisible 有意义
app.processEvents()

# 视觉五项现在在同一个 QFormLayout 里：按行序检查
from PySide6.QtWidgets import QFormLayout


def find_form(l):
    """在布局树里找视觉区那个 QFormLayout。"""
    hits = []

    def walk(x):
        if isinstance(x, QFormLayout):
            # 看它是否包含 vision_key 所在的容器
            for r in range(x.rowCount()):
                it = x.itemAt(r, QFormLayout.ItemRole.FieldRole)
                w = it.widget() if it is not None else None
                if w is not None and (w is sw.vision_base or w is sw.vision_model
                                      or sw.vision_key in w.findChildren(type(sw.vision_key))):
                    hits.append(x)
                    return
        for i in range(x.count() if hasattr(x, "count") else 0):
            it = x.itemAt(i)
            if it is None:
                continue
            sub = it.layout()
            if sub is not None:
                walk(sub)
    walk(l)
    return hits[0] if hits else None


form = find_form(lay)
ck("找到 QFormLayout", form is not None)
if form is not None:
    labels = []
    for r in range(form.rowCount()):
        it = form.itemAt(r, QFormLayout.ItemRole.LabelRole)
        lab = it.widget() if it is not None else None
        labels.append(lab.text() if lab is not None else "")
    ck("基础表单 4 行（高级设置已分离）", len(labels) == 4, labels)
    ck("行序: Key→地址→模型→风格",
       labels == ["视觉 Key", "接口地址", "视觉模型", "描述风格"], labels)
    ck("标签右对齐",
       bool(form.labelAlignment() & Qt.AlignmentFlag.AlignRight),
       form.labelAlignment())


print("=== 5. 风格说明（已改为 tooltip）===")
sw._refresh_vision_style_hint()
h = sw._vision_style_hint.toolTip()
ck("说明非空", bool(h), h)
ck("说明含当前追加句", "用 200 字以内描述。" in h, h)
ck("说明提到 system", "system" in h, h)

print("=== 6. 切换风格 → 立即保存 + 灰字更新 ===")
sw.vision_style.setCurrentIndex(0)   # 简短
app.processEvents()
ls = c.cfg.get("look_screen") or {}
ck("brief 已存", ls.get("style") == "brief", ls.get("style"))
ck("说明变 100 字", "用 100 字以内描述。" in sw._vision_style_hint.toolTip(),
   sw._vision_style_hint.toolTip())
sw.vision_style.setCurrentIndex(2)   # 详细
app.processEvents()
ls = c.cfg.get("look_screen") or {}
ck("detail 已存", ls.get("style") == "detail", ls.get("style"))
ck("说明变 400 字", "用 400 字以内描述。" in sw._vision_style_hint.toolTip())

print("=== 7. 改单次上限 → 立即保存 ===")
s, box = sw.vision_tokens_slider, sw.vision_tokens_spin
box.setValue(600)
app.processEvents()
ls = c.cfg.get("look_screen") or {}
ck("600 已存", ls.get("max_tokens") == 600, ls.get("max_tokens"))
ck("滑块联动", s.value() == 600, s.value())
s.setValue(1500)
app.processEvents()
ck("滑块改后数字框联动", int(round(box.value())) == 1500, box.value())
ls = c.cfg.get("look_screen") or {}
ck("1500 已存", ls.get("max_tokens") == 1500, ls.get("max_tokens"))

print("=== 8. 清空只清 Key/地址/模型，保留风格与上限 ===")
c.cfg["look_screen"]["style"] = "brief"
c.cfg["look_screen"]["max_tokens"] = 700
sw._vision_fill_from_cfg()
sw._clear_vision_cfg()
app.processEvents()
ls = c.cfg.get("look_screen") or {}
ck("Key 清空", ls.get("vision_key") == "")
ck("地址清空", ls.get("vision_base") == "")
ck("模型清空", ls.get("vision_model") == "")
ck("风格保留 brief", ls.get("style") == "brief", ls.get("style"))
ck("上限保留 700", ls.get("max_tokens") == 700, ls.get("max_tokens"))
ck("控件仍显示 brief", sw.vision_style.currentData() == "brief", sw.vision_style.currentData())

print("=== 9. reload 重填两项 ===")
c.cfg["look_screen"]["style"] = "detail"
c.cfg["look_screen"]["max_tokens"] = 1800
sw.reload()
app.processEvents()
ck("reload 后风格=detail", sw.vision_style.currentData() == "detail",
   sw.vision_style.currentData())
ck("reload 后上限=1800", int(round(sw.vision_tokens_spin.value())) == 1800,
   sw.vision_tokens_spin.value())

print("=== 10. show_page 复用窗口不丢 ===")
c.open_settings("chat")
app.processEvents()
ck("复用同一窗口", c.settings_window is sw)
ck("风格仍在 detail", sw.vision_style.currentData() == "detail")
ck("上限仍 1800", int(round(sw.vision_tokens_spin.value())) == 1800)

print("=== 11. 与聊天页完全独立 ===")
chat_tokens = int(c.cfg.get("max_tokens", 400))
chat_style = c.cfg.get("reply_style", "brief")
vis_tokens = (c.cfg.get("look_screen") or {}).get("max_tokens")
vis_style = (c.cfg.get("look_screen") or {}).get("style")
ck("视觉上限 != 聊天上限", vis_tokens != chat_tokens, (vis_tokens, chat_tokens))
ck("视觉风格 != 聊天风格", vis_style != chat_style, (vis_style, chat_style))
# 改聊天的不应影响视觉
sw2 = sw
old_vis_tok = vis_tokens
sw2.max_tokens_spin.setValue(999)
app.processEvents()
ck("改聊天上限不动视觉上限",
   (c.cfg.get("look_screen") or {}).get("max_tokens") == old_vis_tok,
   (c.cfg.get("look_screen") or {}).get("max_tokens"))

print("=== 12. 校验器 ===")
cfg2, _a, bad = mod.migrate_and_validate(
    {"look_screen": {"style": "huge", "max_tokens": 99999}})
ck("非法风格记 issue", "look_screen.style" in bad, bad)
ck("超界上限记 issue", "look_screen.max_tokens" in bad, bad)
ck("合法值保留",
   mod.migrate_and_validate({"look_screen": {"style": "brief", "max_tokens": 300}})[0]
   ["look_screen"]["style"] == "brief")
ck("旧配置补默认",
   mod.migrate_and_validate({})[0]["look_screen"]["style"] == "normal")
ck("旧配置补上限",
   mod.migrate_and_validate({})[0]["look_screen"]["max_tokens"] == 1200)

print()
print("RESULT:", sum(1 for _, v in ok if v), "/", len(ok))
print("FAILED:", [n for n, v in ok if not v] or "none")

sw.hide()
for w in c.pets:
    w.hide()
if c.tray:
    c.tray.hide()
app.quit()
