"""djkit.cli

由早期单文件版本拆分而来，逻辑保持一致。
"""
from __future__ import annotations

import argparse
import sys

from .asr import cmd_asr
from .assemble import cmd_assemble
from .compose import cmd_fit
from .hot import cmd_hot
from .media import cmd_rename, cmd_scan
from .recipe import cmd_run
from .report import cmd_report
from .script import cmd_script
from .selftest import cmd_doctor, cmd_selftest
from .series import cmd_series
from .slicer import cmd_slice
from .subtitle import SUB_STYLE_PRESETS
from .title import TITLE_STYLES
from .tts import cmd_tts


def cmd_app(args) -> int:
    """桌面客户端：pywebview 外壳 + 本地服务。没装 pywebview 时给安装提示。"""
    from .app import main as app_main

    return app_main(list(getattr(args, "rest", []) or []))


def cmd_gui(args) -> int:
    """本地网页界面。延迟导入，不开界面时不加载服务模块。"""
    from .gui_server import serve

    return serve(port=args.port, open_browser=not args.no_browser)


def add_sub_args(p: argparse.ArgumentParser) -> None:
    """slice / fit 共用的字幕参数"""
    g = p.add_argument_group("字幕烧录")
    g.add_argument("--burn-sub", action="store_true", help="把字幕烧进画面")
    g.add_argument("--sub-dir", nargs="+", metavar="DIR",
                   help="字幕所在目录（可给多个），默认在素材目录里递归查找")
    g.add_argument("--sub-style", choices=list(SUB_STYLE_PRESETS), default="default",
                   help="字幕样式预设：" + " / ".join(SUB_STYLE_PRESETS))
    g.add_argument("--sub-size", type=float, default=None,
                   help="字号像素（相对输出画面高度，默认 64px@1920）")
    g.add_argument("--sub-font", default=None, help="字体名，默认微软雅黑")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="djkit",
        description="短剧二创 / 推广素材工具：扫描归档 / 规范命名 / 批量切片 / 烧字幕 / 拼片头尾 / 生成台账",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python -m djkit scan 素材库/\n"
               "  python -m djkit slice 素材库/ --mode head --head 20 -o 切片/\n"
               "  python -m djkit fit 切片/ --burn-sub --sub-dir 字幕/ --intro 片头.mp4 -o 成片/\n",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="扫描素材，识别剧名/集数，导出清单")
    s.add_argument("dir")
    s.add_argument("-o", "--out", help="CSV 输出路径")
    s.add_argument("-j", "--jobs", type=int, default=8)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("rename", help="批量规范命名为 剧名_001.mp4（默认预演）")
    s.add_argument("dir")
    s.add_argument("--apply", action="store_true", help="真正执行重命名")
    s.add_argument("-j", "--jobs", type=int, default=8)
    s.set_defaults(func=cmd_rename)

    s = sub.add_parser("slice", help="批量切片（推广素材核心步骤）")
    s.add_argument("dir")
    s.add_argument("-o", "--out")
    s.add_argument("--mode", choices=["head", "even", "ranges"], default="head",
                   help="head=每集取开头N秒；even=每N秒一段；ranges=指定区间")
    s.add_argument("--head", type=float, default=20, help="mode=head 时的秒数")
    s.add_argument("--seg", type=float, default=30, help="mode=even 时的段长秒数")
    s.add_argument("--ranges", default="0-15", help="mode=ranges 时，如 0-15,30-45")
    s.add_argument("--copy", action="store_true", help="直接流拷贝（快，但切点不一定精准）")
    s.add_argument("--crf", type=int, default=20, help="画质 18-28，越小越好越大越小体积")
    add_sub_args(s)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("-j", "--jobs", type=int, default=8)
    s.set_defaults(func=cmd_slice)

    s = sub.add_parser("fit", help="成片合成：规格 + 字幕 + 变速 + BGM + 片头尾 + 封面（一次编码）")
    s.add_argument("dir")
    s.add_argument("-o", "--out")
    s.add_argument("--size", default="1080x1920", help="单规格尺寸")
    s.add_argument("--specs", help="多规格批量出片，如 1080x1920,1080x1440 或 douyin,xiaohongshu")
    s.add_argument("--mode", choices=["blur", "pad", "crop"], default="blur",
                   help="blur=模糊背景填充；pad=黑边；crop=居中裁切")
    s.add_argument("--crf", type=int, default=20)
    s.add_argument("--fps", type=int, default=25)
    s.add_argument("--intro", help="片头视频路径（自动规整到成片规格，含补静音）")
    s.add_argument("--outro", help="片尾视频路径")

    g = s.add_argument_group("节奏与音频")
    g.add_argument("--speed", type=float, default=1.0, help="变速倍率，如 1.1；只作用于正片")
    g.add_argument("--bgm", help="背景音乐文件（自动循环到成片长度）")
    g.add_argument("--bgm-vol", type=float, default=0.25, help="BGM 音量 0~1，默认 0.25")
    g.add_argument("--bgm-fade", type=float, default=2.0, help="BGM 结尾淡出秒数，0 关闭")
    g.add_argument("--duck", action="store_true", help="人声出现时自动压低 BGM（闪避）")

    g = s.add_argument_group("花字 / 标题条")
    g.add_argument("--title", help="标题文字，所有素材统一用这一条")
    g.add_argument("--title-tpl", help="标题模板，可用 {剧名} {集} {集数} {文件}")
    g.add_argument("--title-file", help="标题表 CSV：剧名,集数,标题")
    g.add_argument("--title-style", choices=list(TITLE_STYLES), default="hook",
                   help="hook 白字黑边 / bar 半透明黑底条 / yellow 黄字 / redbar 红底白字")
    g.add_argument("--title-start", type=float, default=0.3, help="出现时间（相对正片，秒）")
    g.add_argument("--title-dur", type=float, default=3.0, help="持续秒数，0=全程")
    g.add_argument("--title-size", type=float, default=96, help="字号像素@1920，默认 96")
    g.add_argument("--title-pos", choices=["top", "center", "bottom"], default="top")
    g.add_argument("--title-y", type=float, help="自定义纵向位置 0~1，覆盖 --title-pos")
    g.add_argument("--title-color", help="覆盖文字颜色，如 #FFE23D")
    g.add_argument("--title-border", type=float, help="描边像素，默认按字号自动")
    g.add_argument("--title-fade", type=float, default=0.3, help="淡入淡出秒数")
    g.add_argument("--title-font", help="字体文件路径或字体名")

    g = s.add_argument_group("封面")
    g.add_argument("--cover", nargs="?", const="auto", default=None,
                   help="导出首图：auto（片头后1秒）或指定秒数")
    g.add_argument("--cover-sizes", help="额外封面尺寸，如 720x1280")

    add_sub_args(s)
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("-j", "--jobs", type=int, default=8)
    s.set_defaults(func=cmd_fit)

    s = sub.add_parser("report", help="生成 Excel 投放台账")
    s.add_argument("dir")
    s.add_argument("-o", "--out")
    s.add_argument("-j", "--jobs", type=int, default=8)
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("series", help="建剧集总纲（一次性读整季，供各集文案复用）")
    s.add_argument("dir", help="剧集目录（整季）或单集文件")
    s.add_argument("-o", "--out", help="总纲输出目录，默认素材目录")
    s.add_argument("--model", default="small", help="转写模型，默认 small")
    s.add_argument("--lang", default="", help="语言，如 en / zh")
    s.add_argument("--minutes", type=float, default=7,
                   help="每集采样多少分钟，默认 7（越多越准，越慢）")
    s.add_argument("--eps", type=int, default=0, help="只处理前 N 集，用于试跑")
    s.add_argument("--force", action="store_true", help="忽略缓存，重做并覆盖 总纲.md")
    s.add_argument("--remix", action="store_true",
                   help="只重汇总（跳过转写与摘要），调提示词时用")
    s.add_argument("--names", default="", metavar="原名=译名,...",
                   help="手工指定译名（优先级最高），如 \"Michael Burnham=迈克尔·伯纳姆,"
                        "USS Shenzhou=神舟号\"。语音识别把名字听错时用这个钉死")
    s.add_argument("--no-fix", action="store_true",
                   help="不做自动专名校正（默认会请模型把音近错拼改回来）")
    s.add_argument("--dry-run", action="store_true", help="只打印会采样哪几段")
    s.set_defaults(func=cmd_series)

    s = sub.add_parser("script", help="基于转写台词生成解说文案（LLM）")
    s.add_argument("transcript", help="asr 产出的 .txt 或 .json")
    s.add_argument("-o", "--out", help="文案输出目录，默认转写文件同级的 文案/")
    s.add_argument("--seconds", type=float, default=25, help="目标解说时长，默认 25 秒")
    s.add_argument("--style", default="克制的叙述，不煽情，偶尔一句冷判断",
                   help="文案风格描述")
    s.add_argument("--variants", type=int, default=1, help="生成几个版本（不同开场角度）")
    s.add_argument("--context", default="",
                   help="背景设定（剧名、人物关系、前情），能显著提升文案质量")
    s.add_argument("--start", default="", help="只取这段台词之后，如 36:40")
    s.add_argument("--end", default="", help="只取这段台词之前，如 38:00")
    s.add_argument("--config", help="LLM 配置 yaml，默认 ~/.djkit/llm.yaml")
    s.add_argument("--dry-run", action="store_true", help="只打印 prompt，不调用")
    s.set_defaults(func=cmd_script)

    s = sub.add_parser("asr", help="语音转写生成字幕（faster-whisper）")
    s.add_argument("input", help="视频或音频文件")
    s.add_argument("-o", "--out", help="输出 SRT 路径，同时生成 .txt 和 .json")
    s.add_argument("--model", default="small",
                   help="tiny/base/small/medium/large-v3，可加 .en 后缀，默认 small")
    s.add_argument("--lang", default="", help="语言代码如 en/zh，留空自动检测")
    s.add_argument("--device", default="cpu", help="cpu 或 cuda")
    s.add_argument("--compute", default="int8", help="int8 / float16 / float32")
    s.add_argument("--beam", type=int, default=5)
    s.add_argument("--no-vad", dest="vad", action="store_false", help="关闭静音切除")
    s.set_defaults(func=cmd_asr, vad=True)

    s = sub.add_parser("tts", help="文案转配音（edge-tts，无需 key）")
    s.add_argument("input", help="文案文本文件")
    s.add_argument("-o", "--out", help="输出 mp3")
    s.add_argument("--voice", default="zh-CN-YunxiNeural", help="音色，默认云希男声")
    s.add_argument("--rate", default="+0%", help="语速，如 +10%% / -5%%")
    s.add_argument("--volume", default="+0%")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_tts)

    s = sub.add_parser("hot", help="自动找高光片段，输出可喂给 assemble 的候选清单")
    s.add_argument("dir", help="视频文件或目录")
    s.add_argument("--top", type=int, default=10, help="每个文件取几段，默认 10")
    s.add_argument("--len", type=float, default=8.0, help="候选片段长度（秒），默认 8")
    s.add_argument("--min-gap", type=float, default=0,
                   help="两段最小间隔（秒），0=按片长自适应，默认自适应")
    s.add_argument("-o", "--out", help="候选清单 CSV 路径")
    s.add_argument("--work", help="临时文件目录")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_hot)

    s = sub.add_parser("assemble", help="按清单把多段素材拼成一条成片（解说/盘点用）")
    s.add_argument("plan", help="清单文件（.yaml 或 .csv）")
    s.add_argument("-o", "--out", help="输出文件，默认取清单里的「输出」")
    s.add_argument("--size", default="1080x1920")
    s.add_argument("--mode", choices=["blur", "pad", "crop"], default="blur")
    s.add_argument("--fps", type=int, default=25)
    s.add_argument("--crf", type=int, default=20)
    s.add_argument("--bgm", help="覆盖清单里的 BGM")
    s.add_argument("--cover", nargs="?", const="auto", default=None, help="抽首图")
    g = s.add_argument_group("字幕与花字默认样式")
    g.add_argument("--sub-style", choices=list(SUB_STYLE_PRESETS), default="default")
    g.add_argument("--title-style", choices=list(TITLE_STYLES), default="bar")
    g.add_argument("--title-start", type=float, default=0.2)
    g.add_argument("--title-dur", type=float, default=3.0)
    g.add_argument("--title-size", type=float, default=88)
    g.add_argument("--title-pos", choices=["top", "center", "bottom"], default="top")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_assemble)

    s = sub.add_parser("run", help="按配方文件一条命令跑完整条流水线（支持监听目录）")
    s.add_argument("recipe", help="配方文件（.yaml）")
    s.add_argument("--watch", nargs="?", type=int, const=60, default=None,
                   help="持续监听自动处理，可指定间隔秒数，默认 60")
    s.add_argument("--cycles", type=int, default=0, help="最多跑几轮，0=不限（配合 --watch）")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("-j", "--jobs", type=int, default=8)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("app", help="打开桌面客户端窗口（pywebview 外壳）")
    s.add_argument("rest", nargs="*", help="透传给客户端的参数，如 --debug")
    s.set_defaults(func=cmd_app)

    s = sub.add_parser("gui", help="打开图形界面（本地网页，浏览器里操作）")
    s.add_argument("--port", type=int, default=8770)
    s.add_argument("--no-browser", action="store_true", help="只启动服务，不自动开浏览器")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("doctor", help="检查 ffmpeg 与依赖")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("selftest", help="用合成视频跑通全流程，验证工具可用")
    s.add_argument("dir", nargs="?", default=".")
    s.add_argument("--live", action="store_true",
                   help="解说链用真实 LLM/TTS（默认用本地桩，不花钱不联网）")
    s.set_defaults(func=cmd_selftest)

    # 统一给需要扫描元信息的子命令加缓存开关
    for _name in ("scan", "rename", "slice", "fit", "report", "run"):
        sub.choices[_name].add_argument(
            "--no-cache", action="store_true", help="忽略元信息缓存，强制重新探测"
        )
    return p


def _init_console() -> None:
    """Windows 控制台编码修正：避免中文在 cmd / GBK 终端下乱码"""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    _init_console()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
