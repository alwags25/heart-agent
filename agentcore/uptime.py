"""运行稳定性追踪(data/uptime.json)：
记录启动次数、正常/异常退出、宕机(崩溃)次数与连续运行时长,供徽章与监控读取。

宕机判定:上次退出时未写入 clean_shutdown=True(进程崩溃/被杀),且旧 PID 已不存活。
纯标准库,无第三方依赖。
"""
import json
import os
import time


def _pid_alive(pid):
    """判断进程是否仍存活(跨平台尽力而为:失败一律视为不存活)。"""
    if not pid:
        return False
    try:
        pid = int(pid)
        if os.name == "nt":
            import ctypes
            SYNCHRONIZE = 0x00100000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = ctypes.c_void_p
            h = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code))
            kernel32.CloseHandle(ctypes.c_void_p(h))
            return bool(ok) and code.value == STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except Exception:
        return False


class UptimeTracker:
    def __init__(self, data_dir, events=None):
        self.path = os.path.join(data_dir, "uptime.json")
        self.events = events
        self.data = self._load()

    def _load(self):
        base = {"starts": 0, "crashes": 0, "restarts": 0,
                "clean_shutdown": None, "pid": None,
                "last_start": None, "last_stop": None,
                "last_exit_code": None, "last_crash": None,
                "total_uptime_sec": 0, "last_uptime_sec": 0,
                "history": []}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                base.update(d)
        except Exception:
            pass
        return base

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def record_start(self):
        """启动调用一次。返回本次是否检测到上次为异常退出(宕机)。"""
        d = self.data
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        prev_pid = d.get("pid")
        crashed = (d.get("clean_shutdown") is False
                   and int(prev_pid or 0) != os.getpid()
                   and not _pid_alive(prev_pid))
        if crashed:
            d["crashes"] += 1
            d["last_crash"] = d.get("last_start") or now
        if d.get("starts", 0) > 0:
            d["restarts"] += 1
        d["starts"] += 1
        d["clean_shutdown"] = False
        d["pid"] = os.getpid()
        d["last_start"] = now
        d["history"].append({"ts": now, "event": "crash" if crashed else
                             ("restart" if d["starts"] > 1 else "start")})
        del d["history"][:-50]
        self._save()
        if self.events:
            if crashed:
                self.events.publish("error",
                                    text="⚠️ 检测到上次异常退出(宕机),已记录;累计宕机 %d 次"
                                         % d["crashes"])
            self.events.publish("insight", text="🚀 服务启动(第 %d 次)" % d["starts"])
        return crashed

    def record_stop(self, exit_code=0, reason="normal"):
        """正常退出调用。重复调用只记一次。"""
        d = self.data
        if d.get("clean_shutdown") is True:
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        uptime = 0
        started = d.get("last_start")
        try:
            if started:
                uptime = max(0, int(
                    time.mktime(time.strptime(now, "%Y-%m-%d %H:%M:%S"))
                    - time.mktime(time.strptime(started, "%Y-%m-%d %H:%M:%S"))))
        except Exception:
            uptime = 0
        d["clean_shutdown"] = True
        d["last_stop"] = now
        d["last_exit_code"] = exit_code
        d["last_uptime_sec"] = uptime
        d["total_uptime_sec"] = int(d.get("total_uptime_sec", 0)) + uptime
        d["history"].append({"ts": now, "event": "stop", "exit": exit_code, "reason": reason})
        del d["history"][:-50]
        self._save()

    def snapshot(self):
        d = self.data
        return {"starts": d.get("starts", 0), "crashes": d.get("crashes", 0),
                "restarts": d.get("restarts", 0), "last_crash": d.get("last_crash"),
                "last_start": d.get("last_start"), "last_stop": d.get("last_stop"),
                "last_exit_code": d.get("last_exit_code"),
                "total_uptime_sec": d.get("total_uptime_sec", 0),
                "last_uptime_sec": d.get("last_uptime_sec", 0)}
