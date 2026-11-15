"""token 用量 + 成本钱包:按模型累计 calls/tokens,按月归档;结合单价表算花费。
钱包闸门由心跳调用:预算耗尽时暂停高消耗的自主活动(对话不受限)。"""
import json
import os
import threading
import time


class UsageTracker:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"total": {}, "months": {}}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and "total" in loaded:
                    self.data = loaded
                elif isinstance(loaded, dict):  # 旧版格式迁移
                    self.data = {"total": loaded, "months": {}}
            except Exception:
                self.data = {"total": {}, "months": {}}

    def record(self, label, usage, months=None):
        if not usage:
            return
        months = months or [time.strftime("%Y-%m")]
        with self.lock:
            for bucket_key in ["total"] + months:
                bucket = self.data.setdefault(bucket_key, {})
                e = bucket.setdefault(label, {"calls": 0, "prompt_tokens": 0,
                                              "completion_tokens": 0})
                e["calls"] += 1
                e["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
                e["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            self._save()

    def _bucket(self, key):
        return self.data.get(key, {})

    def snapshot(self, month=None):
        month = month or time.strftime("%Y-%m")
        with self.lock:
            return {"total": json.loads(json.dumps(self._bucket("total"))),
                    "month": json.loads(json.dumps(self._bucket(month))),
                    "month_key": month}

    def total_tokens(self):
        with self.lock:
            return sum(v.get("prompt_tokens", 0) + v.get("completion_tokens", 0)
                       for v in self._bucket("total").values())

    @staticmethod
    def cost_of(bucket, prices):
        """prices: {模型名: {"in": 元/1M, "out": 元/1M}};未知模型按 0 计。"""
        cost = 0.0
        for label, v in (bucket or {}).items():
            p = (prices or {}).get(label) or {}
            cost += (v.get("prompt_tokens", 0) * p.get("in", 0)
                     + v.get("completion_tokens", 0) * p.get("out", 0)) / 1e6
        return round(cost, 4)

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass
