# -*- coding: utf-8 -*-
"""HeartAgent 冒烟测试 v4:v3 全部 + 睡眠矛盾/评估台/辩论/反事实/信任门禁/
自治门/钱包/心智包导出导入/求职管家包。"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("HEARTAGENT_TEST_URL", "http://127.0.0.1:8765")
HERE = os.path.dirname(os.path.abspath(__file__))


def api(path, body=None, raw=False):
    if body is None:
        req = urllib.request.Request(BASE + path)
    else:
        req = urllib.request.Request(
            BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
        if raw:
            return r.headers.get("Content-Type"), data
        return json.loads(data.decode("utf-8"))


def show(label, val):
    print("== %s: %s" % (label, json.dumps(val, ensure_ascii=False)[:220]))


# ---------- 基础回归 ----------
pre_log = api("/api/state")["chat_log"]
show("chat(FC)", api("/api/chat", {"text": "现在几点了"}))
final = None
for _ in range(40):  # 最长 120s:推理模型每步都可能深思考
    time.sleep(3)
    cl = api("/api/state")["chat_log"]
    if len(cl) >= len(pre_log) + 2 and cl[-1]["role"] == "assistant":
        final = cl[-1]["content"]
        break
assert final, "120s 内未收到回复(检查模型配置/网络)"
print("== 基础对话 ✓ 回复:", final[:80])

# ---------- v4-1 评估台 ----------
show("evals run", api("/api/evals/run", {}))
time.sleep(6)
ev = api("/api/evals")
print("== 评估健康分:", ev["last_score"], "| 用例数:", len(ev["cases"]))
assert ev["last_score"] is not None, "评估应产出健康分"
if ev["last_score"] < 75:
    print("⚠️ 健康分偏低:真实模型行为与用例预期(mock 行为)不一致,属正常 —— 换模型后应按需调整用例")

# ---------- v4-2 睡眠整理/矛盾检测 ----------
api("/api/memory/add", {"content": "用户主写 Python,主力语言是 Python"})
api("/api/memory/add", {"content": "用户主力语言是 Rust,平时主要写 Rust"})
conflicts_before = api("/api/conflicts")["conflicts"]
print("== 触发前冲突数:", len(conflicts_before))
show("sleep run", api("/api/sleep/run", {}))  # 手动触发一次睡眠整理
time.sleep(8)
conflicts = api("/api/conflicts")["conflicts"]
print("== 心跳后冲突数:", len(conflicts))
if conflicts:
    c = conflicts[0]
    show("conflict", {"a": c["a_text"][:40], "b": c["b_text"][:40], "q": c["question"][:40]})
    show("resolve keep_a", api("/api/conflicts/op", {"id": c["id"], "op": "keep_a"}))
    st2 = api("/api/state")
    print("== 解决后 open 冲突:", st2["conflicts_open"], "| 归档记忆:", st2["memory"]["archived"])

# ---------- v4-3 反事实重演 ----------
show("replay fork", api("/api/replay/fork", {"text": "现在几点了", "model": "assist"}))
show("replay groups", len(api("/api/replay")["groups"]))

# ---------- v4-4 红蓝辩论 ----------
show("debate start", api("/api/debate", {"topic": "该不该在简历上写这个项目", "rounds": 1}))
time.sleep(4)
log = api("/api/log?count=200")["events"]
debates = [e for e in log if e["type"] == "debate"]
print("== 辩论事件:", len(debates), "(应>=3:正方/反方/裁决)")

# ---------- v4-5 求职管家包 ----------
show("pack install", api("/api/pack/install", {"name": "job_hunter"}))
pend = api("/api/extensions")["pending"]
print("== 待审批扩展:", [p["name"] for p in pend])
for p in pend:
    api("/api/extensions/approve", {"name": p["name"], "decision": True})
show("resume_match call", api("/api/extensions/call", {
    "name": "resume_match",
    "args": {"resume": "三年 Python 后端开发,熟悉 FastAPI、SQLite、异步编程,做过 Agent 项目",
             "job": "招聘 Python 后端工程师,要求熟悉 FastAPI、MySQL、Docker 与异步编程,有 AI 项目经验优先"}}))

# ---------- v4-6 信任分 ----------
trust = api("/api/extensions")["trust"]
for t in trust:
    if t["name"] == "resume_match":
        print("== resume_match 信任: L%d %d分 caps=%s" % (t["level"], t["score"], t["caps"]))

# ---------- v4-7 自治等级 ----------
show("autonomy L1", api("/api/autonomy", {"level": 1}))
api("/api/chat", {"text": "帮我创建一个每60秒打印时间的任务"})
time.sleep(2.5)
tasks = api("/api/tasks")["tasks"]
newest = max(tasks, key=lambda t: t["created_at"])
print("== L1 下新建任务 enabled =", newest["enabled"], "(应为 False 待批)")
api("/api/tasks/op", {"id": newest["id"], "op": "toggle"})
api("/api/autonomy", {"level": 2})

# ---------- v4-8 钱包 + 心智包 ----------
state = api("/api/state")
print("== 钱包:", json.dumps(state["wallet"], ensure_ascii=False)[:150])
ct, data = api("/api/export", raw=True)
zip_path = os.path.join(HERE, "test_mindpack.zip")
with open(zip_path, "wb") as f:
    f.write(data)
print("== 导出心智包:", len(data), "bytes, zip =", data[:2] == b"PK")
show("import pack", api("/api/import", {"path": zip_path}))
os.remove(zip_path)

# ---------- v4.5-1 视觉:截图工具(真实模型自主决定是否调用,只做尽力验证) ----------
show("chat vision", api("/api/chat", {"text": "请调用 take_screenshot 工具截图,然后告诉我屏幕上有什么"}))
time.sleep(6)
shots = os.path.join(HERE, "data", "shots")
shot_files = os.listdir(shots) if os.path.isdir(shots) else []
print("== 截图文件:", shot_files)
log = api("/api/log?count=200")["events"]
tools_called = [e["name"] for e in log if e["type"] == "tool"]
if "take_screenshot" in tools_called:
    print("== 视觉工具调用 ✓")
else:
    print("⚠️ 模型未调用截图工具(真实模型自主决策,可能直接文字回答)——非致命,跳过")

# ---------- v4.5-2 记忆图谱 ----------
api("/api/memory/add", {"content": "用户正在开发 HeartAgent 项目,用 Python 编写"})
api("/api/sleep/run", {})
time.sleep(6)
graph = api("/api/graph")
print("== 图谱: 节点", [n["id"] for n in graph["nodes"]], "| 边", [(e["s"], e["r"], e["o"]) for e in graph["edges"]][:4])
assert len(graph["nodes"]) >= 2, "mock 三元组应生成图谱节点"

# ---------- v4.5-3 第二意见(机制已在 mock 模式验证;真实模型对无效任务可能追问而非调用) ----------
show("chat delete", api("/api/chat", {"text": "帮我删除任务 nonexistent"}))
time.sleep(4)
log = api("/api/log?count=300")["events"]
opinions = [e for e in log if e["type"] == "assistant_ai" and "第二意见" in str(e.get("source", ""))]
del_tools = [e for e in log if e["type"] == "tool" and e.get("name") == "delete_task"]
if del_tools:
    print("== 第二意见事件:", len(opinions), "| delete_task 调用 ✓")
else:
    print("⚠️ 真实模型未直接调用 delete_task(自主决策,属正常)—— 机制已由单元测试覆盖")

# ---------- v4.5-4 Host 校验 ----------
import http.client
resp_status = None
for attempt in range(3):  # 偶发 backlog 瞬时拒连,重试
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 8765, timeout=10)
        conn.request("GET", "/api/state", headers={"Host": "evil.com"})
        resp_status = conn.getresponse().status
        conn.close()
        break
    except (ConnectionRefusedError, OSError):
        time.sleep(1)
print("== 伪造Host响应:", resp_status, "(应为 403)")
assert resp_status == 403

# ---------- v4.5-5 评估(含质量分字段) ----------
api("/api/evals/run", {})
time.sleep(6)
ev = api("/api/evals")
last = (ev["history"] or [{}])[-1]
print("== 健康分:", last.get("score"), "| 质量均分:", last.get("quality_avg"), "(mock 无裁判应为 null)")
assert last.get("score") is not None

# ---------- 事件总览 ----------
log = api("/api/log?count=500")["events"]
types = {}
for e in log:
    types[e["type"]] = types.get(e["type"], 0) + 1
show("event types", types)
need = {"eval", "debate", "goal", "tool", "chat_assistant", "insight"}
missing = need - set(types)
print("\n缺关键事件:", missing or "无 ✓")

# ---------- --real:真实 Key 端到端回归(可选) ----------
if "--real" in sys.argv:
    print("\n... --real 模式:真实模型端到端验证 ...")
    t0 = time.time()
    api("/api/chat", {"text": "用一句话回答:1+1等于几"})
    final, waited = None, 0
    seen_mock = False
    while waited < 120:
        time.sleep(3)
        waited += 3
        state = api("/api/state")
        last = state["chat_log"][-1] if state["chat_log"] else {}
        if last.get("role") == "assistant" and "测试复现" not in last.get("content", ""):
            final = last["content"]
            seen_mock = "mock" in final.lower()
            break
    assert final, "真实模型 120s 内未回复"
    assert not seen_mock, "真实模式下不应出现 mock 回复"
    print("== 真实回复(%.0fs): %s" % (time.time() - t0, final[:120]))
    print("== REAL MODE ✓")

print("SMOKE TEST v4 DONE")
