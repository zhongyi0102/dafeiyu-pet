# -*- coding: utf-8 -*-
"""pet_voice.py — 桌宠语音（Edge-TTS → RVC 转换 → 播放）

架构：
  桌宠 exe（小） ──HTTP──▶ 外部 RVC 推理服务（另一台机器/本机另一个 venv）
                            └─ Edge-TTS 生成 → xiaoxiao.safetensors 转换 → 返回 wav

桌宠侧只做三件事：
  1. 组装文本，POST 给语音服务
  2. 拿回 wav（字节）
  3. 用 Qt 自带能力播放（QSoundEffect / QMediaPlayer）
服务不可用时静默降级（不弹气泡不影响文字气泡）。

配置存在 config.json 的 voice 字段：
  {"enabled": false, "url": "http://127.0.0.1:9880",
   "timeout": 20, "volume": 80}
"""
import io
import os
import json
import logging
import tempfile
import time

_logger = logging.getLogger("dafeiyu")


def log(msg, level=logging.INFO):
    """与主程序 桌宠.py 的 log() 签名保持一致：log(text, level)。

    注意：这里必须是**函数**，不能是 Logger 对象——调用方按 log("...") 用。
    """
    try:
        _logger.log(level, msg)
    except Exception:
        pass

DEFAULT_VOICE_CFG = {
    "enabled": False,          # 默认关（外部服务没搭好前不请求）
    # engine: "edge"   = Edge-TTS + RVC（快，2 秒）
    #         "voxcpm" = VoxCPM2 直出（慢，约 3 分钟，48kHz 高音质）
    "engine": "edge",
    "url": "http://127.0.0.1:9880",        # RVC 服务
    "vox_url": "http://127.0.0.1:9881",    # VoxCPM2 服务
    "vox_steps": 24,                       # VoxCPM 推理步数（少=快）
    "vox_control": "",                     # 空=用服务端默认音色描述
    "timeout": 20,
    "volume": 80,              # 0~100
}


def normalize_voice_cfg(raw):
    d = dict(DEFAULT_VOICE_CFG)
    if not isinstance(raw, dict):
        return d
    if isinstance(raw.get("enabled"), bool):
        d["enabled"] = raw["enabled"]
    if isinstance(raw.get("url"), str) and raw["url"].strip():
        d["url"] = raw["url"].strip().rstrip("/")
    if isinstance(raw.get("vox_url"), str) and raw["vox_url"].strip():
        d["vox_url"] = raw["vox_url"].strip().rstrip("/")
    eng = raw.get("engine")
    if eng in ("edge", "voxcpm"):
        d["engine"] = eng
    if isinstance(raw.get("vox_control"), str):
        d["vox_control"] = raw["vox_control"]
    for k in ("timeout", "volume", "vox_steps"):
        try:
            d[k] = int(raw.get(k, d[k]))
        except Exception:
            pass
    d["timeout"] = max(5, min(120, d["timeout"]))
    d["volume"] = max(0, min(100, d["volume"]))
    d["vox_steps"] = max(4, min(60, d["vox_steps"]))
    return d


# 唱歌/发声意图的关键词（只有命中这些才用语音，日常一律文字）
_SING_WORDS = (
    "唱", "唱歌", "唱首", "唱一句", "来一首", "来首", "哼一首", "哼首",
    "哼个", "哼一段", "唱一段", "唱个", "唱歌给我听", "念给我听",
    "念一下", "说给我听", "语音", "读出来", "念念",
)


_ACTION_RE = None   # 延迟编译


