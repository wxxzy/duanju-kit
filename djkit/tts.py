"""djkit.tts

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import probe


TTS_VOICES_HINT = [
    ("zh-CN-YunxiNeural", "男声，年轻，适合影视解说"),
    ("zh-CN-YunjianNeural", "男声，低沉有力，适合悬疑/硬核"),
    ("zh-CN-XiaoxiaoNeural", "女声，亲和，通用"),
    ("zh-CN-XiaoyiNeural", "女声，活泼"),
    ("en-US-GuyNeural", "英文男声"),
]


def cmd_tts(args) -> int:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        print("缺少 edge-tts：pip install edge-tts")
        return 2

    import asyncio

    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        print(f"文案文件不存在：{src}")
        return 2
    text = src.read_text(encoding="utf-8-sig").strip()
    if not text:
        print("文案是空的。")
        return 2

    # 去掉时间码标记和 markdown 强调符（否则会被朗读成"星号星号"）
    text = re.sub(r"^\[\d{2}:\d{2}-\d{2}:\d{2}\]\s*", "", text, flags=re.M)
    text = re.sub(r"(\*{1,3}|_{1,2}|`{1,3}|^#{1,6}\s*)", "", text, flags=re.M)
    text = re.sub(r"[ \t]+", " ", text).strip()
    sentences = [s.strip() for s in re.split(r"[。！？\n]+", text) if s.strip()]
    out = Path(args.out).expanduser().resolve() if args.out else src.with_suffix(".mp3")

    print(f"配音：{len(sentences)} 句 / {len(text)} 字  音色 {args.voice}  语速 {args.rate}")
    if args.dry_run:
        for s in sentences[:5]:
            print(f"  · {s}")
        if len(sentences) > 5:
            print(f"  … 其余 {len(sentences) - 5} 句")
        print("预演结束。")
        return 0

    async def synth() -> None:
        comm = edge_tts.Communicate(text, args.voice, rate=args.rate, volume=args.volume)
        await comm.save(str(out))

    try:
        asyncio.run(synth())
    except Exception as e:
        print(f"合成失败：{e}\n"
              f"edge-tts 需要访问微软语音服务；不通时可换本地 SAPI：\n"
              f"  powershell -c \"Add-Type -AssemblyName System.Speech; "
              f"\\(New-Object System.Speech.Synthesis.SpeechSynthesizer\\).SetOutputToWaveFile('out.wav')\"")
        return 1

    if not out.exists() or out.stat().st_size < 1000:
        print("输出异常（文件过小）。")
        return 1
    m = probe(out)
    print(f"完成：{out.name}  {m['duration']:.1f}s  {out.stat().st_size // 1024}KB")
    if sentences:
        # 中文配音大约每秒 4.5 字，用来估画面够不够
        est = len(text) / 4.5
        print(f"估算语速：{est:.0f}s（约 4.5 字/秒）；实际 {m['duration']:.1f}s")
    return 0
