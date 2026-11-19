"""djkit.gui_server — 本地 Web 界面后端

设计取舍：
  * 不用 FastAPI/Flask，标准库 ThreadingHTTPServer 够用，少一层依赖
  * 视频必须支持 Range 响应，否则浏览器拖不动进度条
  * 长任务跑在单个工作线程里，前端轮询 /api/state —— 比 SSE 简单，断线也不会丢状态
  * 只监听 127.0.0.1，天然只给本机用
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .assemble import assemble_cmd, tc_to_sec
from .core import CACHE_DIR, ffmpeg_exe, fmt_sec, probe, probe_cached, rg, run
from .explainer import auto_segments, build_sample_audio, trim_to_voice
from .media import parse_episode
from .align import build_aligned_voice, fit_segments, script_by_segment, synth_one
from .script import (SCRIPT_ANGLES, SCRIPT_ANGLE_NAMES, cmd_script,
                     load_llm_config, mark_segments)
from .tts import cmd_tts

HERE = Path(__file__).parent
WEB = HERE / "web"


def app_root() -> Path:
    """产物根目录。打包成 exe 后 __file__ 在临时解包目录里，
    产物必须落在 exe 旁边，否则用户根本找不到 gui_out。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()


OUT_ROOT = app_root() / "gui_out"

LOCK = threading.Lock()
STATE: dict = {
    "stage": "idle",          # idle | analyzing | segments | scripting | scripts | rendering | done | error
    "busy": False,
    "pct": 0,
    "msg": "",
    "error": "",
    "src": "",
    "title": "",
    "ep": None,
    "duration": 0.0,
    "params": {"top": 4, "len": 12, "window": 150},
    "segments": [],
    "seg_scripts": [],      # [{i, lines}] 当前选中的那一版逐段文案
    "seg_ids": [],          # 生成文案时的片段序号，改选后要求重生成
    "variants": [],         # 三版候选 [{key, name, chars, full, score, grade, best, score_parts, reasons, seg_scripts}]
    "variant": "",          # 当前选中的版本 key（A/B/C）
    "scripts": [],
    "chosen": "",
    "output": "",
    "outputs": [],          # 多规格出片的所有文件 [{path, spec, size_mb}]
    "transcript": "",
    "cover": "",            # 单集封面的绝对路径
    "batch": {},            # 批量结果 {count, dir}：run_task 结束会把 msg 覆盖成「完成，用时」，所以单独存
    "cache": {},            # 缓存占用明细，由 /api/cache 填
    "log": [],
}


def set_state(**kw) -> None:
    with LOCK:
        STATE.update(kw)


def log(msg: str) -> None:
    with LOCK:
        STATE["log"] = (STATE["log"] + [f"{time.strftime('%H:%M:%S')}  {msg}"])[-120:]
        STATE["msg"] = msg


def snapshot() -> dict:
    with LOCK:
        return json.loads(json.dumps(STATE, ensure_ascii=False, default=str))


class TaskCancelled(Exception):
    """用户点了取消。不当作错误处理。"""


CANCEL = threading.Event()


def check_cancel() -> None:
    """在耗时步骤之间调用。取消只在步骤边界生效——ffmpeg 一旦开始就得等它跑完。"""
    if CANCEL.is_set():
        raise TaskCancelled("已取消")


def request_cancel() -> bool:
    with LOCK:
        if not STATE["busy"]:
            return False
    CANCEL.set()
    log("收到取消请求，等当前步骤结束后停止")
    return True


def run_task(fn) -> None:
    """单工作线程：同一时间只跑一个任务，避免 ffmpeg/模型并发抢资源"""
    def wrapper() -> None:
        CANCEL.clear()
        set_state(busy=True, error="", pct=0)
        t0 = time.perf_counter()
        try:
            fn()
            log(f"完成，用时 {time.perf_counter() - t0:.0f}s")
        except TaskCancelled:
            set_state(stage="idle", pct=0, msg="已取消", error="")
            log("已取消")
        except BaseException as e:  # noqa: BLE001
            # 必须捕获 BaseException：djkit 内部用 raise SystemExit(...) 报错，
            # 它不是 Exception 的子类，漏掉就会让界面永久卡在「进行中」
            if not isinstance(e, Exception):
                traceback.print_exc()
            set_state(stage="error", error=f"{type(e).__name__}: {e}")
            log(f"失败：{e}")
        finally:
            CANCEL.clear()
            set_state(busy=False)

    with LOCK:
        if STATE["busy"]:
            raise RuntimeError("已有任务在跑")
        STATE["busy"] = True
    threading.Thread(target=wrapper, daemon=True).start()


THUMB_DIR = OUT_ROOT / "_thumbs"
LAST = OUT_ROOT / "_last.json"

# 重开窗口要恢复哪些字段。白名单而不是黑名单：
# log/_bible_cache 这类内部字段不该落盘，以后加字段也不会误存。
KEEP = ("stage", "src", "title", "ep", "duration", "params", "segments",
        "seg_scripts", "seg_ids", "variants", "variant", "transcript",
        "output", "outputs", "cover", "batch", "chosen")


STABLE = ("idle", "segments", "scripts", "done", "error")


