"""djkit.title

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from dataclasses import dataclass, field

from .core import ff_path_quoted
from .media import Clip, clean_title


TITLE_STYLES = {
    "hook": {"color": "white", "border": "black", "box": None},
    "bar": {"color": "white", "border": "black", "box": ("black", 0.55)},
    "yellow": {"color": "#FFE23D", "border": "black", "box": None},
    "redbar": {"color": "white", "border": "black", "box": ("#C81E2B", 0.88)},
}


_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]


def default_font() -> str | None:
    """找一个能显示中文的字体文件。drawtext 用 font=名称在 Windows 上常常解析不到，必须给文件。"""
    for c in _FONT_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def _color(c: str) -> str:
    """ffmpeg 的十六进制颜色要写 0xRRGGBB，顺手把 # 转掉"""
    c = c.strip()
    return "0x" + c[1:] if c.startswith("#") else c


@dataclass
class TitleCfg:
    """花字配置。文本按素材逐条解析，每条可以有不同标题。"""

    style: str = "hook"
    start: float = 0.3
    dur: float = 3.0
    size: float = 96.0
    pos: str = "top"
    y: float | None = None
    color: str | None = None
    border: float | None = None
    fade: float = 0.3
    font: str | None = None
    max_lines: int = 3
    fixed: str | None = None
    tpl: str | None = None
    texts: dict | None = None

    def text_for(self, c: Clip) -> str | None:
        txt = None
        if self.texts:
            txt = self.texts.get((c.title, c.ep))
            if txt is None:
                txt = self.texts.get((c.title, None))
        elif self.tpl:
            txt = (
                self.tpl.replace("{剧名}", c.title)
                .replace("{集}", c.ep_label)
                .replace("{集数}", str(c.ep) if c.ep is not None else "")
                .replace("{文件}", c.path.stem)
            )
        else:
            txt = self.fixed
        # 允许在标题里用 \n 手动断行，精确控制排版
        return txt.replace("\\n", "\n") if txt else txt


def load_title_csv(path: Path) -> dict:
    """标题表：剧名,集数,标题（两列时按 剧名,标题 处理）"""
    out: dict = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            row = [x.strip() for x in row]
            if not row or not row[0]:
                continue
            if len(row) >= 3 and row[2]:
                name, ep, txt = row[0], row[1], row[2]
            elif len(row) == 2:
                name, ep, txt = row[0], "", row[1]
            else:
                continue
            key = int(ep) if ep.isdigit() else None
            out[(clean_title(name), key)] = txt
    return out


_WRAP_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9%\.\!\?\-\+/]*|\s+|.")


def _text_width(s: str, font, fs: int) -> float:
    try:
        b = font.getbbox(s)
        return float(b[2] - b[0])
    except Exception:
        # 估算：中文按 1em，ASCII 按 0.55em
        return sum(fs if ord(ch) > 0x2E80 else fs * 0.55 for ch in s)


def wrap_title(text: str, max_w: int, font_path: str | None, fs: int) -> list[str]:
    """按像素宽度断行。drawtext 不会自动换行，长中文标题一定会冲出画面。"""
    font = None
    if font_path:
        try:
            from PIL import ImageFont

            font = ImageFont.truetype(font_path, fs)
        except Exception:
            font = None
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            lines.append("")
            continue
        cur = ""
        for tok in _WRAP_TOKEN.findall(raw):
            # 单个 token 比整行还宽（比如 xxx.yyy.z.1080p 这类没有空格的长串），
            # 只能按字符硬断，否则整行溢出画面
            if _text_width(tok, font, fs) > max_w:
                for ch in tok:
                    if cur and _text_width(cur + ch, font, fs) > max_w:
                        lines.append(cur.rstrip())
                        cur = ch
                    else:
                        cur += ch
                continue
            cand = cur + tok
            if cur and _text_width(cand, font, fs) > max_w:
                lines.append(cur.rstrip())
                cur = tok.lstrip()
            else:
                cur = cand
        if cur.strip():
            lines.append(cur.rstrip())
    return lines or [text]


def fix_orphan(lines: list[str]) -> list[str]:
    """把末行的孤字（1~2 个字）从上一行挪字下来，避免最后一排只剩一个字"""
    lines = list(lines)
    guard = 0
    while len(lines) >= 2 and len(lines[-1]) <= 2 and len(lines[-2]) > 3 and guard < 6:
        lines[-1] = lines[-2][-1] + lines[-1]
        lines[-2] = lines[-2][:-1]
        guard += 1
    return lines


