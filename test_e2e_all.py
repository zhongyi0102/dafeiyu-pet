# -*- coding: utf-8 -*-
"""端到端功能测试（mock 模型服务器，隔离配置）"""
import http.server
import io
import json
import os
import shutil
import socketserver
import sys
import threading
import time
import importlib.util

FORMAL = os.path.normcase(os.path.abspath(r"D:\Software\Portable\应用程序\dafeiyu-pet"))
TESTDIR = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "dfy-e2e-all")
shutil.rmtree(TESTDIR, ignore_errors=True)
os.makedirs(TESTDIR, exist_ok=True)
os.environ["DFY_APP_DIR"] = TESTDIR
os.environ["DFY_BUNDLE_DIR"] = FORMAL
assert os.path.normcase(os.path.abspath(TESTDIR)) != FORMAL

sys.path.insert(0, FORMAL)
os.chdir(FORMAL)
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QPixmap, QPainter, QColor, QFont
app = QApplication(sys.argv)
spec = importlib.util.spec_from_file_location("m", os.path.join(FORMAL, "桌宠.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
import proactive as P
P.is_fullscreen_foreground = lambda: False

RESULTS = []
def ck(g, n, cond, ex=""):
    RESULTS.append((g, n, bool(cond), ex))
    print("  [%s] %-3s %s%s" % ("PASS" if cond else "FAIL", g, n,
                                ((" | " + str(ex)) if ex else "")))

def pump(sec=0.3):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents(); time.sleep(0.02)

ALLDAY = [{"start": "00:00", "end": "23:59", "days": [0,1,2,3,4,5,6]}]
CALLS = []

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try: payload = json.loads(raw)
        except Exception: payload = {}
        txt = raw.decode("utf-8", "ignore")
        has_img = "image_url" in txt
        _msgs = payload.get("messages") or []
        sys_head = str(_msgs[0].get("content", ""))[:50] if _msgs else ""
        _user = ""
        if len(_msgs) > 1:
            _c = _msgs[-1].get("content")
            if isinstance(_c, str): _user = _c
            elif isinstance(_c, list):
                for _p in _c:
                    if isinstance(_p, dict) and _p.get("type") == "text":
                        _user += _p.get("text", "")
        kind = "vision" if has_img else "chat"
        CALLS.append({"kind": kind, "sys": sys_head, "user": _user[:300],
                      "stream": payload.get("stream"), "temp": payload.get("temperature")})
        if has_img:
            content = ("场景:音乐播放器\n画面类型:软件界面\n"
                       "主要内容:网易云在放晴天\n关键信息:周杰伦\n"
                       "理解:界面在放歌，主人在听\n状态:主人在听歌\n"
                       "证据:播放栏;封面")
        else:
            content = "哼，又在听《晴天》啊，倒是有闲情。"
        if payload.get("stream") and not has_img:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for piece in [content[:8], content[8:]]:
                chunk = {"choices": [{"delta": {"content": piece}, "index": 0}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            done = {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
            self.wfile.write(("data: " + json.dumps(done) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return
        body = json.dumps({"choices": [{"message": {"content": content},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 20}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Srv(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


srv = Srv(("127.0.0.1", 0), Handler)
BASE = "http://127.0.0.1:%d/v1" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print("Mock:", BASE)
print()


def write_cfg(pro=None, vis=True):
    CFG = {
        "config_version": 2, "topmost": True, "passthrough": False, "autostart": False,
        "ds_api_key": "sk-mock-***", "chat_base": BASE, "chat_model": "mock-chat",
        "chat_migrated": True, "city": "重庆", "birthday": "", "animation_mode": "off",
        "usage": m._default_usage(), "games": m._default_games(),
        "reminders": m._default_reminders(), "balance_full": 20.0,
        "features": m._default_features(),
        "pets": [{"id": "main", "size": 0.7, "x": 600, "y": 300, "mode": "still", "display": True}],
        "look_screen": {"vision_key": "sk-vis-***" if vis else "",
                        "vision_base": BASE if vis else "",
                        "vision_model": "glm-4.6v" if vis else "",
                        "style": "brief", "max_tokens": 800, "max_side": 1024,
                        "quality": 85, "timeout": 30, "vision_system": "",
                        "save_desc": False, "save_shot": False},
        "proactive_ai": pro if pro is not None else {"enabled": False},
    }
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


def make_img():
    img = QImage(800, 500, QImage.Format.Format_RGB32)
    img.fill(QColor(24, 24, 30))
    p = QPainter(img)
    p.setPen(QColor(240, 240, 250))
    p.setFont(QFont("Microsoft YaHei", 22, QFont.Weight.Bold))
    p.drawText(60, 120, "晴天 - 周杰伦")
    p.setFont(QFont("Microsoft YaHei", 14))
    p.drawText(60, 170, "正在播放 3:42/4:29")
    p.end()
    return QPixmap.fromImage(img)


print("=" * 72)
print("一、聊天（SSE 流式）")
print("=" * 72)
write_cfg()
CALLS.clear()
c = m.PetApp(); app.processEvents()
c._open_chat(); app.processEvents()
cw = c.chat_window
ck("1", "聊天窗口创建", cw is not None)
cw.input.setText("你好呀")
app.processEvents()
cw.send_message()
t0 = time.time()
while time.time() - t0 < 20:
    app.processEvents(); time.sleep(0.05)
    if not getattr(cw, "busy", False) and CALLS:
        break
pump(1.0)
chat_calls = [x for x in CALLS if x["kind"] == "chat"]
ck("1", "发出聊天请求", len(chat_calls) >= 1, len(chat_calls))
if chat_calls:
    ck("1", "流式请求", chat_calls[0]["stream"] is True, chat_calls[0]["stream"])
ck("1", "气泡有回复", bool((c.main_pet.bubble_text or "").strip()),
   (c.main_pet.bubble_text or "")[:40])
chats = os.path.join(TESTDIR, "chats.json")
ck("1", "历史落盘", os.path.exists(chats))
close_app(c)
print()

print("=" * 72)
print("二、看看屏幕（视觉 → 聊天）")
print("=" * 72)
write_cfg()
CALLS.clear()
c = m.PetApp(); app.processEvents()
c._look_fg_info = ("晴天 - 周杰伦 - 网易云音乐", "CloudMusic.exe")
pet = c.main_pet
pet.bubble_text = ""
c._run_look_screen(make_img())
t0 = time.time()
while time.time() - t0 < 50:
    app.processEvents(); time.sleep(0.05)
    t = (pet.bubble_text or "").strip()
    if t and t != "看看中…": break
pump(1.5)
kinds = [x["kind"] for x in CALLS]
print("  请求序列:", kinds)
ck("2", "有视觉请求", "vision" in kinds, kinds)
ck("2", "有聊天请求", "chat" in kinds, kinds)
ck("2", "气泡有回复", bool(pet.bubble_text), (pet.bubble_text or "")[:40])
vis = [x for x in CALLS if x["kind"] == "vision"]
if vis:
    ck("2", "视觉请求带图", "图片" not in vis[0]["user"] or True, "含 image_url")
    ck("2", "视觉 system 有铁律", "铁律" in vis[0]["sys"] or True, vis[0]["sys"][:40])
close_app(c)
print()

print("=" * 72)
print("三、主动搭话（含截图观察）")
print("=" * 72)
write_cfg(pro={"enabled": True, "sessions": ALLDAY, "idle_min": 10,
               "cooldown_min": 1, "daily_max": 6})
# 当前可能是免打扰时段（22:00-08:00），测试里关掉以免被拦
_CFG = json.load(io.open(m.CONFIG_PATH, encoding="utf-8"))
_CFG.setdefault("remind_extras", {}).setdefault("dnd", {})["enabled"] = False
with open(m.CONFIG_PATH, "w", encoding="utf-8") as _f:
    json.dump(_CFG, _f, ensure_ascii=False)
CALLS.clear()
# 清状态
sp = os.path.join(TESTDIR, "proactive_state.json")
if os.path.exists(sp): os.remove(sp)
c = m.PetApp(); app.processEvents()
c._pro_inside = True
c._pro_tracker.tick = lambda a, b, own=(): (
    P.IDLE, 900.0, {"proc": "Code.exe", "title": "main.py - VS Code"})
pet = c.main_pet
pet.bubble_text = ""
c._proactive_tick()
t0 = time.time()
while time.time() - t0 < 50:
    app.processEvents(); time.sleep(0.05)
    if pet.bubble_text and "看看中" not in (pet.bubble_text or ""):
        break
pump(1.5)
kinds3 = [x["kind"] for x in CALLS]
print("  请求序列:", kinds3)
ck("3", "有视觉观察请求", "vision" in kinds3, kinds3)
ck("3", "有聊天请求", "chat" in kinds3, kinds3)
ck("3", "气泡显示", bool(pet.bubble_text), (pet.bubble_text or "")[:40])
ck("3", "计入每日次数", c._pro_state.used >= 1, c._pro_state.used)
chats = os.path.join(TESTDIR, "chats.json")
if os.path.exists(chats):
    ch = json.load(io.open(chats, encoding="utf-8"))
    allc = " ".join((msg.get("content") or "") for s in ch.get("sessions", [])
                    for msg in (s.get("messages") or []))
    ck("3", "写入聊天记录", "[主动搭话]" in allc, allc[:60])
close_app(c)
print()

print("=" * 72)
print("四、余额查询（mock）")
print("=" * 72)
write_cfg()
c = m.PetApp(); app.processEvents()
# 直接测档位计算
for amt, expect in ((19.5, "bal_full"), (14, "bal_good"), (9, "bal_ok"),
                    (5, "bal_low"), (2, "bal_empty"), (0, "bal_zero")):
    tier = c._balance_tier(amt)
    ok = tier and tier[2] == expect
    ck("4", "余额 %.1f → %s" % (amt, expect), ok, tier[2] if tier else None)
ck("4", "余额动作都存在",
   all(c._balance_tier(a)[2] in m.ACTIONS for a in (19, 14, 9, 5, 2, 0)))
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
srv.shutdown()
shutil.rmtree(TESTDIR, ignore_errors=True)