def save_state() -> None:
    """把界面能恢复的字段落盘。写盘失败不该让任务失败，所以全吞。"""
    try:
        with LOCK:
            data = {k: STATE.get(k) for k in KEEP}
        # 跑在中途（analyzing/rendering…）时被调用，stage 别落盘成中间态，
        # 否则重开窗口会恢复出一个「卡在渲染中」的界面。沿用上次的稳定态。
        if data.get("stage") not in STABLE:
            try:
                data["stage"] = json.loads(
                    LAST.read_text(encoding="utf-8")).get("stage", "idle")
            except BaseException:  # noqa: BLE001
                data["stage"] = "idle"
        data["_saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = LAST.with_name(LAST.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(LAST)          # 原子替换：写到一半断电不会留下半个 JSON
    except BaseException as e:  # noqa: BLE001
        log(f"进度保存失败（不影响任务）：{e}")


def load_state() -> str:
    """启动时恢复上次进度。返回一句提示，空串表示没有可恢复的。"""
    if not LAST.exists():
        return ""
    try:
        data = json.loads(LAST.read_text(encoding="utf-8"))
    except BaseException as e:  # noqa: BLE001
        log(f"上次的进度文件读不了（已忽略）：{e}")
        return ""

    src = str(data.get("src") or "")
    if src and not Path(src).exists():
        return f"上次的素材不在了（{Path(src).name}），进度没恢复"
    stage = data.get("stage")
    if stage not in ("segments", "scripts", "done"):
        return ""

    with LOCK:
        for k in KEEP:
            if k in data:
                STATE[k] = data[k]
        STATE["busy"] = False
        STATE["pct"] = 100
        STATE["error"] = ""

    # 成片文件被手工删了 / 换了机器，就退回片段页，别让人对着一个空播放器
    if stage == "done":
        out = str(STATE.get("output") or "")
        if out and not Path(out).exists():
            stage = "scripts" if STATE.get("seg_scripts") else "segments"
            with LOCK:
                STATE.update({"stage": stage, "output": "", "outputs": [],
                              "cover": "", "chosen": ""})

    tail = {"segments": f"{len(STATE['segments'])} 个片段",
            "scripts": f"{len(STATE.get('variants') or [])} 版文案",
            "done": f"成片 {Path(str(STATE.get('output'))).name}"}[stage]
    when = str(data.get("_saved") or "")[:16]
    log(f"已恢复上次进度：{tail}")      # log 会把 msg 写掉，所以先写日志再定稿提示
    note = f"已恢复上次进度（{when}）：{tail}"
    with LOCK:
        STATE["msg"] = note
    return note



def clean_path(raw) -> Path:
    """容忍从资源管理器「复制文件地址」带来的引号和空格"""
    s = str(raw or "").strip()
    for q in ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019"):
        s = s.strip(q)
    s = os.path.expandvars(s.strip())
    return Path(s).expanduser()


def probe_path(raw) -> dict:
    """给前端做即时校验，省得 POST 之后才看到 400"""
    if not str(raw or "").strip():
        return {"ok": False, "error": "路径为空"}
    p = clean_path(raw)
    if not p.exists():
        return {"ok": False, "error": f"文件不存在：{p}"}
    if p.is_dir():
        return {"ok": False, "error": "这是文件夹，需要具体视频文件"}
    try:
        m = probe(p)
        if m["duration"] <= 0 and m["width"] == 0:
            return {"ok": False, "error": "读不到时长和画面，可能不是视频文件（扩展名对不代表内容对）"}
        return {"ok": True, "path": str(p), "duration": m["duration"],
                "width": m["width"], "height": m["height"],
                "size_mb": round(p.stat().st_size / 1048576, 1)}
    except BaseException as e:  # noqa: BLE001
        # 缺 ffmpeg 时 ffmpeg_exe() 抛的是 SystemExit，不是 Exception
        msg = str(e) or type(e).__name__
        return {"ok": False, "error": f"环境或文件有问题：{msg}"}


def thumb_for(src: Path, t: float) -> str:
    """按需生成缩略图并返回相对 URL"""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    key = f"{hashlib.md5(str(src).encode()).hexdigest()[:10]}_{int(t * 10)}"
    dst = THUMB_DIR / f"{key}.jpg"
    if not dst.exists():
        run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
             "-vf", "scale=320:-2", "-q:v", "5", str(dst)], 120)
    return f"/thumb/{dst.name}"


def task_analyze(src: Path, top: int, seg_len: float, window: float,
                 avoid: list[float] | None = None) -> None:
    set_state(stage="analyzing", segments=[], pct=3, msg="读取素材信息…")
    meta = probe_cached(src, OUT_ROOT / CACHE_DIR)
    if meta["duration"] <= 0:
        raise RuntimeError("这个文件读不出一帧画面或时长，换个文件试试")
    title, ep = parse_episode(src)
    set_state(src=str(src), title=title, ep=ep, duration=meta["duration"],
              params={"top": top, "len": seg_len, "window": window})
    log(f"素材 {meta['width']}x{meta['height']} {fmt_sec(meta['duration'])}")

    check_cancel()
    set_state(pct=12, msg="扫描语音区间并计算高光（约 30–60 秒）…")
    if avoid:
        log(f"避开已用过的 {len(avoid)} 个位置，重新挑一批")
    segs = auto_segments(src, OUT_ROOT / CACHE_DIR, top, seg_len, window=window,
                         avoid=avoid)
    if not segs:
        raise RuntimeError("没有找到候选片段，试试调大「连贯窗口」或减少段数")

    out = []
    for i, s in enumerate(segs):
        check_cancel()
        st = float(s.get("_start", tc_to_sec(s["起"])))
        set_state(pct=60 + int(30 * i / max(1, len(segs))), msg=f"生成缩略图 {i + 1}/{len(segs)}")
        out.append({
            "i": i,
            "start": round(st, 2),
            "len": float(s["长"]),
            "score": float(s.get("_score", 0)),
            "tc": fmt_sec(st),
            "thumb": thumb_for(src, st + float(s["长"]) / 2),
            "selected": True,
        })
    set_state(stage="segments", segments=out, pct=100, msg=f"找到 {len(out)} 个候选片段")
    save_state()


BIBLE = OUT_ROOT / "总纲.md"


