# -*- coding: utf-8 -*-
r"""gptsovits_server.py — 桌宠语音服务（GPT-SoVITS / yin1 音色直出）

与现有两个服务并存，互不影响：
  rvc_server.py       : Edge-TTS 生成 → RVC 换成晓晓音色（快，约 2 秒）
  voxcpm_server.py    : VoxCPM2 直出（慢，约 3 分钟，48kHz 高音质）
  gptsovits_server.py : GPT-SoVITS 直出（本文件；CPU 下约为音频时长的 1.5 倍）

和 RVC 的区别：RVC 是「先有基准语音再换音色」，GPT-SoVITS 是
「文本直接合成音色」，少了 Edge-TTS 这一环，也不需要联网。

运行环境：E:\AI\voice-models\yin1\env311（Python 3.11 + torch 2.6 CPU）
模型目录：E:\AI\voice-models\yin1\models
  yin1.ckpt   GPT 语义模型（决定语调、断句）
  yin1.pth    SoVITS 声学模型（决定音色）
  ref9s.wav   参考音频（推理必填，须与参考文本原样配对）

接口：
  GET  /health               -> {"ok": true, "loaded": bool, ...}
  POST /tts  {"text": "..."} -> audio/wav

环境变量（都有默认值，不改也能跑）：
  GPT_ROOT    模型根目录，默认 E:\AI\voice-models\yin1
  GPT_HOST    监听地址，默认 127.0.0.1
  GPT_PORT    监听端口，默认 9882
  GPT_MAXLEN  单次最长文本，默认 800 字
  GPT_REF_TEXT 参考文本，须与参考音频配对
"""
import io
import os
import sys
import threading
import time
import wave

ROOT = os.environ.get("GPT_ROOT", r"E:\AI\voice-models\yin1")
REPO = os.path.join(ROOT, "GPT-SoVITS")
MODELS = os.path.join(ROOT, "models")
HOST = os.environ.get("GPT_HOST", "127.0.0.1")
PORT = int(os.environ.get("GPT_PORT", "9882"))
MAX_LEN = int(os.environ.get("GPT_MAXLEN", "800"))

REF_AUDIO = os.path.join(MODELS, "ref9s.wav")
REF_TEXT = os.environ.get(
    "GPT_REF_TEXT",
    "以前我很期待两个人相处的日子。夏天我穿着小吊带，露出好看的锁骨。")
GPT_WEIGHTS = os.path.join(MODELS, "yin1.ckpt")
SOVITS_WEIGHTS = os.path.join(MODELS, "yin1.pth")

# 官方代码用相对路径找 assets/pretrained_models，必须切到仓库根目录
os.chdir(REPO)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "GPT_SoVITS"))
# jieba_fast 在 CPython 3.11 无预编译包，用兼容补丁映射到纯 python jieba
sys.path.insert(0, os.path.join(ROOT, "shims"))
os.environ.setdefault(
    "PYTHONPATH", REPO + os.pathsep + os.path.join(REPO, "GPT_SoVITS"))

from fastapi import FastAPI, HTTPException, Request, Response  # noqa: E402
from pydantic import BaseModel  # noqa: E402

app = FastAPI(title="dafeiyu gptsovits")
_state = {"tts": None, "loaded": False, "loading": False}
_load_lock = threading.Lock()      # 只保护加载
_run_lock = threading.Lock()       # 推理串行：prompt_cache 是共享状态


def _log(msg):
    print("[gptsovits] %s" % msg, flush=True)


def _write_err(where, ex):
    import traceback
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "gptsovits_error.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n===== %s @ %s =====\n"
                    % (where, time.strftime("%Y-%m-%d %H:%M:%S")))
            f.write(traceback.format_exc())
        _log("%s 失败: %s" % (where, ex))
    except Exception:
        pass


