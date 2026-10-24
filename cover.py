# -*- coding: utf-8 -*-
r"""cover.py — AI 翻唱流水线（桌宠侧调用）

流程：
  1. 按歌名搜索并下载原曲
  2. UVR5 分离人声/伴奏
  3. RVC 把原唱换成目标音色
  4. 混音输出成品

调用方式：后台线程，通过信号回主线程。
"""
import logging
import os
import subprocess
import sys
import time

_logger = logging.getLogger("dafeiyu")


def log(msg, level=logging.INFO):
    """与主程序一致的 log(text, level) 函数式接口。"""
    try:
        _logger.log(level, msg)
    except Exception:
        pass


def _cover_dir():
    """翻唱工作目录。"""
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "cover_cache")


def _python():
    """RVC 环境的 Python。"""
    return r"E:\AI\dafeiyu-voice\engines\rvc311\Scripts\python.exe"


def _tools(name):
    """定位 tools/ 下的脚本，按优先级查找并记录来源。

    打包后（frozen）：
      1. exe 同级 tools/     —— 用户可自行更新脚本，优先
      2. _MEIPASS/tools/     —— exe 内置
      3. 源码路径            —— 兜底
    源码运行：直接用本文件旁的 tools/。
    """
    cands = []
    frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cands.append(("exe同级", os.path.join(exe_dir, "tools", name)))
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(("_MEIPASS", os.path.join(meipass, "tools", name)))
    src_dir = os.path.dirname(os.path.abspath(__file__))
    cands.append(("源码", os.path.join(src_dir, "tools", name)))

    for src, p in cands:
        if os.path.exists(p):
            log("[翻唱] tools 脚本命中(%s): %s" % (src, p))
            return p
    # 全都没找到：返回首选路径，调用方据此报错并显示期望路径
    tried = "; ".join("%s=%s" % (s, p) for s, p in cands)
    log("[翻唱] tools 脚本未找到: %s（已尝试: %s）" % (name, tried),
        logging.WARNING)
    return cands[0][1]


def _child_env():
    """给外部 venv 的子进程准备干净环境。

    exe 运行时 os.environ 里混入了 PyInstaller 注入的变量
    （_MEIPASS、_PYI_*、PYTHONHOME/PYTHONPATH 等），会让外部 venv
    加载到错误的 DLL / 标准库。这里剔除它们，源码运行时原样返回。
    """
    env = dict(os.environ)
    if not getattr(sys, "frozen", False):
        return env
    # PyInstaller 注入的键
    for k in list(env.keys()):
        ku = k.upper()
        if ku.startswith("_PYI_") or ku == "_MEIPASS" or ku == "_MEIPASS2":
            env.pop(k, None)
    # 会干扰外部解释器的 Python 环境变量
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP",
              "PYTHONEXECUTABLE", "PYTHONUSERBASE", "PYTHONNOUSERSITE"):
        env.pop(k, None)
    # 从 PATH 里剔除 exe 解包目录（防止优先加载到 exe 自带的 DLL）
    meipass = getattr(sys, "_MEIPASS", None)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    drop = set()
    for p in (meipass, exe_dir):
        if p:
            drop.add(os.path.normcase(os.path.normpath(p)))
    parts = env.get("PATH", "").split(os.pathsep)
    kept = [p for p in parts
            if os.path.normcase(os.path.normpath(p)) not in drop]
    if kept:
        env["PATH"] = os.pathsep.join(kept)
    log("[翻唱] 子进程环境已净化（移除 %d 个 PyInstaller 变量）"
        % (len(os.environ) - len(env)))
    return env


def _check_python():
    """检查外部 RVC 解释器是否存在。返回 (ok, 错误信息)。"""
    py = _python()
    if os.path.exists(py):
        return True, ""
    msg = ("找不到 RVC 环境的 Python: %s；翻唱需要外部语音环境"
           "（torch + RVC 源码 + 音色模型），请确认该目录存在。" % py)
    log("[翻唱] %s" % msg, logging.ERROR)
    return False, "找不到 RVC 环境（%s）" % py


