"""djkit.align —— 逐段解说（音画对位）

为什么需要这个模块：
    原来的做法是「一条连续旁白铺在一串画面上」。文案是一篇通顺的旁白，
    画面按时间顺序排，两者之间没有任何映射 —— 所以中段必然漂移。
    实测：解说在讲"马特"，画面字幕是"本还活着"；差一个片段（约 8 秒）。

B 方案改的是产出结构，不是提示词：
    第 k 段画面 → 只写第 k 段的 1~2 句 → 单独配音 → 该段画面长度 = 这段配音长度
    这样"说到哪、画面是哪"由数据结构保证，不靠模型自觉。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .compose import atempo_chain
from .core import ffmpeg_exe, probe, run
from .script import llm_chat
from .tts import cmd_tts

SEG_SYSTEM = """你是短剧 / 影视解说文案写手，作品投放在抖音竖屏信息流。

我会按画面顺序把台词分成 {n} 段，每段对应一段画面。请为**每一段**各写 {per} 句解说。

硬性要求：
1. 只能用给定台词里的内容，不许编造剧情、人名、数字、前因后果。
2. **第 k 段的句子只能讲第 k 段台词里的东西，绝对不许提前讲后面的内容。**
   这是最重要的一条：观众看到的画面就是这一段。
3. 每句 10–18 字，中文口语，一句一行。
4. **每段写够约 {per_chars} 字（{per} 句左右）**，全片合计 {lo}–{hi} 字（{seconds} 秒口播）。
   每句尽量写满 14–18 字，太短会撑不满时间。
5. 第 1 段的第 1 句必须是钩子，三秒内抓住人，不要开场白。
6. 最后一段收在悬念上，不要总结、不要升华、不要说教。
7. 不要出现"本期""视频""我们来看看""接下来"这类出戏的词。

风格：{style}
{angle}

