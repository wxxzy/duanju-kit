"""djkit - 短剧二创 / 推广素材工具

面向「已获得授权素材」的短剧推广 / 二创 / 影视解说工作流：
  扫描归档 -> 规范命名 -> 批量切片 -> 成片合成 -> 生成投放台账
  长片场景：高光检测 -> 转写 -> 文案 -> 配音 -> 多段拼接

成片合成（fit）在一条 ffmpeg 命令里完成：改规格 + 烧字幕 + 花字 + 变速 + BGM 混音
+ 拼片头片尾 + 出封面 + 多平台规格，全程只编码一次。

注意：本工具只处理你本地已有的、有权使用的素材文件，不包含任何下载/抓取功能。

用法：
  python -m djkit doctor
  python -m djkit scan    素材库/
  python -m djkit slice   素材库/ --mode head --head 20 -o 切片/
  python -m djkit fit     切片/ --burn-sub --title "标题" --bgm 音乐.mp3 --duck \
                          --intro 片头.mp4 --outro 片尾.mp4 --cover auto \
                          --specs 1080x1920,1080x1440 -o 成片/
  python -m djkit selftest .

包结构：
  core       子进程 / probe / 缓存 / 通用工具
  media      命名识别、扫描、Clip
  subtitle   烧字幕（编码、平移、样式）
  title      花字 / 标题条
  compose    成片合成（规格、字幕、变速、BGM、片头尾）
  slicer     批量切片
  assemble   多段拼接
  hot        高光片段检测
  asr/tts/script   转写 / 配音 / 文案
  recipe     配方流水线
  cli        命令行入口
"""

from .core import (  # noqa: F401  常用接口在包顶层再导出，方便写脚本时直接 import djkit
    CACHE_DIR,
    abs_path,
    as_bool,
    as_list,
    ffmpeg_exe,
    ff_path_quoted,
    fmt_sec,
    human_size,
    probe,
    probe_cached,
    rg,
    run,
)

__version__ = "0.11.0"
