# -*- coding: utf-8 -*-
"""HeartAgent 单元测试:覆盖历史上真实炸过的路径。
运行:python -m unittest discover tests -v"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from agentcore.llm import _openai_tools
from agentcore.brain import sanitize_tool_output
from agentcore.tasks import parse_cron, next_cron, parse_time_spec
from agentcore.evals import EvalHarness
from agentcore.memory import MemoryStore


class TestOpenaiTools(unittest.TestCase):
    """回归:工具表元组/字典双格式 —— 曾经因格式不匹配炸掉所有真实对话。"""

    def test_tuple_format(self):
        tools = [("time_now", "获取时间", {"type": "object", "properties": {}})]
        out = _openai_tools(tools)
        self.assertEqual(out[0]["function"]["name"], "time_now")
        self.assertEqual(out[0]["type"], "function")

    def test_dict_format(self):
        tools = [{"name": "remember", "description": "写入记忆",
                  "parameters": {"type": "object", "properties": {}}}]
        out = _openai_tools(tools)
        self.assertEqual(out[0]["function"]["name"], "remember")

    def test_tuple_without_params(self):
        out = _openai_tools([("list_tasks", "列出任务")])
        self.assertEqual(out[0]["function"]["parameters"], {"type": "object", "properties": {}})

    def test_brain_tools_meta_compatible(self):
        """Brain 的全部工具定义必须能转换(这是线上炸过的集成点)。"""
        from agentcore.brain import TOOLS_META
        out = _openai_tools(list(TOOLS_META))
        self.assertEqual(len(out), len(TOOLS_META))
        for item in out:
            self.assertIn("name", item["function"])
            self.assertIn("parameters", item["function"])


class TestCron(unittest.TestCase):
    def test_daily(self):
        from datetime import datetime
        nr = next_cron("0 9 * * *", after=datetime(2026, 9, 27, 10, 0))
        self.assertEqual((nr.month, nr.day, nr.hour, nr.minute), (9, 28, 9, 0))

    def test_step(self):
        from datetime import datetime
        nr = next_cron("*/15 * * * *", after=datetime(2026, 9, 27, 10, 0))
        self.assertEqual((nr.hour, nr.minute), (10, 15))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_cron("bad")


class TestTimeSpec(unittest.TestCase):
    def test_relative(self):
        from datetime import datetime, timedelta
        t = parse_time_spec("30m")
        self.assertGreater(t, datetime.now() + timedelta(minutes=29))

    def test_hhmm(self):
        t = parse_time_spec("23:59")
        self.assertEqual((t.hour, t.minute), (23, 59))

    def test_too_short(self):
        with self.assertRaises(ValueError):
            parse_time_spec("2s")


class TestMemory(unittest.TestCase):
    def setUp(self):
        self.m = MemoryStore(os.path.join(tempfile.mkdtemp(), "mem.db"))

    def test_dedup_merge(self):
        self.m.add_fact("用户的技术栈是 Python 和 Rust")
        r = self.m.add_fact("用户的技术栈是 Python 和 Rust 语言")
        self.assertTrue(r["merged"])
        self.assertEqual(len(self.m.facts()), 1)

    def test_archived_excluded_from_search(self):
        f = self.m.add_fact("独门暗号是芝麻开门")
        self.m.set_archived(f["id"], 1)
        self.assertEqual(self.m.search_facts("暗号"), [])

    def test_triples_roundtrip(self):
        self.assertEqual(self.m.add_triples(
            [{"s": "用户", "r": "主力语言", "o": "Python"}]), 1)
        self.assertEqual(self.m.add_triples(
            [{"s": "用户", "r": "主力语言", "o": "Python"}]), 0)  # 去重
        g = self.m.graph()
        self.assertEqual(g["nodes"][0]["id"], "用户")
        self.assertTrue(self.m.graph_context("用户用什么语言"))

    def test_backfill_no_embedder_safe(self):
        self.assertEqual(self.m.backfill_embeddings(), 0)


class TestSanitize(unittest.TestCase):
    def test_strips_control_chars_and_wraps(self):
        out = sanitize_tool_output("t", "a\x00b\x1f[c]")
        self.assertNotIn("\x00", out)
        self.assertIn("<<<TOOL_DATA tool=t>>>", out)
        self.assertIn("[c]", out)


class TestEvalJudge(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.h = EvalHarness(os.path.join(d, "c.json"), os.path.join(d, "h.json"), None)

    def test_tool_expectations(self):
        ok, _ = self.h._judge({"expect_tools": ["remember"], "expect_keywords": [],
                               "forbidden": []}, ["remember"], "done")
        self.assertTrue(ok)
        ok2, _ = self.h._judge({"expect_tools": ["remember"], "expect_keywords": [],
                                "forbidden": []}, [], "done")
        self.assertFalse(ok2)
        ok3, _ = self.h._judge({"expect_tools": [], "expect_keywords": [], "forbidden": []},
                               ["remember"], "x")
        self.assertFalse(ok3)  # 不该调工具却调了

    def test_keyword_and_forbidden(self):
        ok, _ = self.h._judge({"expect_tools": [], "expect_keywords": ["时间"],
                               "forbidden": ["抱歉"]}, [], "现在是时间 12:00")
        self.assertTrue(ok)
        ok2, _ = self.h._judge({"expect_tools": [], "expect_keywords": [],
                                "forbidden": ["抱歉"]}, [], "抱歉我不知道")
        self.assertFalse(ok2)


class TestGuardSubprocess(unittest.TestCase):
    """能力围栏:在干净子进程里验证 —— 未授权 net 时 socket 必须被拦。"""

    PROBE = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from agentcore import guard;"
        "guard.install(sys.argv[2].split(',') if sys.argv[2] else [], []);"
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 1), timeout=1)\n"
        "    print('CONN_FAIL')\n"  # 真实发起连接(未拦截),端口1必然拒连
        "except PermissionError as e:\n"
        "    print('BLOCKED', e)\n"
        "except OSError as e:\n"
        "    print('CONN_FAIL', e)\n"
    )

    def _probe(self, caps):
        proc = subprocess.run(
            [sys.executable, "-c", self.PROBE, BASE, caps],
            capture_output=True, timeout=60)
        return proc.stdout.decode("utf-8", "replace")

    def test_net_blocked_without_cap(self):
        self.assertIn("BLOCKED", self._probe(""))

    def test_net_allowed_with_cap(self):
        # 授权后 socket 可用:连接 127.0.0.1:1 得到的是拒连而非能力拦截
        self.assertIn("CONN_FAIL", self._probe("net"))


if __name__ == "__main__":
    unittest.main()