def wrap_and_fit(
    text: str, out_w: int, out_h: int, cfg: "TitleCfg", font_path: str | None
) -> tuple[list[str], int]:
    """换行 + 行数超限时逐级缩小字号，返回 (行列表, 实际字号)"""
    k = out_h / 1920.0
    fs = max(10, round(cfg.size * k))
    max_w = int(out_w * 0.86)
    max_lines = max(1, cfg.max_lines)

    lines = wrap_title(text, max_w, font_path, fs)
    lines = fix_orphan(lines)

    guard = 0
    while len(lines) > max_lines and fs > 18 and guard < 8:
        fs = max(18, int(fs * 0.85))
        lines = wrap_title(text, max_w, font_path, fs)
        lines = fix_orphan(lines)
        guard += 1
    return lines, fs


def build_title_filter(
    text: str, out_w: int, out_h: int, cfg: TitleCfg, cache: Path, key: str
) -> str:
    """生成花字滤镜：底条（drawbox）+ 逐行文字（多条 drawtext）。

    两个关键决定：
    1. 文字一律写文件用 textfile 传，不拼进滤镜串——中文标点、引号、% 、冒号全不用转义。
    2. **每行单独一条 drawtext，行距自己算**。实测 msyh/simhei 在 drawtext 里的默认行高
       是字号的 2.3~2.7 倍，line_spacing 只能在此基础上叠加，压不下去；
       逐行绘制才能让「文字块高度」和「底条高度」严格对齐。
    """
    font_path = cfg.font if (cfg.font and Path(cfg.font).exists()) else default_font()
    lines, fs = wrap_and_fit(text, out_w, out_h, cfg, font_path)
    lines = [ln for ln in lines if ln.strip()] or [text]

    border = cfg.border if cfg.border is not None else max(2, round(fs * 0.07))
    st = TITLE_STYLES.get(cfg.style, TITLE_STYLES["hook"])
    color = _color(cfg.color) if cfg.color else _color(st["color"])

    s = max(0.0, cfg.start)
    if cfg.dur > 0:
        e = s + cfg.dur
        enable = f"between(t,{s:.3f},{e:.3f})"
    else:
        e = s + 1e6
        enable = ""

    pad = round(fs * 0.30)
    pitch = round(fs * 1.28)  # 自定行距，不用字体度量
    block_h = pitch * (len(lines) - 1) + fs + pad * 2

    # 文字块与底条各自的纵向基准。
    # 注意：drawbox 的表达式里没有 h/w 变量，只有 ih/iw；drawtext 才有 h/w。
    if cfg.y is not None:
        frac = max(0.0, min(0.9, cfg.y))
        band_y, text_y0 = f"ih*{frac:.4f}-{pad}", f"h*{frac:.4f}"
    elif cfg.pos == "center":
        band_y = f"(ih-{block_h})/2"
        text_y0 = f"(h-{block_h})/2+{pad}"
    elif cfg.pos == "bottom":
        off = round(out_h * 0.18)
        band_y = f"ih-{block_h}-{off}"
        text_y0 = f"h-{block_h}-{off}+{pad}"
    else:  # top
        top_px = round(out_h * 0.10)
        band_y, text_y0 = str(top_px - pad), str(top_px)

    filters: list[str] = []

    # 标题条：整幅通栏
    if st["box"]:
        bc, ba = st["box"]
        band = f"drawbox=x=0:y={band_y}:w=iw:h={block_h}:color={_color(bc)}@{ba}:t=fill"
        if enable:
            band += f":enable='{enable}'"
        filters.append(band)

    tdir = cache / "titles"
    tdir.mkdir(parents=True, exist_ok=True)

    fade = min(cfg.fade, max(0.001, (e - s) / 3)) if cfg.fade > 0 else 0.0
    alpha = ""
    if fade > 0:
        alpha = (
            f":alpha='if(lt(t,{s:.3f}),0,"
            f"if(lt(t,{s + fade:.3f}),(t-{s:.3f})/{fade:.3f},"
            f"if(lt(t,{e - fade:.3f}),1,"
            f"if(lt(t,{e:.3f}),({e:.3f}-t)/{fade:.3f},0))))'"
        )

    font_opt = ""
    if cfg.font and Path(cfg.font).exists():
        font_opt = f":fontfile={ff_path_quoted(cfg.font)}"
    elif cfg.font:
        font_opt = f":font='{cfg.font}'"
    elif font_path:
        font_opt = f":fontfile={ff_path_quoted(font_path)}"

    for i, line in enumerate(lines):
        tf = tdir / f"{key}_{i}.txt"
        tf.write_text(line, encoding="utf-8")
        y = text_y0 if i == 0 else f"{text_y0}+{i * pitch}"
        dt = (
            f"drawtext=textfile={ff_path_quoted(tf)}:expansion=none"
            f":fontsize={fs}:fontcolor={color}"
            f":borderw={round(border)}:bordercolor={_color(st['border'])}"
            f":x=(w-text_w)/2:y={y}"
            + (f":enable='{enable}'" if enable else "")
            + alpha
            + font_opt
        )
        filters.append(dt)

    return ",".join(filters)