def bible_info() -> dict:
    """给界面用的总纲摘要。每次轮询都读 15KB 太浪费，按 mtime 缓存。"""
    if not BIBLE.exists():
        return {"exists": False, "path": str(BIBLE)}
    try:
        mt = BIBLE.stat().st_mtime
        cache = STATE.get("_bible_cache")
        if cache and cache.get("_mt") == mt:
            return cache["data"]
        from .series import _sections

        sec = _sections(BIBLE.read_text(encoding="utf-8-sig"))
        people = len([l for l in (sec.get("人物", "") or "").splitlines()
                      if l.startswith("- **")])
        nouns = len([l for l in (sec.get("名词与设定", "") or "").splitlines()
                     if l.startswith("- **")])
        terms = len([l for l in (sec.get("译名表", "") or "").splitlines()
                     if l.startswith("|") and "原名" not in l and "---" not in l])
        head = [l for l in BIBLE.read_text(encoding="utf-8-sig").splitlines()
                if l.startswith("> 自动生成")]
        data = {"exists": True, "path": str(BIBLE), "people": people, "nouns": nouns,
                "terms": terms, "main": len(sec.get("主线", "") or ""),
                "when": (head[0][2:].split("·")[0].strip() if head else "")}
        STATE["_bible_cache"] = {"_mt": mt, "data": data}
        return data
    except Exception as e:  # noqa: BLE001
        return {"exists": False, "path": str(BIBLE), "error": str(e)}


class _LogStream(io.TextIOBase):
    """把 series.build 的 print 转成界面日志，免得再写一套进度上报"""

    def __init__(self) -> None:
        self.buf = ""

    def write(self, s: str) -> int:  # noqa: A003
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.replace("\r", "").strip()
            if not line or line.startswith("已转写"):
                continue
            log(line)
            # 批量任务只在日志里报「[3/15] xxx」，从这里反推进度条
            m = re.match(r"^\[(\d+)/(\d+)\]", line)
            if m:
                i, n = int(m.group(1)), int(m.group(2))
                if n > 0:
                    set_state(pct=min(97, 5 + int(90 * (i - 1) / n)))
        return len(s)


def task_series(minutes: float, eps: int, force: bool, remix: bool,
                model: str, lang: str, names: str = "", no_fix: bool = False) -> None:
    """整季建总纲。写到 gui_out/总纲.md，素材盘保持只读。"""
    from . import series

    src = Path(STATE["src"])
    if not src.exists():
        raise RuntimeError("先填一个素材路径（总纲按它所在的整季目录跑）")
    folder = src if src.is_dir() else src.parent

    n_video = len(list(folder.glob("*"))) if folder.is_dir() else 1
    set_state(stage="series", pct=5,
              msg=f"读整季目录（{folder.name}，{n_video} 个文件）…")
    log(f"总纲：{folder}")
    log(f"每集采样 {minutes:g} 分钟 · 最多 {eps or '全部'} 集"
        + (" · 只重汇总" if remix else ""))

    stream = _LogStream()
    with contextlib.redirect_stdout(stream):
        rc = series.build(folder, OUT_ROOT, model=model, lang=lang,
                          per_ep_min=minutes, episodes=eps, force=force,
                          remix=remix, names=names, no_fix=no_fix,
                          should_stop=CANCEL.is_set)
    if rc == 2 and CANCEL.is_set():
        raise TaskCancelled("已取消")
    if rc != 0:
        raise RuntimeError(f"总纲生成失败（返回 {rc}）")

    info = bible_info()
    set_state(stage="idle", pct=100, msg="总纲已生成，回第 1 步继续",
              bible=info)


def task_batch(*, folder: Path, top: int, seg_len: float, window: float,
               seconds: float, model: str, lang: str, voice: str, rate: str,
               src_vol: float, spec, title: str, background: str,
               style: str, bible: str,
               burn_sub: bool = True, sub_style: str = "default",
               bgm: str = "", bgm_vol: float = 0.18, duck: bool = True) -> None:
    """整季批量：直接复用命令行的 run_explainer。

    单集界面那条链是「挑片段 → 改文案 → 出片」，人工介入三次；
    整季要的是挂机，所以走配方那条全自动链路，顺带把封面、台账、多规格都拿到。
    """
    from .explainer import run_explainer

    videos = sorted(p for p in folder.glob("*")
                    if p.suffix.lower() in (".mp4", ".mkv", ".mov", ".ts", ".avi", ".flv", ".wmv"))
    if not videos:
        raise RuntimeError(f"这个目录里没有视频文件：{folder}")
    set_state(stage="batch", pct=4, segments=[], output="",
              msg=f"整季批量：{len(videos)} 集")
    log(f"批量目录 {folder}")
    log(f"{len(videos)} 集 · 每集 {top} 段 × {seg_len:g}s · 窗口 {window:g}s")

    out = OUT_ROOT / "整季"
    job = {
        "素材": str(folder),
        "输出": str(out),
        "规格": parse_specs(spec),          # run_explainer 本来就吃列表，一次出多个画幅
        "缩放": "blur",
        "封面": "auto",
        "台账": True,
        "字幕": bool(burn_sub),
        "字幕样式": sub_style,
        "解说": {
            "段数": top, "段长": seg_len, "窗口": window,
            "按段": True, "语言": lang, "字幕模型": model,
            "解说时长": seconds, "文案版本": 1,
            "背景": background, "风格": style,
            "音色": voice, "语速": rate, "原声音量": src_vol,
            "总纲": bible,
        },
    }
    bgm_path = clean_path(bgm) if str(bgm).strip() else None
    if bgm_path is not None:
        try:
            bgm_path = bgm_path.resolve()
        except OSError:
            pass
        if bgm_path.is_file():
            job["BGM"] = str(bgm_path)
            job["BGM音量"] = max(0.0, min(1.0, float(bgm_vol)))
            job["闪避"] = bool(duck)
            log(f"BGM {bgm_path.name} @{job['BGM音量']:g}")
        else:
            log(f"BGM 找不到（已跳过）：{bgm_path}")
    stream = _LogStream()
    with contextlib.redirect_stdout(stream):
        rc = run_explainer(job, OUT_ROOT.parent, out, 1, False,
                           should_stop=CANCEL.is_set)

    made = sorted(out.rglob("*.mp4"))
    made = [p for p in made if CACHE_DIR not in p.parts]
    if rc == 2 and CANCEL.is_set():
        raise TaskCancelled("已取消")
    if rc != 0 and not made:
        raise RuntimeError(f"批量出片失败（返回 {rc}）")
    set_state(stage="done", pct=100, output=str(made[-1]) if made else "",
              batch={"count": len(made), "dir": str(out), "videos": len(videos)},
              msg=f"批量完成：{len(made)} 个成片，在 {out.name}/")
    log(f"批量完成，{len(made)} 个成片")


