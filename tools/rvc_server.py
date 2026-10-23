# -*- coding: utf-8 -*-
r"""rvc_server.py — 桌宠语音服务（用 RVC 官方推理流程）

为什么用官方 VC 类而不是手写推理：
  手写版本缺了官方 pipeline 的关键步骤（rmvpe 音高提取、分块处理、
  RMS 包络混合、特征索引），导致合成声音和文本内容完全对不上。
  官方 infer/vc/modules.py 的 VC.vc_single() 是训练时配套的完整流程。

运行环境：E:\AI\dafeiyu-voice\engines\rvc311（Python 3.11 + torch 2.14 CPU）
依赖模型：
  E:\AI\dafeiyu-voice\src\rvc-src\assets\rmvpe\rmvpe.pt        172.8 MB（F0 提取）
  E:\AI\dafeiyu-voice\src\rvc-src\assets\hubert_base\           180.4 MB（内容特征）
  E:\AI\dafeiyu-voice\models\rvc\xiaoxiao.safetensors        52.6 MB（音色）

接口：
  GET  /health                  -> {"ok": true, "loaded": bool, ...}
  POST /convert  (audio/wav)    -> audio/wav（转换后的音色）
"""
import io
import os
import sys
import tempfile
import threading
import time

RVC_SRC = os.environ.get("RVC_SRC", r"E:\AI\dafeiyu-voice\src\rvc-src")
MODEL_PATH = os.environ.get(
    "RVC_MODEL", r"E:\AI\dafeiyu-voice\models\rvc\xiaoxiao.pth")
HOST = os.environ.get("RVC_HOST", "127.0.0.1")
PORT = int(os.environ.get("RVC_PORT", "9880"))
SPEAKER_ID = int(os.environ.get("RVC_SPEAKER", "0"))

# 官方代码用相对路径找 i18n/assets，必须在源码根目录下运行
os.chdir(RVC_SRC)
sys.path.insert(0, RVC_SRC)
# 官方 get_vc 里 person = f'{weight_root}/{sid}'，所以 weight_root
# 要指向模型所在目录，sid 只传文件名
os.environ["weight_root"] = os.path.dirname(os.path.abspath(MODEL_PATH))
os.environ.setdefault("index_root", os.path.join(RVC_SRC, "logs"))
os.environ.setdefault("outside_index_root",
                      os.path.join(RVC_SRC, "assets", "indices"))
os.environ.setdefault("rmvpe_root", os.path.join(RVC_SRC, "assets", "rmvpe"))

import numpy as np
import torch

from fastapi import FastAPI, HTTPException, Request, Response

app = FastAPI(title="dafeiyu rvc")
_state = {"vc": None, "loaded": False, "loading": False}
_lock = threading.Lock()


def _torch_cuda():
    """是否有可用 CUDA。"""
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def _log(msg):
    print("[rvc_server] %s" % msg, flush=True)


def _write_err(where, ex):
    import traceback
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "rvc_error.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n===== %s @ %s =====\n"
                    % (where, time.strftime("%Y-%m-%d %H:%M:%S")))
            f.write(traceback.format_exc())
        _log("%s 失败: %s" % (where, ex))
    except Exception:
        pass


def _load_vc():
    if _state["loaded"]:
        return
    with _lock:
        if _state["loaded"]:
            return
        _state["loading"] = True
        try:
            _do_load_vc()
            _state["loaded"] = True
        finally:
            _state["loading"] = False


