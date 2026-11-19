"""djkit.slicer

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

from pathlib import Path

from .core import CACHE_DIR, ffmpeg_exe, run
from .media import Clip, collect_args
from .subtitle import SubIndex, build_sub_style, prepare_sub_file, sub_filter_arg


def parse_ranges(spec: str) -> list[tuple[float, float]]:
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" not in part:
            raise SystemExit(f"区间格式错误：{part}（应形如 0-15）")
        a, b = part.split("-", 1)
        out.append((float(a), float(b)))
    return out


def build_segments(dur: float, args) -> list[tuple[float, float]]:
    if args.mode == "ranges":
        segs = parse_ranges(args.ranges)
        # 越界裁剪
        res = []
        for a, b in segs:
            if a >= dur:
                continue
            res.append((a, min(b, dur)))
        return res
    if args.mode == "head":
        return [(0.0, min(args.head, dur))] if dur > 0 else []
    # even
    seg = max(1.0, args.seg)
    segs = []
    t = 0.0
    while t < dur - 0.05:
        segs.append((t, min(t + seg, dur)))
        t += seg
    return segs


def slice_one(
    c: Clip,
    segs: list[tuple[float, float]],
    outdir: Path,
    copy_mode: bool,
    crf: int,
    sub_index=None,
    sub_style: str | None = None,
    cache: Path | None = None,
) -> tuple[int, int]:
    ok = fail = 0
    d = outdir / c.title
    d.mkdir(parents=True, exist_ok=True)
    for i, (a, b) in enumerate(segs, 1):
        if b - a < 1.0:
            continue
        name = f"{c.title}_{c.ep_label}_p{i:02d}_{int(a)}s-{int(b)}s.mp4"
        dst = d / name
        if dst.exists():
            ok += 1
            continue
        # 烧字幕：把整集字幕平移 -a 秒后叠在本段上
        vf = None
        if sub_index is not None and cache is not None:
            src = sub_index.lookup(c.path)
            if src is not None:
                seg = prepare_sub_file(src, a, b - a, cache)
                if seg.stat().st_size >= 20:
                    vf = sub_filter_arg(seg, sub_style)
        cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{a:.2f}", "-to", f"{b:.2f}", "-i", str(c.path)]
        if vf:
            cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
        elif copy_mode:
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k"]
        cmd.append(str(dst))
        try:
            r = run(cmd, timeout=1800)
            if r.returncode == 0 and dst.exists():
                ok += 1
            else:
                fail += 1
                print(f"\n  失败 {name}: {(r.stderr or '')[-160:]}")
        except Exception as e:
            fail += 1
            print(f"\n  异常 {name}: {e}")
    return ok, fail


def cmd_slice(args) -> int:
    root = Path(args.dir).expanduser().resolve()
    outdir = Path(args.out).expanduser().resolve() if args.out else root / "_切片"
    clips = collect_args(args, root)
    if not clips:
        print("没有找到视频文件。")
        return 1

    plan = []
    for c in clips:
        segs = build_segments(c.duration, args)
        if segs:
            plan.append((c, segs))
    total_segs = sum(len(s) for _, s in plan)
    print(f"待切片：{len(plan)} 个视频 -> {total_segs} 个片段，输出到 {outdir}")

    sub_index = None
    sub_style = None
    cache = outdir / CACHE_DIR
    if getattr(args, "burn_sub", False):
        roots = [Path(p).expanduser().resolve() for p in args.sub_dir] if args.sub_dir else [root]
        sub_index = SubIndex(roots)
        if not sub_index.files:
            print(f"⚠ 在 {'、'.join(str(r) for r in roots)} 里没找到字幕，跳过烧字幕")
            sub_index = None
        else:
            ref_h = next((c.height for c in clips if c.height), 1920)
            sub_style = build_sub_style(args.sub_style, ref_h, args.sub_size, args.sub_font)
            print(f"烧字幕：字幕库 {len(sub_index.files)} 条，按 {ref_h}px 高度排版")
            if args.copy:
                print("⚠ 烧字幕需要重编码，--copy 已自动失效")

    if args.dry_run:
        for c, segs in plan[:5]:
            s = sub_index.lookup(c.path) if sub_index else None
            tag = f"  字幕={s.name}" if s else ""
            print(f"  {c.path.name}: {len(segs)} 段，如 {segs[0][0]:.0f}s-{segs[0][1]:.0f}s{tag}")
        print("\n预演结束，去掉 --dry-run 开始执行。")
        return 0

    ok = fail = 0
    for n, (c, segs) in enumerate(plan, 1):
        print(f"\r  [{n}/{len(plan)}] {c.title} {c.ep_label}", end="", flush=True)
        a, b = slice_one(c, segs, outdir, args.copy, args.crf,
                         sub_index=sub_index, sub_style=sub_style, cache=cache)
        ok += a
        fail += b
    print(f"\n完成：成功 {ok} 段，失败 {fail} 段。")
    if fail:
        print("提示：失败多为源文件损坏或编码不支持，可先跑 doctor 检查 ffmpeg。")
    return 0 if fail == 0 else 1
