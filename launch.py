"""PyInstaller 打包入口。

单独放一个文件在外面，而不是（不能）直接打包 djkit/__main__.py：
那会走 cli，默认行为是打印帮助。客户端要的是 pywebview 窗口。
"""
import multiprocessing
import sys
import time


def _attach_log() -> None:
    """把 stdout/stderr 接到 exe 旁边的 gui_out/app.log。

    打包成窗口程序后 stdout 已经不是控制台了，PyInstaller 各版本处理不一：
    有的给 None，有的给一个「能写但谁都看不到」的对象。所以这里不判断是不是 None，
    一律换成文件句柄——双击启动失败时，这个日志是唯一能查的东西。
    """
    if not getattr(sys, "frozen", False):
        return          # 源码运行时有控制台，别抢
    try:
        from djkit import gui_server as gs

        gs.OUT_ROOT.mkdir(parents=True, exist_ok=True)
        f = open(gs.OUT_ROOT / "app.log", "a", encoding="utf-8", buffering=1)
    except BaseException:  # noqa: BLE001
        return             # 目录不可写就认了，不能因为日志把启动搞挂
    sys.stdout = f
    sys.stderr = f
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====", flush=True)


if __name__ == "__main__":
    # 打包后 __main__ 会被重新执行，没有 freeze_support 会不停开新进程
    multiprocessing.freeze_support()
    _attach_log()
    from djkit.app import main

    sys.exit(main())
