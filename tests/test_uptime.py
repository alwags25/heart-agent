import json
import os
import tempfile
import unittest

from agentcore.uptime import UptimeTracker, _pid_alive


class UptimeTrackerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "uptime.json")

    def _write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_first_start(self):
        up = UptimeTracker(self.dir)
        self.assertFalse(up.record_start())
        snap = up.snapshot()
        self.assertEqual(snap["starts"], 1)
        self.assertEqual(snap["crashes"], 0)
        self.assertEqual(snap["restarts"], 0)

    def test_crash_detected(self):
        # 模拟上次未正常退出且进程已不存在
        self._write({"starts": 3, "crashes": 1, "restarts": 2,
                     "clean_shutdown": False, "pid": 99999999,
                     "last_start": "2026-01-01 10:00:00"})
        up = UptimeTracker(self.dir)
        self.assertTrue(up.record_start())
        snap = up.snapshot()
        self.assertEqual(snap["crashes"], 2)
        self.assertEqual(snap["starts"], 4)
        self.assertIsNotNone(snap["last_crash"])

    def test_clean_shutdown_not_crash(self):
        up = UptimeTracker(self.dir)
        up.record_start()
        up.record_stop(0)
        # 重新加载:上次已正常退出,不应判定宕机
        up2 = UptimeTracker(self.dir)
        self.assertFalse(up2.record_start())
        self.assertEqual(up2.snapshot()["crashes"], 0)

    def test_restart_count(self):
        up = UptimeTracker(self.dir)
        up.record_start()
        up.record_stop(0)
        up2 = UptimeTracker(self.dir)
        up2.record_start()
        snap = up2.snapshot()
        self.assertEqual(snap["starts"], 2)
        self.assertEqual(snap["restarts"], 1)
        self.assertEqual(snap["crashes"], 0)

    def test_stop_idempotent(self):
        up = UptimeTracker(self.dir)
        up.record_start()
        up.record_stop(0)
        first_stop = up.snapshot()["last_stop"]
        up.record_stop(1)  # 重复调用不应覆盖
        self.assertEqual(up.snapshot()["last_stop"], first_stop)
        self.assertEqual(up.snapshot()["last_exit_code"], 0)

    def test_dead_pid_not_alive(self):
        self.assertFalse(_pid_alive(99999999))
        self.assertFalse(_pid_alive(None))

    def test_persisted_file(self):
        up = UptimeTracker(self.dir)
        up.record_start()
        self.assertTrue(os.path.exists(self.path))
        with open(self.path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["starts"], 1)
        self.assertFalse(data["clean_shutdown"])


if __name__ == "__main__":
    unittest.main()
