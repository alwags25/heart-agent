"""目标系统(OKR):长期目标 → 主控AI自主拆解为任务/记忆 → 心跳定期检查进度
→ 进展记入目标卡片、日报汇总。数据持久化到 data/goals.json。"""
import json
import os
import threading
import time
from datetime import datetime

FMT = "%Y-%m-%d %H:%M:%S"


def _parse_ts(s):
    try:
        return datetime.strptime(s, FMT)
    except (ValueError, TypeError):
        return None


class GoalManager:
    def __init__(self, path, events, data_dir):
        self.path = path
        self.events = events
        self.data_dir = data_dir
        self.lock = threading.RLock()
        self.brain = None  # main.py 注入(拆解与检查都需要主控AI)
        self.goals = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    self.goals = loaded
            except Exception:
                pass
        os.makedirs(data_dir, exist_ok=True)

    def set_brain(self, brain):
        self.brain = brain

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.goals, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def add(self, title, description="", check_every_hours=24, auto_decompose=True):
        title = str(title or "").strip()[:120]
        if not title:
            raise ValueError("目标标题为空")
        goal = {
            "id": "g%d" % int(time.time() * 1000),
            "title": title,
            "description": str(description or "").strip()[:2000],
            "status": "active",  # active / paused / done / dropped
            "check_every_hours": max(1, int(check_every_hours or 24)),
            "created_at": time.strftime(FMT),
            "last_check": None,
            "progress": [],  # [{ts, note}]
        }
        with self.lock:
            self.goals.append(goal)
            self._save()
        if self.events:
            self.events.publish("goal", text="🎯 新目标: %s(每 %dh 检查一次)"
                                % (title, goal["check_every_hours"]))
        if auto_decompose and self.brain:
            threading.Thread(target=self._decompose, args=(goal,), daemon=True).start()
        return goal

    def _decompose(self, goal):
        try:
            self.brain.autonomous(
                "长期目标已设定:「%s」(%s)。\n"
                "请把它拆解为可执行的行动:1) 用 add_task 创建所需的定时/循环任务;"
                "2) 用 remember 记录关键背景;3) 不需要创建任何东西就直接汇报拆解思路。"
                % (goal["title"], goal["description"] or "无补充描述"),
                source="目标拆解")
        except Exception as e:
            if self.events:
                self.events.publish("error", text="目标拆解失败: %s" % e)

    def list(self):
        with self.lock:
            return [dict(g) for g in self.goals]

    def get(self, gid):
        with self.lock:
            for g in self.goals:
                if g["id"] == gid:
                    return g
        return None

    def _resolve(self, id_or_title):
        g = self.get(str(id_or_title))
        if g:
            return g
        with self.lock:
            for x in self.goals:
                if x["title"] == id_or_title:
                    return x
        return None

    def update(self, gid, status=None, note=None):
        g = self._resolve(gid)
        if not g:
            raise ValueError("目标不存在: %s" % gid)
        with self.lock:
            if status is not None:
                if status not in ("active", "paused", "done", "dropped"):
                    raise ValueError("status 须为 active/paused/done/dropped")
                g["status"] = status
            if note:
                g["progress"].append({"ts": time.strftime(FMT), "note": str(note)[:600]})
                del g["progress"][:-30]
            self._save()
        if self.events and note:
            self.events.publish("goal", text="🎯 目标「%s」进展: %s" % (g["title"], str(note)[:160]))
        return dict(g)

    def delete(self, gid):
        with self.lock:
            before = len(self.goals)
            self.goals = [g for g in self.goals if g["id"] != gid]
            changed = len(self.goals) != before
            if changed:
                self._save()
            return changed

    def due_checks(self, now=None):
        """到检查时间且仍活跃的目标。"""
        now = now or datetime.now()
        out = []
        with self.lock:
            for g in self.goals:
                if g["status"] != "active":
                    continue
                last = _parse_ts(g.get("last_check") or "")
                due = last is None or (now - last).total_seconds() >= g["check_every_hours"] * 3600
                if due:
                    out.append(g)
        return out

    def touch(self, gid):
        g = self.get(gid)
        if g:
            with self.lock:
                g["last_check"] = time.strftime(FMT)
                self._save()

    def run_check_now(self, gid):
        g = self._resolve(gid)
        if not g:
            raise ValueError("目标不存在: %s" % gid)
        threading.Thread(target=self._check_one, args=(g,), daemon=True).start()
        return {"ok": True}

    def _check_one(self, goal):
        """心跳/手动触发:让主控AI回顾目标并自主推进。"""
        try:
            prog = "\n".join("- %s %s" % (p["ts"], p["note"]) for p in goal["progress"][-5:]) or "(尚无进展记录)"
            out = self.brain.autonomous(
                "回顾长期目标「%s」(%s)。\n近期进展:\n%s\n\n"
                "请评估当前进度:如有必要用 run_task_now 推进相关任务、add_task 补充缺口、"
                "update_goal 记录一条进展(note);最后用一句话汇报当前状态。"
                % (goal["title"], goal["description"] or "无补充描述", prog),
                source="目标检查")
            self.touch(goal["id"])
            return out
        except Exception as e:
            if self.events:
                self.events.publish("error", text="目标检查失败: %s" % e)
            return "检查失败: %s" % e

    def stats_snapshot(self):
        with self.lock:
            return [{"title": g["title"], "status": g["status"],
                     "progress_notes": len(g["progress"]),
                     "last_check": g["last_check"]} for g in self.goals]