def _ensure_pth(model_path):
    """官方 VC 只吃 .pth（含 config/f0/version）。若给的是 safetensors，
    自动转换成同目录的 .pth（只做一次）。"""
    if model_path.lower().endswith(".pth"):
        if os.path.exists(model_path):
            return model_path
        # 没有 .pth，看看有没有同名 safetensors
        alt = os.path.splitext(model_path)[0] + ".safetensors"
        if not os.path.exists(alt):
            raise FileNotFoundError("找不到模型: %s" % model_path)
        model_path = alt
    if model_path.lower().endswith(".safetensors"):
        dst = os.path.splitext(model_path)[0] + ".pth"
        if os.path.exists(dst):
            return dst
        _log("转换 safetensors -> pth: %s" % os.path.basename(dst))
        import json as _json
        import struct as _struct
        from safetensors.torch import load_file
        with open(model_path, "rb") as f:
            n = _struct.unpack("<Q", f.read(8))[0]
            hdr = _json.loads(f.read(n).decode("utf-8"))
        meta = hdr.get("__metadata__") or {}
        cfg = _json.loads(meta.get("config", "[]"))
        sd = load_file(model_path)
        torch.save({
            "weight": sd,
            "config": cfg,
            "f0": 1 if str(meta.get("f0", "true")).lower() == "true" else 0,
            "version": meta.get("version", "v2"),
            "info": meta.get("info", ""),
        }, dst)
        _log("转换完成: %s" % dst)
        return dst
    return model_path


def _do_load_vc():
    _log("加载官方 RVC 流程…")
    global MODEL_PATH
    MODEL_PATH = _ensure_pth(MODEL_PATH)
    from configs.config import Config
    from infer.vc.modules import VC

    # Config 是 @singleton_variable 装饰的无参单例
    cfg = Config()
    _log("Config: device=%s dtype=%s is_half=%s"
         % (cfg.device, cfg.dtype, cfg.is_half))

    vc = VC(cfg)
    # get_vc 只吃文件名（内部拼 weight_root）
    model_name = os.path.basename(MODEL_PATH)
    _log("加载音色模型: %s（weight_root=%s）"
         % (model_name, os.environ.get("weight_root")))
    vc.get_vc(model_name)
    _state["vc"] = vc
    _log("官方 RVC 就绪（speaker=%d）" % SPEAKER_ID)


def _convert_wav(wav_bytes):
    _load_vc()
    vc = _state["vc"]
    if vc is None:
        raise RuntimeError("VC 未加载")

    d = tempfile.mkdtemp(prefix="dfy_rvc_")
    inp = os.path.join(d, "in.wav")
    with open(inp, "wb") as f:
        f.write(wav_bytes)

    t0 = time.time()
    status, result = vc.vc_single(
        SPEAKER_ID, inp, 0, "rmvpe", "", 0.0, 0, 0.25, 0.33)
    dt = time.time() - t0
    if not result or result[0] is None or result[1] is None:
        raise RuntimeError("vc_single 失败: %s" % status)
    sr, audio = result[0], result[1]
    _log("转换完成 %.2fs  %d Hz  %d 采样点" % (dt, sr, len(audio)))

    audio = np.asarray(audio, dtype=np.float32)
    peak = np.abs(audio).max()
    if peak > 0:
        audio = audio / peak * 0.95
    import wave
    pcm = (audio * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    try:
        os.remove(inp)
        os.rmdir(d)
    except Exception:
        pass
    return buf.getvalue()


@app.get("/health")
def health():
    return {
        "ok": True,
        "mode": "official-vc",
        "model": os.path.basename(MODEL_PATH),
        "model_exists": os.path.exists(MODEL_PATH),
        "loaded": bool(_state["loaded"]),
        "loading": bool(_state["loading"]),
        "device": (str(getattr(_state.get("cfg"), "device", "cpu"))
                   if _state.get("cfg") else
                   ("cuda" if _torch_cuda() else "cpu")),
    }


@app.post("/convert")
async def convert(request: Request):
    body = await request.body()
    if not body or len(body) < 100:
        raise HTTPException(400, "empty audio")
    try:
        out = _convert_wav(body)
    except Exception as ex:
        _write_err("convert", ex)
        raise HTTPException(500, "rvc failed: %s" % ex)
    return Response(content=out, media_type="audio/wav")


if __name__ == "__main__":
    import uvicorn
    _log("源码: %s" % RVC_SRC)
    _log("模型: %s（存在=%s）" % (MODEL_PATH, os.path.exists(MODEL_PATH)))
    _log("监听: http://%s:%d" % (HOST, PORT))
    threading.Thread(target=_load_vc, daemon=True).start()
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