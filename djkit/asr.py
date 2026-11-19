"""djkit.asr

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import json
import os
import time
import wave
from pathlib import Path

from .core import CACHE_DIR, ffmpeg_exe, fmt_sec, probe, run


def _fmt_srt_tc(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms = 999
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def extract_audio_16k(src: Path, dst: Path) -> bool:
    r = run(
        [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        timeout=7200,
    )
    return r.returncode == 0 and dst.exists()


def ensure_whisper_model(model: str) -> str:
    """解析模型：本地路径直接用，否则下载到实体目录。

    为什么不依赖 huggingface 默认缓存：Windows 下它用符号链接组织 snapshots，
    非管理员/未开开发者模式时链不出来，结果 blobs 有 141MB 而 snapshot 目录是空的，
    faster-whisper 报 "Unable to open model.bin"。用 local_dir 落实体文件最稳。
    """
    if Path(model).exists():
        return model
    repo = model if "/" in model else f"Systran/faster-whisper-{model}"
    target = Path.home() / ".cache" / "djkit" / "models" / repo.split("/")[-1]
    if (target / "model.bin").exists() and (target / "config.json").exists():
        return str(target)
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        raise SystemExit("缺少 huggingface_hub：pip install huggingface_hub")

    print(f"拉取模型 {repo} → {target}")
    print("（首次约 75–500MB，走 hf-mirror；国内一般 1–5 分钟）")
    snapshot_download(
        repo_id=repo,
        local_dir=str(target),
        allow_patterns=["model.bin", "config.json", "tokenizer.json", "vocabulary.*",
                        "preprocessor_config.json", "*.txt"],
    )
    if not (target / "model.bin").exists():
        raise SystemExit(f"模型下载不完整：{target}")
    return str(target)


_MODEL_CACHE: dict = {}


def load_model(model: str, device: str = "cpu", compute: str = "int8"):
    """按 (模型, 设备, 精度) 缓存已加载的模型。

    批量处理多集时模型只加载一次——small 模型每次加载要 2~3 秒，
    10 集就是半分钟纯等待。
    """
    from faster_whisper import WhisperModel

    key = (model, device, compute)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = WhisperModel(
            ensure_whisper_model(model), device=device, compute_type=compute
        )
    return _MODEL_CACHE[key]


def cmd_asr(args) -> int:
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        print("缺少 faster-whisper：pip install faster-whisper")
        return 2

    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        print(f"文件不存在：{src}")
        return 2

    # 国内直连 huggingface 基本不通，默认走镜像；用户可自己覆盖
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    # hf_xet 的新传输协议会直连 cas-server.xethub.hf.co，走镜像时必然 401，必须关掉
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

    out = Path(args.out).expanduser().resolve() if args.out else src.with_suffix(".srt")
    work = src.parent / CACHE_DIR / "asr"
    work.mkdir(parents=True, exist_ok=True)
    wav = work / f"{src.stem}.16k.wav"

    meta = probe(src)
    print(f"素材 {src.name}  {fmt_sec(meta['duration'])}  "
          f"模型 {args.model}  语言 {args.lang or '自动'}")
    if not extract_audio_16k(src, wav):
        print("抽音频失败。")
        return 1

    print("加载模型…")
    try:
        model = load_model(args.model, args.device, args.compute)
    except SystemExit:
        raise
    except Exception as e:
        print(f"模型加载失败：{e}\n可尝试换小模型 --model tiny.en，或手动设置 HF_ENDPOINT 镜像地址。")
        return 1

    t0 = time.perf_counter()
    # 自己解码成 float32 数组直接喂给模型，绕开 faster-whisper 内部的 PyAV 解码
    # （PyAV 19 不认 av.open(metadata_errors=...) 这个参数，直接用数组最稳也最快）
    try:
        import numpy as np
        import wave

        with wave.open(str(wav), "rb") as f:
            sr = f.getframerate()
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        audio = pcm.astype(np.float32) / 32768.0
        if sr != 16000:
            raise SystemExit(f"采样率异常 {sr}，期望 16000")
    except SystemExit:
        raise
    except Exception as e:
        print(f"读取音频失败：{e}")
        return 1

    try:
        segments, info = model.transcribe(
            audio,
            language=args.lang or None,
            vad_filter=args.vad,
            beam_size=args.beam,
            condition_on_previous_text=False,
        )
    except Exception as e:
        print(f"转写失败：{e}")
        return 1

    print(f"检测语言：{info.language}（置信度 {info.language_probability:.2f}）")
    rows = []
    last = 0.0
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        rows.append((seg.start, seg.end, text))
        if seg.end - last > 30:
            last = seg.end
            print(f"\r  已转写 {fmt_sec(seg.end)} / {fmt_sec(meta['duration'])}", end="", flush=True)
    print()

    if not rows:
        print("没识别出任何内容（可能是纯音乐/无对白）。")
        return 1

    # SRT
    lines = []
    for i, (s, e, txt) in enumerate(rows, 1):
        lines.append(f"{i}\n{_fmt_srt_tc(s)} --> {_fmt_srt_tc(e)}\n{txt}\n")
    out.write_text("\n".join(lines), encoding="utf-8")

    # 带时间码的纯文本，写解说文案时最有用
    txt_out = out.with_suffix(".txt")
    txt_out.write_text(
        "\n".join(f"[{fmt_sec(s)}-{fmt_sec(e)}] {t}" for s, e, t in rows),
        encoding="utf-8",
    )

    # JSON，给后续程序用
    json_out = out.with_suffix(".json")
    json_out.write_text(
        json.dumps(
            [{"start": round(s, 3), "end": round(e, 3), "text": t} for s, e, t in rows],
            ensure_ascii=False, indent=1,
        ),
        encoding="utf-8",
    )

    dt = time.perf_counter() - t0
    print(f"完成：{len(rows)} 条字幕，耗时 {dt:.0f}s（{meta['duration'] / max(dt, 0.1):.1f}x 实时）")
    print(f"  {out.name} / {txt_out.name} / {json_out.name}")
    try:
        wav.unlink()
    except OSError:
        pass
    return 0
