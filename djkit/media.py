"""djkit.media

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .core import CACHE_DIR, fmt_sec, human_size, probe_cached, prune_probe_cache, quick_hash


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".ts", ".m4v", ".wmv", ".webm", ".mpg", ".mpeg"}


EP_PATTERNS = [
    re.compile(r"^(?P<title>.+?)[\s_\-·]*第\s*(?P<ep>\d{1,4})\s*[集话話期]"),
    re.compile(r"^(?P<title>.+?)[\s_\-\[]*(?:EP|Ep|ep|E|e)\.?\s*(?P<ep>\d{1,4})\]?$"),
    re.compile(r"^(?P<title>.+?)[\s_\-]+(?P<ep>\d{1,4})(?:[\s_\-][^\\/]*)?$"),
    re.compile(r"^(?P<title>.+?)[\s_\-]+(?P<ep>\d{1,4})$"),
]


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 欧美剧 SxxExx 标记，之后的全是压制组信息
_SE_PATTERN = re.compile(r"^(?P<title>.+?)[\s._\-]*[Ss](?P<s>\d{1,2})[Ee](?P<ep>\d{1,3})(?!\d)")


_SEP_RUN = re.compile(r"[\s_\-·.]{2,}")


def clean_title(s: str) -> str:
    s = _ILLEGAL.sub("", s)
    s = _SEP_RUN.sub("_", s)
    return s.strip(" _-·.")[:60] or "未命名"


def parse_episode(path: Path) -> tuple[str, int | None]:
    """从文件名解析 (剧名, 集数)；解析不出剧名时退化为上级目录名"""
    stem = path.stem
    # 去掉常见的清晰度/平台后缀，避免污染剧名
    stem = re.sub(r"[\[\(]?(1080p|720p|4k|hd|高清|超清|无水印|完整版)[\]\)]?", "", stem, flags=re.I)
    # 去掉本工具切片产生的后缀（_p01_0s-5s），让切片产物能反解析回剧名/集数
    stem = re.sub(r"[\s_\-]*_?[pP]\d{1,2}[\s_\-]+\d+(?:\.\d+)?s-\d+(?:\.\d+)?s$", "", stem)
    stem = re.sub(r"[\s_\-]+\d+(?:\.\d+)?s-\d+(?:\.\d+)?s$", "", stem)

    # 欧美剧命名：Show.Name.2011.S01E01.BD1080P...  —— SxxExx 之后全是压制信息
    m = _SE_PATTERN.match(stem.strip())
    if m:
        title = m.group("title").replace(".", " ").replace("_", " ")
        title = re.sub(r"\s{2,}", " ", title).strip(" -._")
        if title:
            return clean_title(title), int(m.group("ep"))

    for pat in EP_PATTERNS:
        m = pat.match(stem.strip())
        if m:
            title = clean_title(m.group("title"))
            ep = int(m.group("ep"))
            if title and title != "未命名":
                return title, ep
    # 纯数字文件名：用父目录名当剧名
    if re.fullmatch(r"\d{1,4}", stem.strip()):
        return clean_title(path.parent.name), int(stem.strip())
    return clean_title(stem), None


@dataclass
class Clip:
    path: Path
    title: str
    ep: int | None
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    size: int = 0
    fp: str = ""
    dup_group: int = 0

    @property
    def res(self) -> str:
        return f"{self.width}x{self.height}" if self.width else "-"

    @property
    def ep_label(self) -> str:
        return f"EP{self.ep:03d}" if self.ep is not None else "-"


def iter_videos(root: Path) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS and CACHE_DIR not in p.parts:
            out.append(p)
    return sorted(out)


def collect(
    root: Path,
    jobs: int = 8,
    verbose: bool = True,
    need_hash: bool = False,
    use_cache: bool = True,
) -> list[Clip]:
    """扫描并读取元信息。

    need_hash=False 时跳过首尾 2MB 哈希——slice/fit 用不到指纹，省掉纯随机的磁盘读。
    use_cache 默认开，第二次处理同一批素材直接免掉 probe 的子进程开销。
    """
    files = iter_videos(root)
    cache = (root / CACHE_DIR) if use_cache else None
    if cache is not None:
        prune_probe_cache(cache)
    hits = 0
    if verbose:
        print(f"发现 {len(files)} 个视频文件，正在读取元信息…")

    def one(p: Path) -> Clip:
        title, ep = parse_episode(p)
        meta = probe_cached(p, cache)
        return Clip(
            path=p,
            title=title,
            ep=ep,
            duration=meta["duration"],
            width=meta["width"],
            height=meta["height"],
            fps=meta["fps"],
            has_audio=meta["has_audio"],
            size=p.stat().st_size,
            fp=quick_hash(p) if need_hash else "",
        )

    done = 0
    clips: list[Clip] = []
    show_progress = len(files) > 20
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for c in pool.map(one, files):
            clips.append(c)
            done += 1
            if verbose and show_progress and (done % 10 == 0 or done == len(files)):
                print(f"\r  进度 {done}/{len(files)}", end="", flush=True)
    if verbose and show_progress:
        print()

    # 查重标记（只有算了指纹才有意义）
    if need_hash:
        seen: dict[str, int] = {}
        gid = 0
        for c in clips:
            if c.fp in seen:
                c.dup_group = seen[c.fp]
            else:
                gid += 1
                seen[c.fp] = gid
                c.dup_group = gid
    return clips


def collect_args(args, root: Path, need_hash: bool = False) -> list[Clip]:
    """按命令行参数调用 collect（统一处理 --no-cache）"""
    return collect(
        root, args.jobs, need_hash=need_hash, use_cache=not getattr(args, "no_cache", False)
    )


def print_table(clips: list[Clip], limit: int = 40) -> None:
    dups = {c.dup_group for c in clips if sum(1 for x in clips if x.dup_group == c.dup_group) > 1}
    rows = []
    for c in clips[:limit]:
        rows.append(
            [
                c.path.name[:34],
                c.title[:16],
                c.ep_label,
                fmt_sec(c.duration),
                c.res,
                human_size(c.size),
                "重复" if c.dup_group in dups else "",
            ]
        )
    if rows:
        from tabulate import tabulate

        print(
            tabulate(
                rows,
                headers=["文件", "剧名", "集", "时长", "分辨率", "大小", "标记"],
                tablefmt="simple",
                disable_numparse=True,
            )
        )
    if len(clips) > limit:
        print(f"… 其余 {len(clips) - limit} 条见 CSV")


def cmd_scan(args) -> int:
    root = Path(args.dir).expanduser().resolve()
    if not root.exists():
        print(f"目录不存在：{root}")
        return 2
    clips = collect_args(args, root, need_hash=True)
    if not clips:
        print("没有找到视频文件。")
        return 1

    total = sum(c.duration for c in clips)
    dup_files = len(clips) - len({c.fp for c in clips})
    titles = sorted({c.title for c in clips})
    no_ep = [c for c in clips if c.ep is None]
    mismatch = [c for c in clips if c.width and c.height and c.width > c.height]

    print_table(clips)

    out = Path(args.out) if args.out else root / "_素材清单.csv"
    write_csv(clips, out)
    print(f"\n清单已保存：{out}")

    print("\n—— 概览 ——")
    print(f"文件数    {len(clips)}（重复 {dup_files} 个）")
    print(f"剧目数    {len(titles)}")
    print(f"总时长    {fmt_sec(total)}")
    print(f"总体积    {human_size(sum(c.size for c in clips))}")
    if no_ep:
        print(f"⚠ 集数未识别 {len(no_ep)} 个，例如：{no_ep[0].path.name}")
    if mismatch:
        print(f"⚠ 横屏素材 {len(mismatch)} 个（推广一般需要竖屏，可先跑 fit 转换）")
    if dup_files:
        print("⚠ 存在重复文件，可用清单里「指纹」列核对")
    return 0


def write_csv(clips: list[Clip], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["剧名", "集数", "时长(秒)", "分辨率", "大小(MB)", "指纹", "相对路径"])
        for c in clips:
            w.writerow(
                [
                    c.title,
                    c.ep if c.ep is not None else "",
                    round(c.duration, 2),
                    c.res,
                    round(c.size / 1048576, 2),
                    c.fp,
                    str(c.path),
                ]
            )


def cmd_rename(args) -> int:
    root = Path(args.dir).expanduser().resolve()
    clips = collect_args(args, root)
    if not clips:
        print("没有找到视频文件。")
        return 1

    plan: list[tuple[Path, Path]] = []
    for c in clips:
        if c.ep is not None:
            new = c.path.with_name(f"{c.title}_{c.ep:03d}{c.path.suffix.lower()}")
        else:
            new = c.path.with_name(f"{c.title}{c.path.suffix.lower()}")
        if new != c.path:
            plan.append((c.path, new))

    if not plan:
        print("文件名已经是规范格式，无需改动。")
        return 0

    # 冲突检测（含大小写不敏感）
    existing = {str(p).lower() for p in root.rglob("*") if p.is_file()}
    used: dict[str, int] = {}
    final: list[tuple[Path, Path]] = []
    for src, dst in plan:
        key = str(dst).lower()
        src_key = str(src).lower()
        if key != src_key and key in existing:
            stem, suf = dst.stem, dst.suffix
            n = used.get(key, 1) + 1
            used[key] = n
            dst = dst.with_name(f"{stem}_dup{n}{suf}")
        final.append((src, dst))

    print(f"重命名计划（{len(final)} 个）：")
    for src, dst in final[:20]:
        print(f"  {src.name}\n    -> {dst.name}")
    if len(final) > 20:
        print(f"  … 其余 {len(final) - 20} 个省略")

    if not args.apply:
        print("\n当前是预演模式，未改动任何文件。确认无误后加 --apply 执行。")
        return 0

    undo = []
    ok = 0
    for src, dst in final:
        try:
            src.rename(dst)
            undo.append({"from": str(dst), "to": str(src)})
            ok += 1
        except Exception as e:
            print(f"  失败：{src.name} -> {e}")
    # 记录撤销信息
    cache = root / CACHE_DIR
    cache.mkdir(exist_ok=True)
    (cache / f"rename_undo_{time.strftime('%Y%m%d_%H%M%S')}.json").write_text(
        json.dumps(undo, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n完成 {ok}/{len(final)} 个。撤销记录已存入 {cache}")
    return 0
