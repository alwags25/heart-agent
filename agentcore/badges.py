"""运行数据徽章:纯计算模块 —— 收集运行数据并渲染 SVG 徽章内容(不含任何文件 IO)。
写盘由 main.refresh_badges 统一完成(集中校验,防路径穿越)。
- 自主运行天数 / 任务执行与成功率 / 记忆条数 / 回归健康分 / 本月花费
- 心跳每日刷新一次,也可 POST /api/badges 手动刷新"""


def _esc(s):
    return (str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _badge(label, value, color="#4c1"):
    """标准扁平双段徽章。中文按 1.1 倍宽估算。"""
    def w(t):
        return sum(11 if ord(c) > 0x2E80 else 6.5 for c in str(t))
    lw = int(w(label) + 12)
    vw = int(w(value) + 12)
    total = lw + vw
    lx = int(lw * 5 - w(label) * 5)
    vx = int((lw + vw / 2) * 10 - w(value) * 5)
    L, V = _esc(label), _esc(value)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{total}" height="20" role="img"><title>{L}: {V}</title><linearGradient id="s" x2="0" y2="100%"><stop offset="0" stop-color="#bbb" stop-opacity=".1"/><stop offset="1" stop-opacity=".1"/></linearGradient><clipPath id="r"><rect width="{total}" height="20" rx="3" fill="#fff"/></clipPath><g clip-path="url(#r)"><rect width="{lw}" height="20" fill="#dfdfdf"/><rect x="{lw}" width="{vw}" height="20" fill="{color}"/><rect width="{total}" height="20" fill="url(#s)"/></g><g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="110" text-rendering="geometricPrecision"><text transform="scale(.1)" x="{lx}" y="145" fill="#010101" fill-opacity=".3">{L}</text><text transform="scale(.1)" x="{lx}" y="140">{L}</text><text transform="scale(.1)" x="{vx}" y="145" fill="#010101" fill-opacity=".3">{V}</text><text transform="scale(.1)" x="{vx}" y="140">{V}</text></g></svg>'''


def collect(app):
    """汇总运行数据(供徽章与日报共用)。"""
    hb = app["heartbeat"]
    st = hb.state or {}
    first = st.get("first_beat_date") or __import__("time").strftime("%Y-%m-%d")
    try:
        days = max(1, (__import__("time").time()
                       - __import__("time").mktime(__import__("time").strptime(first, "%Y-%m-%d"))) // 86400 + 1)
    except Exception:
        days = 1
    total_runs = failed = 0
    for t in app["tasks"].list():
        for h in t.get("history", []):
            total_runs += 1
            failed += 0 if h.get("ok") else 1
    rate = round(100.0 * (total_runs - failed) / total_runs) if total_runs else 100
    spent = app["usage"].cost_of(
        app["usage"].snapshot().get("month"),
        (app["cfg"].get("wallet") or {}).get("prices") or {})
    mem = app["memory"].overview()
    try:
        stability = app["uptime"].snapshot()
    except Exception:
        stability = {}
    return {"days": int(days), "runs": total_runs, "rate": rate,
            "facts": mem.get("facts", 0), "knowledge": mem.get("knowledge", 0),
            "spent": round(spent, 2), "beats": hb.beats,
            "eval_score": app["evals"].last_score(),
            "stability": stability}


def render(d):
    """把 collect() 的数据渲染为 {文件名: SVG内容}。纯函数,无 IO。"""
    import json as _json
    out = {
        "uptime.svg": _badge("自主运行", "%d 天" % d["days"], "#1f6feb"),
        "tasks.svg": _badge("任务执行", "%d 次 · 成功率 %d%%" % (d["runs"], d["rate"]),
                            "#2da44e" if d["rate"] >= 80 else "#bf8700"),
        "memory.svg": _badge("长期记忆", "%d 条" % d["facts"], "#8250df"),
        "eval.svg": _badge("回归健康分",
                           ("%.0f" % d["eval_score"]) if d["eval_score"] is not None else "未测",
                           "#0969da"),
        "cost.svg": _badge("本月花费", "¥%.2f" % d["spent"], "#cf222e"),
    }
    up = d.get("stability") or {}
    crashes = up.get("crashes", 0)
    if crashes:
        stab_val = "宕机 %d 次" % crashes
        stab_color = "#cf222e"
    elif up.get("starts"):
        stab_val = "无宕机 · 启动 %d 次" % up.get("starts", 0)
        stab_color = "#2da44e"
    else:
        stab_val = "未记录"
        stab_color = "#8b949e"
    out["stability.svg"] = _badge("稳定性", stab_val, stab_color)
    out["summary.json"] = _json.dumps(d, ensure_ascii=False, indent=1)
    return out


def ensure_first_beat(app):
    """记录首次心跳日期(运行天数的起点)。"""
    hb = app["heartbeat"]
    if not hb.state.get("first_beat_date"):
        hb.state["first_beat_date"] = __import__("time").strftime("%Y-%m-%d")
        hb._save_state()
