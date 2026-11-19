"""djkit.assemble

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import csv
import time
import hashlib
from pathlib import Path

from .compose import A_FMT, build_filter, extract_cover, parse_cover_time
from .core import CACHE_DIR, abs_path, as_bool, ffmpeg_exe, fmt_sec, probe, probe_cached, rg, run, yaml
from .subtitle import build_sub_style, prepare_sub_file, sub_filter_arg
from .title import TitleCfg, build_title_filter


def tc_to_sec(v) -> float:
    """时间码解析：90 / 1:30 / 00:01:30.5 / 01:02:03 都认"""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("：", ":")
    if not s:
        return 0.0
    if ":" not in s:
        return float(s)
    parts = s.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"时间码格式不对：{v}")


def load_plan(path: Path) -> tuple[dict, list[dict]]:
    """读清单。YAML 支持完整配置，CSV 只给片段行"""
    if path.suffix.lower() in (".csv", ".tsv"):
        segs = []
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                row = [x.strip() for x in row]
                if not row or not row[0] or row[0].lower() in ("源", "source", "文件"):
                    continue
                if len(row) < 2:
                    continue
                segs.append({
                    "源": row[0],
                    "起": row[1] if len(row) > 1 else 0,
                    "长": row[2] if len(row) > 2 and row[2] else None,
                    "花字": row[3] if len(row) > 3 else None,
                })
        return {}, segs

    if yaml is None:
        raise SystemExit("缺少 PyYAML，请先 pip install pyyaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    segs = data.get("片段") or data.get("clips") or []
    if not isinstance(segs, list):
        raise SystemExit("清单里的「片段」必须是列表")
    return data, [s for s in segs if isinstance(s, dict)]


def assemble_cmd(
    segs: list[dict],
    cfg: dict,
    base: Path,
    out: Path,
    cache: Path,
) -> tuple[list[str], float]:
    """把片段清单翻译成一条 ffmpeg 命令，返回 (命令, 预计总时长)"""
    w = int(cfg.get("宽") or 1080)
    h = int(cfg.get("高") or 1920)
    fps = int(cfg.get("帧率", 25))
    mode = cfg.get("缩放", "blur")
    crf = int(cfg.get("CRF", 20))

    exe = ffmpeg_exe()
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"]
    inputs: list[str] = []
    parts: list[str] = []
    labels: list[str] = []
    total = 0.0
    n_in = 0

    # 全局花字（片名/主题）默认占顶部；段内标签让到底部安全区，避免两条叠在一起
    # 也避免压在画面主体上（横屏转竖屏时底部是模糊边）
    gtitle = cfg.get("花字")
    gtext = rg(gtitle, "文字", "text") if isinstance(gtitle, dict) else None
    seg_pos_default = "bottom" if gtext else cfg.get("_title_pos", "bottom")

    def add(argv: list[str]) -> int:
        nonlocal n_in
        i = n_in
        inputs.extend(argv)
        n_in += 1
        return i

    # ---- 预扫描：先算清每段时长，以便为配音让出空间 ----
    seg_info: list[list] = []
    for i, s in enumerate(segs, 1):
        src_s = abs_path(base, rg(s, "源", "source", "文件"))
        src = Path(src_s) if src_s else None
        if not src or not Path(src).exists():
            raise SystemExit(f"第 {i} 段素材不存在：{src}")
        start = tc_to_sec(rg(s, "起", "start", default=0))
        meta = probe_cached(Path(src), cache)
        avail = max(0.1, meta["duration"] - start)
        dur_raw = rg(s, "长", "时长", "dur", "duration")
        dur = min(tc_to_sec(dur_raw), avail) if dur_raw else avail
        seg_info.append([src, start, dur, avail, meta])
    total = sum(x[2] for x in seg_info)

    # 配音比画面长时，把最后一段往后延（用更多原片，而不是冻结最后一帧）
    voice_dur = 0.0
    if rg(cfg, "配音", "voice"):
        vp0 = abs_path(base, rg(cfg, "配音", "voice"))
        if vp0 and Path(vp0).exists():
            voice_dur = probe_cached(Path(vp0), cache)["duration"]
            if voice_dur > total + 0.2:
                extra = voice_dur - total
                src, start, dur, avail, meta = seg_info[-1]
                room = max(0.0, avail - dur)
                extend = min(extra, room)
                if extend > 0.05:
                    seg_info[-1][2] = dur + extend
                    total += extend
                    print(f"  配音 {voice_dur:.1f}s 长于画面，末段自动延长 {extend:.1f}s")
                if voice_dur > total + 0.3:
                    print(f"  ⚠ 仍差 {voice_dur - total:.1f}s：末段源素材不够，"
                          f"建议加片段或把语速调快")

    for i, (src, start, dur, avail, meta) in enumerate(seg_info, 1):
        vi = add(["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)])
        ai = vi
        if not meta["has_audio"]:
            ai = add(["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"])

        vf = [build_filter(mode, w, h)]
        # 段内字幕：整份字幕平移 -start，裁到本段
        sub = rg(s, "字幕", "sub")
        if sub:
            sf = prepare_sub_file(Path(abs_path(base, sub)), start, dur, cache)
            try:
                if sf.stat().st_size >= 20:
                    vf.append(sub_filter_arg(sf, cfg.get("_sub_style")))
            except OSError:
                pass
        # 段内花字：时间从本段 0 开始
        seg_title = rg(s, "花字", "标题", "title")
        if seg_title:
            tcfg = TitleCfg(
                style=cfg.get("_title_style", "bar"),
                start=float(cfg.get("_title_start", 0.2)),
                dur=min(float(cfg.get("_title_dur", 3.0)), dur),
                size=float(cfg.get("_title_size", 88)),
                pos=rg(s, "花字位置", "pos", default=seg_pos_default),
            )
            key = hashlib.md5(f"seg{str(seg_title)}|{w}x{h}".encode("utf-8")).hexdigest()[:12]
            vf.append(build_title_filter(str(seg_title), w, h, tcfg, cache, key))
        vf += [f"fps={fps}", "setsar=1"]

        parts.append(f"[{vi}:v]{','.join(vf)}[v{i}]")
        parts.append(f"[{ai}:a]{A_FMT}[a{i}]")
        labels.append(f"[v{i}][a{i}]")

    if len(segs) > 1:
        parts.append("".join(labels) + f"concat=n={len(segs)}:v=1:a=1[vcat][acat]")
        vtail, atail = "[vcat]", "[acat]"
    else:
        vtail, atail = "[v1]", "[a1]"

    # 全局花字：拼完之后按成片时间轴烧
    gtitle = cfg.get("花字")
    if isinstance(gtitle, dict):
        gtext = rg(gtitle, "文字", "text")
        if gtext:
            tcfg = TitleCfg(
                style=rg(gtitle, "样式", "style", default=cfg.get("_title_style", "bar")),
                start=float(rg(gtitle, "开始", "start", default=0.3)),
                dur=float(rg(gtitle, "时长", "dur", default=3.0)),
                size=float(rg(gtitle, "字号", "size", default=96)),
                pos=rg(gtitle, "位置", "pos", default="top"),
            )
            key = hashlib.md5(f"glb{str(gtext)}|{w}x{h}".encode("utf-8")).hexdigest()[:12]
            parts.append(f"{vtail}{build_title_filter(str(gtext), w, h, tcfg, cache, key)}[vt]")
            vtail = "[vt]"

    # 全局字幕：整条成片时间轴上的一份 SRT（解说片就是按文案生成的），
    # 拼完再烧，所以时间码天然和成片对齐，不需要逐段平移。
    # 花字在它之前烧，字幕压在上面 —— 两者一上一下，不冲突。
    gsub = rg(cfg, "字幕", "sub")
    if gsub:
        sf = Path(abs_path(base, gsub))
        if not sf.is_file():
            raise SystemExit(f"字幕文件不存在：{sf}")
        sf = prepare_sub_file(sf, 0.0, 0.0, cache)
        style = cfg.get("_sub_style") or build_sub_style(
            rg(cfg, "字幕样式", "sub_style", default="default") or "default", h, None, None)
        parts.append(f"{vtail}{sub_filter_arg(sf, style)}[vs]")
        vtail = "[vs]"

    # 原声压低 + 解说人声。解说场景下原声必须让路，否则听不清旁白。
    voice = rg(cfg, "配音", "voice")
    src_vol = rg(cfg, "原声音量", "source_vol")
    if voice and src_vol is None:
        src_vol = 0.12
    if src_vol is not None and abs(float(src_vol) - 1.0) > 1e-6:
        parts.append(f"{atail}volume={float(src_vol):.3f}[asrc]")
        atail = "[asrc]"

    if voice:
        vp = Path(abs_path(base, voice) or "")
        if not vp.exists():
            raise SystemExit(f"配音不存在：{vp}")
        vi = add(["-i", str(vp)])
        vm = probe_cached(vp, cache)
        parts.append(f"[{vi}:a]{A_FMT},apad[avo]")
        parts.append(
            f"{atail}[avo]amix=inputs=2:duration=first"
            f":dropout_transition=0:normalize=0[amix_v]"
        )
        atail = "[amix_v]"
        if vm["duration"] > total + 0.5:
            print(f"  ⚠ 配音 {vm['duration']:.1f}s 长于画面 {total:.1f}s，"
                  f"尾部会被截断；考虑减少片段或加快语速")

    # BGM
    bgm = rg(cfg, "BGM", "bgm")
    if bgm:
        bp = Path(abs_path(base, bgm))
        if not bp.exists():
            raise SystemExit(f"BGM 不存在：{bp}")
        bi = add(["-stream_loop", "-1", "-t", f"{max(total, 0.5):.2f}", "-i", str(bp)])
        vol = float(cfg.get("BGM音量", cfg.get("bgm_vol", 0.25)))
        parts.append(f"[{bi}:a]{A_FMT},volume={vol:.3f}[bgmv]")
        fade = float(cfg.get("BGM淡出", cfg.get("bgm_fade", 2.0)))
        fstr = f",afade=t=out:st={max(0.0, total - fade):.2f}:d={fade:.2f}" if fade > 0 else ""
        if as_bool(rg(cfg, "闪避", "duck")):
            parts.append(f"{atail}asplit[a_voice][a_key]")
            parts.append("[bgmv][a_key]sidechaincompress=threshold=0.05:ratio=6:attack=15:release=300:makeup=1[bgmd]")
            parts.append(f"[a_voice][bgmd]amix=inputs=2:duration=first:dropout_transition=0:normalize=0{fstr}[aout]")
        else:
            parts.append(f"{atail}[bgmv]amix=inputs=2:duration=first:dropout_transition=0:normalize=0{fstr}[aout]")
        atail = "[aout]"

    cmd += inputs
    cmd += ["-filter_complex", ";".join(parts), "-map", vtail, "-map", atail]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    cmd.append(str(out))
    return cmd, total


def cmd_assemble(args) -> int:
    plan = Path(args.plan).expanduser().resolve()
    if not plan.exists():
        print(f"清单不存在：{plan}")
        return 2
    base = plan.parent
    try:
        cfg, segs = load_plan(plan)
    except SystemExit as e:
        print(e)
        return 2
    except Exception as e:
        print(f"清单解析失败：{e}")
        return 2

    if not segs:
        print("清单里没有片段。")
        return 2

    # 命令行参数覆盖清单
    w, h = (int(x) for x in args.size.lower().split("x"))
    cfg.setdefault("宽", w)
    cfg.setdefault("高", h)
    cfg.setdefault("缩放", args.mode)
    cfg.setdefault("帧率", args.fps)
    cfg.setdefault("CRF", args.crf)
    if args.bgm:
        cfg["BGM"] = args.bgm
    cfg["_sub_style"] = build_sub_style(args.sub_style, int(cfg["高"]), None, None)
    cfg["_title_style"] = args.title_style
    cfg["_title_start"] = args.title_start
    cfg["_title_dur"] = args.title_dur
    cfg["_title_size"] = args.title_size
    cfg["_title_pos"] = args.title_pos

    out = Path(abs_path(base, rg(cfg, "输出", "out"))) if rg(cfg, "输出", "out") else None
    if args.out:
        out = Path(args.out).expanduser().resolve()
    if out is None:
        out = plan.with_suffix(".mp4")

    cache = out.parent / CACHE_DIR
    print(f"清单 {plan.name}：{len(segs)} 段 → {out.name}")
    for i, s in enumerate(segs, 1):
        src_s = abs_path(base, rg(s, "源", "source", "文件")) or ""
        st = tc_to_sec(rg(s, "起", "start", default=0))
        du = rg(s, "长", "时长", "dur", "duration")
        tag = rg(s, "花字", "标题", "title") or ""
        print(f"  {i}. {Path(src_s).name}  {fmt_sec(st)} 起" + (f"  {du}s" if du else "  到结尾")
              + (f"  花字「{tag}」" if tag else ""))

    try:
        cmd, total = assemble_cmd(segs, cfg, base, out, cache)
    except SystemExit as e:
        print(e)
        return 2
    except Exception as e:
        print(f"构建命令失败：{e}")
        return 2

    print(f"\n规格 {cfg['宽']}x{cfg['高']}（{cfg['缩放']}），预计时长 {fmt_sec(total)}")
    if args.dry_run:
        print("预演结束，去掉 --dry-run 开始执行。")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    r = run(cmd, timeout=7200)
    if r.returncode != 0 or not out.exists():
        print(f"失败：{(r.stderr or '')[-300:]}")
        return 1

    m = probe(out)
    print(f"完成：{out.name}  {m['width']}x{m['height']}  {m['duration']:.1f}s  "
          f"音频={'有' if m['has_audio'] else '无'}  耗时 {time.perf_counter() - started:.1f}s")

    if args.cover:
        cover_t = parse_cover_time(args.cover, 0.0)
        shot = out.with_suffix(".jpg")
        if extract_cover(out, shot, cover_t):
            print(f"封面：{shot.name}")
    return 0


# ---------- 6. 校验 ----------
    print("\n6) 校验")
    finals = [p for p in sorted(out3.rglob("*.mp4")) if CACHE_DIR not in p.parts]
    targets = {}
    for p in finals:
        targets.setdefault(p.parts[-2] if p.parts[-2].count("x") else "flat", p)
    expect = 2 + 12 / 1.1 + 2
    for spec, p in sorted(targets.items()):
        m = probe(p)
        print(f"   [{spec}] {p.name}: {m['width']}x{m['height']}  {m['duration']:.1f}s  "
              f"音频={'有' if m['has_audio'] else '无'}（期望 {expect:.1f}s = 2+12/1.1+2）")

    covers = [p for p in out3.rglob("*.jpg")]
    if covers:
        c0 = covers[0]
        cm = probe(c0)
        print(f"   封面 {len(covers)} 张，首张 {c0.name} {cm['width']}x{cm['height']}")
    if len(targets) < 2:
        print("   ⚠ 多规格输出异常，期望 2 个规格目录")

    shot_dir = base / "_自检截图"
    shot_dir.mkdir(exist_ok=True)
    target = targets.get("1080x1920") or finals[0]
    sliced = next((p for p in out2.rglob("*.mp4") if CACHE_DIR not in p.parts and "EP001" in p.name), None)
    shots = [
        (target, 0.5, "1_片头"),
        (target, 3.5, "2_花字字幕"),   # 片头2s + 正片约1.5s：顶部花字 + 底部字幕同框
        (target, 13.8, "3_片尾"),
    ]
    if sliced:
        shots.append((sliced, 2.0, "4_slice字幕"))  # 原片 5+2=7s，落在第2条字幕(6-9s)内
    for src_file, t, tag in shots:
        run([exe, "-y", "-hide_banner", "-loglevel", "error", "-ss", str(t),
             "-i", str(src_file), "-frames:v", "1", str(shot_dir / f"{tag}_{t}s.png")])
    print(f"   截图 {len(shots)} 张：{shot_dir}")
    if sliced:
        m = probe(sliced)
        print(f"   切片 {sliced.name}: {m['width']}x{m['height']}  {m['duration']:.1f}s（期望 6.0s）")
    print("\n自检完成。产物在", base)
    return 0
