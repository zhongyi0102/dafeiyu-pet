# -*- coding: utf-8 -*-
"""验证第 24 批视觉面板改动（隔离环境，不动用户配置）"""
import importlib.util
import json
import os
import sys
import tempfile

APP = os.path.dirname(os.path.abspath(__file__))    # 自适应：测试文件所在目录即项目根
# ★ 用隔离目录，避免覆盖用户配置
TESTDIR = os.path.join(tempfile.gettempdir(), "vision24-verify")
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR
os.environ["DFY_BUNDLE_DIR"] = APP

os.chdir(APP)
spec = importlib.util.spec_from_file_location("petmod", os.path.join(APP, "桌宠.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

from PySide6.QtWidgets import QApplication, QLineEdit, QComboBox, QPushButton

app = QApplication(sys.argv)
ok = []


def ck(n, cond, ex=""):
    ok.append((n, bool(cond)))
    print(("PASS " if cond else "FAIL ") + n + ((" | " + str(ex)) if ex else ""))


# 确认隔离生效
assert mod.APP_DIR == TESTDIR, "隔离失败！APP_DIR=%s" % mod.APP_DIR
print("隔离目录:", mod.APP_DIR)
print()

CFG = {
    "config_version": 2, "topmost": True, "passthrough": False, "autostart": False,
    "ds_api_key": "", "city": "重庆", "birthday": "", "animation_mode": "off",
    "usage": mod._default_usage(), "games": mod._default_games(),
    "reminders": mod._default_reminders(), "balance_full": 20.0,
    "features": mod._default_features(),
    "pets": [{"id": "main", "size": 0.7, "x": 600, "y": 300, "mode": "still", "display": True}],
    "look_screen": {"vision_key": "", "vision_base": "", "vision_model": ""},
}
with open(mod.CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(CFG, f, ensure_ascii=False)

c = mod.PetApp()
app.processEvents()

print("=== A1. 常量：VISION_MAX_TOKENS 已不存在 ===")
ck("无 VISION_MAX_TOKENS", not hasattr(mod, "VISION_MAX_TOKENS"))
ck("有 VISION_TOKENS_DEFAULT", hasattr(mod, "VISION_TOKENS_DEFAULT"),
   getattr(mod, "VISION_TOKENS_DEFAULT", None))
ck("有 VISION_MAX_VERIFY", hasattr(mod, "VISION_MAX_VERIFY"))

print("=== A2. 候选服务商表（含完整前缀）===")
# 本版按需求只保留 DeepSeek 与智谱
_names = [_n for _n, b in mod.VISION_PROVIDERS] if mod.VISION_PROVIDERS else []
ck("只留 DeepSeek", "DeepSeek" in _names, _names)
ck("只留智谱", "智谱" in _names, _names)
ck("至少 3 家", len(_names) >= 3, _names)
ck("含智谱 paas/v4", any("paas/v4" in b for _n, b in mod.VISION_PROVIDERS))
ck("含 Gemini", any("generativelanguage" in b
                        for _n, b in mod.VISION_PROVIDERS))
ck("无阿里/OpenAI", not any(
    ("compatible-mode" in b) or ("openai.com" in b)
    for _n, b in mod.VISION_PROVIDERS))
ck("有已知视觉模型兜底", hasattr(mod, "VISION_KNOWN_MODELS")
   and "智谱" in mod.VISION_KNOWN_MODELS)
ck("有 vision_join", callable(getattr(mod, "vision_join", None)))
if hasattr(mod, "vision_join"):
    ck("vision_join 去尾斜杠",
       mod.vision_join("https://a.com/v1/", "models") == "https://a.com/v1/models",
       mod.vision_join("https://a.com/v1/", "models"))

print("=== B1. 打开设置不触发识别 ===")
c.open_settings("chat")
app.processEvents()
sw = c.settings_window
ck("未处于识别中", getattr(sw, "_vision_testing", None) in (False, None),
   getattr(sw, "_vision_testing", None))
ck("测试按钮可用", sw._vision_test_btn.isEnabled())
ck("取消按钮隐藏", not sw._vision_cancel_btn.isVisible())
ck("地址初始为空（未识别）", sw.vision_base.text() == "", repr(sw.vision_base.text()))

print("=== B2. 三行控件 + 按钮 ===")
ck("有 视觉 Key（密码）", sw.vision_key.echoMode() == QLineEdit.EchoMode.Password)
ck("有 显示按钮", sw._eye_btn.text() == "显示")
ck("接口地址可编辑", not sw.vision_base.isReadOnly())
ck("有 视觉模型（下拉）", isinstance(sw.vision_model, QComboBox))
ck("有 保存", sw._vision_save_btn.text() == "保存")
ck("有 测试", sw._vision_test_btn.text() == "测试")
ck("有 取消", sw._vision_cancel_btn.text() == "取消")
ck("有 清除 Key", sw._vision_clear_btn.text() == "清除 Key")
ck("Key 无占位提示", not sw.vision_key.placeholderText(), repr(sw.vision_key.placeholderText()))
ck("地址有占位提示", bool(sw.vision_base.placeholderText()))
ck("下拉框为空（模型不再有默认值）",
   sw.vision_model.count() == 0 and sw.vision_model.currentText() == "",
   (sw.vision_model.count(), sw.vision_model.currentText()))
ck("显示按钮同高", sw._eye_btn.height() == 30, sw._eye_btn.height())

print("=== B3. Key 变化 → 只清空地址和模型，不识别 ===")
sw.vision_key.setText("sk-test-key-1234")
app.processEvents()
ck("地址被清空", sw.vision_base.text() == "")
ck("模型被清空", sw.vision_model.count() == 0)
ck("未触发识别", not getattr(sw, "_vision_testing", False))
ck("状态提示要点测试", "测试" in sw._vision_hint.text(), sw._vision_hint.text())

print("=== B4. 模型切换 → 立即保存 ===")
sw.vision_base.setText("https://api.example.com/v1")
sw._fill_vision_models(["m-a", "m-b"])
sw.vision_model.setCurrentText("m-b")
app.processEvents()
ls = c.cfg.get("look_screen") or {}
ck("切到 m-b 已存", ls.get("vision_model") == "m-b", ls.get("vision_model"))
sw.vision_model.setCurrentText("m-a")
app.processEvents()
ls = c.cfg.get("look_screen") or {}
ck("切到 m-a 已存", ls.get("vision_model") == "m-a", ls.get("vision_model"))

print("=== B5. 保存（不发请求）===")
r = sw._save_vision_cfg()
ck("保存成功", r is True)
ck("地址已存", (c.cfg.get("look_screen") or {}).get("vision_base") == "https://api.example.com/v1")

print("=== B6. 清空 ===")
sw._clear_vision_cfg()
ls = c.cfg.get("look_screen") or {}
ck("Key 清空", ls.get("vision_key") == "")
ck("地址清空", ls.get("vision_base") == "")
ck("模型清空", ls.get("vision_model") == "")
ck("控件同步清空",
   sw.vision_key.text() == "" and sw.vision_base.text() == "" and sw.vision_model.count() == 0)

print("=== B7. 空 Key 点测试 → 提示，不发请求 ===")
sw._test_vision()
ck("提示填 Key", "Key" in sw._vision_hint.text(), sw._vision_hint.text())
ck("未进入识别", not getattr(sw, "_vision_testing", False))

print("=== C1. 取消按钮复位 ===")
sw.vision_key.setText("sk-fake-key")
sw._vision_testing = True
sw._vision_cancel_btn.show()
sw._vision_test_btn.setEnabled(False)
sw._cancel_vision_test()
ck("取消后 _vision_testing=False", sw._vision_testing is False)
ck("取消后测试按钮可用", sw._vision_test_btn.isEnabled())
ck("取消后取消按钮隐藏", not sw._vision_cancel_btn.isVisible())
ck("状态显示已取消", "取消" in sw._vision_hint.text(), sw._vision_hint.text())

print("=== C2. 隐藏窗口复位（模拟识别中关闭）===")
sw._vision_testing = True
sw._vision_cancel_btn.show()
sw._vision_test_btn.setEnabled(False)
from PySide6.QtGui import QHideEvent
sw.hideEvent(QHideEvent())
app.processEvents()
ck("隐藏后 _vision_testing=False", sw._vision_testing is False)
ck("隐藏后按钮复位",
   sw._vision_test_btn.isEnabled() and not sw._vision_cancel_btn.isVisible())

print("=== C3. 关闭窗口复位 ===")
sw._vision_testing = True
sw._vision_cancel_btn.show()
from PySide6.QtGui import QCloseEvent
sw.closeEvent(QCloseEvent())
app.processEvents()
ck("关闭后 _vision_testing=False", sw._vision_testing is False)

print("=== C4. 旧结果被丢弃（stale 机制）===")
sw._vision_token = 0
sw._vision_testing = True
tok_before = getattr(sw, "_vision_token", 0)
sw._cancel_vision_test()
tok_after = getattr(sw, "_vision_token", 0)
ck("取消后 token 递增", tok_after > tok_before, (tok_before, tok_after))
# 模拟旧线程带旧 token 回调
old_stale = (lambda t=tok_before: getattr(sw, "_vision_token", 0) != t)
ck("旧 token 判定为 stale", old_stale() is True)
new_stale = (lambda t=tok_after: getattr(sw, "_vision_token", 0) != t)
ck("新 token 非 stale", new_stale() is False)

print("=== B8. reload 重填视觉字段 ===")
c.cfg["look_screen"] = {"vision_key": "sk-keep-me",
                        "vision_base": "https://api.deepseek.com/v1",
                        "vision_model": "vision-x"}
sw.reload()
app.processEvents()
ck("Key 被重填", sw.vision_key.text() == "sk-keep-me", sw.vision_key.text())
ck("地址被重填", sw.vision_base.text() == "https://api.deepseek.com/v1", sw.vision_base.text())
ck("模型被重填", sw.vision_model.currentText() == "vision-x", sw.vision_model.currentText())

print("=== B9. 复用窗口后不空 ===")
c.open_settings("chat")
app.processEvents()
sw2 = c.settings_window
ck("复用同一窗口", sw2 is sw)
ck("Key 仍在", sw2.vision_key.text() == "sk-keep-me", sw2.vision_key.text())
ck("模型仍在", sw2.vision_model.currentText() == "vision-x")

print("=== D. 单次输出上限（SpinBox）===")
ck("有 max_tokens_spin", hasattr(sw, "max_tokens_spin"))
sw.max_tokens_spin.setValue(777)
app.processEvents()
ck("改值已存配置", int(c.cfg.get("max_tokens")) == 777, c.cfg.get("max_tokens"))

print("=== E. 对话参数已移到聊天窗口（大设置不再有）===")
sw._refresh_chat_hint()   # 不应抛异常
ck("_chat_preview 已移除", not hasattr(sw, "_chat_preview"))
ck("大设置无 _dialog_section", not hasattr(sw, "_dialog_section"))
ck("大设置无 history_spin", not hasattr(sw, "history_spin"))
ck("大设置无 temp_spin", not hasattr(sw, "temp_spin"))
ck("大设置无 stream_sw", not hasattr(sw, "stream_sw"))

print()
print("RESULT:", sum(1 for _, v in ok if v), "/", len(ok))
print("FAILED:", [n for n, v in ok if not v] or "none")

sw.hide()
for w in c.pets:
    w.hide()
if c.tray:
    c.tray.hide()
app.quit()
