# -*- coding: utf-8 -*-
r"""voxcpm_server.py — VoxCPM2 语音服务（高质量语音 / 音色克隆）

与现有 rvc_server.py 并存：
  rvc_server.py   : Edge-TTS 生成的语音 → RVC 换成晓晓音色（快，2 秒）
  voxcpm_server.py: 文本 → VoxCPM2 直接生成（慢，3 分钟，48kHz 高音质）

运行环境：E:\AI\dafeiyu-voice\engines\voxcpm-env（Python 3.11 + torch CPU）
模型：openbmb/VoxCPM2（首次运行自动下载，约 5GB）

接口：
  GET  /health                    -> {"ok": true, "loaded": bool, ...}
  POST /tts   {"text": "...", "control": "...", "steps": N}
       -> audio/wav
  POST /clone {"text": "...", "reference": "<base64 wav>"}
       -> audio/wav（用参考音频克隆音色）
"""
import base64
import io
import os
import tempfile
import threading
import time

HOST = os.environ.get("VOX_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOX_PORT", "9881"))
HF_HOME = os.environ.get("HF_HOME", r"E:\AI\dafeiyu-voice\models\hf-cache")
# 默认音色描述（大肥鱼：傲娇鲸鱼少女）
DEFAULT_CONTROL = os.environ.get(
    "VOX_CONTROL",
    "年轻女声，清亮甜美，略带傲娇和俏皮，语速自然")
# 推理步数（CPU 下越少越快，质量略降）
DEFAULT_STEPS = int(os.environ.get("VOX_STEPS", "24"))

os.environ.setdefault("HF_HOME", HF_HOME)

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

app = FastAPI(title="dafeiyu voxcpm")
_state = {"model": None, "loaded": False, "loading": False}
_lock = threading.Lock()


def _log(msg):
    print("[voxcpm] %s" % msg, flush=True)


def _write_err(where, ex):
    import traceback
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "voxcpm_error.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n===== %s @ %s =====\n"
                    % (where, time.strftime("%Y-%m-%d %H:%M:%S")))
            f.write(traceback.format_exc())
        _log("%s 失败: %s" % (where, ex))
    except Exception:
        pass


def _load_model():
    """加载 VoxCPM2（首次会下载约 5GB）。"""
    if _state["loaded"]:
        return
    with _lock:
        if _state["loaded"]:
            return
        _state["loading"] = True
        try:
            _log("加载 VoxCPM2（首次需下载模型，可能很久）…")
            from voxcpm import VoxCPM
            t0 = time.time()
            # CPU 上必须 optimize=False（torch.compile 与多线程不兼容）
            m = VoxCPM.from_pretrained("openbmb/VoxCPM2", optimize=False)
            _state["model"] = m
            _state["loaded"] = True
            _log("加载完成 %.0f 秒" % (time.time() - t0))
        finally:
            _state["loading"] = False


def _wav_bytes(audio, sr):
    """numpy 波形 → WAV 字节。"""
    import wave
    import numpy as np
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim > 1:
        a = a.mean(axis=1)
    peak = np.abs(a).max()
    if peak > 0:
        a = a / peak * 0.95
    pcm = (a * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _generate(text, control=None, steps=None, ref_audio_path=None):
    _load_model()
    m = _state["model"]
    if m is None:
        raise RuntimeError("模型未加载")
    t0 = time.time()
    kwargs = {}
    if ref_audio_path:
        # 克隆模式
        wav = m.generate(
            text=text,
            reference_wav_path=ref_audio_path,
            cfg_value=2.0,
            inference_timesteps=int(steps or DEFAULT_STEPS),
        )
    else:
        # 音色设计模式：官方做法是把 control 拼在文本前 "(control)text"
        _ctrl = (control or DEFAULT_CONTROL or "").strip()
        final_text = ("(%s)%s" % (_ctrl, text)) if _ctrl else text
        wav = m.generate(
            text=final_text,
            cfg_value=2.0,
            inference_timesteps=int(steps or DEFAULT_STEPS),
        )
    dt = time.time() - t0
    sr = getattr(getattr(m, "tts_model", None), "sample_rate", 48000)
    _log("生成完成 %.1f 秒  %d Hz  %d 采样点" % (dt, sr, len(wav)))
    return _wav_bytes(wav, sr)


# ---------- 接口 ----------
class TTSReq(BaseModel):
    text: str
    control: str = ""
    steps: int = 0


class CloneReq(BaseModel):
    text: str
    reference: str          # base64 编码的 WAV
    steps: int = 0


@app.get("/health")
def health():
    return {
        "ok": True,
        "mode": "voxcpm2",
        "loaded": bool(_state["loaded"]),
        "loading": bool(_state["loading"]),
        "device": "cpu",
        "default_control": DEFAULT_CONTROL,
        "default_steps": DEFAULT_STEPS,
    }


@app.post("/tts")
def tts(req: TTSReq):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    if len(text) > 500:
        raise HTTPException(400, "text too long (max 500)")
    try:
        out = _generate(text, req.control or None, req.steps or None)
    except Exception as ex:
        _write_err("tts", ex)
        raise HTTPException(500, "voxcpm failed: %s" % ex)
    return Response(content=out, media_type="audio/wav")


@app.post("/clone")
def clone(req: CloneReq):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    if not req.reference:
        raise HTTPException(400, "missing reference audio")
    d = tempfile.mkdtemp(prefix="dfy_vox_")
    ref = os.path.join(d, "ref.wav")
    try:
        with open(ref, "wb") as f:
            f.write(base64.b64decode(req.reference))
        out = _generate(text, steps=req.steps or None, ref_audio_path=ref)
    except Exception as ex:
        _write_err("clone", ex)
        raise HTTPException(500, "voxcpm clone failed: %s" % ex)
    finally:
        try:
            os.remove(ref)
            os.rmdir(d)
        except Exception:
            pass
    return Response(content=out, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    _log("监听: http://%s:%d" % (HOST, PORT))
    _log("默认音色: %s" % DEFAULT_CONTROL)
    _log("推理步数: %d" % DEFAULT_STEPS)
    threading.Thread(target=_load_model, daemon=True).start()
    cfg = uvicorn.Config(app, host=HOST, port=PORT, log_level="info")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=False)
    t.start()
    try:
        while t.is_alive():
            t.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        t.join(5)