def _build_tts():
    """构建 TTS 实例（CPU 模式，version=v2Pro 对应训练时的版本）。"""
    from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config
    cfg = TTS_Config({"custom": {
        "device": "cpu",          # 本机无 NVIDIA 显卡，只能 CPU
        "is_half": False,         # CPU 不支持半精度，必须 False
        "version": "v2Pro",
        "t2s_weights_path": GPT_WEIGHTS,
        "vits_weights_path": SOVITS_WEIGHTS,
        "cnhuhbert_base_path": os.path.join(
            "GPT_SoVITS", "pretrained_models", "chinese-hubert-base"),
        "bert_base_path": os.path.join(
            "GPT_SoVITS", "pretrained_models", "chinese-roberta-wwm-ext-large"),
    }})
    _log("device=%s is_half=%s version=%s"
         % (cfg.device, cfg.is_half, cfg.version))
    t0 = time.time()
    tts = TTS(cfg)
    _log("模型加载完成 %.1f 秒" % (time.time() - t0))
    return tts


def _load_model():
    """加载模型（首次较慢，之后常驻）。"""
    if _state["loaded"]:
        return
    with _load_lock:
        if _state["loaded"]:
            return
        _state["loading"] = True
        try:
            _state["tts"] = _build_tts()
            _state["loaded"] = True
        finally:
            _state["loading"] = False


def _wav_bytes(pcm, sr):
    """int16 波形 → WAV 字节。

    注意：TTS.run() 产出的是 **int16**，不能再乘 32768，
    否则每个采样都会顶到满量程造成爆音。
    """
    import numpy as np
    a = np.asarray(pcm)
    if a.dtype != np.int16:
        a = np.clip(a.astype("float32"), -1.0, 1.0)
        a = (a * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(a.tobytes())
    return buf.getvalue()


def _synthesize(text):
    _load_model()
    tts = _state["tts"]
    if tts is None:
        raise RuntimeError("模型未加载")

    inputs = {
        "text": text,
        "text_lang": "zh",
        "ref_audio_path": REF_AUDIO,
        "prompt_text": REF_TEXT,
        "prompt_lang": "zh",
        "text_split_method": "cut5",
        "batch_size": 1,
        "speed_factor": 1.0,
        "seed": 42,
        # CPU 上并行推理容易把内存吃满，串行更稳
        "parallel_infer": False,
    }

    t0 = time.time()
    # prompt_cache 是共享状态，并发会互相污染，这里串行
    with _run_lock:
        chunks = []
        sr = 32000
        for sr, audio in tts.run(inputs):
            chunks.append(audio)
    if not chunks:
        raise RuntimeError("推理没有产出音频")

    import numpy as np
    pcm = np.concatenate([np.asarray(c) for c in chunks])
    dt = time.time() - t0
    _log("合成完成 %.1f 秒  %d Hz  %d 采样点" % (dt, sr, len(pcm)))
    return _wav_bytes(pcm, sr)


class TTSReq(BaseModel):
    text: str


@app.get("/health")
def health():
    return {
        "ok": True,
        "mode": "gptsovits",
        "loaded": bool(_state["loaded"]),
        "loading": bool(_state["loading"]),
        "device": "cpu",
        "voice": "yin1",
        "model_exists": all(os.path.exists(p) for p in
                            (GPT_WEIGHTS, SOVITS_WEIGHTS, REF_AUDIO)),
    }


@app.post("/tts")
async def tts(request: Request):
    """接受 JSON {"text": ...}，也兼容 audio/plain 直传文本。"""
    text = ""
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
            text = (body or {}).get("text") or ""
        except Exception:
            raise HTTPException(400, "bad json")
    else:
        raw = await request.body()
        text = raw.decode("utf-8", errors="ignore")

    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    if len(text) > MAX_LEN:
        _log("文本过长 %d 字，截断到 %d 字" % (len(text), MAX_LEN))
        text = text[:MAX_LEN]

    try:
        out = _synthesize(text)
    except Exception as ex:
        _write_err("tts", ex)
        raise HTTPException(500, "gptsovits failed: %s" % ex)
    return Response(content=out, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    _log("模型根目录: %s" % ROOT)
    _log("权重: %s（存在=%s）" % (GPT_WEIGHTS, os.path.exists(GPT_WEIGHTS)))
    _log("音色: %s（存在=%s）"
         % (SOVITS_WEIGHTS, os.path.exists(SOVITS_WEIGHTS)))
    _log("参考音频: %s（存在=%s）"
         % (REF_AUDIO, os.path.exists(REF_AUDIO)))
    _log("监听: http://%s:%d" % (HOST, PORT))
    # 后台预加载，让首个请求不必等太久
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
