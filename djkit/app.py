"""djkit.app — 桌面客户端（pywebview 外壳 + 本地服务）

为什么不直接用 pywebview 加载 `file://` 页面：

  * 实测 WebView2 会把本地视频判成 `media error 4`（源不受支持）——`<video src="file://…">`
    直接被拦。而页面用 `html=` 注入时是「不透明源」，连读文件都不允许。
  * 隐藏窗口下桥接和媒体管线都会被节流，没法拿它做无头自动化测试。
  * WebView2 子进程会残留（实测反复开窗后留下 12 个），桥接从此不再就绪。

所以架构固定成两层：

    浏览器内核（系统 WebView2） ──HTTP──> ThreadingHTTPServer（本机 127.0.0.1）
                                            ├── 页面 /app.js /style.css
                                            ├── /video 支持 Range，能拖进度条
                                            └── /api/* 长任务进工作线程

外壳只负责「开一个窗口指向 127.0.0.1」，这也带来一个白送的好处：
**服务出问题时，用户在浏览器里打开同一个地址照样能用**。

相比浏览器版，客户端多两件事：原生文件选择框、双击启动。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

from . import gui_server as gs

# 单实例用的互斥端口。pywebview 没有单实例机制，两个窗口会抢同一份 STATE。
LOCK_PORT = 8971
_LOCK_SOCK: socket.socket | None = None

# 窗口对象必须放模块级，不能挂在 Api 上。
# 挂上去会被 JS 桥当成可访问属性递归遍历，日志刷屏
# 「window.native.ActiveForm.ModifierKeys.A.A.A…: maximum recursion depth exceeded」，桥接直接哑掉。
_WIN = None

TITLE = "djkit 解说工坊"
WIN_W, WIN_H = 1380, 900
MIN_W, MIN_H = 1040, 700


def _log(msg: str) -> None:
    print(f"[app] {msg}", flush=True)


def acquire_lock() -> bool:
    """占住一个本地端口当互斥锁。第二次双击直接退出，不再开第二个窗口。"""
    global _LOCK_SOCK
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _LOCK_SOCK = s
    return True


WEBVIEW_CACHE_DIRS = (
    ("EBWebView", "Default", "Cache"),
    ("EBWebView", "Default", "Code Cache"),
    ("EBWebView", "Default", "GPUCache"),
    ("EBWebView", "Default", "DawnCache"),
    ("EBWebView", "Default", "Service Worker", "CacheStorage"),
)


def trim_webview_cache(limit_mb: float = 150.0) -> None:
    """窗口起来之前清一次页面内核缓存。

    为什么不在界面上做按钮：WebView2 一启动就把这些文件锁死了，
    清理必然失败。而它攒的缓存实测能到 100MB+，只能赶在开窗前删。
    只删纯缓存目录，不碰 Local Storage / IndexedDB —— 那里面可能有偏好设置。
    """
    base = gs.OUT_ROOT / "_webview"
    if not base.exists():
        return
    total = 0
    targets: list[Path] = []
    for parts in WEBVIEW_CACHE_DIRS:
        d = base.joinpath(*parts)
        if not d.exists():
            continue
        size = 0
        for f in d.rglob("*"):
            try:
                if f.is_file():
                    size += f.stat().st_size
            except OSError:
                continue
        total += size
        targets.append(d)
    if total < limit_mb * 1048576:
        return
    freed = 0
    for d in targets:
        for f in sorted(d.rglob("*"), reverse=True):
            try:
                if f.is_file():
                    freed += f.stat().st_size
                    f.unlink()
                else:
                    f.rmdir()
            except OSError:
                continue          # 被别的进程占着就跳过，下次再说
    _log(f"页面缓存 {total / 1048576:.0f}MB → 清理约 {freed / 1048576:.0f}MB")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    """暴露给前端的 Python 方法（`window.pywebview.api.xxx()`）

    只放方法，不放任何对象属性 —— 见上面 _WIN 的注释。
    """

    # 前端启动时探一次，用来确认「壳」和「页面」都活着
    def ping(self) -> dict:
        _log(f"桥接就绪，页面已连上（pid {os.getpid()}）")
        return {"ok": True, "python": sys.executable, "desktop": True}

    def pick_video(self) -> str:
        """原生文件选择框。浏览器版没有这个能力，只能粘路径。"""
        import webview

        if _WIN is None:
            return ""
        try:
            picked = _WIN.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("视频 (*.mp4;*.mkv;*.mov;*.ts;*.avi;*.flv;*.wmv)",
                            "所有文件 (*.*)"),
            )
        except Exception as e:  # noqa: BLE001
            _log(f"文件选择失败：{e}")
            return ""
        if not picked:
            return ""
        p = picked[0] if isinstance(picked, (list, tuple)) else picked
        _log(f"选中 {p}")
        return str(p)

    def pick_folder(self) -> str:
        """原生目录选择框，用于整季批量。"""
        import webview

        if _WIN is None:
            return ""
        try:
            picked = _WIN.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as e:  # noqa: BLE001
            _log(f"目录选择失败：{e}")
            return ""
        if not picked:
            return ""
        p = picked[0] if isinstance(picked, (list, tuple)) else picked
        _log(f"选中目录 {p}")
        return str(p)

    def pick_audio(self) -> str:
        """原生文件选择框，选 BGM。"""
        import webview

        if _WIN is None:
            return ""
        try:
            picked = _WIN.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=False,
                file_types=("音频 (*.mp3;*.m4a;*.wav;*.aac;*.flac;*.ogg)",
                            "所有文件 (*.*)"),
            )
        except Exception as e:  # noqa: BLE001
            _log(f"音频选择失败：{e}")
            return ""
        if not picked:
            return ""
        p = picked[0] if isinstance(picked, (list, tuple)) else picked
        _log(f"选中音频 {p}")
        return str(p)

    def quit(self) -> None:
        """前端「退出」按钮调用：先关窗，再走正常的收尾流程。"""
        if _WIN:
            _WIN.destroy()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    debug = "--debug" in argv

    # pythonw 启动时 sys.stdout / sys.stderr 是 None，任何 print 都会抛
    # AttributeError: 'NoneType' object has no attribute 'write' ——
    # 表现就是「双击没反应、进程瞬间消失」。这里把输出接到日志文件。
    if sys.stdout is None or sys.stderr is None:
        try:
            gs.OUT_ROOT.mkdir(parents=True, exist_ok=True)
            f = open(gs.OUT_ROOT / "app.log", "a", encoding="utf-8", buffering=1)
        except OSError:
            f = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f
        print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====")

    try:
        import webview
    except ImportError:
        print("缺少 pywebview：")
        print("  pip install -i https://mirrors.aliyun.com/pypi/simple/ pywebview")
        print("（不需要 PySide6 / Electron，用的是系统自带的 Edge WebView2）")
        return 2

    if not acquire_lock():
        print("已经有一个窗口在跑了。")
        print("如果之前异常退出留下的，等几秒再试，或重启一次即可。")
        return 0

    # 必须在 create_window 之前：WebView2 一起来这些文件就全锁住了
    trim_webview_cache()

    # 依赖预检：缺 ffmpeg 时直接说清楚，别让每个请求各错一次
    problems = gs.preflight()
    if problems:
        print("启动失败，环境缺东西：")
        for x in problems:
            print("  - " + x)
        print(f"\n当前解释器：{sys.executable}")
        print("用项目自带的启动脚本（已绑定装好依赖的环境）：启动客户端.bat")
        return 2

    port = free_port()
    try:
        srv = gs.make_server("127.0.0.1", port)
    except OSError as e:
        print(f"起服务失败（端口 {port}）：{e}")
        return 2
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    _log(f"服务已起：{url}")

    global _WIN
    api = Api()
    window = webview.create_window(
        TITLE,
        url=url,
        js_api=api,
        width=WIN_W,
        height=WIN_H,
        min_size=(MIN_W, MIN_H),
        background_color="#14161a",   # 和页面底色一致，开窗不白闪
        text_select=False,
        confirm_close=False,
    )
    _WIN = window

    # 关窗前先把服务停掉，避免端口悬空
    def _on_closed(*_args) -> None:
        _log("窗口已关闭")
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:  # noqa: BLE001
            pass
        sys.stdout.flush()
        # WebView2 的子进程不一定跟着退，直接结束进程最干净。
        # 不这么做的话，反复开关几次就会出现「桥接再也不就绪」。
        os._exit(0)

    try:
        window.events.closed += _on_closed
    except Exception:  # noqa: BLE001
        pass

    try:
        webview.start(
            gui="edgechromium",
            debug=debug,
            private_mode=False,          # 保留 localStorage，刷新不丢偏好
            storage_path=str(gs.OUT_ROOT / "_webview"),
        )
    except TypeError:
        # 老版本没有 private_mode/storage_path
        webview.start(debug=debug)
    except Exception as e:  # noqa: BLE001
        _log(f"窗口启动失败：{e}")
        print("如果提示 WebView2 相关错误，装一下运行时：")
        print("  https://developer.microsoft.com/microsoft-edge/webview2/")
        return 2
    finally:
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass

    _on_closed()
    return 0