def task_script(variants: int, seconds: float, background: str, style: str) -> None:
    """逐段写文案：第 k 段只写第 k 段画面里的内容"""
    src = Path(STATE["src"])
    work = OUT_ROOT / "_work" / src.stem
    work.mkdir(parents=True, exist_ok=True)

    sel = [x for x in STATE["segments"] if x["selected"]]
    if not sel:
        raise RuntimeError("没有选中任何片段")
    segs = [
        {"源": str(src),
         "起": f"{int(x['start'] // 3600)}:{int(x['start'] % 3600 // 60):02d}:{x['start'] % 60:06.3f}",
         "长": float(x["len"]), "_start": float(x["start"])}
        for x in sel
    ]

    check_cancel()
    sample = work / "采样.m4a"
    set_state(stage="scripting", pct=5, msg="拼接选中片段的音轨…")
    if not build_sample_audio(segs, sample, OUT_ROOT / CACHE_DIR):
        raise RuntimeError("采样音频生成失败")

    txt = work / "转写.txt"
    if not txt.exists():
        set_state(pct=15, msg="转写台词（首次需下载模型）…")
        if cmd_asr_safe(sample, work / "转写.srt") != 0:
            raise RuntimeError("转写失败")

    check_cancel()
    set_state(pct=42, msg="按片段切分台词…")
    marked = mark_segments(txt, [s["长"] for s in segs], work / "转写_分段.txt")
    set_state(transcript=marked.read_text(encoding="utf-8"))

    # 有总纲就带上：统一称呼、知道前情。没有也不影响，只是每集从零开始
    ctx = background
    if BIBLE.exists():
        from .series import load_context

        bctx = load_context(BIBLE, STATE.get("ep"))
        if bctx:
            ctx = (bctx + "\n\n" + background).strip() if background else bctx
            set_state(pct=50, msg=f"注入剧集总纲（{len(bctx)} 字）")
            log(f"注入剧集总纲 {len(bctx)} 字（第 {STATE.get('ep') or '?'} 集）")

    n_var = max(1, min(variants, 3))
    set_state(pct=55, msg=f"逐段写文案（{len(segs)} 段，{n_var} 版）…")
    cfg_llm = load_llm_config(None)
    want = seconds * 4.1          # 这个目标时长该有多少字
    versions: list[dict] = []
    for k in range(n_var):
        check_cancel()
        name = SCRIPT_ANGLE_NAMES[k % len(SCRIPT_ANGLE_NAMES)]
        set_state(msg=f"写第 {k + 1}/{n_var} 版（{name}）…")
        try:
            got = script_by_segment(cfg_llm, marked, len(segs), seconds, style,
                                    SCRIPT_ANGLES[k % len(SCRIPT_ANGLES)], context=ctx)
        except BaseException as e:  # noqa: BLE001
            # 单版失败不该毁掉整轮：剩下两版还有机会
            log(f"第 {k + 1} 版（{name}）失败：{e}")
            continue
        data = [{"i": i, "lines": next((g["lines"] for g in got if g["i"] == i), [])}
                for i in range(1, len(segs) + 1)]
        full = sum(1 for d in data if d["lines"])
        if not full:
            continue
        chars = sum(len("".join(d["lines"]).replace(" ", "")) for d in data)
        versions.append({
            "key": "ABC"[k % 3],
            "name": name,
            "chars": chars,
            "full": full,
            "want": int(want),
            "seg_scripts": data,
        })
        log(f"第 {k + 1} 版（{name}）：{chars} 字 / 目标 {int(want)} 字，覆盖 {full}/{len(segs)} 段")

    if not versions:
        raise RuntimeError("逐段文案生成失败（3 版都没拿到可用结果）")

    # 择优：不再只比字数。六个维度各自打分、各自给理由，
    # 界面上能说清「为什么选这版、别的版差在哪」。
    from .score import rank

    best_key = rank(versions, want_chars=want, n_seg=len(segs))
    best = next(v for v in versions if v["key"] == best_key)
    tie = sum(1 for v in versions if v.get("score") == best.get("score")) > 1
    for v in versions:
        if v["key"] != best_key and v.get("reasons"):
            log(f"{v['key']} 版（{v['name']}）扣分：{v['reasons'][0]}")

    set_state(stage="scripts", variants=versions, variant=best["key"],
              seg_scripts=best["seg_scripts"], seg_ids=[x["i"] for x in sel],
              scripts=[], chosen="", pct=100,
              msg=f"{len(versions)} 版文案就绪，推荐 {best['key']}（{best['grade']} "
                  f"{best['score']:.0f} 分：{best['chars']} 字 / 覆盖 {best['full']}/{len(segs)} 段）"
                  + ("；几版分数接近，建议并排看一眼" if tie else ""))
    save_state()



def dir_stat(p: Path) -> dict:
    """目录里有多少文件、占多少 MB。逐个 stat，出错就跳过（可能在写/被占用）"""
    n, size = 0, 0
    if p.exists():
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    n += 1
                    size += f.stat().st_size
            except OSError:
                continue
    return {"files": n, "mb": round(size / 1048576, 1), "path": str(p)}


# 只允许清这三个。成片、整季、总纲、台账都在 gui_out 根下，绝不能被扫到。
# `_webview`（页面内核缓存，实测 100MB+）不在这里：窗口一开文件全被锁住，
# 做成按钮就是假按钮。它在客户端启动前清，见 app.py。
CACHE_PARTS = {
    "cache": "分析缓存（语音区间、转场位置）",
    "thumbs": "缩略图",
    "work": "工作目录（转写、逐段配音、对齐音频）",
}


