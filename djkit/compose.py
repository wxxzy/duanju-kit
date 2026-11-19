"""djkit.compose

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import time
import hashlib
from pathlib import Path
from dataclasses import dataclass, field

from .core import CACHE_DIR, ffmpeg_exe, fmt_sec, probe, probe_cached, run
from .media import Clip, clean_title, collect_args
from .subtitle import SubIndex, build_sub_style, prepare_sub_file, slice_range_of, sub_filter_arg
from .title import TitleCfg, build_title_filter, load_title_csv


def prepare_overlay(path: Path, w: int, h: int, fps: int, cache: Path) -> Path:
    """把片头/片尾预先规整成与成片完全一致的规格（含补齐静音轨），只做一次"""
    cache.mkdir(parents=True, exist_ok=True)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        raise SystemExit(f"片头/片尾不存在：{path}")
    key = hashlib.md5(f"{path}|{mtime}|{w}x{h}@{fps}".encode("utf-8")).hexdigest()[:10]
    dst = cache / "overlay" / f"{clean_title(path.stem)}_{key}.mp4"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return dst

    meta = probe(path)
    dur = meta["duration"] or 0
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(path)]
    if not meta["has_audio"]:
        cmd += ["-f", "lavfi", "-t", f"{max(dur, 0.1):.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,fps={fps},setsar=1"
    )
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    cmd += ["-map", "0:v:0"]
    if meta["has_audio"]:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-ar", "48000", "-ac", "2"]
    else:
        cmd += ["-map", "1:a:0", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest"]
    cmd.append(str(dst))
    r = run(cmd, timeout=1800)
    if r.returncode != 0 or not dst.exists():
        raise SystemExit(f"片头/片尾预处理失败：{path.name}\n{(r.stderr or '')[-400:]}")
    return dst


@dataclass
class ComposeOpts:
    """一次编码里完成：改规格 + 烧字幕 + 变速 + BGM 混音 + 拼片头片尾

    统一走 filter_complex，好处是所有能力可以自由组合，且只编码一次。
    """

    w: int
    h: int
    fps: int = 25
    mode: str = "blur"
    crf: int = 20
    sub_index: SubIndex | None = None
    sub_style: str | None = None
    intro: Path | None = None
    outro: Path | None = None
    intro_dur: float = 0.0
    outro_dur: float = 0.0
    cache: Path | None = None
    speed: float = 1.0
    bgm: Path | None = None
    bgm_vol: float = 0.25
    bgm_fade: float = 2.0
    duck: bool = False
    title: TitleCfg | None = None
    title_count: int = 0

    @property
    def speed_on(self) -> bool:
        return abs(self.speed - 1.0) > 1e-6


A_FMT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


def atempo_chain(speed: float) -> str:
    """atempo 单级只接受 0.5~2.0，超出范围要串联多级"""
    s = max(0.1, float(speed))
    parts: list[str] = []
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.6f}")
    return ",".join(parts)


def resolve_sub(c: Clip, opts: ComposeOpts) -> Path | None:
    """定位并准备好该素材要烧的字幕（切片产物会自动平移时间轴）"""
    if opts.sub_index is None:
        return None
    src = opts.sub_index.lookup(c.path)
    if src is None:
        return None
    rng = slice_range_of(c.path)
    if rng:
        # 切片产物：把整集字幕平移 -start 并裁到本段长度
        start, _end = rng
        dur = max(0.2, c.duration)
        out = prepare_sub_file(src, start, dur, opts.cache)
    else:
        out = prepare_sub_file(src, 0.0, 0.0, opts.cache)
    # 平移后若一条不剩，就别拿空字幕去烧（libass 会报错）
    try:
        if out.stat().st_size < 20:
            return None
    except OSError:
        return None
    return out


def compose_cmd(c: Clip, dst: Path, opts: ComposeOpts) -> list[str]:
    """把一条素材组装成最终成片命令（单次编码）"""
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    inputs: list[str] = []
    n_in = 0

    def add(argv: list[str]) -> int:
        nonlocal n_in
        i = n_in
        inputs.extend(argv)
        n_in += 1
        return i

    # 主片段视频链：规格 -> 字幕 -> 变速 -> 统一帧率
    # 字幕必须在 setpts 之前烧，否则文字时间轴会和变速后的画面对不上
    sub = resolve_sub(c, opts)
    vf = [build_filter(opts.mode, opts.w, opts.h)]
    if sub is not None:
        vf.append(sub_filter_arg(sub, opts.sub_style))
    # 花字：时间和变速无关，跟着正片内容走，所以也放在 setpts 之前
    if opts.title is not None:
        txt = opts.title.text_for(c)
        if txt:
            tkey = hashlib.md5(
                f"{txt}|{opts.w}x{opts.h}|{opts.title.style}".encode("utf-8")
            ).hexdigest()[:12]
            vf.append(build_title_filter(txt, opts.w, opts.h, opts.title, opts.cache, tkey))
    if opts.speed_on:
        vf.append(f"setpts=PTS/{opts.speed:.6f}")
    vf.append(f"fps={opts.fps}")
    vf.append("setsar=1")
    main_v = ",".join(vf)

    pad_v = (
        f"scale={opts.w}:{opts.h}:force_original_aspect_ratio=decrease,"
        f"pad={opts.w}:{opts.h}:(ow-iw)/2:(oh-ih)/2:color=black,fps={opts.fps},setsar=1"
    )

    segs: list[tuple[int, int, str]] = []  # (视频输入序号, 音频输入序号, 标签)
    if opts.intro is not None:
        i = add(["-i", str(opts.intro)])
        segs.append((i, i, "intro"))
    main_i = add(["-i", str(c.path)])
    main_a = main_i
    if not c.has_audio:
        # 无音轨时补一条等长静音，保证后面的 concat / 混音结构统一
        main_a = add(
            ["-f", "lavfi", "-t", f"{max(c.duration, 0.1):.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        )
    segs.append((main_i, main_a, "main"))
    if opts.outro is not None:
        i = add(["-i", str(opts.outro)])
        segs.append((i, i, "outro"))

    parts: list[str] = []
    for vi, ai, tag in segs:
        chain = main_v if tag == "main" else pad_v
        parts.append(f"[{vi}:v]{chain}[v_{tag}]")
        af = [A_FMT]
        # 变速只作用于正片，片头片尾保持原速
        if tag == "main" and opts.speed_on:
            af.append(atempo_chain(opts.speed))
        parts.append(f"[{ai}:a]{','.join(af)}[a_{tag}]")

    if len(segs) > 1:
        labels = "".join(f"[v_{t}][a_{t}]" for _, _, t in segs)
        parts.append(f"{labels}concat=n={len(segs)}:v=1:a=1[vcat][acat]")
        vtail, atail = "[vcat]", "[acat]"
    else:
        vtail, atail = "[v_main]", "[a_main]"

    # 成片总时长（变速后的正片 + 片头片尾），供 BGM 截断和淡出定位
    total = (opts.intro_dur or 0.0) + c.duration / max(opts.speed, 0.01) + (opts.outro_dur or 0.0)

    if opts.bgm is not None:
        bi = add(["-stream_loop", "-1", "-t", f"{max(total, 0.5):.2f}", "-i", str(opts.bgm)])
        parts.append(f"[{bi}:a]{A_FMT},volume={opts.bgm_vol:.3f}[bgmv]")
        fade = ""
        if opts.bgm_fade > 0:
            fade = (
                f",afade=t=out:st={max(0.0, total - opts.bgm_fade):.2f}"
                f":d={opts.bgm_fade:.2f}"
            )
        if opts.duck:
            # 人声出现时自动压低 BGM；同一个流要被两处使用，必须先 asplit
            parts.append(f"{atail}asplit[a_voice][a_key]")
            parts.append(
                "[bgmv][a_key]sidechaincompress="
                "threshold=0.05:ratio=6:attack=15:release=300:makeup=1[bgmd]"
            )
            parts.append(
                f"[a_voice][bgmd]amix=inputs=2:duration=first"
                f":dropout_transition=0:normalize=0{fade}[aout]"
            )
        else:
            parts.append(
                f"{atail}[bgmv]amix=inputs=2:duration=first"
                f":dropout_transition=0:normalize=0{fade}[aout]"
            )
        atail = "[aout]"

    cmd += inputs
    cmd += ["-filter_complex", ";".join(parts), "-map", vtail, "-map", atail]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(opts.crf), "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    cmd.append(str(dst))
    return cmd


def build_filter(mode: str, w: int, h: int) -> str:
    if mode == "crop":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if mode == "blur":
        return (
            f"split[bg][fg];"
            f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:3[b];"
            f"[fg]scale={w}:{h}:force_original_aspect_ratio=decrease[f];"
            f"[b][f]overlay=(W-w)/2:(H-h)/2"
        )
    # pad
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


PLATFORM_SPECS = {
    "douyin": (1080, 1920),
    "kuaishou": (1080, 1920),
    "shipinhao": (1080, 1920),
    "xiaohongshu": (1080, 1440),
    "bilibili": (1920, 1080),
}


def parse_specs(spec_arg: str | None, fallback: str) -> list[tuple[int, int]]:
    """解析规格：支持 1080x1920 这类数字，也支持 douyin 这类平台名，自动去重"""
    raw = spec_arg if spec_arg else fallback
    specs: list[tuple[int, int]] = []
    for part in raw.split(","):
        part = part.strip().lower().replace("*", "x")
        if not part:
            continue
        if part in PLATFORM_SPECS:
            spec = PLATFORM_SPECS[part]
        else:
            w, h = part.split("x")
            spec = (int(w), int(h))
        if spec not in specs:
            specs.append(spec)
    return specs


def extract_cover(src: Path, dst: Path, t: float, size: tuple[int, int] | None = None) -> bool:
    """从成片里抽一帧做首图"""
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{max(0.0, t):.3f}", "-i", str(src), "-frames:v", "1"]
    if size:
        w, h = size
        cmd += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"]
    cmd += ["-q:v", "2", str(dst)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = run(cmd, timeout=180)
    return r.returncode == 0 and dst.exists()


def parse_cover_time(cover: str, intro_dur: float) -> float:
    """--cover auto：取片头结束 +1 秒，通常正好是正片第一个有效画面"""
    if str(cover).strip().lower() in ("auto", "yes", "true", ""):
        return (intro_dur or 0.0) + 1.0
    return float(cover)


def cmd_fit(args) -> int:
    root = Path(args.dir).expanduser().resolve()
    outdir = Path(args.out).expanduser().resolve() if args.out else root / "_成片"
    try:
        specs = parse_specs(args.specs, args.size)
    except Exception:
        print("--specs 格式应为 1080x1920,1080x1440,1920x1080 或 douyin,xiaohongshu")
        return 2
    if not specs:
        print("--specs 解析不出任何规格。")
        return 2

    speed = max(0.1, min(10.0, args.speed))
    if abs(speed - 1.0) > 1e-6 and not (0.5 <= speed <= 2.0):
        print(f"⚠ 变速 {speed:g}x 超出单级 atempo 范围，会串联多级，音质有可闻损失")

    cover_sizes: list[tuple[int, int]] = []
    if args.cover_sizes:
        try:
            cover_sizes = parse_specs(args.cover_sizes, args.cover_sizes)
        except Exception:
            print("--cover-sizes 格式应为 720x1280,1280x720")
            return 2

    clips = collect_args(args, root)
    if not clips:
        print("没有找到视频文件。")
        return 1

    cache = outdir / CACHE_DIR

    sub_index = None
    style_by_h: dict[int, str | None] = {}
    if args.burn_sub:
        roots = [Path(p).expanduser().resolve() for p in args.sub_dir] if args.sub_dir else [root]
        sub_index = SubIndex(roots)
        if not sub_index.files:
            print(f"⚠ 在 {'、'.join(str(p) for p in roots)} 里没找到字幕（.srt/.ass/.ssa/.vtt），跳过烧字幕")
            sub_index = None
        else:
            for _w, _h in specs:
                style_by_h[_h] = build_sub_style(args.sub_style, _h, args.sub_size, args.sub_font)

    title_cfg = None
    if args.title or args.title_tpl or args.title_file:
        texts = None
        if args.title_file:
            tf_path = Path(args.title_file).expanduser().resolve()
            if not tf_path.exists():
                print(f"标题表不存在：{tf_path}")
                return 2
            texts = load_title_csv(tf_path)
        title_cfg = TitleCfg(
            style=args.title_style, start=args.title_start, dur=args.title_dur,
            size=args.title_size, pos=args.title_pos, y=args.title_y,
            color=args.title_color, border=args.title_border,
            fade=args.title_fade, font=args.title_font,
            fixed=args.title, tpl=args.title_tpl, texts=texts,
        )
        hit = sum(1 for c in clips if title_cfg.text_for(c))
        print(f"花字 {hit}/{len(clips)} 条" + ("" if hit else "  ⚠ 没有可用的标题文字"))
        if hit == 0:
            title_cfg = None

    intro_src = Path(args.intro).expanduser().resolve() if args.intro else None
    outro_src = Path(args.outro).expanduser().resolve() if args.outro else None
    bgm_src = Path(args.bgm).expanduser().resolve() if args.bgm else None
    if bgm_src and not bgm_src.exists():
        print(f"BGM 不存在：{bgm_src}")
        return 2

    parts = ["规格 " + "、".join(f"{w}x{h}" for w, h in specs) + f"（{args.mode}）", f"{args.fps}fps"]
    if sub_index is not None:
        parts.append(f"字幕库 {len(sub_index.files)} 条")
    if intro_src or outro_src:
        parts.append("＋".join([x for x, y in (("片头", intro_src), ("片尾", outro_src)) if y]))
    if bgm_src:
        parts.append(f"BGM {bgm_src.name}@{args.bgm_vol:g}")
    if abs(speed - 1.0) > 1e-6:
        parts.append(f"变速 {speed:g}x")
    if title_cfg is not None:
        parts.append(f"花字 {title_cfg.style}")
    if args.cover:
        parts.append("出封面")
    print("成片合成：" + "，".join(parts))
    print(f"输出到 {outdir}")

    # 字幕匹配率先摸一遍，别跑完才发现一集都没配上
    if sub_index is not None:
        matched = sum(1 for c in clips if sub_index.lookup(c.path) is not None)
        print(f"字幕匹配 {matched}/{len(clips)}")
        if matched == 0:
            print("⚠ 一条都没匹配上，常见原因：字幕文件名与视频对不上（建议先跑 scan 核对剧名/集数）")

    total_runs = len(specs) * len(clips)
    if len(specs) > 1:
        print(f"共 {len(specs)} 个规格 × {len(clips)} 条 = {total_runs} 次编码")

    if args.dry_run:
        for w, h in specs:
            print(f"\n[{w}x{h}]")
            for c in clips[:5]:
                s = sub_index.lookup(c.path) if sub_index is not None else None
                print(f"  {c.path.name}  {c.res} -> {w}x{h}" + (f"  字幕={s.name}" if s else ""))
            if len(clips) > 5:
                print(f"  … 其余 {len(clips) - 5} 个")
        print("\n预演结束，去掉 --dry-run 开始执行。")
        return 0

    ok = fail = covers = 0
    started = time.perf_counter()
    intro_dur = outro_dur = 0.0

    for si, (w, h) in enumerate(specs, 1):
        out_spec = outdir if len(specs) == 1 else outdir / f"{w}x{h}"
        intro = prepare_overlay(intro_src, w, h, args.fps, cache) if intro_src else None
        outro = prepare_overlay(outro_src, w, h, args.fps, cache) if outro_src else None
        intro_dur = probe_cached(intro, cache)["duration"] if intro else 0.0
        outro_dur = probe_cached(outro, cache)["duration"] if outro else 0.0

        opts = ComposeOpts(
            w=w, h=h, fps=args.fps, mode=args.mode, crf=args.crf,
            sub_index=sub_index, sub_style=style_by_h.get(h),
            intro=intro, outro=outro, intro_dur=intro_dur, outro_dur=outro_dur,
            cache=cache, speed=speed,
            bgm=bgm_src, bgm_vol=args.bgm_vol, bgm_fade=args.bgm_fade, duck=args.duck,
            title=title_cfg,
        )
        if len(specs) > 1:
            print(f"\n[{si}/{len(specs)}] {w}x{h}")

        cover_t = parse_cover_time(args.cover, intro_dur) if args.cover else None

        for n, c in enumerate(clips, 1):
            # 保留输入目录结构，避免不同剧的同名切片互相覆盖
            try:
                sub = c.path.parent.relative_to(root)
            except ValueError:
                sub = Path(".")
            dst = out_spec / sub / c.path.name
            dst.parent.mkdir(parents=True, exist_ok=True)
            fresh = False
            if dst.exists():
                ok += 1
            else:
                print(f"\r  [{n}/{len(clips)}] {dst.name}", end="", flush=True)
                try:
                    r = run(compose_cmd(c, dst, opts), timeout=7200)
                    if r.returncode == 0 and dst.exists():
                        ok += 1
                        fresh = True
                    else:
                        fail += 1
                        print(f"\n  失败 {dst.name}: {(r.stderr or '')[-220:]}")
                        continue
                except Exception as e:
                    fail += 1
                    print(f"\n  异常 {dst.name}: {e}")
                    continue

            if cover_t is not None:
                stem = f"{c.title}_{c.ep:03d}" if c.ep is not None else c.path.stem
                shot = out_spec / "封面" / f"{stem}.jpg"
                if not shot.exists() and extract_cover(dst, shot, cover_t):
                    covers += 1
                for cw, ch in cover_sizes:
                    extra = shot.with_name(f"{stem}_{cw}x{ch}.jpg")
                    if not extra.exists():
                        extract_cover(dst, extra, cover_t, (cw, ch))

    print()
    print(f"完成：成功 {ok}，失败 {fail}。" + (f" 封面 {covers} 张。" if args.cover else ""))
    if intro or outro:
        bits = []
        if intro:
            bits.append(f"片头 {intro_dur:.1f}s")
        if outro:
            bits.append(f"片尾 {outro_dur:.1f}s")
        print("已拼接：" + "，".join(bits))
    if abs(speed - 1.0) > 1e-6:
        print(f"变速：{speed:g}x（只作用于正片，片头片尾保持原速）")
    print(f"耗时 {fmt_sec(time.perf_counter() - started)}")
    return 0 if fail == 0 else 1
