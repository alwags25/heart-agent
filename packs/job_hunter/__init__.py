"""求职管家心智包:HeartAgent 自指型演示 —— 用 Agent 找工作。
安装后:简历进记忆、目标系统追踪投递、两个扩展(简历匹配/面试题库)进审批队列。
"""
import json
import os

DIR = os.path.dirname(os.path.abspath(__file__))

RESUME_MATCH = '''META = {"name": "resume_match", "description": "简历与岗位描述匹配度打分(纯本地关键词分析)"}

import re
from collections import Counter

STOP = set("的 了 和 与 及 或 在 是 对 从 被 把 一个 我们 你们 他们 这个 那个 以及 所以 但是 因为 如果 要求 负责 参与 使用 熟悉 精通 了解 相关 优先 经验 岗位 工作 公司 团队 能力 以上职者".split())

def _kw(text, n=25):
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_+#.]{1,30}|[\\u4e00-\\u9fff]{2,6}", (text or "").lower())
    words = [w for w in words if w not in STOP and not w.isdigit()]
    return [w for w, _c in Counter(words).most_common(n)]

def run(args):
    resume = str((args or {}).get("resume", ""))
    job = str((args or {}).get("job", ""))
    if len(resume) < 30 or len(job) < 30:
        return {"ok": False, "error": "resume 与 job 都需要较完整的文本(>=30字)"}
    rk, jk = _kw(resume), _kw(job)
    matched = [w for w in jk if w in set(rk)]
    missing = [w for w in jk if w not in set(rk)]
    score = round(100.0 * len(matched) / max(1, len(jk)), 1)
    verdict = "强烈建议投递" if score >= 60 else "可以一试" if score >= 40 else "匹配度偏低"
    return {"ok": True, "score": score, "verdict": verdict,
            "matched": matched[:12], "missing_keywords": missing[:12],
            "tip": "把 missing_keywords 里的词自然写进简历再投,命中率更高"}
'''

INTERVIEW_BANK = '''META = {"name": "interview_bank", "description": "按主题抽一道面试题并给评分要点"}

import random

BANK = {
  "python": [("解释 GIL 及其对多线程的影响", "IO密集vs CPU密集、multiprocessing、asyncio"),
             ("装饰器的实现原理与带参装饰器写法", "闭包、functools.wraps、语法糖")],
  "agent": [("Agent 如何避免陷入无效的工具调用循环?", "步数上限、结果校验、反思机制、评估回归"),
            ("长期记忆的写入时机该怎么设计?", "对话后提炼、睡眠整理、矛盾检测、重要性评分"),
            ("如何评估一个 Agent 的可靠性?", "用例回归、健康分、工具轨迹断言、防提示词注入")],
  "system": [("设计一个定时任务调度器要考虑什么?", "持久化、错过触发、并发上限、重试退避、幂等"),
             ("SQLite 在什么场景下优于独立数据库服务?", "单机嵌入式、WAL并发、零运维、备份即文件")],
  "behavior": [("讲一个你独立排查的最难 bug", "STAR:现象-定位-根因-预防"),
               ("你如何判断一个技术方案该自研还是用现成库?", "成本/可控性/维护面/逃生通道")]
}

def run(args):
    cat = str((args or {}).get("category", "")).lower()
    cats = [c for c in BANK if not cat or c == cat] or list(BANK)
    c = random.choice(cats)
    q, rubric = random.choice(BANK[c])
    return {"ok": True, "category": c, "question": q, "scoring_points": rubric,
            "tip": "先给结论再展开,面试官平均90秒决定印象分"}
'''

PACK = {
    "name": "job_hunter",
    "title": "💼 求职管家",
    "description": "岗位追踪 + 简历匹配 + 模拟面试:HeartAgent 自己帮你找工作",
    "extensions": [("resume_match", RESUME_MATCH, "简历与岗位描述匹配度打分"),
                   ("interview_bank", INTERVIEW_BANK, "按主题抽面试题+评分要点")],
    "goals": [{"title": "每周追踪并评估 3 个目标岗位",
               "description": "用 curiosity.sources 订阅招聘 RSS/页面;发现合适岗位用 resume_match 对照简历打分,60分以上建投递任务。",
               "check_every_hours": 72}],
    "appendix": ("求职管家准则:\n"
                 "1. 涉及简历/岗位的内容,优先用 resume_match 扩展量化匹配度;\n"
                 "2. 帮我改简历时,保留事实,只优化表述与关键词覆盖;\n"
                 "3. 模拟面试每次只问一题,答完先按评分要点打分再给参考答案;\n"
                 "4. 提到投递进度时,主动更新对应目标的进展(update_goal)。"),
}


def install(pack_name, memory, extensions, goals, events, data_dir):
    if pack_name != PACK["name"]:
        raise ValueError("未知扩展包: %s" % pack_name)
    result = {"extensions_pending": [], "goals": []}
    for name, code, desc in PACK["extensions"]:
        r = extensions.install(name, code, desc, approve=False)  # 走审批门
        result["extensions_pending"].append(r.get("name", name))
    for g in PACK["goals"]:
        goal = goals.add(g["title"], g["description"], g["check_every_hours"],
                         auto_decompose=False)
        result["goals"].append(goal["title"])
    if events:
        events.publish("insight",
                       text="💼 求职管家包已安装:扩展 %s 进入审批队列;目标「%s」已创建。"
                            "别忘了在 config.json 的 curiosity.sources 里添加招聘信息源(RSS/URL),"
                            "并把简历文本让我 remember 进记忆。"
                            % (",".join(result["extensions_pending"]),
                               ";".join(result["goals"])))
    return {"ok": True, **result}
