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
    "url": "http://127.0.0.1:9880",
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
    for k in ("timeout", "volume"):
        try:
            d[k] = int(raw.get(k, d[k]))
        except Exception:
            pass
    d["timeout"] = max(5, min(120, d["timeout"]))
    d["volume"] = max(0, min(100, d["volume"]))
    return d


# 唱歌/发声意图的关键词（只有命中这些才用语音，日常一律文字）
_SING_WORDS = (
    "唱", "唱歌", "唱首", "唱一句", "来一首", "来首", "哼一首", "哼首",
    "哼个", "哼一段", "唱一段", "唱个", "唱歌给我听", "念给我听",
    "念一下", "说给我听", "语音", "读出来", "念念",
)


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


def rvc_convert(url, wav_bytes, timeout=120):
    """把基准语音发给服务做 RVC 音色转换。返回 (wav_bytes, err)。

    服务端约定：POST {url}/convert，body 是 audio/wav，返回 audio/wav。
    """
    import requests
    if not wav_bytes:
        return None, "无输入音频"
    try:
        r = requests.post(
            (url or "").rstrip("/") + "/convert",
            data=wav_bytes,
            timeout=timeout,
            headers={"Content-Type": "audio/wav"})
    except Exception as ex:
        return None, "%s: %s" % (type(ex).__name__, str(ex)[:80])
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
        """把一句话变成语音播放（网络+播放都在子线程做，绝不卡 UI）。"""
        cfg = self._cfg_getter()
        if not cfg.get("enabled"):
            return False
        text = (text or "").strip()
        if not text or len(text) > 200:
            return False
        now = time.time()
        if now - self._last_ok_ts < self._cooldown:
            return False

        import threading

        def work():
            wav, err = tts_rvc(cfg.get("url"), text,
                               timeout=cfg.get("timeout", 20))
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