def make_cover(query, progress=None, timeout=1800):
    """完整翻唱流水线。

    query: 歌名/歌手，如 "勇气 梁静茹"
    progress: 可选回调 fn(stage_text)，用于报告进度
    返回 (成品路径, 歌名, 歌手, err)
    """
    def report(msg):
        log("[翻唱] %s" % msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    import lyrics as L

    work = _cover_dir()
    os.makedirs(work, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe = "".join(c for c in query if c not in r'\/:*?"<>|')[:20] or "song"
    mp3 = os.path.join(work, "%s_%s.mp3" % (safe, stamp))

    # ---- 1. 下载 ----
    report("正在搜索并下载《%s》…" % query)
    name, artist, err = L.download_song(query, mp3, br=320)
    if err:
        return "", "", "", "下载失败：%s" % err
    report("已下载《%s》- %s" % (name, artist))

    # ---- 2. UVR5 分离 ----
    report("正在分离人声和伴奏…（约 3 分钟）")
    out_dir = os.path.join(work, "%s_%s" % (safe, stamp))
    os.makedirs(out_dir, exist_ok=True)
    vocals, inst, err = _separate(mp3, out_dir)
    if err:
        return "", name, artist, "分离失败：%s" % err
    report("分离完成")

    # ---- 3. RVC 换音色 ----
    report("正在换成大肥鱼的声音…（约 3 分钟）")
    converted = os.path.join(out_dir, "converted.wav")
    err = _convert(vocals, converted)
    if err:
        return "", name, artist, "换音色失败：%s" % err
    report("换音色完成")

    # ---- 4. 混音 ----
    report("正在混音…")
    final = os.path.join(out_dir, "%s_翻唱.wav" % safe)
    err = _mix(converted, inst, final)
    if err:
        return "", name, artist, "混音失败：%s" % err
    report("完成！")

    return final, name, artist, ""


def _separate(mp3_path, out_dir):
    """UVR5 分离。返回 (vocals, instrumental, err)。"""
    okpy, pyerr = _check_python()
    if not okpy:
        return "", "", pyerr
    script = _tools("_separate.py")
    if not os.path.exists(script):
        msg = "找不到分离脚本: %s" % script
        log("[翻唱] %s" % msg, logging.ERROR)
        return "", "", msg
    try:
        r = subprocess.run(
            [_python(), "-u", script, mp3_path, out_dir],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=900, env=_child_env(),
            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as ex:
        log("[翻唱] 分离子进程异常: %s: %s"
            % (type(ex).__name__, ex), logging.ERROR)
        return "", "", "%s: %s" % (type(ex).__name__, ex)
    if r.returncode != 0:
        return "", "", (r.stderr or r.stdout or "")[-300:]

    vocals = inst = ""
    for f in os.listdir(out_dir):
        low = f.lower()
        p = os.path.join(out_dir, f)
        if "vocal" in low and "instrument" not in low:
            vocals = p
        elif "instrument" in low:
            inst = p
    if not vocals:
        return "", "", "没找到人声文件"
    return vocals, inst, ""


def _convert(vocals, out_path):
    """RVC 换音色。"""
    okpy, pyerr = _check_python()
    if not okpy:
        return pyerr
    script = _tools("rvc_convert_cli.py")
    if not os.path.exists(script):
        msg = "找不到 RVC 转换脚本: %s" % script
        log("[翻唱] %s" % msg, logging.ERROR)
        return msg
    try:
        r = subprocess.run(
            [_python(), "-u", script, vocals, out_path,
             "--protect", "0.5"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1200, env=_child_env(),
            creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as ex:
        log("[翻唱] 换音色子进程异常: %s: %s"
            % (type(ex).__name__, ex), logging.ERROR)
        return "%s: %s" % (type(ex).__name__, ex)
    if r.returncode != 0:
        return (r.stderr or r.stdout or "")[-300:]
    if not os.path.exists(out_path):
        return "转换没产出文件"
    return ""


def _read_wav(path):
    """读 WAV -> (samples(float list), sr, channels)。纯标准库。"""
    import array
    import wave
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        a = array.array("h")
        a.frombytes(raw)
        scale = 32768.0
    elif sw == 4:
        a = array.array("i")
        a.frombytes(raw)
        scale = 2147483648.0
    else:
        raise RuntimeError("不支持的位深: %d" % sw)
    samples = [x / scale for x in a]
    return samples, sr, ch


def _resample(samples, src_sr, dst_sr, ch=1):
    """线性插值重采样（够用，避免依赖 librosa）。"""
    if src_sr == dst_sr or not samples:
        return samples
    ratio = float(dst_sr) / float(src_sr)
    n_out = int(len(samples) / ch * ratio) * ch
    out = [0.0] * n_out
    src_n = len(samples) // ch
    for i in range(n_out // ch):
        pos = i / ratio
        i0 = int(pos)
        i1 = min(i0 + 1, src_n - 1)
        frac = pos - i0
        for c in range(ch):
            out[i * ch + c] = (samples[i0 * ch + c] * (1 - frac)
                               + samples[i1 * ch + c] * frac)
    return out


def _mix(converted, inst, out_path):
    """混音（纯标准库，不依赖 numpy/librosa）。"""
    try:
        import array
        import wave
        v, sr1, ch1 = _read_wav(converted)
        if ch1 > 1:
            v = v[::ch1]          # 取第一声道
            ch1 = 1
        if inst and os.path.exists(inst):
            b, sr2, ch2 = _read_wav(inst)
            if ch2 > 1:
                b = b[::ch2]
            if sr2 != sr1:
                b = _resample(b, sr2, sr1, 1)
            n = min(len(v), len(b))
            out = [v[i] * 0.8 + b[i] * 0.6 for i in range(n)]
        else:
            out = v
        peak = max(abs(x) for x in out) if out else 0.0
        if peak > 0:
            k = 0.95 / peak
            out = [x * k for x in out]
        pcm = array.array("h", [max(-32768, min(32767, int(x * 32767)))
                                for x in out])
        with wave.open(out_path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr1)
            w.writeframes(pcm.tobytes())
        return ""
    except Exception as ex:
        import traceback
        log("[翻唱] 混音异常: %s" % traceback.format_exc()[-400:])
        return "%s: %s" % (type(ex).__name__, ex)
