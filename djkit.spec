# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（one-folder）

为什么用 one-folder 而不是 one-file：
  * one-file 每次启动都要把 600MB+ 解压到临时目录，冷启动 20 秒起，
    而用户的素材转码本来就在等 I/O，不能把时间浪费在这。
  * one-file 的临时目录路径每次都变，webview 的 storage_path 跟着变，
    等于每次启动都丢本地状态。

打包命令：pyinstaller djkit.spec --noconfirm
产物：dist/djkit/djkit.exe
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

# ---------- 数据 ----------
datas = [
    ("djkit/web", "djkit/web"),           # 页面三件套，gui_server 按 HERE/web 找
]
# imageio-ffmpeg 把 ffmpeg.exe 放在包内的 binaries/ 下，不走常规二进制收集
datas += collect_data_files("imageio_ffmpeg")
datas += collect_data_files("certifi")          # edge-tts 走 https 需要根证书
datas += collect_data_files("edge_tts")

# ---------- 隐式导入 ----------
hidden = []
# pywebview 的各平台后端是按字符串动态加载的，静态分析看不到
hidden += [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "clr", "pythonnet",
]
# 我们自己的模块：series/asr 等有一堆函数内 import，静态扫描未必全
hidden += collect_submodules("djkit")
# 转写 / 合成 / 表格 链条上全是延迟 import
hidden += [
    "faster_whisper", "ctranslate2", "tokenizers", "huggingface_hub",
    "onnxruntime", "onnxruntime.capi", "onnxruntime.capi._pybind_state",
    "edge_tts", "imageio_ffmpeg",
    "openpyxl", "openpyxl.styles", "openpyxl.utils",
    "yaml",
]

# 明确排除：这些一进来体积翻倍，而且本项目根本不用
excludes = [
    "PySide6", "PySide2", "PyQt5", "PyQt6", "shiboken6",
    "tkinter", "matplotlib", "pandas", "scipy", "IPython",
    "pytest", "sphinx", "notebook", "jupyter",
]

a = Analysis(
    ["launch.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="djkit",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 压 dll 容易把 ctranslate2 压坏，别用
    console=False,             # 双击不弹黑框；出错信息走 gui_out/app.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="djkit",
)
