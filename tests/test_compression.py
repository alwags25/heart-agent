# -*- coding: utf-8 -*-
"""上下文压缩回归:30 轮对话后滚动摘要必须生效(此路径曾无实测覆盖)。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentcore.brain import Brain
from agentcore.events import EventBus
from agentcore.llm import MockLLM
from agentcore.memory import MemoryStore


class TestContextCompression(unittest.TestCase):
    def test_rolling_summary_kicks_in(self):
        from agentcore.assistant import AssistantAI
        from agentcore.extensions import ExtensionManager
        from agentcore.tasks import TaskManager

        d = tempfile.mkdtemp()
        mem = MemoryStore(os.path.join(d, "memory.db"))
        bus = EventBus()
        llm = MockLLM()
        assist = MockLLM()
        ext = ExtensionManager(os.path.join(d, "extensions"), bus, d, auto_approve=True)
        tasks = TaskManager(os.path.join(d, "tasks.json"), bus, d)
        brain = Brain(llm, mem, AssistantAI(assist, mem, bus), ext, tasks, bus, d,
                      assist_llm=assist)
        sid = mem.current_session()

        # 30 轮对话:超过 COMPRESS_KEEP(12) + COMPRESS_BATCH(16) 阈值
        for i in range(30):
            brain.handle_user("第 %d 个问题:请记住数字 %d" % (i, i))

        summary = mem.get_meta("ctx_summary:" + sid)
        self.assertIsNotNone(summary, "应生成滚动摘要")
        self.assertGreater(len(summary), 10, "摘要不应为空")
        self.assertLessEqual(len(summary), 1600, "摘要应受长度约束")
        # 最新对话仍在原文保留
        last = mem.chat_log(3, sid)
        self.assertEqual(last[-1]["role"], "assistant")

        # 再来一轮:系统提示里应包含摘要块(通过 eval_run 捕获行为而非提示词本身)
        r = brain.eval_run("综合以上所有内容,汇报目前对话轮数")
        self.assertIn("final", r)


if __name__ == "__main__":
    unittest.main()