def cache_info() -> dict:
    parts = {k: {**dir_stat(OUT_ROOT / (CACHE_DIR if k == "cache" else "_" + k)),
                 "label": label}
             for k, label in CACHE_PARTS.items()}
    total = round(sum(v["mb"] for v in parts.values()), 1)
    try:
        import shutil as _sh

        usage = _sh.disk_usage(str(OUT_ROOT))
        free_mb = round(usage.free / 1048576, 1)
    except OSError:
        free_mb = 0.0
    data = {"parts": parts, "total_mb": total, "free_mb": free_mb,
            "out": str(OUT_ROOT)}
    with LOCK:
        STATE["cache"] = data
    return data


def cache_clear(what: str) -> dict:
    """清缓存。忙的时候直接拒绝——_work 里正有文件在被 ffmpeg 读。"""
    with LOCK:
        if STATE["busy"]:
            raise RuntimeError("任务正在跑，跑完再清")
    keys = list(CACHE_PARTS) if what in ("all", "") else [what]
    bad = [k for k in keys if k not in CACHE_PARTS]
    if bad:
        raise RuntimeError("不认识的清理目标："
                           f"{bad[0]}（只支持 all/{'/'.join(CACHE_PARTS)}）")

    before = cache_info()
    freed = 0.0
    for k in keys:
        d = OUT_ROOT / (CACHE_DIR if k == "cache" else "_" + k)
        if not d.exists():
            continue
        freed += dir_stat(d)["mb"]
        # 一个一个删再删目录：一次 rmtree 遇到被占用的文件会整批放弃
        for f in sorted(d.rglob("*"), reverse=True):
            try:
                if f.is_file():
                    f.unlink()
                else:
                    f.rmdir()
            except OSError:
                continue          # 被占用 / 只读，下次再说
        try:
            d.rmdir()
        except OSError:
            pass
    after = cache_info()
    log(f"清理 {','.join(keys)}：释放约 {freed:.0f}MB，剩 {after['total_mb']:.0f}MB")
    return {"ok": True, "freed_mb": round(freed, 1),
            "before_mb": before["total_mb"], "after": after}


MODEL_ROOT = Path.home() / ".cache" / "djkit" / "models"


def season_info(spec: str = "") -> dict:
    """整季汇总。结果按「目录 mtime + 成片数量」缓存 —— 轮询时不该每次都扫盘。"""
    from .season import scan_season

    root = OUT_ROOT / "整季"
    try:
        sig = f"{root.stat().st_mtime_ns}" if root.exists() else "none"
        n = len(list(root.rglob("*.mp4"))) if root.exists() else 0
        sig += f"|{n}|{spec}"
    except OSError:
        sig = f"err|{spec}"
    cache = STATE.get("_season_cache")
    if cache and cache.get("_sig") == sig:
        return cache["data"]
    data = scan_season(root, spec)
    with LOCK:
        STATE["_season_cache"] = {"_sig": sig, "data": data}
    return data


def whisper_ready(model: str) -> bool:
    """模型是否已经下载到本地。没下过就提示一下，否则界面看起来像卡死。"""
    name = model if "/" in model else f"faster-whisper-{model}"
    p = MODEL_ROOT / name.split("/")[-1]
    return (p / "model.bin").exists() and (p / "config.json").exists()


def cmd_asr_safe(sample: Path, srt: Path) -> int:
    from .asr import cmd_asr

    model = STATE.get("model", "small.en")
    if not whisper_ready(model):
        log(f"首次用 {model}：要先下载模型（约 0.5–1.5GB，国内 1–5 分钟），"
            "这期间界面没有进度是正常的，别关窗口")
        set_state(msg=f"下载转写模型 {model}…")

    return cmd_asr(argparse.Namespace(
        input=str(sample), out=str(srt), model=model,
        lang=STATE.get("lang", "en"), device="cpu", compute="int8", beam=5, vad=True,
    ))


def parse_specs(raw) -> list[str]:
    """把界面传来的规格归一化成 ["1080x1920", …]。

    配音和编码是分开的：配音只做一次，多规格只是多编码几遍。
    所以这里宁可宽容一点，也不因为一个写错的值把整条链废掉。
    """
    if isinstance(raw, str):
        raw = [raw]
    seen: list[str] = []
    for s in raw or []:
        s = str(s).strip().lower().replace("×", "x").replace("*", "x")
        m = re.fullmatch(r"(\d{2,4})x(\d{2,4})", s)
        if not m:
            continue
        w, h = int(m.group(1)), int(m.group(2))
        w -= w % 2          # yuv420p 要求偶数，奇数宽高直接编码失败
        h -= h % 2
        if w < 240 or h < 240:
            continue
        key = f"{w}x{h}"
        if key not in seen:
            seen.append(key)
    if not seen:
        raise RuntimeError("规格没填对，形如 1080x1920；竖屏至少 240 宽")
    return seen[:4]


