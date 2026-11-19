"""解说流水线：长片 / 剧集 -> 高光片段 -> 转写 -> 文案 -> 配音 -> 成片

一条配方跑完，对应 README「十三、AI 三件套」里的完整链路。

设计要点：
  * 中间产物（片段/转写/文案/配音）按内容签名缓存在 _工作/<剧集>/ 下，
    重跑或 --watch 时直接复用，不会重复调用 LLM 和 TTS（这两项是花钱的）。
  * 一个素材只做一次 ASR + 文案 + 配音，然后按每个规格各编码一次。
  * 失败带重试：网络抖动不该让整集报废。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

from .asr import cmd_asr
from .assemble import assemble_cmd, tc_to_sec
from .compose import A_FMT, extract_cover, parse_cover_time, parse_specs
from .core import CACHE_DIR, abs_path, as_bool, ffmpeg_exe, fmt_sec, probe_cached, rg, run
from .hot import analyze_motion, pick_highlights
from .media import iter_videos, parse_episode
from .report import cmd_report
from .script import SCRIPT_ANGLES, cmd_script, mark_segments
from .tts import cmd_tts


# 选段算法一改就要动这个值：片段清单按签名缓存，不换签名会一直用旧结果
SEG_VER = "sep2"


def _sig(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def cached_ok(artifact: Path, sig_file: Path, key: str) -> bool:
    """产物存在且签名一致，才算可复用"""
    if not artifact.exists():
        return False
    try:
        return sig_file.read_text(encoding="utf-8").strip() == key
    except OSError:
        return False


def mark(artifact: Path, sig_file: Path, key: str) -> None:
    if artifact.exists():
        try:
            sig_file.write_text(key, encoding="utf-8")
        except OSError:
            pass


def with_retry(fn, label: str, times: int = 2, delay: float = 3.0) -> int:
    """网络类调用重试。LLM 和 TTS 抖一下不该让整集失败。"""
    last = 1
    for attempt in range(times + 1):
        try:
            rc = fn()
            if rc == 0:
                return 0
            last = rc
        except SystemExit as e:
            print(f"    {label} 失败：{e}")
        except Exception as e:  # noqa: BLE001
            print(f"    {label} 异常：{type(e).__name__} {e}")
        if attempt < times:
            wait = delay * (attempt + 1)
            print(f"    {label} 第 {attempt + 1} 次重试（{wait:.0f}s 后）…")
            time.sleep(wait)
    return last


def with_retry_ret(fn, label: str, times: int = 2, delay: float = 3.0):
    """像 with_retry，但把函数的返回值带出来（用于 LLM 这类返回内容的调用）"""
    last: BaseException | None = None
    for attempt in range(times + 1):
        try:
            return fn()
        except BaseException as e:  # noqa: BLE001
            last = e
            print(f"    {label} 失败：{e}")
        if attempt < times:
            time.sleep(delay * (attempt + 1))
    raise last if last else RuntimeError(f"{label} 失败")


def speech_windows(src: Path, cache: Path) -> list[tuple[float, float]] | None:
    """用 VAD 找出有语音的区间。返回 None 表示 VAD 不可用，空列表表示确实没检测到语音。

    为什么必须做这一步：`hot` 挑的是「声音大 + 画面动」，而短剧/影视里声大画面动的
    通常是爆炸、打斗、配乐——恰好没有台词。解说需要的是「有信息量的对话」，
    这两个目标并不一致。实测直接拿 hot 的候选去转写，5 段里一句词都没有。
    """
    try:
        import numpy as np
        from faster_whisper.vad import get_speech_timestamps

        from .asr import extract_audio_16k
    except ImportError:
        return None

    wav = cache / "vad" / f"{_sig(src)}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    fresh = not wav.exists()
    if fresh and not extract_audio_16k(src, wav):
        return []
    try:
        import wave

        with wave.open(str(wav), "rb") as f:
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        audio = pcm.astype(np.float32) / 32768.0
        ts = get_speech_timestamps(audio, sampling_rate=16000)
        return [(d["start"] / 16000, d["end"] / 16000) for d in ts]
    except Exception:
        return None
    finally:
        if fresh:
            try:
                wav.unlink()
            except OSError:
                pass


def snap_to_speech(start: float, seg_len: float, speech: list, dur: float,
                   avoid: list | None = None,
                   lo: float = 0.0, hi: float | None = None,
                   min_gap: float = 0.0) -> tuple[float, float]:
    """把片段起点挪到语音最密集的位置，返回 (起点, 覆盖率)。

    两个必须做的事：
    1. 吸附到语音：一段 12 秒里没有台词，文案就无处可写，模型只能借用下一段内容，
       顺序约束立刻失效（实测「解说在讲马特，画面字幕是本还活着」）。
    2. 互斥避让：几个片段各自找「语音最密处」会全部落在同一处，
       实测段 1 和段 2 变成了同一段画面。所以已占用的区间必须排除。
    """
    if not speech:
        return start, 1.0
    avoid = avoid or []
    hi = dur if hi is None else hi
    # 只在允许的区间内找，否则吸附会把片段挪到窗口外（实测挪到了片头 0:05）
    speech = [(a, b) for a, b in speech if b > lo and a < hi]

    def cov(a: float) -> float:
        lo, hi = a, a + seg_len
        return sum(max(0.0, min(b, hi) - max(x, lo)) for x, b in speech)

    def conflicts(a: float) -> bool:
        lo, hi = a, a + seg_len
        if any(min(hi, y) - max(lo, x) > 2.0 for x, y in avoid):
            return True
        # 只看重叠不够：吸附会把几段全拉到同一处语音密集区。
        # 实测 4 段起点落在 34.2/34.5/34.8/36.1 分钟，整支片子只讲了一场戏。
        return min_gap > 0 and any(abs(a - x) < min_gap for x, _ in avoid)

    cands: list[float] = [start]
    for a, b in speech:
        cands.append(a)
        cands.append(a - seg_len + (b - a))

    best, best_c = None, -1.0
    for cand in cands:
        cand = max(lo, min(hi - seg_len, cand))
        if conflicts(cand):
            continue
        c = cov(cand)
        if c > best_c:
            best, best_c = cand, c
    if best is None:          # 全被占了，退回原位置
        return start, cov(start) / max(seg_len, 1e-6)
    return best, best_c / max(seg_len, 1e-6)


def auto_segments(src: Path, cache: Path, top: int, seg_len: float,
                  voice_only: bool = True, window: float = 0.0,
                  avoid: list[float] | None = None) -> list[dict]:
    """用 hot 的算法挑片段，直接返回 assemble 能吃的结构。

    window > 0 时先把候选限制在**分数最高的连续窗口**里。
    这不是锦上添花：散落的高光点拼起来，转写是一堆无关对白，
    LLM 只能凭空编故事线（实测会写出「他想起七个月前孩子骑车」这种台词里没有的情节）。
    限定在一个完整段落内，转写才有连贯的戏，文案才站得住。
    """
    t0 = time.perf_counter()
    t, audio, motion = analyze_motion(src, cache / "hot")
    meta = probe_cached(src, cache)

    speech = speech_windows(src, cache) if voice_only else []
    if speech:
        import numpy as np

        mask = np.zeros(len(t), dtype=bool)
        for a, b in speech:
            mask[(t >= a) & (t <= b)] = True
        # 没有语音的地方直接压死，让候选落在有台词的位置
        audio = audio * np.where(mask, 1.0, 0.02)
        motion = motion * np.where(mask, 1.0, 0.02)

    win_note = ""
    t_use, a_use, m_use = t, audio, motion
    span = meta["duration"]
    if window and window > seg_len * 2 and len(t) > 4:
        import numpy as np

        def pct(x):
            if x.max() <= x.min():
                return np.zeros_like(x)
            return x.argsort().argsort().astype(np.float32) / max(1, len(x) - 1)

        score = 0.68 * pct(audio) + 0.32 * pct(motion)
        hop = float(t[1] - t[0]) if len(t) > 1 else 0.5
        k = max(2, int(window / max(hop, 1e-6)))
        if len(score) > k:
            rolling = np.convolve(score, np.ones(k, dtype=np.float32) / k, mode="valid")
            best = int(rolling.argmax())
            a0 = float(t[best])
            i0 = max(0, int(a0 / max(hop, 1e-6)))
            i1 = min(len(t), int((a0 + window) / max(hop, 1e-6)) + 1)
            if i1 - i0 > 8:
                # 关键：切片而不是乘掩码。掩码会让窗口外变成一片 0，
                # 而分位排名会给这些并列 0 随机名次，越界片段就混进来了（实测选出过窗口外 6 分钟的片段）
                t_use, a_use, m_use = t[i0:i1], audio[i0:i1], motion[i0:i1]
                span = window
                win_note = f"，锁定 {fmt_sec(t_use[0])}–{fmt_sec(t_use[-1])} 这一段"

    # 间隔按时长自适应。下限不能用固定 15s，否则 12 秒素材只能挑出 1 段
    gap = min(180.0, max(3.0, span / max(1, top) * 1.2))
    picks = pick_highlights(t_use, a_use, m_use, seg_len, top, gap, avoid=avoid)
    dt = time.perf_counter() - t0

    if speech is None:
        note = "，VAD 不可用（未做语音门控）"
    elif not speech:
        note = "，未检测到语音（纯音乐/音效）"
    else:
        pct_v = sum(b - a for a, b in speech) / max(meta["duration"], 1) * 100
        note = f"，语音占 {pct_v:.0f}%"
    print(f"  高光检测：{fmt_sec(meta['duration'])} 素材 → {len(picks)} 段（{dt:.0f}s）{note}{win_note}")

    segs = []
    weak = 0
    # 被排除的位置也占坑，免得吸附又把这些片段拉回去
    taken: list[tuple[float, float]] = [
        (float(a), float(a) + seg_len) for a in (avoid or [])
    ]
    win_lo = float(t_use[0]) if window else 0.0
    win_hi = float(t_use[-1]) if window else float(meta["duration"])
    for p in picks:
        st = float(p["起秒"])
        if speech:
            st2, covv = snap_to_speech(st, seg_len, speech, meta["duration"], taken,
                                       lo=win_lo, hi=win_hi,
                                       min_gap=max(seg_len, gap * 0.6))
            if covv < 0.35:
                weak += 1
            st = st2
            p["_cov"] = round(covv, 2)
        taken.append((st, st + seg_len))
        segs.append({
            "源": str(src),
            "起": f"{int(st // 3600)}:{int(st % 3600 // 60):02d}:{st % 60:06.3f}",
            "长": round(float(p["长"]), 2),
            "_start": st,
            "_score": round(float(p["分数"]), 3),
        })
    # 吸附会改变起点，必须按新位置重新排序：否则画面顺序会在时间上来回跳，
    # 而台词分段是按片段顺序标的，顺序一乱，文案讲的顺序就和画面顺序对不上了
    segs.sort(key=lambda x: x["_start"])
    if speech and weak:
        print(f"  ⚠ 其中 {weak} 段语音偏少（<35%），文案可能接不上 —— 减少段数或换个窗口再试")
    return segs


def build_sample_audio(segs: list[dict], dst: Path, cache: Path) -> bool:
    """把选中片段的音轨拼成一条，只转写这些内容。

    为什么不转写整集：42 分钟全量转写要几分钟，而文案只需要屏幕上这几段说了什么。
    """
    cmd = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error"]
    parts, labels = [], []
    n_in = 0
    for i, s in enumerate(segs):
        src = Path(s["源"])
        start = s.get("_start", tc_to_sec(s["起"]))
        dur = float(s["长"])
        meta = probe_cached(src, cache)
        if not meta["has_audio"]:
            cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        else:
            cmd += ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
        parts.append(f"[{n_in}:a]{A_FMT}[a{i}]")
        labels.append(f"[a{i}]")
        n_in += 1
    if n_in == 0:
        return False
    parts.append("".join(labels) + f"concat=n={n_in}:v=0:a=1[aout]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[aout]",
            "-ar", "16000", "-ac", "1", str(dst)]
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = run(cmd, timeout=1800)
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 1000


def trim_to_voice(segs: list[dict], voice_dur: float, tail: float = 1.2) -> list[dict]:
    """按配音长度裁剪画面。

    解说片的画面应该跟着解说走。配音 20 秒配 35 秒画面，后面十几秒等于无声放片，
    观众直接划走。这里只从尾部裁，因为最后一个片段通常和钩子关系最弱。
    """
    budget = voice_dur + tail
    out: list[dict] = []
    acc = 0.0
    for s in segs:
        d = float(s["长"])
        if acc + d <= budget + 0.3:
            out.append(s)
            acc += d
            continue
        remain = budget - acc
        if remain >= 3.0:
            s2 = dict(s)
            s2["长"] = round(remain, 2)
            out.append(s2)
        break
    return out or segs[:1]


def _fill_tpl(tpl: str, src: Path) -> str:
    title, ep = parse_episode(src)
    return (tpl.replace("{剧名}", title)
               .replace("{集}", f"EP{ep:03d}" if ep is not None else src.stem)
               .replace("{集数}", str(ep) if ep is not None else "")
               .replace("{文件}", src.stem))


def _spec_list(v) -> list[tuple[int, int]]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        raw = ",".join(str(x) for x in v)
    else:
        raw = str(v)
    return parse_specs(raw, "1080x1920")


def run_explainer(job: dict, base: Path, out_root: Path, workers: int, dry_run: bool,
                  should_stop=None) -> int:
    """配方里的「模式: 解说」走这条路径"""
    ex = job.get("解说") if isinstance(job.get("解说"), dict) else {}
    src_arg = abs_path(base, rg(job, "素材", "输入", "src", "input"))
    if not src_arg or not Path(src_arg).exists():
        print(f"✗ 素材不存在：{src_arg}")
        return 2
    src_path = Path(src_arg)
    files = iter_videos(src_path) if src_path.is_dir() else [src_path]
    if not files:
        print("没有找到视频文件。")
        return 1

    specs = _spec_list(rg(job, "规格", "specs", "尺寸", "size")) or [(1080, 1920)]
    fps = int(rg(job, "帧率", "fps", default=25))
    mode = rg(job, "缩放", "mode", default="blur")
    crf = int(rg(job, "CRF", "crf", default=20))

    top = int(rg(ex, "段数", "top", default=4))
    seg_len = float(rg(ex, "段长", "len", default=12))
    window = float(rg(ex, "窗口", "window", default=150) or 0)
    per_seg = as_bool(rg(ex, "按段", "aligned"), True)
    lang = str(rg(ex, "语言", "lang", default=""))
    model = str(rg(ex, "字幕模型", "asr_model", default="small"))
    target = rg(ex, "解说时长", "seconds")
    variants = int(rg(ex, "文案版本", "variants", default=1))
    style = str(rg(ex, "风格", "style", default="克制的叙述，不煽情，偶尔一句冷判断"))
    context = str(rg(ex, "背景", "context", default=""))
    bible_opt = str(rg(ex, "总纲", "bible", default="自动")).strip()
    voice = str(rg(ex, "音色", "voice", default="zh-CN-YunxiNeural"))
    rate = str(rg(ex, "语速", "rate", default="+0%"))
    src_vol = float(rg(ex, "原声音量", "source_vol", default=0.12))
    do_asr = as_bool(rg(ex, "转写", "asr"), True)
    force = as_bool(rg(ex, "重跑", "force"), False)

    # 剧集总纲：路径在素材目录、上一级或输出目录里任一存在就自动带上。
    # 它的作用是跨集一致——同一个角色十集叫同一个名字，不用每集重写背景。
    bible_path: Path | None = None
    if bible_opt.lower() not in ("关", "关闭", "false", "0", "off", "none", ""):
        if bible_opt.lower() in ("自动", "auto", "true", "1", "是"):
            base_dir = src_path if src_path.is_dir() else src_path.parent
            for c in (base_dir / "总纲.md", base_dir.parent / "总纲.md",
                      out_root / "总纲.md"):
                if c.exists():
                    bible_path = c
                    break
        else:
            c = abs_path(base, bible_opt)
            if c and Path(c).exists():
                bible_path = Path(c)
            else:
                print(f"⚠ 总纲文件不存在：{bible_opt}")
    if bible_path:
        print(f"剧集总纲：{bible_path}")

    multi = len(specs) > 1
    print(f"解说模式：{len(files)} 个素材，每个挑 {top} 段 × {seg_len:g}s，"
          f"输出 {len(specs)} 档规格（" + "、".join(f"{w}x{h}" for w, h in specs) + "）")

    ok = fail = reused = 0

    for i, f in enumerate(files, 1):
        # 界面点取消时，在集与集之间退出（正在跑的这一集不打断）
        if should_stop and should_stop():
            print(f"\n已取消（完成 {i - 1}/{len(files)} 集）")
            return 2
        print(f"\n[{i}/{len(files)}] {f.name}")
        work = out_root / "_工作" / f.stem
        work.mkdir(parents=True, exist_ok=True)
        cache = out_root / CACHE_DIR

        # ---- 快路径：所有规格的成片都在，整集跳过 ----
        targets = []
        for w, h in specs:
            d = out_root / f"{w}x{h}" if multi else out_root
            targets.append((w, h, d / f"{f.stem}_解说.mp4"))
        if not force and all(t[2].exists() for t in targets):
            print(f"  已存在 {len(targets)} 档成片，跳过（要重做在配方里写 重跑: true）")
            reused += 1
            continue

        # ---- 1. 片段（带缓存：重跑不必再做 40 秒的高光检测）----
        seg_key = _sig(f, int(f.stat().st_mtime), top, seg_len, window, SEG_VER)
        seg_json = work / "片段.json"
        segs = None
        if not force and seg_json.exists():
            try:
                d = json.loads(seg_json.read_text(encoding="utf-8"))
                if d.get("_key") == seg_key:
                    segs = d["data"]
                    print(f"  复用片段清单（{len(segs)} 段）")
            except Exception:
                segs = None
        if segs is None:
            explicit = job.get("片段")
            if isinstance(explicit, list) and explicit and len(files) == 1:
                segs = []
                for s in explicit:
                    if not isinstance(s, dict):
                        continue
                    ss = abs_path(base, rg(s, "源", "source", "文件")) or str(f)
                    segs.append({
                        "源": ss,
                        "起": str(rg(s, "起", "start", default=0)),
                        "长": float(rg(s, "长", "时长", "dur", default=seg_len)),
                        "_start": tc_to_sec(rg(s, "起", "start", default=0)),
                    })
                print(f"  使用配方里指定的 {len(segs)} 段")
            else:
                segs = auto_segments(f, cache, top, seg_len, window=window)
            if segs:
                seg_json.write_text(
                    json.dumps({"_key": seg_key, "data": segs}, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
        if not segs:
            print("  没有可用片段，跳过")
            fail += 1
            continue
        total = sum(float(s["长"]) for s in segs)
        print(f"  片段合计 {fmt_sec(total)}")

        # 本集用的背景 = 总纲（截到本集之前）+ 配方里手写的背景
        file_ctx = context
        if bible_path:
            from .series import load_context

            bctx = load_context(bible_path, parse_episode(f)[1])
            if bctx:
                file_ctx = (bctx + "\n\n" + context).strip() if context else bctx
                print(f"  注入总纲 {len(bctx)} 字")

        if dry_run:
            for s in segs:
                print(f"    {fmt_sec(s.get('_start', 0))} 起  {s['长']}s")
            ok += 1
            continue

        # ---- 2. 转写（只转这几段，带缓存）----
        script_file = work / "文案" / "文案_A.txt"
        txt = work / "转写.txt"
        # 转写签名必须含片段：片段一变，拼出来的采样音频就变了，
        # 只按片源+参数签名会拿旧转写去配新画面
        asr_key = _sig(seg_key, model, lang)

        # 已经有台词本就直接用，跳过 ASR（也用于离线测试）
        lines_src = abs_path(base, rg(ex, "台词", "transcript"))
        if do_asr and lines_src and Path(lines_src).exists():
            txt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(lines_src, txt)
            mark(txt, work / "转写.sig", asr_key)
            print("  使用指定台词，跳过转写")

        if do_asr and not (not force and cached_ok(txt, work / "转写.sig", asr_key)):
            sample = work / "采样.m4a"
            if not build_sample_audio(segs, sample, cache):
                print("  采样音频生成失败")
            else:
                with_retry(lambda: cmd_asr(argparse.Namespace(
                    input=str(sample), out=str(work / "转写.srt"), model=model,
                    lang=lang, device="cpu", compute="int8", beam=5, vad=True,
                )), "转写")
                mark(txt, work / "转写.sig", asr_key)
        elif do_asr:
            print("  复用已有转写")

        secs = float(target) if target else total
        marked = work / "转写_分段.txt"
        if txt.exists():
            mark_segments(txt, [float(x["长"]) for x in segs], marked)

        voice_f = work / "配音.mp3"
        cfg_sub: dict = {}          # 只有逐段分支会填，连续旁白那条分支不烧字幕

        if per_seg and marked.exists():
            # ============ B 方案：逐段写 + 逐段配音 ============
            seg_script = work / "按段文案.json"
            script_key = _sig(asr_key, secs, style, seg_key, len(segs), file_ctx)
            data = None
            if not force and seg_script.exists():
                try:
                    d = json.loads(seg_script.read_text(encoding="utf-8"))
                    if d.get("_key") == script_key:
                        data = d["data"]
                        print(f"  复用按段文案（{len(data)} 段）")
                except Exception:
                    data = None
            if data is None:
                from .align import script_by_segment
                from .script import load_llm_config

                cfg_llm = load_llm_config(None)
                best = None
                for k in range(max(1, variants)):
                    angle = SCRIPT_ANGLES[k % len(SCRIPT_ANGLES)]
                    try:
                        got = with_retry_ret(
                            lambda a=angle: script_by_segment(
                                cfg_llm, marked, len(segs), secs, style, a,
                                context=file_ctx),
                            "逐段写文案")
                    except Exception as e:  # noqa: BLE001
                        print(f"    第 {k + 1} 版失败：{e}")
                        continue
                    got = {x["i"]: x["lines"] for x in got}
                    if best is None or len(got) > len(best):
                        best = got
                    if k == 0:
                        break
                if not best:
                    print("  ⚠ 逐段文案生成失败")
                    fail += 1
                    continue
                data = [{"i": i, "lines": best.get(i, [])} for i in range(1, len(segs) + 1)]
                seg_script.write_text(
                    json.dumps({"_key": script_key, "data": data}, ensure_ascii=False, indent=1),
                    encoding="utf-8")

            # 没有文案的段落直接丢掉：宁可不放这段画面，也不要无声空镜
            keep = [(s, d) for s, d in zip(segs, data) if d.get("lines")]
            if not keep:
                print("  ⚠ 所有段落都没写出文案")
                fail += 1
                continue
            if len(keep) < len(segs):
                print(f"  {len(segs) - len(keep)} 段没有文案，已从成片里去掉")
            segs = [s for s, _ in keep]
            data = [d for _, d in keep]

            from .align import build_aligned_voice, fit_segments, synth_one

            aligned = work / "配音_对齐.m4a"      # AAC 别写进 .mp3，容器会不匹配
            audios = [work / f"_seg{i:02d}.mp3" for i in range(len(data))]
            failed = False
            for i, a in enumerate(audios):
                if force or not a.exists():
                    if not synth_one("\n".join(data[i]["lines"]), a, voice, rate):
                        print(f"  第 {i + 1} 段配音失败")
                        failed = True
            if failed:
                fail += 1
                continue

            # 关键：每次都按音频实际时长重算片段长度。
            # 缓存的片段清单里存的是源段长（12s），不复算就会 48s 画面配 29s 配音。
            segs, fit_targets = fit_segments(segs, [d["lines"] for d in data], audios)

            voice_key = _sig(script_key, voice, rate, [len(d["lines"]) for d in data])
            need_build = force or not cached_ok(aligned, work / "配音_对齐.sig", voice_key)
            if not need_build:
                try:
                    if abs(probe(aligned)["duration"] - sum(fit_targets)) > 0.4:
                        need_build = True
                except Exception:
                    need_build = True
            if need_build:
                if not build_aligned_voice(audios, fit_targets, aligned, work):
                    print("  对齐配音生成失败")
                    fail += 1
                    continue
                mark(aligned, work / "配音_对齐.sig", voice_key)
                print(f"  逐段配音对齐：{len(segs)} 段 → {fmt_sec(sum(fit_targets))}")
            else:
                print(f"  复用已对齐配音（{len(segs)} 段 / {fmt_sec(sum(fit_targets))}）")
            voice_f = aligned

            # 字幕：和单集界面同一条路 —— 文案已经有时间信息（每段画面多长），
            # 直接排 SRT，不用再拿配音去跑一次 ASR。
            if rg(job, "字幕", "burn_sub", default=None) is not None:
                do_sub = as_bool(rg(job, "字幕", "burn_sub"), True)
            else:
                do_sub = False          # 配方没写就不烧，别改老配方的产出
            if do_sub:
                from .subtitle import build_script_srt

                sub_style = str(rg(job, "字幕样式", "sub_style", default="default"))
                srt_f, n_cue = build_script_srt(data, fit_targets,
                                                work / "字幕.srt", width=16)
                cfg_sub = {"字幕": str(srt_f), "字幕样式": sub_style}
                print(f"  字幕 {n_cue} 条（{sub_style}）")
            else:
                cfg_sub = {}

            # 供人工查看的纯文本
            flat = []
            for d in data:
                flat.extend(d["lines"])
            script_file.parent.mkdir(parents=True, exist_ok=True)
            script_file.write_text("\n".join(flat) + "\n", encoding="utf-8")

        else:
            # ============ 原方案：一条连续旁白 ============
            script_key = _sig(asr_key, secs, style, variants, file_ctx, seg_key)
            if do_asr and txt.exists():
                if force or not cached_ok(script_file, work / "文案.sig", script_key):
                    with_retry(lambda: cmd_script(argparse.Namespace(
                        transcript=str(marked), out=str(work / "文案"), seconds=secs,
                        style=style, variants=variants, start="", end="",
                        context=file_ctx or f"《{parse_episode(f)[0]}》片段", config=None,
                        dry_run=False,
                    )), "写文案")
                    mark(script_file, work / "文案.sig", script_key)
                else:
                    print("  复用已有文案")
            if not script_file.exists():
                print("  ⚠ 没有文案，跳过这一集")
                fail += 1
                continue

            try:
                st = script_file.stat()
                script_fp = f"{st.st_size}:{int(st.st_mtime)}"
            except OSError:
                script_fp = "missing"
            voice_key = _sig(script_fp, voice, rate)
            if force or not cached_ok(voice_f, work / "配音.sig", voice_key):
                with_retry(lambda: cmd_tts(argparse.Namespace(
                    input=str(script_file), out=str(voice_f), voice=voice,
                    rate=rate, volume="+0%", dry_run=False,
                )), "配音")
                mark(voice_f, work / "配音.sig", voice_key)
            else:
                print("  复用已有配音")
            if not voice_f.exists():
                print("  配音缺失，跳过")
                fail += 1
                continue

            vm = probe_cached(voice_f, cache)
            segs = trim_to_voice(segs, vm["duration"])
            new_total = sum(float(s["长"]) for s in segs)
            if abs(new_total - total) > 0.5:
                print(f"  配音 {vm['duration']:.1f}s，画面从 {fmt_sec(total)} 收到 {fmt_sec(new_total)}")

        # ---- 6. 逐规格出片 ----
        gtitle = job.get("花字")
        gcfg = None
        if isinstance(gtitle, dict):
            txt_t = rg(gtitle, "文字", "text") or rg(gtitle, "模板", "tpl")
            if txt_t:
                gcfg = {
                    "文字": _fill_tpl(str(txt_t), f),
                    "样式": rg(gtitle, "样式", "style", default="bar"),
                    "开始": float(rg(gtitle, "开始", "start", default=0.3)),
                    "时长": float(rg(gtitle, "时长", "dur", default=3.0)),
                    "字号": float(rg(gtitle, "字号", "size", default=96)),
                    "位置": rg(gtitle, "位置", "pos", default="top"),
                }

        ep_ok = 0
        for w, h, final in targets:
            if final.exists():
                print(f"  {w}x{h} 已存在，跳过")
                ep_ok += 1
                continue
            cfg = {
                "宽": w, "高": h, "缩放": mode, "帧率": fps, "CRF": crf,
                "配音": str(voice_f), "原声音量": src_vol,
                "_sub_style": None,
                "_title_style": "bar", "_title_start": 0.2, "_title_dur": 3.0,
                "_title_size": 88, "_title_pos": "bottom",
            }
            cfg.update(cfg_sub)
            if gcfg:
                cfg["花字"] = gcfg
            bgm = rg(job, "BGM", "bgm")
            if bgm:
                cfg["BGM"] = abs_path(base, bgm)
                cfg["BGM音量"] = float(rg(job, "BGM音量", "bgm_vol", default=0.2))
                cfg["闪避"] = as_bool(rg(job, "闪避", "duck"), True)

            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                cmd, _ = assemble_cmd(segs, cfg, base, final, cache)
            except SystemExit as e:
                print(f"  {w}x{h} 参数错误：{e}")
                continue
            r = run(cmd, timeout=7200)
            if r.returncode != 0 or not final.exists():
                print(f"  {w}x{h} 失败：{(r.stderr or '')[-160:]}")
                continue
            m = probe_cached(final, cache)
            print(f"  ✓ {final.name}  {m['width']}x{m['height']}  {m['duration']:.1f}s")
            ep_ok += 1

            cover = rg(job, "封面", "cover")
            if cover is not None and str(cover).lower() not in ("false", "no", "0", "否"):
                shot = final.with_suffix(".jpg")
                if extract_cover(final, shot, parse_cover_time(str(cover), 0.0)):
                    print(f"    封面 {shot.name}")

        if ep_ok == len(targets):
            ok += 1
        else:
            fail += 1

    print(f"\n解说成片：成功 {ok}，失败 {fail}"
          + (f"，跳过 {reused}（已存在）" if reused else "") + f"。输出在 {out_root}")

    # ---- 台账 ----
    rep = rg(job, "台账", "report", default=True)
    if dry_run:
        return 0 if fail == 0 else 1
    if isinstance(rep, str):
        do_rep = rep.strip().lower() not in ("false", "no", "0", "否", "关")
        rep_out = abs_path(base, rep) if do_rep else None
    else:
        do_rep = as_bool(rep, True)
        rep_out = None
    if do_rep:
        report_dir = out_root / f"{specs[0][0]}x{specs[0][1]}" if multi else out_root
        cmd_report(argparse.Namespace(
            dir=str(report_dir),
            out=rep_out or str(out_root / "_投放台账.xlsx"),
            jobs=workers, no_cache=False,
        ))
    return 0 if fail == 0 else 1
