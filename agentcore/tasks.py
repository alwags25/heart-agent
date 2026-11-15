"""任务管理 v2:
- schedule: once(定时时点)/ loop(间隔秒)/ cron(标准 5 段表达式,支持 */n、a-b、列表)
- action: prompt(主控AI自主执行)/ code(子进程执行)
- retry: 失败自动重试(次数 + 退避秒数)
- chain: on_success/on_failure 触发后续任务
- 并发上限 + 失败通知(notify 模块)
持久化到 data/tasks.json,重启按 next_run 继续。"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

from . import notify
from .extensions import run_module

FMT = "%Y-%m-%d %H:%M:%S"
DAY_MINUTES = 366 * 24 * 60  # cron 扫描上限:一年
MAX_CHAIN_DEPTH = 10         # 任务链防环:最多级联 10 层


# ---------------- cron 解析 ----------------
def _parse_field(field, lo, hi, is_dow=False):
    """返回 (ints集合, 是否受限)。'*' → 不受限。"""
    field = (field or "").strip()
    unrestricted = field in ("*", "") or field.startswith("*/") and field[2:].isdigit()
    vals = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s) if step_s.isdigit() else 1
            if part in ("*", ""):
                part_range = range(lo, hi + 1)
            elif "-" in part:
                a, b = part.split("-", 1)
                part_range = range(int(a), int(b) + 1)
            else:
                part_range = range(int(part), hi + 1)
            vals.update(x for x in part_range if (x - lo) % step == 0)
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            vals.update(range(int(a), int(b) + 1))
        elif part == "*":
            unrestricted = True
            vals.update(range(lo, hi + 1))
        else:
            vals.add(int(part))
    if is_dow:
        if 7 in vals:
            vals.discard(7)
            vals.add(0)
        vals = {v % 7 for v in vals}
    return vals, not (unrestricted and len(vals) >= hi - lo + 1)


def parse_cron(expr):
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError("cron 表达式需 5 段(分 时 日 月 周): %r" % expr)
    minute, mc = _parse_field(parts[0], 0, 59)
    hour, hc = _parse_field(parts[1], 0, 23)
    dom, dc = _parse_field(parts[2], 1, 31)
    mon, moc = _parse_field(parts[3], 1, 12)
    dow, wc = _parse_field(parts[4], 0, 6, is_dow=True)
    return {"m": minute, "h": hour, "dom": dom, "mon": mon, "dow": dow,
            "dc": dc, "wc": wc}


def next_cron(expr, after=None):
    c = parse_cron(expr)
    t = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(DAY_MINUTES):
        dom_ok = t.day in c["dom"]
        dow_ok = (t.weekday() + 1) % 7 in c["dow"]
        # 标准cron:日与周都受限时,任一命中即可
        day_ok = (dom_ok or dow_ok) if (c["dc"] and c["wc"]) else (dom_ok and dow_ok)
        if (t.minute in c["m"] and t.hour in c["h"] and t.month in c["mon"] and day_ok):
            return t
        t += timedelta(minutes=1)
    raise ValueError("cron 表达式一年内无命中: %r" % expr)


# ---------------- 时间描述 ----------------
def parse_time_spec(s):
    s = str(s).strip()
    m = _re_fullmatch(r"(?:in\s+)?(\d+)\s*([smhd])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * n
        if delta < 5:
            raise ValueError("时间间隔太短(至少 5 秒)")
        return datetime.now() + timedelta(seconds=delta)
    if len(s) == 5 and s[2] == ":" and s[:2].isdigit() and s[3:].isdigit():
        hh, mm = int(s[:2]), int(s[3:])
        t = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= datetime.now():
            t += timedelta(days=1)
        return t
    for fmt in (FMT, "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return next_cron(s)  # 顺手支持把 cron 表达式当 at 用


def _re_fullmatch(pattern, s):
    import re
    return re.fullmatch(pattern, s, re.I)


def describe_schedule(sch):
    if sch.get("kind") == "once":
        return "一次性 @ %s" % sch.get("at", "?")
    if sch.get("kind") == "cron":
        return "cron %s" % sch.get("expr", "?")
    return "循环 每 %ss" % sch.get("interval_sec", "?")


def describe_action(act):
    return "提示词任务" if act.get("kind") == "prompt" else "代码任务"


class TaskManager:
    def __init__(self, path, events, data_dir, notify_cfg=None, max_concurrent=2, ext_mem_mb=512):
        self.path = path
        self.events = events
        self.data_dir = data_dir
        self.notify_cfg = notify_cfg or {}
        self.max_concurrent = max(1, int(max_concurrent))
        self.ext_mem_mb = ext_mem_mb
        self.sem = threading.BoundedSemaphore(self.max_concurrent)
        self.lock = threading.RLock()
        self.brain = None
        self.tasks = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    self.tasks = loaded
            except Exception:
                pass
        os.makedirs(data_dir, exist_ok=True)

    def set_brain(self, brain):
        self.brain = brain

    def update_notify_cfg(self, cfg):
        self.notify_cfg = cfg or {}

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.tasks, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # ---------- 增删改查 ----------
    def add(self, name, schedule, action, retry=None, chain=None, notify_policy=None,
            enabled=True):
        name = str(name or "未命名任务").strip()[:60]
        schedule, action = schedule or {}, action or {}
        kind = schedule.get("kind")
        if kind == "cron":
            next_run = next_cron(str(schedule.get("expr", "")))
        elif kind == "once":
            if not schedule.get("at"):
                raise ValueError('一次性任务缺少 schedule.at')
            next_run = parse_time_spec(schedule["at"])
            schedule["at"] = next_run.strftime(FMT)
        elif kind == "loop":
            interval = int(schedule.get("interval_sec") or 0)
            if interval < 10:
                raise ValueError("循环任务 interval_sec 至少 10 秒")
            schedule["interval_sec"] = interval
            next_run = datetime.now() + timedelta(seconds=interval)
        else:
            raise ValueError('schedule.kind 必须是 "once" / "loop" / "cron"')
        if action.get("kind") not in ("prompt", "code"):
            raise ValueError('action.kind 必须是 "prompt" 或 "code"')
        if action["kind"] == "prompt" and not str(action.get("text", "")).strip():
            raise ValueError("提示词任务缺少 action.text")
        if action["kind"] == "code" and not str(action.get("code", "")).strip():
            raise ValueError("代码任务缺少 action.code")
        task = {
            "id": "t%d" % int(time.time() * 1000),
            "name": name, "schedule": schedule, "action": action,
            "retry": retry or {"max": 0, "backoff_sec": 60},
            "chain": chain or {},
            "notify": notify_policy,  # "failure"|"always"|None;None 跟随全局配置
            "enabled": bool(enabled),
            "created_at": datetime.now().strftime(FMT),
            "last_run": None, "next_run": next_run.strftime(FMT),
            "history": [], "_retries": 0,
        }
        with self.lock:
            self.tasks.append(task)
            self._save()
        if self.events:
            self.events.publish("task", text="📋 新任务: %s(%s / %s,下次 %s)%s"
                                % (name, describe_schedule(schedule), describe_action(action),
                                   task["next_run"],
                                   " —— ⏸ 已创建但停用,待用户批准(自治等级低)"
                                   if not enabled else ""))
        return task

    def update(self, tid, patch):
        """UI 编辑:支持 name/enabled/schedule/action/retry/chain/notify。"""
        with self.lock:
            t = self.get(tid)
            if not t:
                raise ValueError("任务不存在: %s" % tid)
            if "name" in patch:
                t["name"] = str(patch["name"]).strip()[:60]
            if "enabled" in patch:
                t["enabled"] = bool(patch["enabled"])
            if "notify" in patch:
                t["notify"] = patch["notify"]
            if "schedule" in patch and patch["schedule"]:
                sch = patch["schedule"]
                kind = sch.get("kind")
                if kind == "cron":
                    t["next_run"] = next_cron(str(sch.get("expr", t["schedule"].get("expr", "")))).strftime(FMT)
                elif kind == "loop" and int(sch.get("interval_sec", 0) or 0) >= 10:
                    t["schedule"]["interval_sec"] = int(sch["interval_sec"])
                elif kind == "once":
                    t["schedule"]["at"] = str(sch.get("at", ""))
                    t["next_run"] = parse_time_spec(t["schedule"]["at"]).strftime(FMT)
                if sch.get("expr"):
                    t["schedule"]["expr"] = str(sch["expr"])
                t["schedule"]["kind"] = kind or t["schedule"]["kind"]
            if "action" in patch and patch["action"]:
                a = patch["action"]
                if a.get("kind") in ("prompt", "code"):
                    t["action"]["kind"] = a["kind"]
                if a.get("text") is not None:
                    t["action"]["text"] = str(a["text"])
                if a.get("code") is not None:
                    t["action"]["code"] = str(a["code"])
            if "retry" in patch and patch["retry"]:
                t["retry"] = {"max": int(patch["retry"].get("max", 0)),
                              "backoff_sec": int(patch["retry"].get("backoff_sec", 60))}
            if t["enabled"] and not t.get("next_run") and t["schedule"]["kind"] == "loop":
                t["next_run"] = (datetime.now()
                                 + timedelta(seconds=t["schedule"]["interval_sec"])).strftime(FMT)
            self._save()
            return dict(t)

    def list(self):
        with self.lock:
            return [dict(t) for t in self.tasks]

    def get(self, tid):
        with self.lock:
            for t in self.tasks:
                if t["id"] == tid:
                    return t
        return None

    def _resolve(self, id_or_name):
        t = self.get(str(id_or_name))
        if t:
            return t
        with self.lock:
            for x in self.tasks:
                if x["name"] == id_or_name:
                    return x
        return None

    def delete(self, tid):
        with self.lock:
            before = len(self.tasks)
            self.tasks = [t for t in self.tasks if t["id"] != tid]
            changed = len(self.tasks) != before
            if changed:
                self._save()
            return changed

    def toggle(self, tid):
        with self.lock:
            t = self.get(tid)
            if not t:
                return None
            t["enabled"] = not t["enabled"]
            if t["enabled"] and t["schedule"]["kind"] == "loop" and not t.get("next_run"):
                t["next_run"] = (datetime.now()
                                 + timedelta(seconds=t["schedule"]["interval_sec"])).strftime(FMT)
            self._save()
            return t

    def run_now(self, tid):
        t = self._resolve(tid)
        if not t:
            raise ValueError("任务不存在: %s" % tid)
        threading.Thread(target=self._execute, args=(t, "手动触发"), daemon=True).start()
        return {"ok": True}

    # ---------- 调度 ----------
    def worker(self, stop_event):
        while not stop_event.wait(3):
            try:
                self._scan()
            except Exception as e:
                if self.events:
                    self.events.publish("error", text="任务调度异常: %s" % e)

    def _scan(self):
        now = datetime.now()
        with self.lock:
            due = [dict(t) for t in self.tasks
                   if t.get("enabled") and t.get("next_run") and self._parse_dt(t["next_run"], now)]
        for t in due:
            real = self.get(t["id"])
            if real and real["enabled"] and self._parse_dt(real["next_run"], now):
                threading.Thread(target=self._execute, args=(real, "定时触发"),
                                 daemon=True).start()

    @staticmethod
    def _parse_dt(s, now):
        for fmt in (FMT, "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt) <= now
            except (ValueError, TypeError):
                pass
        return False

    # ---------- 执行 ----------
    def _execute(self, task, trigger, depth=0):
        if not self.sem.acquire(blocking=False):
            with self.lock:  # 并发已满:推迟 5 秒,不丢任务
                task["next_run"] = (datetime.now() + timedelta(seconds=5)).strftime(FMT)
                self._save()
            if self.events:
                self.events.publish("task", text="⏸ 并发已满,任务「%s」推迟 5s" % task["name"])
            return
        try:
            if not task.get("enabled") and trigger != "手动触发":
                return
            if self.events:
                self.events.publish("task", text="▶ 开始执行任务「%s」(%s)"
                                    % (task["name"], trigger))
            act = task["action"]
            ok, output = True, ""
            try:
                if act["kind"] == "code":
                    code_path = os.path.join(self.data_dir, "task_code_%s.py" % task["id"])
                    with open(code_path, "w", encoding="utf-8") as f:
                        f.write(act.get("code", ""))
                    res = run_module(code_path, {}, timeout=120, mem_mb=self.ext_mem_mb)
                    ok = bool(res.get("ok"))
                    output = json.dumps(res, ensure_ascii=False, default=str)
                else:
                    if self.brain is None:
                        raise RuntimeError("主控AI尚未就绪")
                    output = self.brain.autonomous(str(act.get("text", "")),
                                                   source="任务: %s" % task["name"])
            except Exception as e:
                ok, output = False, "%s: %s" % (type(e).__name__, e)
            self._finish(task, trigger, ok, output, depth=depth)
        finally:
            self.sem.release()

    def _finish(self, task, trigger, ok, output, depth=0):
        retry = task.get("retry") or {}
        retries_left = int(retry.get("max", 0)) - int(task.get("_retries", 0))
        now = datetime.now()
        with self.lock:
            task["last_run"] = now.strftime(FMT)
            task["history"].append({"ts": task["last_run"], "trigger": trigger, "ok": ok,
                                    "retry": task.get("_retries", 0),
                                    "output": output[:600]})
            del task["history"][:-20]
            if not ok and retries_left > 0:  # 失败重试:退避后重跑,不推进周期
                task["_retries"] = task.get("_retries", 0) + 1
                backoff = max(5, int(retry.get("backoff_sec", 60)))
                task["next_run"] = (now + timedelta(seconds=backoff)).strftime(FMT)
                self._save()
            else:
                task["_retries"] = 0
                if task["schedule"]["kind"] == "once":
                    task["enabled"] = False
                    task["next_run"] = None
                elif task["schedule"]["kind"] == "cron":
                    task["next_run"] = next_cron(str(task["schedule"].get("expr", "")),
                                                 after=now).strftime(FMT)
                else:
                    task["next_run"] = (now + timedelta(
                        seconds=task["schedule"]["interval_sec"])).strftime(FMT)
                self._save()
        if self.events:
            if not ok and retries_left > 0:
                self.events.publish("task", text="↻ 任务「%s」失败,%.0f 秒后重试(%d/%d):%s"
                                    % (task["name"], max(5, int(retry.get("backoff_sec", 60))),
                                       task.get("_retries", 0), retry.get("max", 0), output[:200]))
            else:
                self.events.publish("task", text="■ 任务「%s」%s:%s"
                                    % (task["name"], "完成" if ok else "失败", output[:220]))
        self._notify(task, ok, output)
        self._chain(task, ok, depth=depth)

    def _notify(self, task, ok, output):
        policy = task.get("notify")
        if policy is None:
            policy = "failure" if self.notify_cfg.get("on_task_failure", True) else None
        if not policy or (policy == "failure" and ok):
            return
        title = "[HeartAgent] 任务%s: %s" % ("成功" if ok else "失败", task["name"])
        body = "触发时间: %s\n结果: %s" % (task.get("last_run"), output[:800])
        try:
            notify.send(self.notify_cfg, title, body)
        except Exception as e:
            if self.events:
                self.events.publish("error", text="通知发送失败: %s" % e)

    def _chain(self, task, ok, depth=0):
        """任务链:DAG 多后继(字符串或数组),带防环深度上限。"""
        chain = task.get("chain") or {}
        target = chain.get("on_success" if ok else "on_failure")
        if not target:
            return
        if depth >= MAX_CHAIN_DEPTH:
            if self.events:
                self.events.publish("error", text="任务链深度超限(疑似循环链),已截断:「%s」" % task["name"])
            return
        targets = target if isinstance(target, (list, tuple)) else [target]
        for tgt in targets:
            nxt = self._resolve(str(tgt))
            if not nxt:
                if self.events:
                    self.events.publish("error", text="任务链目标不存在: %s" % tgt)
                continue
            if self.events:
                self.events.publish("task", text="🔗 任务链:「%s」→ 触发「%s」"
                                    % (task["name"], nxt["name"]))
            threading.Thread(target=self._execute, args=(nxt, "任务链触发", depth + 1),
                             daemon=True).start()
