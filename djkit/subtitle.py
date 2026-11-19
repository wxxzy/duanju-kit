"""djkit.subtitle

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import os
import re
import hashlib
from pathlib import Path

from .core import ff_path_quoted
from .media import parse_episode


SUB_EXTS = (".srt", ".ass", ".ssa", ".vtt")


SLICE_RANGE_RE = re.compile(r"_p\d{1,2}_(\d+(?:\.\d+)?)s-(\d+(?:\.\d+)?)s$", re.I)


_TC_RE = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})")


_ASS_TC_RE = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})\.(\d{1,2})")


SUB_STYLE_PRESETS = {"default": 64, "big": 82, "small": 48, "yellow": 64, "plain": 56}


def read_text_any(p: Path) -> tuple[str, str]:
    """按常见编码依次尝试解码，返回 (文本, 实际编码)"""
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "utf-16", "gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _tc_to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / (10 ** len(ms))


def _fmt_tc(t: float, sep: str = ",", ms_digits: int = 3) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    frac = int(round((t - int(t)) * (10 ** ms_digits)))
    if frac >= 10 ** ms_digits:
        frac = 10 ** ms_digits - 1
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{frac:0{ms_digits}d}"


def shift_sub_text(text: str, suffix: str, offset: float, duration: float) -> str:
    """把字幕时间轴整体前移 offset 秒，并裁掉落在 [0, duration] 之外的条目"""
    suffix = suffix.lower()
    if offset <= 0.001 and duration <= 0:
        return text
    if suffix in (".srt", ".vtt"):
        sep = "," if suffix == ".srt" else "."
        blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
        cues = []
        for b in blocks:
            lines = [ln for ln in b.splitlines() if ln.strip()]
            ti = next((i for i, ln in enumerate(lines) if _TC_RE.search(ln)), None)
            if ti is None:
                continue
            tcs = _TC_RE.findall(lines[ti])
            if len(tcs) < 2:
                continue
            s = _tc_to_sec(*tcs[0]) - offset
            e = _tc_to_sec(*tcs[1]) - offset
            if e <= 0.02 or s >= duration:
                continue
            s, e = max(0.0, s), min(duration, e)
            cues.append((s, e, lines[ti + 1 :]))
        if not cues:
            return ""
        chunks = []
        for n, (s, e, body) in enumerate(cues, 1):
            # libass 要求每条 cue 之间空行分隔、文件以空行收尾，否则直接打开失败
            head = f"{_fmt_tc(s, sep)} --> {_fmt_tc(e, sep)}"
            if suffix == ".srt":
                head = f"{n}\n{head}"
            chunks.append(head + ("\n" + "\n".join(body) if body else ""))
        prefix = "WEBVTT\n\n" if suffix == ".vtt" else ""
        return prefix + "\n\n".join(chunks) + "\n\n"
    # ass / ssa
    out = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("Dialogue:"):
            parts = ln.split(",", 9)
            if len(parts) >= 10:
                ms = _ASS_TC_RE.search(parts[1])
                me = _ASS_TC_RE.search(parts[2])
                if ms and me:
                    s = _tc_to_sec(*ms.groups()) - offset
                    e = _tc_to_sec(*me.groups()) - offset
                    if e <= 0.02 or s >= duration:
                        continue
                    s, e = max(0.0, s), min(duration, e)
                    parts[1] = _fmt_tc(s, ".", 2)
                    parts[2] = _fmt_tc(e, ".", 2)
                    out.append(",".join(parts))
                    continue
        out.append(ln)
    return "\n".join(out) + "\n"


def slice_range_of(path: Path) -> tuple[float, float] | None:
    """从切片产物名（…_p01_212s-232s.mp4）里取出原始区间"""
    m = SLICE_RANGE_RE.search(path.stem)
    return (float(m.group(1)), float(m.group(2))) if m else None


def prepare_sub_file(src: Path, offset: float, duration: float, cache: Path) -> Path:
    """统一转 UTF-8 并按需平移时间轴，产物缓存到 .djkit/subs/"""
    text, enc = read_text_any(src)
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if src.suffix.lower() == ".srt":
        # 少数 SRT 用点号作毫秒分隔，libass 会拒收
        text = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})(?=\s*-->)", r"\1,\2", text)
    if offset > 0.001 or duration > 0:
        text = shift_sub_text(text, src.suffix, offset, duration)
    key = hashlib.md5(f"{src}|{src.stat().st_mtime}|{offset}|{duration}".encode("utf-8")).hexdigest()[:10]
    subdir = cache / "subs"
    subdir.mkdir(parents=True, exist_ok=True)
    dst = subdir / f"{src.stem}_{key}{src.suffix.lower()}"
    dst.write_text(text, encoding="utf-8")
    return dst


class SubIndex:
    """字幕索引：先按同目录同名匹配，再按 剧名+集数 匹配"""

    def __init__(self, roots: list[Path]):
        self.files: list[Path] = []
        for r in roots:
            if r.is_file() and r.suffix.lower() in SUB_EXTS:
                self.files.append(r)
            elif r.is_dir():
                self.files += [p for p in r.rglob("*") if p.is_file() and p.suffix.lower() in SUB_EXTS]
        self.by_key: dict[tuple[str, int], Path] = {}
        self.by_stem: dict[str, Path] = {}
        for p in self.files:
            self.by_stem.setdefault(p.stem.lower(), p)
            t, ep = parse_episode(p)
            if ep is not None:
                self.by_key.setdefault((t, ep), p)

    def lookup(self, video: Path) -> Path | None:
        for ext in SUB_EXTS:
            cand = video.with_suffix(ext)
            if cand.exists():
                return cand
        stem = video.stem.lower()
        if stem in self.by_stem:
            return self.by_stem[stem]
        t, ep = parse_episode(video)
        if ep is not None and (t, ep) in self.by_key:
            return self.by_key[(t, ep)]
        # 宽松匹配：字幕名是否包含在视频名里
        for s, p in self.by_stem.items():
            if len(s) > 3 and s in stem:
                return p
        return None


def build_sub_style(style: str, out_h: int, size_px: float | None, font: str | None) -> str:
    """生成 force_style 串。libass 的字号/边距单位是脚本坐标（默认 PlayResY=288），
    这里统一按输出像素换算，保证不同分辨率下字幕观感一致。"""
    px = float(size_px) if size_px else SUB_STYLE_PRESETS.get(style, 64)
    k = 288.0 / max(out_h, 1)
    fs = max(5, round(px * k))
    margin = round(out_h * 0.085 * k)
    plain = style == "plain"
    font = font or ("Microsoft YaHei" if os.name == "nt" else "Noto Sans CJK SC")
    primary = "&H0000FFFF" if style == "yellow" else "&H00FFFFFF"
    return ",".join(
        [
            f"FontName={font}",
            f"FontSize={fs}",
            f"PrimaryColour={primary}",
            "SecondaryColour=&H00FFFFFF",
            "OutlineColour=&H00000000",
            "BackColour=&H80000000",
            "BorderStyle=1",
            f"Bold={0 if plain else 1}",
            f"Outline={0 if plain else 1}",
            f"Shadow={0 if plain else 1}",
            f"MarginV={margin}",
            "Alignment=2",
        ]
    )


def sub_filter_arg(sub: Path, style_str: str | None) -> str:
    arg = f"subtitles={ff_path_quoted(sub)}"
    if style_str:
        arg += f":force_style='{style_str}'"
    return arg


# ---------------------------------------------------------------- 由文案生成字幕
# 解说片不需要 ASR 转写：文案和每段时长本来就在手里，直接排时间轴，
# 比「先念再听」准确得多（专名一个都不会错），还省掉一次转写。

def wrap_cjk(text: str, width: int = 16) -> str:
    """中文按字数折行，最多两行。字幕一行超过 16 字就该换行了。"""
    text = text.strip()
    if width <= 0 or len(text) <= width:
        return text
    # 优先在标点处断开，断不开再硬切
    cut = -1
    for i in range(width, max(width - 5, 3), -1):
        if text[i - 1] in "，。！？、；：,.!?;: ":
            cut = i
            break
    if cut < 0:
        cut = width
    head, tail = text[:cut].rstrip("，、；：,;: "), text[cut:].lstrip()
    if not tail:
        return head
    if len(tail) > width:
        tail = tail[: width - 1] + "…"
    return head + "\n" + tail


def cues_from_scripts(seg_scripts: list[dict], durations: list[float],
                      *, start_at: float = 0.0, gap: float = 0.04,
                      min_cue: float = 0.55) -> list[tuple[float, float, str]]:
    """把「逐段文案 + 每段时长」排成字幕条目 [(起, 止, 文本)]。

    时间按每句字数分配：字多的句子本来就念得久，等比分配比平均分更贴配音。
    太短的条目（< min_cue）并进上一条，避免字幕一闪而过。
    """
    cues: list[tuple[float, float, str]] = []
    t = max(0.0, start_at)
    for seg, dur in zip(seg_scripts, durations):
        lines = [str(x).strip() for x in (seg.get("lines") or []) if str(x).strip()]
        dur = max(0.2, float(dur))
        if not lines:
            t += dur
            continue
        weights = [max(1, len(x)) for x in lines]
        total_w = sum(weights)
        # 段内留一点间隙，字幕别首尾相接
        usable = max(0.2, dur - gap * (len(lines) - 1) - gap)
        span = usable / total_w
        cursor = t + gap / 2
        for ln, w in zip(lines, weights):
            d = max(min_cue, span * w)
            s, e = cursor, min(t + dur, cursor + d)
            if e - s < min_cue and cues:
                ps, _pe, pt = cues[-1]
                cues[-1] = (ps, max(e, _pe), pt + "\n" + ln)   # 太短就并进上一条
            else:
                cues.append((s, e, ln))
            cursor = e + gap
        t += dur
    return [(s, e, txt) for s, e, txt in cues if e - s > 0.05]


def write_srt(cues: list[tuple[float, float, str]], dst: Path,
              width: int = 16) -> Path:
    """写标准 SRT。libass 要求条目间空行、末尾空行，少一个就直接打不开。"""
    chunks = []
    for n, (s, e, txt) in enumerate(cues, 1):
        body = wrap_cjk(txt, width)
        chunks.append(f"{n}\n{_fmt_tc(s)} --> {_fmt_tc(e)}\n{body}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n\n".join(chunks) + "\n\n", encoding="utf-8")
    return dst


def build_script_srt(seg_scripts: list[dict], durations: list[float], dst: Path,
                     *, start_at: float = 0.0, width: int = 16) -> tuple[Path, int]:
    """文案 → SRT。返回 (路径, 条数)"""
    cues = cues_from_scripts(seg_scripts, durations, start_at=start_at)
    return write_srt(cues, dst, width), len(cues)
