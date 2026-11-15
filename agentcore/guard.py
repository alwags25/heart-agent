"""能力围栏(尽力而为的沙箱):按能力令牌限制扩展/代码能做什么。
- net  : 未授权时 socket 层直接抛 PermissionError
- proc : 未授权时 subprocess / os.system 不可用
- fs-write: 未授权时只允许写入工作目录(data/extensions/临时目录)
诚实声明:这是 monkeypatch 级别的围栏,防误伤不防绕过;
真正的强隔离需要容器/系统级方案,但这一层已能挡住绝大多数事故。
在 runner 子进程最早期安装,先于加载扩展代码。"""
import builtins
import json
import os
import tempfile


def _under(path, roots):
    try:
        p = os.path.abspath(str(path))
        return any(p.startswith(r + os.sep) or p == r for r in roots)
    except Exception:
        return False


def install(caps, roots=None):
    caps = set(caps or [])
    roots = [os.path.abspath(r) for r in (roots or [os.getcwd(), tempfile.gettempdir()])]
    if os.name == "nt":
        roots = [r.rstrip("\\").lower() for r in roots]

        def under(path, rs):
            try:
                p = os.path.abspath(str(path)).rstrip("\\").lower()
                return any(p == r or p.startswith(r + "\\") for r in rs)
            except Exception:
                return False
    else:
        under = _under

    def _make_blocked(msg):
        err = PermissionError(msg)

        def _blocked(*a, **k):
            raise err
        return _blocked

    if "net" not in caps:
        import socket
        socket.socket = _make_blocked("能力未授权: net(联网)。请通过扩展审批声明该能力。")
        socket.create_connection = _make_blocked("能力未授权: net(联网)。")
        socket.getaddrinfo = _make_blocked("能力未授权: net(联网)。")

    if "proc" not in caps:
        import subprocess
        subprocess.Popen = _make_blocked("能力未授权: proc(子进程)。")
        subprocess.run = _make_blocked("能力未授权: proc(子进程)。")
        subprocess.call = _make_blocked("能力未授权: proc(子进程)。")
        subprocess.check_output = _make_blocked("能力未授权: proc(子进程)。")
        os.system = _make_blocked("能力未授权: proc(子进程)。")
        os.popen = _make_blocked("能力未授权: proc(子进程)。")

    if "fs-write" not in caps:
        real_open = builtins.open

        def guarded_open(file, mode="r", *a, **k):
            if any(m in str(mode) for m in ("w", "a", "x", "+")) and not under(file, roots):
                raise PermissionError(
                    "能力未授权: fs-write(在 %s 之外写入)" % str(file)[:120])
            return real_open(file, mode, *a, **k)
        builtins.open = guarded_open
        real_remove = os.remove

        def guarded_remove(path, *a, **k):
            if not under(path, roots):
                raise PermissionError("能力未授权: fs-write(删除 %s)" % str(path)[:120])
            return real_remove(path, *a, **k)
        os.remove = guarded_remove

    return sorted(caps)


def caps_from_env():
    caps = os.environ.get("HEARTAGENT_CAPS", "[]")
    roots = os.environ.get("HEARTAGENT_ROOTS", "[]")
    try:
        return json.loads(caps), json.loads(roots)
    except Exception:
        return [], []
