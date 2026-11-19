"""djkit.season —— 整季批量结果汇总

整季跑完，成片散在 `整季/1080x1920/*.mp4` 里，用户得自己去翻目录、一个个点开看。
这里把「这一季出了什么」压成一张表：每集时长 / 体积 / 有没有配音 / 有没有封面 / 文案字数。

设计取舍：
  * 元信息全部走 probe_cached（路径+大小+mtime 做 key），轮询不重复开 ffmpeg
  * 只读目录，不改任何文件；成片被手工删了就当它没出过
  * 只看「成片」这一层，`_工作` / `.djkit` / `封面` 一律不进去 —— 那是中间产物
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import CACHE_DIR, fmt_sec, probe_cached
from .media import VIDEO_EXTS, parse_episode


# 成片文件名形如 `Show.Name.S01E01..._解说.mp4` / `剧名_第1集_解说.mp4`
_SPEC_RE = re.compile(r"^(\d{3,4})x(\d{3,4})$")

# 这些目录名不是「一档画幅」，是中间工作区
_SKIP_DIRS = {CACHE_DIR, "_工作", "_work", "封面", "_thumbs"}


def _is_spec_dir(p: Path) -> bool:
    return bool(_SPEC_RE.match(p.name))


def _script_chars(work_dir: Path, stem: str) -> int:
    """从工作目录里读这一集文案的字数。读不到就返回 0，不报错。"""
    for name in ("文案/选定.txt", "文案_逐段.txt", "文案.txt"):
        f = work_dir / stem / name
        try:
            if f.is_file():
                return len(re.sub(r"\s", "", f.read_text(encoding="utf-8-sig")))
        except OSError:
            continue
    # 连续旁白那条链是个目录：文案/文案_A.txt
    d = work_dir / stem / "文案"
    if d.is_dir():
        for f in sorted(d.glob("*.txt")):
            try:
                return len(re.sub(r"\s", "", f.read_text(encoding="utf-8-sig")))
            except OSError:
                continue
    return 0


def scan_season(root: Path, spec: str = "") -> dict:
    """扫一遍整季输出目录，返回 {count, episodes[], specs[], total_mb, ...}

    spec 为空时，若有多档画幅默认只看人数最全的那一档（通常就是第一档）。
    """
    if not root.is_dir():
        # 字段必须给全：前端直接读 specs / total_mb / problems，
        # 少给一个就是 undefined，靠前端每处都判空太脆
        return {
            "exists": False, "root": str(root), "specs": [], "spec": "",
            "count": 0, "episodes": [], "total_mb": 0.0, "total_txt": "00:00",
            "problems": [], "ledger": False,
            "ledger_path": str(root / "_投放台账.xlsx"),
        }

    specs: list[str] = []
    flat: list[Path] = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and _is_spec_dir(p):
            specs.append(p.name)
    if specs:
        pool = [root / (spec if spec in specs else specs[0])]
    else:
        pool = [root]

    for base_dir in pool:
        for f in sorted(base_dir.rglob("*")):
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                continue
            if any(part in _SKIP_DIRS for part in f.parts):
                continue
            flat.append(f)

    cache = root / CACHE_DIR
    work = root / "_工作"
    eps: list[dict] = []
    total_mb = 0.0
    total_sec = 0.0
    for f in flat:
        try:
            st = f.stat()
        except OSError:
            continue
        m = probe_cached(f, cache)
        size_mb = round(st.st_size / 1048576, 1)
        total_mb += size_mb
        total_sec += m["duration"]
        title, ep = parse_episode(f)
        # 文案在 _工作/<素材 stem> 下，而素材 stem 不带 `_解说` 后缀
        stem = re.sub(r"_解说([_ ].*)?$", "", f.stem)
        cover_f = f.with_suffix(".jpg")
        has_cover = cover_f.is_file()
        eps.append({
            "path": str(f),
            "name": f.name,
            "ep": ep,
            "title": title,
            "dur": round(m["duration"], 2),
            "dur_txt": fmt_sec(m["duration"]),
            "size_mb": size_mb,
            "w": m["width"], "h": m["height"],
            "audio": bool(m["has_audio"]),
            # 封面路径由后端给，别让前端拿 .mp4 去 replace 猜 —— 成片是 .mov 时就猜错了
            "cover": has_cover,
            "cover_path": str(cover_f) if has_cover else "",
            "chars": _script_chars(work, stem),
        })

    # 按集数排，解析不出集数的排最后（按名字）
    eps.sort(key=lambda e: (e["ep"] is None, e["ep"] or 0, e["name"]))

    problems = []
    no_audio = [e for e in eps if not e["audio"]]
    no_cover = [e for e in eps if not e["cover"]]
    if no_audio:
        problems.append(f"{len(no_audio)} 集成片没有音轨")
    if no_cover:
        problems.append(f"{len(no_cover)} 集没有封面")
    if eps and len({e["ep"] for e in eps if e["ep"] is not None}) < len(eps):
        problems.append("有集数重复（可能同集跑了两次）")

    return {
        "exists": True,
        "root": str(root),
        "specs": specs,
        "spec": spec if spec in specs else (specs[0] if specs else ""),
        "count": len(eps),
        "episodes": eps,
        "total_mb": round(total_mb, 1),
        "total_txt": fmt_sec(total_sec),
        "problems": problems,
        "ledger": (root / "_投放台账.xlsx").is_file(),
        "ledger_path": str(root / "_投放台账.xlsx"),
    }
