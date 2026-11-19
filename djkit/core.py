"""djkit.core

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import hashlib
from pathlib import Path



CACHE_DIR = ".djkit"


def ffmpeg_exe() -> str:
    """按优先级解析 ffmpeg：环境变量 > PATH > imageio-ffmpeg 内置"""
    env = os.environ.get("DJKIT_FFMPEG")
    if env and Path(env).exists():
        return env
    which = shutil.which("ffmpeg")
    if which:
        return which
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise SystemExit(
        "找不到 ffmpeg。请任选一种方式：\n"
        "  1) pip install imageio-ffmpeg\n"
        "  2) 设置环境变量 DJKIT_FFMPEG 指向 ffmpeg.exe"
    )


# 客户端用 pythonw 启动，本身没有控制台。这时再拉 ffmpeg 这类控制台程序，
# Windows 会给它**单独分配一个新控制台窗口**——表现就是每点一步就弹个黑框。
# CREATE_NO_WINDOW 明确告诉系统「不要建控制台」。
# 在控制台里跑（python -m djkit ...）时加这个参数没有副作用。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def child_kwargs() -> dict:
    """给所有子进程调用用的通用参数：Windows 上不要弹窗。"""
    return {"creationflags": NO_WINDOW} if NO_WINDOW else {}


def run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, **child_kwargs()
    )


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def fmt_sec(s: float) -> str:
    """73.5 -> '01:13'"""
    s = int(round(s))
    return f"{s // 60:02d}:{s % 60:02d}"


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


_RES_RE = re.compile(r",\s*(\d{2,5})x(\d{2,5})[\s,\[]")


_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


def probe(path: Path) -> dict:
    """用 ffmpeg -i 读取元信息（不依赖 ffprobe）"""
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0, "vcodec": "", "has_audio": False}
    try:
        r = run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], timeout=60)
    except Exception:
        return info
    text = (r.stderr or "") + (r.stdout or "")

    m = _DUR_RE.search(text)
    if m:
        h, mi, se = m.groups()
        info["duration"] = int(h) * 3600 + int(mi) * 60 + float(se)

    for line in text.splitlines():
        if "Stream #" in line and "Video:" in line:
            mr = _RES_RE.search(line)
            if mr:
                info["width"], info["height"] = int(mr.group(1)), int(mr.group(2))
            mf = _FPS_RE.search(line)
            if mf:
                info["fps"] = float(mf.group(1))
            vm = re.search(r"Video:\s*([a-zA-Z0-9_]+)", line)
            if vm:
                info["vcodec"] = vm.group(1)
            break
    info["has_audio"] = "Audio:" in text
    # 竖屏素材常见 rotate 元数据
    if "rotate" in text or "displaymatrix" in text.lower():
        info["rotated"] = True
    return info


def quick_hash(path: Path, chunk: int = 1 << 20) -> str:
    """首尾各取 1MB 做指纹，用于快速查重（比全量 md5 快得多）"""
    h = hashlib.md5()
    size = path.stat().st_size
    with path.open("rb") as f:
        h.update(f.read(chunk))
        if size > chunk * 2:
            f.seek(-chunk, os.SEEK_END)
            h.update(f.read(chunk))
    h.update(str(size).encode())
    return h.hexdigest()[:12]


def probe_cached(path: Path, cache: Path | None) -> dict:
    """带缓存的 probe。

    缓存 key 用 路径+大小+mtime，文件一改自动失效，不存在脏读。
    省掉的是每文件一次 ffmpeg 子进程（实测 ~146ms）。
    """
    if cache is None:
        return probe(path)
    try:
        st = path.stat()
    except OSError:
        return probe(path)
    key = hashlib.md5(f"{path}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")).hexdigest()[:16]
    f = cache / "probe" / f"{key}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    info = probe(path)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return info


def prune_probe_cache(cache: Path, keep_days: int = 30, limit: int = 20000) -> None:
    """缓存目录膨胀时清掉老条目，避免无限增长"""
    d = cache / "probe"
    if not d.exists():
        return
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return
    if len(files) <= limit:
        return
    cutoff = time.time() - keep_days * 86400
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def ff_path_quoted(p) -> str:
    """把路径转成 ffmpeg 滤镜参数里可用的形式。

    Windows 路径必须转正斜杠、对盘符冒号加单反斜杠转义、整体用单引号包住。
    实测这是唯一能同时兼容空格、中文、方括号的写法（双反斜杠或裸路径都会挂）。
    """
    s = str(p).replace("\\", "/").replace("'", "")
    return "'" + s.replace(":", "\\:") + "'"


try:
    import yaml
except ImportError:  # 配方是可选能力，缺了也不影响单命令
    yaml = None


def rg(d: dict, *keys, default=None):
    """按多个可能的键名取值（中英文都认）"""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def as_bool(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "是", "开")


def as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [x.strip() for x in str(v).replace("，", ",").split(",") if x.strip()]


def abs_path(base: Path, v) -> str | None:
    """配方里的相对路径一律相对配方文件所在目录"""
    if v is None or str(v).strip() == "":
        return None
    p = Path(str(v)).expanduser()
    return str(p if p.is_absolute() else (base / p).resolve())