def strip_actions(text):
    """剥掉括号舞台动作：（捂脸）（小声）【表情】等。

    唱歌/念台词时这些指令不该被念出来。全剥完为空则返回空串。
    """
    global _ACTION_RE
    if _ACTION_RE is None:
        import re
        _ACTION_RE = re.compile(
            r"[（(【\[][^（）()【\]】]{0,40}[）)】\]]")
    t = _ACTION_RE.sub("", text or "")
    if not t:
        return ""
    # 保留换行（歌词要一句一行），只压缩行内多余空格和空行
    lines = [" ".join(ln.split()) for ln in t.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def is_sing_request(*texts):
    """判断这轮是否在要求「唱/念出来」（决定要不要用语音）。"""
    blob = " ".join(str(t or "") for t in texts).lower()
    if not blob.strip():
        return False
    return any(w in blob for w in _SING_WORDS)


def _detect_proxy():
    """探测可用代理（Edge-TTS 走 WebSocket，aiohttp 需显式传入）。

    优先级：DFY_PROXY 环境变量 → HTTPS_PROXY/HTTP_PROXY → 系统 IE 代理设置。
    都没有则返回 None（直连）。
    """
    for k in ("DFY_PROXY", "HTTPS_PROXY", "https_proxy",
              "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if v and v.strip():
            return v.strip()
    # Windows 系统代理（IE/系统设置里配的那种）
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                if server and server.strip():
                    s = server.strip()
                    # "127.0.0.1:3067" → 补 http://；"http=..;https=.." 取 https
                    if "=" in s:
                        for part in s.split(";"):
                            if part.lower().startswith("https="):
                                s = part.split("=", 1)[1]
                                break
                        else:
                            s = s.split("=", 1)[-1]
                    if not s.startswith("http"):
                        s = "http://" + s
                    return s
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass
    return None


def edge_tts_wav(text, voice="zh-CN-XiaoxiaoNeural", speed=1.0):
    """用 Edge-TTS 生成基准语音（在桌宠进程里跑，这里工作正常）。

    返回 (wav_bytes, err)。
    """
    try:
        import asyncio
        import edge_tts
    except Exception as ex:
        return None, "edge-tts 未安装: %s" % ex

    text = (text or "").strip()
    if not text:
        return None, "空文本"

    async def run():
        rate = "+0%"
        if speed > 1.0:
            rate = "+%d%%" % int((speed - 1) * 100)
        elif speed < 1.0:
            rate = "-%d%%" % int((1 - speed) * 100)
        kwargs = {"rate": rate}
        proxy = _detect_proxy()
        if proxy:
            kwargs["proxy"] = proxy
        comm = edge_tts.Communicate(text, voice, **kwargs)
        buf = io.BytesIO()
        async for ch in comm.stream():
            if ch["type"] == "audio":
                buf.write(ch["data"])
        return buf.getvalue()

    try:
        data = asyncio.run(run())
    except Exception as ex:
        return None, "edge-tts: %s: %s" % (type(ex).__name__, str(ex)[:80])
    if not data:
        return None, "edge-tts 返回空音频"
    return data, ""




# ---------- 语音服务管理 ----------
# 服务脚本路径（exe 版：解包目录/tools；源码版：项目/tools）
def _server_script():
    import sys
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        cand = os.path.join(base, "tools", "rvc_server.py")
        if os.path.exists(cand):
            return cand
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tools", "rvc_server.py")


SERVER_PY = r"E:\AI\engines\rvc311\Scripts\python.exe"
VOX_PY = r"E:\AI\engines\voxcpm-env\Scripts\python.exe"
SERVER_PORT_DEFAULT = 9880
VOX_PORT_DEFAULT = 9881


def is_server_running(url="http://127.0.0.1:9880", timeout=2.0,
                      want_loaded=False):
    """检查语音服务是否在线（/health）。

    want_loaded=True 时还要求模型已加载（loaded=true），
    用于 start_server：只等 HTTP 在线不代表能立刻转换。
    """
    import requests
    try:
        s = requests.Session()
        s.trust_env = False
        r = s.get(url.rstrip("/") + "/health", timeout=timeout)
        if r.status_code != 200:
            return False
        if want_loaded:
            try:
                return bool(r.json().get("loaded"))
            except Exception:
                return False
        return True
    except Exception:
        return False


def start_server(url="http://127.0.0.1:9880", wait=20):
    """拉起语音服务（独立窗口）。返回 (ok, msg)。

    ok=True 时服务已就绪；False 时 msg 说明原因。
    """
    if is_server_running(url):
        return True, "服务已在运行"
    script = _server_script()
    if not os.path.exists(script):
        return False, "找不到服务脚本：%s" % script
    if not os.path.exists(SERVER_PY):
        return False, "找不到服务 Python：%s" % SERVER_PY
    # 端口已被占用 = 已有实例在跑，绝不再启动第二个
    if _port_busy(url):
        return False, "端口已被占用（可能已有实例在启动中）"
    try:
        import subprocess
        d = os.path.dirname(script)
        # 后台静默启动：不弹黑窗口（CREATE_NO_WINDOW），
        # 日志重定向到文件，出错时可查
        log_path = os.path.join(d, "rvc_server.log")
        logf = open(log_path, "ab")
        subprocess.Popen(
            [SERVER_PY, "-u", script],
            cwd=d, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as ex:
        return False, "启动失败: %s: %s" % (type(ex).__name__, ex)
    # 等服务就绪 + 模型预加载完成（否则首个转换请求会撞上加载期）
    t0 = time.time()
    while time.time() - t0 < wait:
        if is_server_running(url):
            # HTTP 在线了；再等模型加载（最多再等 wait 秒）
            while time.time() - t0 < wait * 2:
                if is_server_running(url, want_loaded=True):
                    return True, "服务已启动（模型已就绪）"
                time.sleep(1.0)
            # 模型没等到就绪也返回成功——首个转换会自己加载，
            # 但客户端要用足够长的超时
            return True, "服务已启动（模型加载中，首次转换稍慢）"
        time.sleep(0.5)
    return False, "启动超时（%d 秒），请查看弹出的服务窗口" % wait



def _port_busy(url, timeout=1.0):
    """端口是否已被监听（用于避免重复启动服务）。"""
    import socket
    try:
        from urllib.parse import urlparse
        u = urlparse(url if "://" in url else "http://" + url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            return s.connect_ex((host, port)) == 0
        finally:
            s.close()
    except Exception:
        return False


def _vox_script():
    import sys
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
        cand = os.path.join(base, "tools", "voxcpm_server.py")
        if os.path.exists(cand):
            return cand
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tools", "voxcpm_server.py")


def start_vox_server(url="http://127.0.0.1:9881", wait=180):
    """后台启动 VoxCPM2 服务。返回 (ok, msg)。

    首次启动要下载 5GB 模型 + 加载，可能几分钟，wait 给足。
    """
    if is_server_running(url):
        return True, "VoxCPM 服务已在运行"
    script = _vox_script()
    if not os.path.exists(script):
        return False, "找不到服务脚本：%s" % script
    if not os.path.exists(VOX_PY):
        return False, "找不到 VoxCPM 环境：%s" % VOX_PY
    if _port_busy(url):
        return False, "端口已被占用（可能已有实例在启动中）"
    try:
        import subprocess
        d = os.path.dirname(script)
        log_path = os.path.join(d, "voxcpm_server.log")
        logf = open(log_path, "ab")
        subprocess.Popen(
            [VOX_PY, "-u", script],
            cwd=d, stdout=logf, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as ex:
        return False, "启动失败: %s: %s" % (type(ex).__name__, ex)
    t0 = time.time()
    while time.time() - t0 < wait:
        if is_server_running(url, timeout=3.0):
            return True, "VoxCPM 服务已启动"
        time.sleep(2.0)
    return False, "启动超时（%d 秒），看 tools/voxcpm_server.log" % wait


def rvc_convert(url, wav_bytes, timeout=120):
    """把基准语音发给服务做 RVC 音色转换。返回 (wav_bytes, err)。

    服务端约定：POST {url}/convert，body 是 audio/wav，返回 audio/wav。
    注意：语音服务在本机/局域网，**必须绕过系统代理直连**——否则请求
    会被代理拦截，服务没开时返回的是代理的 502，掩盖真实原因。
    """
    import requests
    if not wav_bytes:
        return None, "无输入音频"
    try:
        s = requests.Session()
        s.trust_env = False          # 忽略系统/环境代理，直连
        r = s.post(
            (url or "").rstrip("/") + "/convert",
            data=wav_bytes,
            timeout=timeout,
            headers={"Content-Type": "audio/wav"})
    except Exception as ex:
        msg = "%s: %s" % (type(ex).__name__, str(ex)[:80])
        if "ConnectionError" in type(ex).__name__ or "refused" in str(ex):
            msg += "（语音服务没启动？请先运行 tools\\启动语音服务.bat）"
        return None, msg
    if r.status_code != 200:
        try:
            msg = r.json().get("detail", "")
        except Exception:
            msg = r.text[:80]
        return None, "HTTP %s: %s" % (r.status_code, msg)
    if not r.content or len(r.content) < 100:
        return None, "空音频"
    return r.content, ""


def tts_rvc(url, text, timeout=120, speaker=None, speed=1.0):
    """完整链路：Edge-TTS（本地）→ RVC 转换（服务）。返回 (wav_bytes, err)。"""
    base, err = edge_tts_wav(text, speaker or "zh-CN-XiaoxiaoNeural", speed)
    if err:
        return None, err
    return rvc_convert(url, base, timeout=timeout)


def voxcpm_tts(vox_url, text, control="", steps=0, timeout=600):
    """VoxCPM2 直出语音（48kHz）。返回 (wav_bytes, err)。

    慢：CPU 下一句话约 3 分钟（取决于步数和文本长度）。
    """
    import requests
    text = (text or "").strip()
    if not text:
        return None, "空文本"
    body = {"text": text}
    if control:
        body["control"] = control
    if steps:
        body["steps"] = int(steps)
    try:
        s = requests.Session()
        s.trust_env = False
        r = s.post((vox_url or "").rstrip("/") + "/tts",
                   json=body, timeout=timeout)
    except Exception as ex:
        msg = "%s: %s" % (type(ex).__name__, str(ex)[:80])
        if "ConnectionError" in type(ex).__name__ or "refused" in str(ex):
            msg += "（VoxCPM 服务没启动？点「启动语音服务」）"
        return None, msg
    if r.status_code != 200:
        try:
            msg = r.json().get("detail", "")
        except Exception:
            msg = r.text[:100]
        return None, "HTTP %s: %s" % (r.status_code, msg)
    if not r.content or len(r.content) < 100:
        return None, "空音频"
    return r.content, ""


def songs_dir():
    """翻唱成品目录（放 cover.wav / *.wav 的地方）。"""
    import sys
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "songs")


def list_songs():
    """列出可用翻唱曲目：返回 [(显示名, 路径)]。"""
    d = songs_dir()
    out = []
    try:
        if not os.path.isdir(d):
            return out
        for f in sorted(os.listdir(d)):
            if f.lower().endswith((".wav", ".mp3", ".flac", ".m4a")):
                name = os.path.splitext(f)[0]
                out.append((name, os.path.join(d, f)))
    except Exception:
        pass
    return out


def pick_song(text=""):
    """按文本里的歌名挑一首；没匹配就随机。返回 (名字, 路径) 或 (None, None)。"""
    songs = list_songs()
    if not songs:
        return None, None
    t = (text or "").lower()
    # 优先按歌名包含匹配
    for name, path in songs:
        if name.lower() and name.lower() in t:
            return name, path
    # 退而求其次：随机
    import random
    return random.choice(songs)


def synthesize(cfg, text):
    """按配置的引擎合成语音。返回 (wav_bytes, err)。"""
    eng = (cfg or {}).get("engine") or "edge"
    if eng == "voxcpm":
        return voxcpm_tts(
            cfg.get("vox_url"), text,
            control=cfg.get("vox_control") or "",
            steps=cfg.get("vox_steps") or 0,
            timeout=max(300, int(cfg.get("timeout", 120) or 120) * 5))
    # 默认 edge + rvc
    return tts_rvc(cfg.get("url"), text,
                   timeout=max(120, int(cfg.get("timeout", 120) or 120)))


class VoicePlayer:
    """管理播放器（QMediaPlayer 延迟创建，线程安全）。"""

    def __init__(self, cfg_getter):
        self._cfg_getter = cfg_getter   # 返回 normalize 后的 voice 配置
        self._player = None
        self._last_ok_ts = 0.0
        self._cooldown = 3.0            # 两次语音至少隔 3 秒，防止刷屏轰炸

    # ---- 内部 ----
    def _ensure_player(self):
        if self._player is not None:
            return self._player
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            self._player = QMediaPlayer()
            self._audio = QAudioOutput()
            self._player.setAudioOutput(self._audio)
        except Exception as ex:
            log("[语音] QtMultimedia 不可用: %s: %s"
                % (type(ex).__name__, ex), logging.WARNING)
            self._player = False      # False = 明确不可用，不再尝试
        return self._player

    # ---- 对外 ----
    def speak(self, text):
        """把一句话变成语音播放（网络+播放都在子线程做，绝不卡 UI）。

        超长文本会截断（不是拒绝）；被跳过时都记日志，方便排查。
        """
        cfg = self._cfg_getter()
        if not cfg.get("enabled"):
            return False
        # 念之前剥掉括号舞台动作：模型回复常带（捂脸）（小声）等，
        # 念出来很怪；剥完为空则不念
        text = strip_actions(text)
        if not text:
            log("[语音] 跳过：剥掉括号动作后没有可念的内容")
            return False
        # 语音不受 token 限制：整段都念，只留一个防呆上限（2000 字）
        # 防止异常超长文本把 CPU 推理拖死
        if len(text) > 2000:
            log("[语音] 文本过长 %d 字，按 2000 字截断" % len(text))
            text = text[:2000]
        now = time.time()
        if now - self._last_ok_ts < self._cooldown:
            log("[语音] 跳过：冷却中（%.1f 秒）"
                % (self._cooldown - (now - self._last_ok_ts)))
            return False

        import threading

        def work():
            wav, err = synthesize(cfg, text)
            if err or not wav:
                log("[语音] 生成失败（静默）：%s" % err)
                return
            self._last_ok_ts = time.time()
            self._play_bytes(wav, cfg.get("volume", 80))

        threading.Thread(target=work, daemon=True).start()
        return True

    def _play_bytes(self, wav_bytes, volume):
        """主线程播放（由信号切回）。"""
        try:
            player = self._ensure_player()
            if not player:
                return
            import os as _os
            tmp = _os.path.join(tempfile.gettempdir(), "dfy_voice.wav")
            with open(tmp, "wb") as f:
                f.write(wav_bytes)
            try:
                vol = max(0, min(100, int(volume))) / 100.0
                self._audio.setVolume(vol)
            except Exception:
                pass
            from PySide6.QtCore import QUrl
            player.setSource(QUrl.fromLocalFile(tmp))
            player.play()
        except Exception as ex:
            log("[语音] 播放失败: %s: %s" % (type(ex).__name__, ex),
                logging.WARNING)
