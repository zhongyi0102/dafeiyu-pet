# -*- coding: utf-8 -*-
"""edge-tts 独立子进程：文本 → WAV 字节写到 stdout"""
import asyncio
import io
import os
import sys

# 代理（aiohttp 需要显式传入，光设环境变量不生效）
PROXY = os.environ.get("DFY_PROXY", "http://127.0.0.1:3067") or None


def main():
    if len(sys.argv) < 3:
        print("usage: _edge_tts_cli.py TEXT VOICE [SPEED]", file=sys.stderr)
        return 2
    text = sys.argv[1]
    voice = sys.argv[2]
    try:
        speed = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    except Exception:
        speed = 1.0

    import edge_tts

    async def run():
        rate = "+0%"
        if speed > 1.0:
            rate = "+%d%%" % int((speed - 1) * 100)
        elif speed < 1.0:
            rate = "-%d%%" % int((1 - speed) * 100)
        kwargs = {"rate": rate}
        if PROXY:
            kwargs["proxy"] = PROXY          # ← 显式传代理
        comm = edge_tts.Communicate(text, voice, **kwargs)
        buf = io.BytesIO()
        async for ch in comm.stream():
            if ch["type"] == "audio":
                buf.write(ch["data"])
        return buf.getvalue()

    try:
        data = asyncio.run(run())
    except Exception as ex:
        print("%s: %s" % (type(ex).__name__, ex), file=sys.stderr)
        return 1
    if not data:
        print("空音频", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())