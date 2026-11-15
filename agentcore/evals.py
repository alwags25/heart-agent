"""Agent 评估台:给智能体写"单元测试"。
用例 = 模拟输入 + 预期工具集合 + 预期/禁止关键词;心跳定期回归跑分,
健康分历史画成曲线 —— 改提示词/换模型后立刻看到哪些行为退化了。"""
import json
import os
import re
import threading
import time

DEFAULT_CASES = [
    {"id": "e1", "name": "时间查询应走扩展", "input": "现在几点了",
     "expect_tools": ["call_extension"], "expect_keywords": [], "forbidden": []},
    {"id": "e2", "name": "记忆写入应走 remember", "input": "请记住我喜欢深色主题",
     "expect_tools": ["remember"], "expect_keywords": [], "forbidden": []},
    {"id": "e3", "name": "闲聊不应调工具", "input": "你好呀",
     "expect_tools": [], "expect_keywords": [], "forbidden": []},
    {"id": "e4", "name": "写扩展应走审批", "input": "帮我写一个问候扩展",
     "expect_tools": ["write_extension"], "expect_keywords": [], "forbidden": []},
]


class EvalHarness:
    def __init__(self, path, history_path, events, judge_llm=None):
        self.path = path
        self.history_path = history_path
        self.events = events
        self.judge_llm = judge_llm  # LLM-as-judge 给回答质量打分(可选)
        self.lock = threading.Lock()
        self.cases = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.cases = json.load(f)
            except Exception:
                self.cases = []
        if not self.cases:
            self.cases = [dict(c) for c in DEFAULT_CASES]
            self._save()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.cases, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def list_cases(self):
        with self.lock:
            return [dict(c) for c in self.cases]

    def add_case(self, name, input_text, expect_tools=None, expect_keywords=None,
                 forbidden=None):
        case = {"id": "e%d" % int(time.time() * 1000),
                "name": str(name or "未命名用例").strip()[:60],
                "input": str(input_text or "").strip(),
                "expect_tools": [str(t) for t in (expect_tools or [])],
                "expect_keywords": [str(k) for k in (expect_keywords or [])],
                "forbidden": [str(k) for k in (forbidden or [])]}
        if not case["input"]:
            raise ValueError("用例输入为空")
        with self.lock:
            self.cases.append(case)
            del self.cases[:-50]
            self._save()
        return case

    def delete_case(self, cid):
        with self.lock:
            before = len(self.cases)
            self.cases = [c for c in self.cases if c["id"] != cid]
            if len(self.cases) != before:
                self._save()
                return True
            return False

    @staticmethod
    def _judge(case, actual_tools, final):
        reasons = []
        expect = set(case.get("expect_tools") or [])
        actual = set(actual_tools or [])
        if expect and not expect <= actual:
            reasons.append("缺少工具调用: %s" % sorted(expect - actual))
        if not expect and actual:
            reasons.append("不应调用工具却调用了: %s" % sorted(actual))
        low = (final or "").lower()
        for k in case.get("expect_keywords") or []:
            if str(k).lower() not in low:
                reasons.append("回答缺少关键词: %s" % k)
        for k in case.get("forbidden") or []:
            if str(k).lower() in low:
                reasons.append("回答含禁止内容: %s" % k)
        return (not reasons), reasons

    def _judge_quality(self, question, answer):
        """LLM-as-judge:回答质量 1-10 分;不可用返回 None。"""
        if not self.judge_llm:
            return None
        try:
            import json as _json
            res = self.judge_llm.chat(
                [{"role": "user",
                  "content": "评估以下回答质量(相关性/准确性/帮助性),1-10 分。"
                             "只输出 JSON:{\"score\": 分数}\n问题: %s\n回答: %s"
                             % (question[:300], answer[:600])}],
                temperature=0.1)
            m = re.search(r"\{[\s\S]*\}", res.content or "")
            obj = _json.loads(m.group(0)) if m else {}
            score = obj.get("score")
            return round(float(score), 1) if score is not None else None
        except Exception:
            return None

    def run(self, brain):
        results = []
        for case in self.list_cases():
            try:
                r = brain.eval_run(case["input"])
                ok, reasons = self._judge(case, r["tools"], r["final"])
                quality = self._judge_quality(case["input"], r["final"]) if ok else None
                results.append({"id": case["id"], "name": case["name"], "ok": ok,
                                "tools": r["tools"], "reasons": reasons,
                                "quality": quality,
                                "llm_calls": r.get("stats", {}).get("llm_calls", 0),
                                "final": r["final"][:200]})
            except Exception as e:
                results.append({"id": case["id"], "name": case["name"], "ok": False,
                                "tools": [], "reasons": ["异常: %s" % e],
                                "quality": None, "llm_calls": 0, "final": ""})
        passed = sum(1 for r in results if r["ok"])
        score = round(100.0 * passed / len(results), 1) if results else 0.0
        qualities = [r["quality"] for r in results if r["quality"] is not None]
        quality_avg = round(sum(qualities) / len(qualities), 1) if qualities else None
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "score": score,
                 "quality_avg": quality_avg,
                 "passed": passed, "total": len(results),
                 "results": [{"id": r["id"], "name": r["name"], "ok": r["ok"],
                              "quality": r["quality"], "llm_calls": r["llm_calls"],
                              "reasons": r["reasons"]} for r in results]}
        with self.lock:
            hist = self._history()
            hist.append(entry)
            del hist[:-60]
            try:
                with open(self.history_path, "w", encoding="utf-8") as f:
                    json.dump(hist, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
        if self.events:
            prev = hist[-2]["score"] if len(hist) > 1 else None
            trend = ""
            if prev is not None and score < prev:
                trend = " ⚠️ 比上次下降(%.1f → %.1f),最近改动可能引入回归!" % (prev, score)
            self.events.publish("eval",
                                text="🧪 回归评估:健康分 %.1f(%d/%d 通过)%s"
                                     % (score, passed, len(results), trend))
        return entry

    def _history(self):
        try:
            with open(self.history_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def history(self, n=40):
        return self._history()[-n:]

    def last_score(self):
        h = self._history()
        return h[-1]["score"] if h else None
