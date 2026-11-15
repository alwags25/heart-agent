"""提示词自进化:定期从近期对话取样,让主模型提出"附加行为准则"改进候选,
用同一批问题 A/B 对比(候选 vs 现行),由 LLM 评分,胜出才替换。
候选以"附加准则"形式叠加在系统提示词之后 —— 不动主提示词,可一键回滚。"""
import json
import os
import re

APPENDIX_MAX = 1200
BASE_PROMPT = ("你是 HeartAgent 智能体。根据记忆和常识,用中文简洁、准确、有帮助地回答用户问题。"
               "不要编造事实。")


class PromptEvolver:
    def __init__(self, main_llm, memory, events, data_dir):
        self.llm = main_llm
        self.memory = memory
        self.events = events
        self.path = os.path.join(data_dir, "prompt_appendix.txt")
        self.history_path = os.path.join(data_dir, "prompt_evolution.json")

    # ---------- 附录管理 ----------
    def appendix(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read().strip()[:APPENDIX_MAX]
        except OSError:
            return ""

    def set_appendix(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write((text or "").strip()[:APPENDIX_MAX])
        self._log("applied", (text or "")[:200])

    def reset(self):
        if os.path.exists(self.path):
            os.remove(self.path)
        self._log("reset", "")

    def _log(self, action, detail):
        try:
            hist = self._read_history()
            hist.append({"ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                         "action": action, "detail": str(detail)[:200]})
            with open(self.history_path, "w", encoding="utf-8") as f:
                json.dump(hist[-30:], f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def _read_history(self):
        try:
            with open(self.history_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def history(self):
        return self._read_history()

    # ---------- 进化实验 ----------
    def _sample_qa(self, n):
        msgs = self.memory.chat_log(80)
        qa = []
        i = 0
        while i < len(msgs) - 1 and len(qa) < n:
            if msgs[i]["role"] == "user" and msgs[i + 1]["role"] == "assistant":
                q = msgs[i]["content"].strip()
                if 8 <= len(q) <= 200:
                    qa.append((q, msgs[i + 1]["content"]))
                    i += 2
                    continue
            i += 1
        return qa

    def maybe_run(self, state, sample_size=4, min_gain=0.4):
        qa = self._sample_qa(sample_size)
        if len(qa) < 3:
            return None
        current = self.appendix()
        cur_block = ("\n\n附加行为准则:\n" + current) if current else ""
        # 1) 提出候选
        sample_text = "\n".join("问: %s\n答: %s" % (q, a[:200]) for q, a in qa)[-3000:]
        res = self.llm.chat(
            [{"role": "user",
              "content": "你是 HeartAgent 的提示词优化器。以下是近期的问答样本:\n%s\n\n"
                         "请指出当前回答的不足,并起草一份'附加行为准则'(不超过300字,5条以内,"
                         "可执行的中文指令,不要客套),用于改进今后所有回答。"
                         "只输出 JSON: {\"critique\": \"...\", \"candidate\": \"准则全文\"}"
                         % sample_text}], temperature=0.5)
        obj = self._extract_json(res.content or "")
        cand = str((obj or {}).get("candidate", "")).strip()
        if not cand or cand == current:
            return {"ran": True, "result": "未产生有效候选"}
        cand_block = "\n\n附加行为准则:\n" + cand
        # 2) A/B:同一批问题,当前 vs 候选
        cur_answers, cand_answers = [], []
        for q, _a in qa:
            try:
                cur_answers.append(self.llm.chat(
                    [{"role": "system", "content": BASE_PROMPT + cur_block},
                     {"role": "user", "content": q}], temperature=0.3).content)
                cand_answers.append(self.llm.chat(
                    [{"role": "system", "content": BASE_PROMPT + cand_block},
                     {"role": "user", "content": q}], temperature=0.3).content)
            except Exception:
                return {"ran": True, "result": "A/B 调用失败,跳过本轮"}
        # 3) 评审
        judge_text = "\n\n".join(
            "问题%d: %s\n[现行回答] %s\n[候选回答] %s" % (i + 1, qa[i][0],
                                                        cur_answers[i][:400], cand_answers[i][:400])
            for i in range(len(qa)))
        judge = self.llm.chat(
            [{"role": "user",
              "content": "对以下每组回答按 质量/准确性/帮助性 打分(1-10)。只输出 JSON:"
                         "{\"current\": [分数数组], \"candidate\": [分数数组]}\n\n%s" % judge_text}],
            temperature=0.1)
        scores = self._extract_json(judge.content or "") or {}
        cur_s = self._avg(scores.get("current"))
        cand_s = self._avg(scores.get("candidate"))
        verdict = {"ran": True, "current": cur_s, "candidate": cand_s,
                   "critique": str((obj or {}).get("critique", ""))[:300]}
        if cur_s is not None and cand_s is not None and cand_s >= cur_s + min_gain:
            self.set_appendix(cand)
            verdict["applied"] = True
            if self.events:
                self.events.publish("insight",
                                    text="🧬 提示词进化:候选准则胜出(%.1f vs %.1f)并已应用。%s"
                                         % (cand_s, cur_s, verdict["critique"]))
        else:
            verdict["applied"] = False
            self._log("rejected", "cand=%s cur=%s" % (cand_s, cur_s))
            if self.events:
                self.events.publish("insight",
                                    text="🧬 提示词进化:候选未胜出(%.1f vs %.1f),保持现状。"
                                         % (cand_s or 0, cur_s or 0))
        return verdict

    @staticmethod
    def _extract_json(text):
        text = (text or "").strip()
        try:
            return json.loads(text)
        except Exception:
            s, e = text.find("{"), text.rfind("}")
            if s != -1 and e > s:
                try:
                    return json.loads(text[s:e + 1])
                except Exception:
                    return None
        return None

    @staticmethod
    def _avg(arr):
        try:
            nums = [float(x) for x in (arr or []) if isinstance(x, (int, float))]
            return round(sum(nums) / len(nums), 2) if nums else None
        except Exception:
            return None
