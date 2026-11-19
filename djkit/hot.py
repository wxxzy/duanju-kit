"""djkit.hot

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import csv
import re
import subprocess
import time
import hashlib
import wave
from pathlib import Path

from .core import (CACHE_DIR, child_kwargs, ffmpeg_exe, fmt_sec, probe,
                   probe_cached, run)
from .media import iter_videos


def _decode_audio_mono(src: Path, dst: Path, ar: int = 8000) -> bool:
    """只解音频轨，8kHz 单声道 PCM。42 分钟素材约 15 秒。"""
    r = run(
        [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", str(ar), "-c:a", "pcm_s16le", str(dst)],
        timeout=3600,
    )
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 44


def _decode_keyframes(src: Path, raw: Path, w: int = 64, h: int = 36) -> tuple[bool, list[float]]:
    """只解 I 帧（-skip_frame nokey），缩成 64x36 灰度并记录时间戳。

    为什么不逐帧解：42 分钟 1080p 全解码要 8-15 分钟，只解关键帧是几十秒。
    代价是画面变化的时间精度只有 1-2 秒，对"找高光"够用。
    """
    import subprocess

    cmd = [
        ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "info",
        "-skip_frame", "nokey", "-i", str(src), "-an",
        "-vf", f"scale={w}:{h}:flags=fast_bilinear,format=gray,showinfo",
        "-vsync", "0", "-f", "rawvideo", str(raw),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **child_kwargs())
    ts = [float(m) for m in re.findall(r"pts_time:([\d.]+)", proc.stderr or "")]
    ok = proc.returncode == 0 and raw.exists() and raw.stat().st_size > 0
    return ok, ts


def analyze_motion(src: Path, work: Path, hop: float = 0.5) -> tuple:
    """返回 (时间轴数组, 音频分, 画面分)，都按 hop 秒对齐"""
    import numpy as np

    work.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(str(src).encode("utf-8")).hexdigest()[:10]
    wav = work / f"{key}.wav"
    raw = work / f"{key}.raw"

    meta = probe(src)
    dur = max(0.1, meta["duration"])
    n = int(dur / hop) + 1
    t = np.arange(n) * hop

    # ---- 音频能量 ----
    audio = np.zeros(n)
    if meta["has_audio"] and _decode_audio_mono(src, wav):
        import wave

        with wave.open(str(wav), "rb") as f:
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32)
            sr = f.getframerate()
        if pcm.size:
            win = max(1, int(sr * hop))
            m = pcm.size // win
            if m:
                frames = pcm[: m * win].reshape(m, win)
                rms = np.sqrt((frames ** 2).mean(axis=1))
                audio[: min(n, m)] = rms[: min(n, m)]
        try:
            wav.unlink()
        except OSError:
            pass

    # ---- 画面变化 ----
    motion = np.zeros(n)
    ok, ts = _decode_keyframes(src, raw)
    if ok and ts:
        W, H = 64, 36
        buf = np.fromfile(str(raw), dtype=np.uint8)
        cnt = buf.size // (W * H)
        if cnt >= 2:
            fr = buf[: cnt * W * H].reshape(cnt, H, W).astype(np.float32)
            diff = np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))
            for i, d in enumerate(diff):
                ti = ts[i + 1] if i + 1 < len(ts) else ts[-1]
                b = int(ti / hop)
                if 0 <= b < n:
                    motion[b] = max(motion[b], d)
        try:
            raw.unlink()
        except OSError:
            pass
    elif raw.exists():
        try:
            raw.unlink()
        except OSError:
            pass

    return t, audio, motion


def pick_highlights(
    t, audio, motion, clip_len: float = 8.0, top: int = 10, min_gap: float = 25.0,
    avoid: list[float] | None = None,
) -> list[dict]:
    """把两路信号排名后加权，取局部峰，按最小间隔去重"""
    import numpy as np

    if len(t) == 0:
        return []

    def pct(x):
        if x.max() <= x.min():
            return np.zeros_like(x)
        order = x.argsort().argsort().astype(np.float32)
        return order / max(1, len(x) - 1)

    score = 0.68 * pct(audio) + 0.32 * pct(motion)
    # 按片段长度做滑动平均，避免选出"尖峰一秒"
    k = max(1, int(round(clip_len / max(1e-6, t[1] - t[0]))))
    if k > 1 and len(score) > k:
        kernel = np.ones(k, dtype=np.float32) / k
        score = np.convolve(score, kernel, mode="same")

    order = score.argsort()[::-1]
    picked: list[dict] = []
    # 间隔太长会挑不满（短素材尤其明显），逐级放宽；但不低于 0.6 倍片段长，
    # 否则会把几乎重叠的片段当成两个候选
    gap = float(min_gap)
    floor = max(1.0, clip_len * 0.6)
    while True:
        picked = []
        for idx in order:
            center = float(t[idx])
            start = max(0.0, center - clip_len / 2)
            # 避开已经用过（或用户不想要）的位置，用来实现「换一批」
            if avoid and any(abs(start - x) < clip_len * 0.8 for x in avoid):
                continue
            if any(abs(start - p["起秒"]) < gap for p in picked):
                continue
            picked.append({
                "起秒": start,
                "长": clip_len,
                "分数": float(score[idx]),
                "音频分": float(pct(audio)[idx]),
                "画面分": float(pct(motion)[idx]),
            })
            if len(picked) >= top:
                break
        if len(picked) >= top or gap <= floor:
            break
        gap = max(floor, gap / 2)
    picked.sort(key=lambda x: x["起秒"])
    return picked


def cmd_hot(args) -> int:
    root = Path(args.dir).expanduser().resolve()
    if not root.exists():
        print(f"路径不存在：{root}")
        return 2
    files = iter_videos(root) if root.is_dir() else [root]
    if not files:
        print("没有找到视频文件。")
        return 1

    clip_len = float(args.len)
    base_dir = root if root.is_dir() else root.parent
    work = (Path(args.work) if args.work else base_dir / CACHE_DIR / "hot").expanduser().resolve()
    rows: list[dict] = []

    print(f"分析 {len(files)} 个文件，候选 {args.top} 段/文件，片段长 {clip_len:g}s")
    for i, f in enumerate(files, 1):
        t0 = time.perf_counter()
        print(f"\n[{i}/{len(files)}] {f.name}")
        try:
            t, audio, motion = analyze_motion(f, work)
            m = probe_cached(f, work.parent)
            # 间隔默认按时长自适应：太密会挑出同一场戏的连续切点，失去多样性
            gap = float(args.min_gap) if args.min_gap and args.min_gap > 0 else (
                # 下限不能用固定 15s：12 秒的素材会被这个下限卡死成只能挑 1 段
                min(180.0, max(3.0, m["duration"] / max(1, args.top) * 1.2))
            )
            picks = pick_highlights(t, audio, motion, clip_len, args.top, gap)
        except Exception as e:
            print(f"  失败：{e}")
            continue
        dt = time.perf_counter() - t0
        print(f"  {fmt_sec(m['duration'])} 素材，分析耗时 {dt:.1f}s，"
              f"{m['width']}x{m['height']}，最小间隔 {gap:.0f}s"
              + ("（无音轨，只用画面）" if not m["has_audio"] else ""))
        if args.dry_run:
            for p in picks[:5]:
                print(f"    {fmt_sec(p['起秒'])}  分数 {p['分数']:.2f}")
            continue
        for p in picks:
            rows.append({
                "源": str(f),
                "起": f"{int(p['起秒'] // 3600)}:{int(p['起秒'] % 3600 // 60):02d}:{p['起秒'] % 60:06.3f}",
                "长": round(p["长"], 2),
                "花字": "",
                "分数": round(p["分数"], 3),
                "音频分": round(p["音频分"], 3),
                "画面分": round(p["画面分"], 3),
            })
        # 打印前几名的位置，方便直接去验
        for p in picks[: min(5, len(picks))]:
            print(f"    {fmt_sec(p['起秒']):>8}  {p['分数']:.2f}  "
                  f"音{p['音频分']:.2f} 画{p['画面分']:.2f}")

    if args.dry_run:
        print("\n预演结束。")
        return 0
    if not rows:
        print("没有产生候选。")
        return 1

    out = Path(args.out).expanduser().resolve() if args.out else (
        root / "高光候选.csv" if root.is_dir() else root.with_suffix(".高光候选.csv")
    )
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["源", "起", "长", "花字", "分数", "音频分", "画面分"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\n候选清单：{out}")
    print(f"共 {len(rows)} 段。这份 CSV 可以直接喂给 assemble："
          f"\n  python -m djkit assemble {out.name} -o out/合集.mp4")
    print("提示：分数只代表「声音大 + 画面动得多」，剧情类的静场表演（下跪、对视）抓不到，"
          "必须人工过一遍再挑。")
    return 0
