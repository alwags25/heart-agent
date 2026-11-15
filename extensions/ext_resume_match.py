META = {"name": "resume_match", "description": "简历与岗位描述匹配度打分(纯本地关键词分析)"}

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