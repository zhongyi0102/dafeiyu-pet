# -*- coding: utf-8 -*-
"""rvc_server.py — 桌宠语音服务（只做 RVC 音色转换）

架构（方案 2）：
  桌宠（Qt 进程）──Edge-TTS 生成基准语音──▶ WAV 字节
       │
       └──HTTP POST /convert（WAV 字节）──▶ 本服务：RVC 转成晓晓音色
                                              │
  桌宠 ◀──────────── WAV（晓晓音色）──────────┘

为什么这样拆：
  Edge-TTS 用 WebSocket，在 uvicorn 里会 NoAudioReceived；
  但在桌宠的普通 Qt 进程里完全正常。所以 TTS 放桌宠侧，
  服务只保留 RVC 转换（纯本地计算，不联网）。

用法：
  E:\AI\rvc311\Scripts\python.exe rvc_server.py
  或双击 tools\启动语音服务.bat

接口：
  GET  /health                 → {"ok": true, ...}
  POST /convert  (audio/wav)   → audio/wav（转换后的音色）
"""
import io
import os
import sys
import threading
import tempfile
import time

MODEL_PATH = os.environ.get("RVC_MODEL", r"E:\AI\rvc-models\xiaoxiao.safetensors")
RVC_DIR = os.environ.get("RVC_SRC", r"E:\AI\rvc-src")
HOST = os.environ.get("RVC_HOST", "0.0.0.0")
PORT = int(os.environ.get("RVC_PORT", "9880"))
SAMPLE_RATE = 40000

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from infer.module import models as rvc_models
except Exception as _e:
    print("[rvc_server] 导入 RVC 模型定义失败:", _e)
    raise

import numpy as np
import torch

from fastapi import FastAPI, HTTPException, Request, Response

app = FastAPI(title="dafeiyu rvc")
_state = {"net": None, "hubert": None}
_load_lock = threading.Lock()   # 预加载线程与首个转换请求的竞态保护


def _write_err(where, ex):
    import traceback
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rvc_error.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n===== %s @ %s =====\n" % (where, time.strftime("%Y-%m-%d %H:%M:%S")))
            f.write(traceback.format_exc())
        print("[rvc_server] %s 失败（详见 rvc_error.log）: %s" % (where, ex))
    except Exception:
        pass


def _load_models():
    # 双重检查 + 全程持锁：预加载线程和首个转换请求并发进入时，
    # 后到者要么等加载完、要么直接复用，绝不拿到半初始化的 state
    if _state["net"] is not None and _state["hubert"] is not None:
        return
    with _load_lock:
        if _state["net"] is not None and _state["hubert"] is not None:
            return
        _do_load_models()

def _do_load_models():
    print("[rvc_server] 加载 RVC 模型:", MODEL_PATH)
    from safetensors.torch import load_file
    sd = load_file(MODEL_PATH)

    import json as _json
    import struct as _struct
    with open(MODEL_PATH, "rb") as f:
        n = _struct.unpack("<Q", f.read(8))[0]
        meta = _json.loads(f.read(n).decode("utf-8")).get("__metadata__") or {}
    cfg = _json.loads(meta.get("config", "[]"))
    print("[rvc_server] 配置:", cfg[:8], "... sr=", cfg[-1] if cfg else "?")

    inter = int(cfg[2])
    hidden = int(cfg[3])
    filt = int(cfg[4])
    n_heads = int(cfg[5])
    n_layers = int(cfg[6])
    kernel = int(cfg[7])
    spk_dim = int(cfg[15]) if len(cfg) > 15 else 109
    gin = int(cfg[16]) if len(cfg) > 16 else 256
    sr = int(cfg[17]) if len(cfg) > 17 else 40000

    net = rvc_models.SynthesizerTrnMs768NSFsid(
        spec_channels=int(cfg[0]), segment_size=int(cfg[4]),
        inter_channels=inter, hidden_channels=hidden,
        filter_channels=filt, n_heads=n_heads, n_layers=n_layers,
        kernel_size=kernel, p_dropout=0.0,
        resblock="1", resblock_kernel_sizes=[3, 7, 11],
        resblock_dilation_sizes=[[1, 3, 5], [1, 3, 5], [1, 3, 5]],
        upsample_rates=[10, 10, 2, 2],
        upsample_initial_channel=int(cfg[13]),
        upsample_kernel_sizes=[16, 16, 4, 4],
        spk_embed_dim=spk_dim, gin_channels=gin, sr=sr,
        version="v2", f0=1, is_half=False)
    net.eval()
    missing, unexpected = net.load_state_dict(sd, strict=False)
    if missing:
        print("[rvc_server] 缺失张量:", len(missing), "（enc_q 属正常）")
    _state["net"] = net
    print("[rvc_server] RVC 模型加载完成")

    print("[rvc_server] 加载特征提取器...")
    from transformers import AutoFeatureExtractor, HubertModel
    name = os.environ.get("HUBERT_NAME", "facebook/hubert-base-ls960")
    fe = AutoFeatureExtractor.from_pretrained(name)
    hm = HubertModel.from_pretrained(name).eval()
    _state["hubert"] = (fe, hm)
    print("[rvc_server] 特征提取器加载完成")


