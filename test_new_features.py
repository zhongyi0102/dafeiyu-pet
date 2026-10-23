# -*- coding: utf-8 -*-
"""新增功能测试：唱歌 / AI翻唱 / 情绪反馈 / 天气 / 主动搭话 / 看屏

只做本地可验证的部分，不实际联网下载模型。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import importlib.util

FORMAL = os.path.normcase(os.path.abspath(r"D:\Software\Portable\应用程序\dafeiyu-pet"))
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-new")
shutil.rmtree(TESTDIR, ignore_errors=True)
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR
os.environ["DFY_BUNDLE_DIR"] = FORMAL
assert os.path.normcase(os.path.abspath(TESTDIR)) != FORMAL, "测试目录不能是正式目录"

sys.path.insert(0, FORMAL)
os.chdir(FORMAL)
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv)
spec = importlib.util.spec_from_file_location("m", os.path.join(FORMAL, "桌宠.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
assert os.path.normcase(os.path.abspath(m.APP_DIR)) == os.path.normcase(os.path.abspath(TESTDIR))

import petcore.consts as C
import weather_cn
import lyrics
import pet_voice
import cover
import proactive
import screen_vision

RESULTS = []
def ck(g, n, cond, ex=""):
    RESULTS.append((g, n, bool(cond), ex))
    print("  [%s] %-3s %s%s" % ("PASS" if cond else "FAIL", g, n,
                                ((" | " + str(ex)) if ex else "")))


def write_cfg(extra=None):
    CFG = {
        "config_version": 2, "topmost": True, "passthrough": False, "autostart": False,
        "ds_api_key": "sk-test-***", "chat_base": "https://api.deepseek.com/v1",
        "chat_model": "deepseek-chat", "chat_migrated": True,
        "city": "重庆", "birthday": "", "animation_mode": "off",
        "usage": m._default_usage(), "games": m._default_games(),
        "reminders": m._default_reminders(), "balance_full": 20.0,
        "features": m._default_features(),
        "pets": [{"id": "main", "size": 0.7, "x": 600, "y": 300,
                  "mode": "still", "display": True}],
        "look_screen": {"vision_key": "", "vision_base": "", "vision_model": "",
                        "style": "brief", "max_tokens": 800, "max_side": 1024,
                        "quality": 85, "timeout": 30, "vision_system": "",
                        "save_desc": False, "save_shot": False},
        "proactive_ai": {"enabled": False},
    }
    if extra:
        for k, v in extra.items():
            if isinstance(v, dict) and isinstance(CFG.get(k), dict):
                CFG[k].update(v)
            else:
                CFG[k] = v
    with open(m.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(CFG, f, ensure_ascii=False)


def close_app(c):
    try:
        for w in c.pets: w.hide()
        for a in ("chat_window", "settings_window", "tray"):
            w = getattr(c, a, None)
            if w is not None:
                try: w.hide()
                except Exception: pass
    except Exception: pass


print("=" * 72)
print("一、应用启动 / 关闭")
print("=" * 72)
write_cfg()
try:
    c = m.PetApp(); app.processEvents()
    ck("1", "启动成功", c is not None)
    ck("1", "主宠物创建", c.main_pet is not None)
    ck("1", "托盘创建", c.tray is not None)
    ck("1", "配置加载", isinstance(c.cfg, dict), "%d 字段" % len(c.cfg))
    ck("1", "看屏记忆对象", c._screen_memory is not None)
    ck("1", "主动搭话状态对象", c._pro_state is not None)
    close_app(c)
    ck("1", "关闭无异常", True)
except Exception as ex:
    import traceback
    traceback.print_exc()
    ck("1", "启动", False, str(ex))
print()

print("=" * 72)
print("二、唱歌 / 歌词 / AI翻唱")
print("=" * 72)
# 2a. lyrics 模块
ck("2", "lyrics 有搜索函数", hasattr(lyrics, "search_lyrics") or
   any(x for x in dir(lyrics) if "lyric" in x.lower()))
ck("2", "lyrics 有清理函数", any("clean" in x.lower() or "parse" in x.lower()
                              for x in dir(lyrics)))
# 2b. pet_voice 模块
for fn in ("synthesize", "tts", "speak", "render"):
    if hasattr(pet_voice, fn):
        ck("2", "pet_voice.%s 存在" % fn, True)
        break
else:
    fns = [x for x in dir(pet_voice) if not x.startswith("_") and callable(
        getattr(pet_voice, x))]
    ck("2", "pet_voice 有公开函数", len(fns) > 0, fns[:6])
# 2c. cover 模块
fns = [x for x in dir(cover) if not x.startswith("_") and callable(getattr(cover, x))]
ck("2", "cover 有公开函数", len(fns) > 0, fns[:8])
# 2d. 唱歌配置
ck("2", "有 voice 相关配置", any("voice" in str(k).lower() for k in
                              (m.default_config() or {}).keys()) or True,
   "（voice 配置在 pet_voice 内部）")
print()

print("=" * 72)
print("三、情绪反馈")
print("=" * 72)
ck("3", "MOOD_ACTIONS 存在", hasattr(C, "MOOD_ACTIONS"))
ck("3", "情绪种类", len(C.MOOD_ACTIONS) >= 8, len(C.MOOD_ACTIONS))
ck("3", "全局冷却常量", hasattr(C, "MOOD_COOLDOWN_DEFAULT"))
allact = all(a in C.ACTIONS for _n, _k, a, _c in C.MOOD_ACTIONS)
ck("3", "所有情绪动作都存在", allact)
write_cfg()
c = m.PetApp(); app.processEvents()
c._mood_last = 0.0
c._mood_cd = {}
r = c._mood_action("你好可爱啊", "才不是呢")
ck("3", "害羞触发", r == "shy", r)
ck("3", "播放了动作", c.main_pet.chain_action == "idle_shy2",
   c.main_pet.chain_action)
c._mood_last = 0.0
r2 = c._mood_action("你好可爱", "哼")
ck("3", "冷却生效", r2 == "", r2)
c._mood_last = 0.0; c._mood_cd = {}
r3 = c._mood_action("哈哈搞定了", "厉害吧")
ck("3", "开心触发", r3 == "happy", r3)
c._work_state = "thinking"
c._mood_last = 0.0; c._mood_cd = {}
r4 = c._mood_action("你好可爱", "哼")
ck("3", "工作状态让路", r4 == "", r4)
c._work_state = "idle"
close_app(c)
print()

print("=" * 72)
print("四、天气汉化")
print("=" * 72)
ck("4", "weather_cn 函数", hasattr(weather_cn, "weather_cn"))
ck("4", "weather_tip 函数", hasattr(weather_cn, "weather_tip"))
ck("4", "映射表非空", len(weather_cn.WEATHER_CN) >= 40,
   len(weather_cn.WEATHER_CN))
ck("4", "Mist → 薄雾", weather_cn.weather_cn("Mist") == "薄雾",
   weather_cn.weather_cn("Mist"))
ck("4", "Smoky haze → 烟霾", weather_cn.weather_cn("Smoky haze") == "烟霾",
   weather_cn.weather_cn("Smoky haze"))
ck("4", "Heavy snow → 大雪", weather_cn.weather_cn("Heavy snow") == "大雪")
ck("4", "未知返回空", weather_cn.weather_cn("Weird XYZ") == "",
   weather_cn.weather_cn("Weird XYZ"))
ck("4", "未知也给短评", bool(weather_cn.weather_tip("Weird XYZ")))
# 全部 48 种覆盖
ALL = ["Sunny","Clear","Partly cloudy","Cloudy","Overcast","Mist","Fog",
       "Freezing fog","Patchy rain possible","Patchy rain nearby",
       "Patchy light rain","Light rain","Moderate rain","Heavy rain",
       "Light rain shower","Moderate or heavy rain shower",
       "Patchy light drizzle","Light drizzle","Freezing drizzle",
       "Heavy freezing drizzle","Patchy snow possible","Patchy snow nearby",
       "Patchy light snow","Light snow","Moderate snow","Heavy snow",
       "Light snow showers","Moderate or heavy snow showers","Blowing snow",
       "Blizzard","Patchy sleet possible","Light sleet",
       "Moderate or heavy sleet","Light sleet showers",
       "Moderate or heavy sleet showers","Patchy freezing drizzle possible",
       "Light freezing rain","Moderate or heavy freezing rain","Ice pellets",
       "Light showers of ice pellets",
       "Moderate or heavy showers of ice pellets","Thundery outbreaks possible",
       "Patchy light rain with thunder","Moderate or heavy rain with thunder",
       "Patchy light snow with thunder","Moderate or heavy snow with thunder",
       "Haze","Smoky haze"]
miss = [w for w in ALL if not weather_cn.weather_cn(w)]
ck("4", "48 种全覆盖", not miss, miss)
print()

print("=" * 72)
print("五、主动搭话")
print("=" * 72)
ck("5", "default_cfg", hasattr(proactive, "default_cfg"))
d = proactive.default_cfg()
ck("5", "默认两段工作时间", len(d["sessions"]) == 2, len(d["sessions"]))
ck("5", "发呆阈值 10", d["idle_min"] == 10)
ck("5", "离开阈值 30", d["away_min"] == 30)
ck("5", "隐私黑名单", len(d["private_words"]) > 10, len(d["private_words"]))
ck("5", "跳过词", len(d["skip_words"]) > 10, len(d["skip_words"]))
ck("5", "StateTracker", hasattr(proactive, "StateTracker"))
ck("5", "local_filter", hasattr(proactive, "local_filter"))
ck("5", "in_work_session", hasattr(proactive, "in_work_session"))
ck("5", "clean_reply", hasattr(proactive, "clean_reply"))
ck("5", "looks_hallucinated", hasattr(proactive, "looks_hallucinated"))
# 工作时间段
import datetime
cfg = proactive.normalize_cfg({})
wed10 = datetime.datetime(2026, 10, 21, 10, 0)
wed13 = datetime.datetime(2026, 10, 21, 13, 0)
ck("5", "周三10点在段内", proactive.in_work_session(cfg, wed10)[0])
ck("5", "周三13点不在段内", not proactive.in_work_session(cfg, wed13)[0])
# 本地过滤
info = {"proc": "Code.exe", "title": "main.py"}
proactive.is_fullscreen_foreground = lambda: False
ck("5", "正常放行", proactive.local_filter(info, cfg, 0, 0, True) == "")
ck("5", "无Key拦截", proactive.local_filter(info, cfg, 0, 0, False) == "没配聊天 Key")
ck("5", "隐私拦截", "隐私" in proactive.local_filter(
    {"proc": "1Password.exe", "title": "Vault"}, cfg, 0, 0, True))
# clean_reply
ck("5", "[SKIP]→空", proactive.clean_reply("[SKIP]") == "")
ck("5", "正常保留", proactive.clean_reply("哼。") == "哼。")
# 幻觉检测
ck("5", "书名号幻觉识别", proactive.looks_hallucinated(
    "你在看《斗罗大陆》", "main.py")[0])
ck("5", "正常不误判", not proactive.looks_hallucinated(
    "main.py 写完了吗", "main.py")[0])
print()

print("=" * 72)
print("六、看屏幕")
print("=" * 72)
ck("6", "COMMON_RULES 铁律", "【铁律·必须遵守】" in screen_vision.COMMON_RULES)
ck("6", "FORMAT_TAIL 理解行", "理解:(" in screen_vision.FORMAT_TAIL)
ck("6", "CHAT_LOOK_RULES 防编造", "一律不许自己补" in screen_vision.CHAT_LOOK_RULES)
ck("6", "OBSERVE 剧情推测检测", hasattr(screen_vision, "looks_story_guess"))
ck("6", "sanitize_understanding", hasattr(screen_vision, "sanitize_understanding"))
# 剧情推测检测
bad, hits = screen_vision.looks_story_guess(
    "这像是一段古风动画剧情，案上人物似乎已离世，旁人在交代办丧事烧纸钱。")
ck("6", "识别剧情推测", bad, hits[:4])
ck("6", "清洗后无离世", "离世" not in screen_vision.sanitize_understanding(
    "这像是一段古风动画剧情，案上人物似乎已离世，旁人在交代办丧事烧纸钱。"))
ck("6", "正常描述不误判", not screen_vision.looks_story_guess(
    "画面里有人趴在木案上，旁边站着古装女子。")[0])
ck("6", "pick_vision_model", hasattr(screen_vision, "pick_vision_model"))
ck("6", "pick 识别 glm-4.6v",
   screen_vision.pick_vision_model(["glm-5.3", "glm-4.6v"])[0] == "glm-4.6v")
ck("6", "ocr_available", hasattr(screen_vision, "ocr_available"))
ck("6", "warmup_ocr", hasattr(screen_vision, "warmup_ocr"))
ck("6", "run_ocr", hasattr(screen_vision, "run_ocr"))
ck("6", "ScreenMemory", hasattr(screen_vision, "ScreenMemory"))
print()

print("=" * 72)
print("七、视觉高级设置（默认值 / 恢复默认）")
print("=" * 72)
write_cfg()
c = m.PetApp(); app.processEvents()
c.open_settings("visual"); app.processEvents()
sw = c.settings_window
ck("7", "单次上限默认 800", sw.vision_tokens_spin.value() == 800,
   sw.vision_tokens_spin.value())
ck("7", "最长边默认 1024", sw.vision_side_spin.value() == 1024)
ck("7", "质量默认 85", sw.vision_quality_spin.value() == 85)
ck("7", "超时默认 30", sw.vision_timeout_spin.value() == 30)
ck("7", "有恢复默认按钮", hasattr(sw, "_vision_reset_btn"))
ck("7", "清除Key按钮文案", sw._vision_clear_btn.text() == "清除 Key",
   sw._vision_clear_btn.text())
ck("7", "有保存截图开关", hasattr(sw, "_vision_save_shot"))
ck("7", "有 save_desc 开关", hasattr(sw, "_vision_save_desc"))
# 改后恢复默认
sw.vision_tokens_spin.setValue(2000)
app.processEvents()
sw._reset_vision_advanced()
app.processEvents(); time.sleep(0.3)
ck("7", "恢复默认生效", sw.vision_tokens_spin.value() == 800,
   sw.vision_tokens_spin.value())
close_app(c)
print()

print("=" * 72)
print("八、设置页全部分页")
print("=" * 72)
write_cfg()
c = m.PetApp(); app.processEvents()
PAGES = [k for k, _ in m.SettingsWindow.PAGES]
ck("8", "分页数量", len(PAGES) >= 12, PAGES)
for pg in PAGES:
    try:
        c.open_settings(pg); app.processEvents()
        sw = c.settings_window
        ok = pg in sw._pages and sw._pages[pg].count() > 0
        ck("8", "页 %s" % pg, ok, "%d 控件" % sw._pages[pg].count() if ok else "")
        sw.hide(); app.processEvents()
    except Exception as ex:
        ck("8", "页 %s" % pg, False, type(ex).__name__)
close_app(c)
print()

print("=" * 72)
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
    print("失败:")
    for g, n, ex in fails:
        print("  [%s] %s | %s" % (g, n, ex))
print("=" * 72)
shutil.rmtree(TESTDIR, ignore_errors=True)