def task_render(voice: str, rate: str, src_vol: float, specs,
                title_tpl: str, seg_scripts: list,
                burn_sub: bool = True, sub_style: str = "default",
                bgm: str = "", bgm_vol: float = 0.18,
                duck: bool = True) -> None:
    """逐段配音 → 每段画面长 = 这段配音长 → 拼接 → 按规格各编码一遍

    字幕不走 ASR：文案和每段时长都在手里，直接排时间轴，专名一个都不会错，
    还省掉一次转写。BGM 在 assemble 里混，原声压低 + 闪避都在那边。
    """
    specs = parse_specs(specs)
    src = Path(STATE["src"])
    work = OUT_ROOT / "_work" / src.stem
    work.mkdir(parents=True, exist_ok=True)

    sel = [x for x in STATE["segments"] if x["selected"]]
    if [x["i"] for x in sel] != (STATE.get("seg_ids") or []):
        raise RuntimeError("片段选择变了，请重新生成文案（否则文案和画面对不上）")

    data = [{"i": d.get("i"), "lines": [x for x in (d.get("lines") or []) if x.strip()]}
            for d in (seg_scripts or [])]
    data = [d for d in data if d["lines"]]
    if len(data) != len(sel):
        raise RuntimeError(f"文案 {len(data)} 段，画面 {len(sel)} 段，数量对不上")

    segs = [
        {"源": str(src),
         "起": f"{int(x['start'] // 3600)}:{int(x['start'] % 3600 // 60):02d}:{x['start'] % 60:06.3f}",
         "长": float(x["len"]), "_start": float(x["start"])}
        for x in sel
    ]

    set_state(stage="rendering", pct=8, msg=f"逐段配音（{len(data)} 段）…")
    audios: list = []
    for i, d in enumerate(data):
        check_cancel()
        a = work / f"_seg{i:02d}.mp3"
        a.unlink(missing_ok=True)
        if not synth_one("\n".join(d["lines"]), a, voice, rate):
            raise RuntimeError(f"第 {i + 1} 段配音失败")
        audios.append(a)
        set_state(pct=8 + int(30 * (i + 1) / len(data)))

    set_state(pct=42, msg="按配音长度调整每段画面…")
    segs, fit_targets = fit_segments(segs, [d["lines"] for d in data], audios)

    aligned = work / "配音_对齐.m4a"
    set_state(pct=52, msg="拼接对齐配音…")
    if not build_aligned_voice(audios, fit_targets, aligned, work):
        raise RuntimeError("对齐配音生成失败")

    # 字幕：直接用刚算出来的每段画面时长排时间轴。
    # 这是整套里唯一「不需要 ASR 也能准」的地方 —— 文案是我们自己写的。
    sub_file = None
    if burn_sub and str(sub_style).lower() not in ("off", "none", "关", "false", ""):
        from .subtitle import build_script_srt

        set_state(pct=56, msg="按文案生成字幕…")
        sub_file, n_cue = build_script_srt(data, fit_targets, work / "字幕.srt")
        log(f"字幕 {n_cue} 条（{sub_style}）")

    w0, h0 = (int(x) for x in specs[0].split("x"))
    cfg = {
        "宽": w0, "高": h0, "缩放": "blur", "帧率": 25, "CRF": 20,
        "配音": str(aligned), "原声音量": src_vol,
        "_sub_style": None, "_title_style": "bar", "_title_start": 0.2,
        "_title_dur": 3.0, "_title_size": 88, "_title_pos": "bottom",
    }
    if sub_file is not None:
        cfg["字幕"] = str(sub_file)
        cfg["字幕样式"] = sub_style

    # BGM：文件丢了不该让整条链失败，只是没音乐。
    # 必须转绝对路径 —— assemble_cmd 的 base 是 work 目录，相对路径会被拼到那儿去。
    bgm_path = None
    if str(bgm).strip():
        cand = clean_path(bgm)
        try:
            cand = cand.resolve()
        except OSError:
            pass
        if cand.is_file():
            bgm_path = cand
            cfg["BGM"] = str(cand)
            cfg["BGM音量"] = max(0.0, min(1.0, float(bgm_vol)))
            cfg["闪避"] = bool(duck)
            log(f"BGM {cand.name} @{cfg['BGM音量']:g}" + ("（人声闪避）" if duck else ""))
        else:
            log(f"BGM 找不到（已跳过）：{cand}")

    if title_tpl:
        txt = (title_tpl.replace("{剧名}", STATE["title"])
                        .replace("{集}", f"EP{STATE['ep']:03d}" if STATE["ep"] else src.stem))
        cfg["花字"] = {"文字": txt, "样式": "bar", "开始": 0.3, "时长": 3.0,
                      "字号": 96, "位置": "top"}

    # 多规格：配音/对齐只做一次，这里只是按不同画幅重编码。
    # 第一个规格沿用原来的文件名，后面几个带后缀，避免覆盖老成片。
    made: list[dict] = []
    primary = None
    for k, spec in enumerate(specs):
        check_cancel()
        w, h = (int(x) for x in spec.split("x"))
        cfg["宽"], cfg["高"] = w, h
        set_state(pct=62 + int(30 * k / len(specs)),
                  msg=f"编码 {spec}（{k + 1}/{len(specs)}）…")
        out = (OUT_ROOT / f"{src.stem}_解说.mp4" if k == 0
               else OUT_ROOT / f"{src.stem}_解说_{spec}.mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)
        cmd, _ = assemble_cmd(segs, cfg, work, out, OUT_ROOT / CACHE_DIR)
        r = run(cmd, timeout=7200)
        if r.returncode != 0 or not out.exists():
            # 一个规格失败不该把已经出来的干掉，最后统一报
            log(f"规格 {spec} 编码失败：{(r.stderr or '')[-160:]}")
            continue
        made.append({"path": str(out), "spec": spec,
                     "size_mb": round(out.stat().st_size / 1048576, 1)})
        if primary is None:
            primary = out
        log(f"成片 {out.name}（{spec}，{made[-1]['size_mb']}MB）")

    if not made:
        raise RuntimeError("所有规格都编码失败")
    out = primary

    flat = work / "文案" / "选定.txt"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_text("\n".join(x for d in data for x in d["lines"]) + "\n", encoding="utf-8")

    m = probe(out)

    # 封面：投放必须要有首图。取开头一点的位置，避开纯黑帧
    from .compose import extract_cover

    cover = out.with_suffix(".jpg")
    if extract_cover(out, cover, min(1.2, max(0.2, m["duration"] * 0.08))):
        log(f"封面 {cover.name}")

    # 台账：把这次出片记进 gui_out/_投放台账.xlsx
    try:
        from .report import cmd_report

        cmd_report(argparse.Namespace(
            dir=str(out.parent), out=str(OUT_ROOT / "_投放台账.xlsx"),
            jobs=1, no_cache=False))
    except BaseException as e:  # noqa: BLE001
        # 台账失败不该判定整条成片失败
        log(f"台账生成失败（不影响成片）：{e}")

    segs_txt = " · ".join(f"{t:.1f}s" for t in fit_targets)
    spec_txt = "＋".join(x["spec"] for x in made)
    extra = []
    if sub_file is not None:
        extra.append(f"字幕 {sub_style}")
    if bgm_path is not None:
        extra.append(f"BGM {bgm_vol:g}")
    set_state(stage="done", output=str(out), outputs=made,
              chosen=flat.read_text(encoding="utf-8"),
              pct=100, cover=str(cover) if cover.exists() else "",
              msg=f"成片 {m['duration']:.1f}s · {spec_txt}"
                  f"（{len(data)} 段：{segs_txt}）"
                  + ("，带" + "、".join(extra) if extra else ""))
    save_state()


class Handler(BaseHTTPRequestHandler):
    server_version = "djkit-gui"

    def log_message(self, *a) -> None:  # 安静点
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def _file(self, path: Path, ctype: str) -> None:
        data = path.read_bytes()
        self._send(200, data, ctype)

    def _stream_video(self, path: Path) -> None:
        """Range 响应：没有它浏览器无法拖动进度条"""
        size = path.stat().st_size
        rng = self.headers.get("Range")
        if not rng:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with path.open("rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
            return
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        start = int(m.group(1) or 0)
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                self._file(WEB / "index.html", "text/html; charset=utf-8")
            elif p == "/app.js":
                self._file(WEB / "app.js", "application/javascript; charset=utf-8")
            elif p == "/style.css":
                self._file(WEB / "style.css", "text/css; charset=utf-8")
            elif p == "/api/state":
                self._json(snapshot())
            elif p == "/api/bible":
                self._json(bible_info())
            elif p == "/api/cache":
                self._json(cache_info())
            elif p == "/api/season":
                q = urllib.parse.parse_qs(u.query)
                self._json(season_info(q.get("spec", [""])[0]))
            elif p == "/api/check":
                q = urllib.parse.parse_qs(u.query).get("path", [""])[0]
                self._json(probe_path(q))
            elif p.startswith("/thumb/"):
                f = THUMB_DIR / Path(p).name
                if not f.exists():
                    self._json({"error": "no thumb"}, 404)
                else:
                    self._file(f, "image/jpeg")
            elif p == "/cover":
                # ?p= 用来给整季汇总里的每一集取封面
                q = urllib.parse.parse_qs(u.query).get("p", [""])[0]
                cov = q or STATE.get("cover")
                fp = Path(cov) if cov else None
                if fp and q:
                    try:
                        if not fp.resolve().is_relative_to(OUT_ROOT.resolve()):
                            return self._json({"error": "只能看输出目录里的封面"}, 403)
                    except OSError:
                        pass
                if not fp or not fp.exists():
                    self._json({"error": "还没有封面"}, 404)
                else:
                    self._file(fp, "image/jpeg")
            elif p == "/video":
                # ?p= 用来在「多规格」里切着看，只允许 gui_out 里的文件
                q = urllib.parse.parse_qs(u.query).get("p", [""])[0]
                out = q or STATE.get("output")
                fp = Path(out) if out else None
                if fp and q:
                    try:
                        if not fp.resolve().is_relative_to(OUT_ROOT.resolve()):
                            return self._json({"error": "只能播放输出目录里的文件"}, 403)
                    except OSError:
                        pass
                if not fp or not fp.exists():
                    self._json({"error": "还没有成片"}, 404)
                else:
                    self._stream_video(fp)
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"请求体不是合法 JSON：{e}"}, 400)
        try:
            if u.path == "/api/analyze":
                chk = probe_path(body.get("path", ""))
                if not chk["ok"]:
                    return self._json({"error": chk["error"]}, 400)
                src = Path(chk["path"])
                top = int(body.get("top", 4))
                seg_len = float(body.get("len", 12))
                window = float(body.get("window", 150) or 0)
                set_state(model=body.get("model", "small.en"), lang=body.get("lang", "en"))
                avoid = [float(x) for x in (body.get("avoid") or []) if x is not None]
                run_task(lambda: task_analyze(src, top, seg_len, window, avoid))
                return self._json({"ok": True})

            if u.path == "/api/segments":
                segs = body.get("segments") or []
                with LOCK:
                    STATE["segments"] = segs
                save_state()
                return self._json({"ok": True})

            if u.path == "/api/series":
                # 总纲按素材所在目录跑，产物写到 gui_out/，素材盘保持只读
                raw = str(body.get("path") or STATE.get("src") or "").strip()
                if not raw:
                    return self._json({"error": "先在素材页填一个视频路径"}, 400)
                pp = clean_path(raw)
                if not pp.exists():
                    return self._json({"error": f"路径不存在：{pp}"}, 400)
                with LOCK:
                    STATE["src"] = str(pp)
                    STATE["title"], STATE["ep"] = parse_episode(pp)
                run_task(lambda: task_series(
                    float(body.get("minutes", 7) or 7),
                    int(body.get("eps", 0) or 0),
                    bool(body.get("force")),
                    bool(body.get("remix")),
                    str(body.get("model", "small.en")),
                    str(body.get("lang", "en")),
                    str(body.get("names") or ""),
                    bool(body.get("no_fix")),
                ))
                return self._json({"ok": True})

            if u.path == "/api/batch":
                raw = str(body.get("dir") or "").strip()
                if not raw:
                    return self._json({"error": "选一个剧集目录"}, 400)
                folder = clean_path(raw)
                if not folder.is_dir():
                    return self._json({"error": f"不是目录：{folder}"}, 400)
                bible = str(body.get("bible") or "").strip()
                if not bible:
                    bible = str(BIBLE) if BIBLE.exists() else "关"
                run_task(lambda: task_batch(
                    folder=folder,
                    top=int(body.get("top", 4)),
                    seg_len=float(body.get("len", 12)),
                    window=float(body.get("window", 150)),
                    seconds=float(body.get("seconds", 40) or 40),
                    model=str(body.get("model", "small.en")),
                    lang=str(body.get("lang", "en")),
                    voice=str(body.get("voice", "zh-CN-YunxiNeural")),
                    rate=str(body.get("rate", "+0%")),
                    src_vol=float(body.get("src_vol", 0.12)),
                    spec=body.get("specs") or body.get("spec") or "1080x1920",
                    title=str(body.get("title", "")),
                    background=str(body.get("background", "")),
                    style=str(body.get("style", "克制的叙述，不煽情，偶尔一句冷判断")),
                    bible=bible,
                    burn_sub=bool(body.get("burn_sub", True)),
                    sub_style=str(body.get("sub_style") or "default"),
                    bgm=str(body.get("bgm") or ""),
                    bgm_vol=float(body.get("bgm_vol", 0.18) or 0),
                    duck=bool(body.get("duck", True)),
                ))
                return self._json({"ok": True})

            if u.path == "/api/script":
                variants = int(body.get("variants", 3))
                seconds = float(body.get("seconds", 45))
                bg = body.get("background", "")
                style = body.get("style", "克制的叙述，不煽情，偶尔一句冷判断")
                run_task(lambda: task_script(variants, seconds, bg, style))
                return self._json({"ok": True})

            if u.path == "/api/render":
                # specs 是数组；老调用方传单个 spec 字符串也认
                raw_specs = body.get("specs") or body.get("spec") or "1080x1920"
                run_task(lambda: task_render(
                    body.get("voice", "zh-CN-YunjianNeural"),
                    body.get("rate", "+2%"),
                    float(body.get("src_vol", 0.14)),
                    raw_specs,
                    body.get("title", ""),
                    body.get("seg_scripts") or [],
                    burn_sub=bool(body.get("burn_sub", True)),
                    sub_style=str(body.get("sub_style") or "default"),
                    bgm=str(body.get("bgm") or ""),
                    bgm_vol=float(body.get("bgm_vol", 0.18) or 0),
                    duck=bool(body.get("duck", True)),
                ))
                return self._json({"ok": True})

            if u.path == "/api/variant":
                # 切版本只改「当前用哪版」，不动 variants 本身，切回来还能切回去
                key = str(body.get("key") or "").upper()[:1]
                with LOCK:
                    vs = STATE.get("variants") or []
                    hit = next((v for v in vs if v["key"] == key), None)
                    if hit:
                        STATE["variant"] = key
                        STATE["seg_scripts"] = hit["seg_scripts"]
                if not hit:
                    return self._json({"error": f"没有 {key} 版"}, 400)
                save_state()
                return self._json({"ok": True, "key": key})

            if u.path == "/api/open":
                target = Path(body.get("path") or OUT_ROOT)
                if target.is_file():
                    os.startfile(str(target))  # noqa: S606
                else:
                    os.startfile(str(target))
                return self._json({"ok": True})

            if u.path == "/api/reset":
                with LOCK:
                    busy = STATE["busy"]
                if busy:
                    return self._json(
                        {"error": "任务正在跑，先点「取消」再重置"}, 409)
                reset()
                return self._json({"ok": True})

            if u.path == "/api/cancel":
                return self._json({"ok": True, "cancelled": request_cancel()})

            if u.path == "/api/log":
                # 客户端版没有控制台，前端报错只能靠这里落到 gui_out/app.log，
                # 否则用户只会说「白屏了」，而日志里干干净净
                msg = str(body.get("msg") or "").strip()[:300]
                if msg:
                    log("前端：" + msg)
                return self._json({"ok": True})

            if u.path == "/api/cache":
                # 参数错是用户输入问题，回 400 就够了，别打一整段 traceback 吓人
                try:
                    return self._json(cache_clear(str(body.get("what") or "all")))
                except RuntimeError as e:
                    return self._json({"error": str(e)}, 400)

            self._json({"error": "not found"}, 404)
        except BaseException as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": str(e) or type(e).__name__}, 500)