@torch.no_grad()
def _rvc_convert(wav_bytes):
    _load_models()
    net = _state["net"]
    fe, hm = _state["hubert"]

    import librosa
    import soundfile as sf

    data, sr = sf.read(io.BytesIO(wav_bytes))
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = data.astype(np.float32)
    if sr != 16000:
        data16 = librosa.resample(data, orig_sr=sr, target_sr=16000)
    else:
        data16 = data

    inputs = fe(data16, sampling_rate=16000, return_tensors="pt")
    out = hm(inputs.input_values).last_hidden_state
    feats = out.transpose(1, 2)
    T = feats.shape[-1]

    f0 = librosa.yin(data16, fmin=50, fmax=1100, sr=16000)
    f0 = torch.from_numpy(f0.astype(np.float32)).unsqueeze(0)
    if f0.shape[-1] != T:
        f0 = torch.nn.functional.interpolate(
            f0.unsqueeze(1), size=T, mode="nearest").squeeze(1)
    f0 = f0.unsqueeze(1)
    f0_coarse = torch.clamp((f0 / 16000 * 256), 1, 255).long().squeeze(1)

    phone_in = feats.transpose(1, 2)
    nsff0 = f0.squeeze(1)
    sid = torch.LongTensor([0])
    lengths = torch.LongTensor([T])
    audio = net.infer(phone_in, lengths, f0_coarse, nsff0, sid)
    audio = audio[0][0].detach().cpu().numpy()

    peak = np.abs(audio).max()
    if peak > 0:
        audio = audio / peak * 0.95

    import wave
    pcm = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


@app.get("/health")
def health():
    return {
        "ok": True,
        "mode": "rvc-only",
        "model": os.path.basename(MODEL_PATH),
        "model_exists": os.path.exists(MODEL_PATH),
        "loaded": _state["net"] is not None,
        "device": "cpu",
    }


@app.post("/convert")
async def convert(request: Request):
    body = await request.body()
    if not body or len(body) < 100:
        raise HTTPException(400, "empty audio")
    t0 = time.time()
    try:
        out = _rvc_convert(body)
    except Exception as ex:
        _write_err("rvc-convert", ex)
        raise HTTPException(500, "rvc failed: %s" % ex)
    print("[rvc_server] RVC 转换完成 %.2fs  %d → %d 字节"
          % (time.time() - t0, len(body), len(out)))
    return Response(content=out, media_type="audio/wav")


if __name__ == "__main__":
    import threading
    import uvicorn
    print("模型:", MODEL_PATH, "存在:", os.path.exists(MODEL_PATH))
    print("监听: http://%s:%d" % (HOST, PORT))
    print("模式: 只做 RVC 转换（TTS 由桌宠侧完成）")
    # 启动即后台预加载模型：/health 的 loaded 变 true 后，
    # 客户端才认为服务就绪（首个转换不用再等加载）
    threading.Thread(target=_load_models, daemon=True).start()
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