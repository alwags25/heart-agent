"""协作AI(记忆管家):与主控AI组成双模型协作。
主控AI负责思考决策,协作AI专职记忆 —— 从对话中提炼长期记忆、基于记忆回答检索问题。"""
import json
import re

from .llm import LLMError

ORG_SYS = "你是「协作AI」,主控AI的专职记忆管家。你严谨、简洁,只输出被要求的内容。"


def _extract_json_array(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
    except Exception:
        pass
    s, e = text.find("["), text.rfind("]")
    if s != -1 and e > s:
        try:
            obj = json.loads(text[s:e + 1])
            if isinstance(obj, list):
                return obj
        except Exception:
            pass
    return None


class AssistantAI:
    def __init__(self, llm, memory, events=None):
        self.llm = llm
        self.memory = memory
        self.events = events

    def organize_memory(self, chat_records):
        """从近期对话中提炼值得长期记住的事实,写入记忆库。返回新增事实列表。"""
        lines = ["%s: %s" % (r.get("role"), r.get("content")) for r in chat_records]
        prompt = (
            "以下是主控AI与用户的近期对话记录。请提炼值得长期记住的信息"
            "(用户偏好、项目背景、重要决定、新知识),输出 JSON 数组,每项格式:\n"
            '{"content": "事实描述(一句话)", "tags": ["标签"]}\n'
            "没有值得记的就输出 []。只输出 JSON,不要解释。\n\n对话:\n"
            + "\n".join(lines)
        )[-6000:]
        raw = self.llm.chat(
            [{"role": "system", "content": ORG_SYS},
             {"role": "user", "content": prompt}],
            temperature=0.3)
        raw_text = raw.content if hasattr(raw, "content") else str(raw)
        items = _extract_json_array(raw_text) or []
        added = []
        for it in items[:10]:
            if isinstance(it, dict) and it.get("content"):
                tags = it.get("tags") if isinstance(it.get("tags"), list) else []
                f = self.memory.add_fact(str(it["content"]), tags=tags, source="协作AI")
                added.append(f["content"])
        if self.events and added:
            preview = "; ".join(added[:3]) + ("…" if len(added) > 3 else "")
            self.events.publish("assistant_ai",
                                text="协作AI 整理记忆:沉淀 %d 条 → %s" % (len(added), preview))
        return added

    def recall(self, query):
        """基于记忆库回答检索类问题。"""
        hits = self.memory.search_facts(query, top=8)
        if not hits:
            return "记忆库中没有相关内容。"
        ctx = "\n".join("- " + h["content"] for h in hits)
        try:
            ans = self.llm.chat(
                [{"role": "system", "content": ORG_SYS},
                 {"role": "user",
                  "content": "根据以下记忆回答问题,简洁作答;记忆不足就如实说明。\n\n记忆:\n%s\n\n问题:%s"
                             % (ctx, query)}],
                temperature=0.3).content
        except LLMError:
            ans = ctx
        return ans

    def handle(self, task):
        """主控AI委托入口:整理类请求 → 提炼记忆;其余 → 记忆检索。"""
        if any(k in task for k in ("整理", "提炼", "归纳", "总结记忆", "沉淀")):
            added = self.organize_memory(self.memory.chat_log(30))
            return "已整理记忆,新增 %d 条。" % len(added) if added else "没有提炼出新的长期记忆。"
        return self.recall(task)