只输出 JSON，不要解释、不要代码块标记，格式严格如下：
{{"segments":[{{"i":1,"lines":["句子","句子"]}},{{"i":2,"lines":["句子"]}}]}}"""


def _norm(data, n: int) -> list[dict]:
    out = []
    for s in (data.get("segments") or []):
        if not isinstance(s, dict):
            continue
        i = int(s.get("i", 0))
        lines = [str(x).strip() for x in (s.get("lines") or []) if str(x).strip()]
        if 1 <= i <= n and lines:
            out.append({"i": i, "lines": lines})
    return sorted(out, key=lambda x: x["i"])


def _looks_ok(segs: list[dict]) -> bool:
    """文案必须像句子：不能带 JSON 残渣，长度也得像话"""
    if not segs:
        return False
    for s in segs:
        for ln in s["lines"]:
            if any(ch in ln for ch in "{}[]") or '\\"' in ln or ":" in ln[:3]:
                return False
            if not 2 <= len(ln) <= 60:
                return False
    return True


def parse_segments_json(text: str, n: int) -> list[dict]:
    """解析模型返回的逐段文案。

    宁可抛错让上层重试，也不接受解析残渣 —— 之前兜底逻辑把整段 JSON
    当成了一句文案，成片里就真的念出了一串大括号。
    """
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.M).strip()

    # 1) 原样解析
    if "{" in raw:
        body = raw[raw.find("{"):raw.rfind("}") + 1]
        try:
            segs = _norm(json.loads(body), n)
            if _looks_ok(segs):
                return segs
        except Exception:
            pass

    # 2) 修常见毛病：中文引号、尾逗号
    fixed = raw.replace("\u201c", '"').replace("\u201d", '"')
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    if "{" in fixed:
        body = fixed[fixed.find("{"):fixed.rfind("}") + 1]
        try:
            segs = _norm(json.loads(body), n)
            if _looks_ok(segs):
                return segs
        except Exception:
            pass

    # 3) 正则兜底：逐段抓 "i":N 后面的 lines 数组
    segs: list[dict] = []
    for m in re.finditer(r'"i"\s*:\s*(\d+)[\s\S]{0,40}?"lines"\s*:\s*\[([\s\S]*?)\]', raw):
        i = int(m.group(1))
        lines = [x.strip() for x in re.findall(r'"([^"]{2,60})"', m.group(2))]
        if 1 <= i <= n and lines:
            segs.append({"i": i, "lines": lines})
    if _looks_ok(segs):
        return sorted(segs, key=lambda x: x["i"])

    raise RuntimeError("逐段文案没按 JSON 返回（或内容不像句子），重试")


def script_by_segment(cfg: dict, marked: Path, n: int, seconds: float,
                      style: str, angle: str, per: int = 0,
                      context: str = "") -> list[dict]:
    """让模型逐段写。返回 [{i, lines}]

    per=0 时按目标时长反推每段该写几句：目标秒数 / 段数 → 该段字数 → 行数。
    否则固定 2 句，4 段最多撑 20 秒，用户设的「解说时长 40」就形同虚设。
    """
    chars_per_seg = int((seconds / max(1, n)) * 4.1)
    if not per:
        per = max(1, min(4, round(chars_per_seg / 13)))
    text = marked.read_text(encoding="utf-8")
    chars = int(seconds * 4.1)
    lo, hi = int(chars * 0.85), int(chars * 1.15)
    sys_msg = SEG_SYSTEM.format(
        n=n, per=per, per_chars=chars_per_seg, lo=lo, hi=hi,
        seconds=int(seconds), style=style, angle=angle)
    user = ""
    if context.strip():
        user += "背景设定（可以引用，不要扩写）：\n" + context.strip() + "\n\n"
    user += "台词如下（按画面顺序分段）：\n" + text
    msg = [{"role": "system", "content": sys_msg},
           {"role": "user", "content": user}]
    # 逐段文案要求「全覆盖」：少写一段，那一段画面就得丢掉。
    # 所以解析失败或段数不够都重试，最多 3 次，返回最全的一次。
    best: list[dict] = []
    for attempt in range(3):
        raw = llm_chat(cfg, msg, temperature=0.85 + 0.05 * attempt)
        try:
            segs = parse_segments_json(raw, n)
        except RuntimeError as e:
            print(f"    第 {attempt + 1} 次解析失败：{e}")
            continue
        if len(segs) > len(best):
            best = segs
        if len(segs) >= n:
            return segs
        print(f"    只写了 {len(segs)}/{n} 段，重试")
    if best:
        return best
    raise RuntimeError("逐段文案生成失败（3 次都没拿到可用 JSON）")


def synth_one(text: str, out: Path, voice: str, rate: str) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_txt = out.with_suffix(".txt")
    tmp_txt.write_text(text, encoding="utf-8")
    rc = cmd_tts(argparse.Namespace(input=str(tmp_txt), out=str(out),
                                    voice=voice, rate=rate, volume="+0%", dry_run=False))
    return rc == 0 and out.exists() and out.stat().st_size > 800


def build_aligned_voice(audios: list[Path], targets: list[float], dst: Path,
                        work: Path) -> bool:
    """把每段音频补齐静音到目标长度，再拼成一条。

    补齐而不是直接拼：assemble 只认「一条配音 + 一串带时长的片段」，
    只要这条配音的分段边界和视频片段边界严格重合，就等于逐段对齐了。
    """
    if not audios:
        return False
    parts: list[Path] = []
    for i, (a, tgt) in enumerate(zip(audios, targets)):
        dur = probe(a)["duration"]
        pad = max(0.0, tgt - dur)
        p = work / f"_pad{i:02d}.m4a"
        run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(a), "-af", f"apad=pad_dur={pad:.3f}",
             "-t", f"{tgt:.3f}", "-ar", "24000", "-ac", "2",
             "-c:a", "aac", "-b:a", "128k", str(p)], 300)
        if not p.exists():
            return False
        parts.append(p)

    lst = work / "_voice_list.txt"
    # 必须用绝对路径：concat 解析相对路径时是相对「清单文件所在目录」，
    # 会拼成 产出/E01_A/.../产出/E01_A/... 这种重复路径
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
                   encoding="utf-8")
    r = run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c:a", "aac", "-b:a", "160k", str(dst)], 900)
    if r.returncode != 0:
        print("  拼接配音失败：" + (r.stderr or "")[-300:])
    for p in parts:
        p.unlink(missing_ok=True)
    lst.unlink(missing_ok=True)
    return r.returncode == 0 and dst.exists() and dst.stat().st_size > 1000


def fit_segments(segs: list[dict], texts: list[str], audios: list[Path],
                 tail: float = 0.35) -> tuple[list[dict], list[float]]:
    """按配音时长反推每段画面该多长，并在必要时加速配音塞进源片段。

    返回 (新的 segs, 每段目标时长)。源片段不够长时用 atempo 提语速，
    而不是把画面拉长——画面一旦超出源素材可用范围就黑屏了。
    """
    out_segs: list[dict] = []
    targets: list[float] = []
    for s, a in zip(segs, audios):
        avail = float(s["长"])
        dur = probe(a)["duration"]
        need = dur + tail
        if need <= avail:
            target = max(2.5, need)
        else:
            # 音频太长：加速，最多 1.6 倍
            ratio = min(1.6, dur / max(0.5, avail - tail))
            if ratio > 1.02:
                tmp = a.with_suffix(".fast.m4a")
                ok = run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
                          "-i", str(a), "-filter:a", atempo_chain(ratio),
                          "-c:a", "aac", "-b:a", "128k", str(tmp)], 300).returncode == 0
                if ok and tmp.exists():
                    tmp.replace(a)
                    dur = probe(a)["duration"]
            target = min(avail, max(2.5, dur + tail))
        s2 = dict(s)
        s2["长"] = round(target, 3)
        out_segs.append(s2)
        targets.append(target)
    return out_segs, targets