def reset() -> None:
    """清空界面状态。忙的时候不允许——否则 busy 被强制置 False，
    下一次 run_task 就能再起一个线程，两个任务并行写同一份 STATE。"""
    with LOCK:
        if STATE["busy"]:
            raise RuntimeError("任务正在跑")
        CANCEL.clear()
        STATE.update({"stage": "idle", "busy": False, "pct": 0, "msg": "", "error": "",
                      "segments": [], "scripts": [], "seg_scripts": [], "seg_ids": [],
                      "variants": [], "variant": "", "outputs": [],
                      "chosen": "", "output": "", "cover": "", "batch": {},
                      "transcript": "", "log": []})
    LAST.unlink(missing_ok=True)   # 删掉落盘进度，重开不会又弹回来


def preflight() -> list[str]:
    """启动前检查关键依赖。缺了就直接告诉人怎么办，而不是让每个请求各错一次。"""
    problems: list[str] = []
    try:
        exe = ffmpeg_exe()
        print(f"ffmpeg   {exe}")
    except SystemExit as e:
        problems.append(str(e))
    for mod, how in (("openpyxl", "pip install openpyxl"),
                     ("yaml", "pip install pyyaml")):
        try:
            __import__(mod)
        except ImportError:
            problems.append(f"缺少 {mod}：{how}")
    return problems


def make_server(host: str = "127.0.0.1", port: int = 8770) -> ThreadingHTTPServer:
    """单独造一个 server 对象，供客户端（pywebview 外壳）复用。"""
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    note = load_state()
    if note:
        print(f"[恢复] {note}", flush=True)
    return ThreadingHTTPServer((host, port), Handler)


def serve(port: int = 8770, open_browser: bool = True, host: str = "127.0.0.1") -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Python   {sys.executable}")
    problems = preflight()
    if problems:
        print("\n启动失败，环境缺东西：")
        for x in problems:
            print("  - " + x)
        print("\n你现在用的解释器是：")
        print(f"  {sys.executable}   ← 这个环境缺依赖")
        print("\n两种解决方式，任选其一：")
        print("  1) 用项目自带的启动脚本（已绑定装好依赖的环境）")
        print("       djkit.cmd gui")
        print("  2) 给当前这个 Python 装依赖")
        print("       pip install -r requirements.txt")
        return 2
    url = f"http://{host}:{port}/"
    try:
        srv = make_server(host, port)
    except OSError as e:
        print(f"端口 {port} 被占用：{e}\n换个端口：--port 8771")
        return 2
    print(f"djkit 界面已启动：{url}")
    print("按 Ctrl+C 结束")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0
