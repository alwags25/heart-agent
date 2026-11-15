"""事件总线:主AI/协作AI 的思考、工具调用、心跳、任务等一切行为都发布到这里,
右侧"思考过程"屏幕通过 SSE 实时消费;可选 JSONL 落盘供思考回放器使用。"""
import collections
import json
import os
import queue
import threading
import time


class EventBus:
    def __init__(self, maxlen=800):
        self._lock = threading.Lock()
        self._subs = []  # 订阅者的 queue.Queue 列表
        self._recent = collections.deque(maxlen=maxlen)  # 环形缓冲,新客户端可回看
        self._sink_path = None
        self._skip_sink = {"status", "chat_stream"}  # 高频/低信息量事件不落盘

    def set_sink(self, path):
        """开启落盘(JSONL,8MB 轮转),供思考回放器使用。"""
        self._sink_path = path

    def _sink_write(self, ev):
        if not self._sink_path or ev["type"] in self._skip_sink:
            return
        try:
            if os.path.exists(self._sink_path) and \
                    os.path.getsize(self._sink_path) > 8 * 1024 * 1024:
                os.replace(self._sink_path, self._sink_path + ".1")
            with open(self._sink_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, type_, **data):
        ev = {"type": type_, "ts": time.strftime("%H:%M:%S")}
        ev.update(data)
        with self._lock:
            self._recent.append(ev)
            subs = list(self._subs)
        for q in subs:
            q.put(ev)
        self._sink_write(ev)

    def recent(self, n=200):
        with self._lock:
            return list(self._recent)[-n:]
