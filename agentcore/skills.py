"""技能日志:记录 run_code 成功执行的片段,供心跳"技能收割"提炼为可复用扩展。
用得越多 → 沉淀越多 → 扩展库越厚(自我进化闭环的原料侧)。"""
import json
import os
import threading

CAP = 120


class SkillsLog:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.items = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.items = data
            except Exception:
                self.items = []

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def record(self, intent, code, ok=True):
        code = (code or "").strip()
        if not code or not ok:
            return False
        with self.lock:
            for e in self.items[-10:]:  # 短窗口去重
                if e.get("code", "").strip() == code:
                    return False
            self.items.append({"ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                               "intent": str(intent or "")[:200],
                               "code": code[:2500], "harvested": False})
            del self.items[:-CAP]
            self._save()
        return True

    def unharvested(self):
        with self.lock:
            return [dict(e) for e in self.items if not e.get("harvested")]

    def mark_harvested(self, count):
        with self.lock:
            n = 0
            for e in self.items:
                if not e.get("harvested") and n < count:
                    e["harvested"] = True
                    n += 1
            if n:
                self._save()
            return n

    def recent(self, n=10):
        with self.lock:
            return [dict(e) for e in self.items[-n:]]
