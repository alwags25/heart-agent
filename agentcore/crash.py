"""全局异常捕获:把未捕获异常与线程异常写入 data/crash.log,便于宕机溯源。

Python 默认只把未捕获异常的 traceback 打到 stderr(控制台),重定向/关闭控制台后即丢失;
本模块接管 sys.excepthook 与 threading.excepthook,持久化到 crash.log 并同步到事件总线。
"""
import os
import sys
import threading
import time
import traceback


def install(log_path, events=None):
    """安装全局异常钩子。log_path 为 crash.log 路径。"""

    def _write(kind, exc_type, exc_value, exc_tb, thread_name=None):
        try:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n[%s] %s%s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), kind,
                    (" thread=%s" % thread_name) if thread_name else ""))
                f.write("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        except OSError:
            pass
        if events:
            try:
                events.publish("error", text="💥 未捕获异常(%s): %s" % (kind, exc_value))
            except Exception:
                pass

    def _hook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        _write("crash", exc_type, exc_value, exc_tb)

    def _thread_hook(args):
        if args.exc_type is SystemExit:
            return
        _write("thread-crash", args.exc_type, args.exc_value, args.exc_traceback,
               getattr(args.thread, "name", None))

    sys.excepthook = _hook
    threading.excepthook = _thread_hook
