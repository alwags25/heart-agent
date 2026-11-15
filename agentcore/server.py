"""Web 服务 v3:双屏 UI 后端。
在 v2 基础上新增:目标系统 CRUD/手动检查、A2A 同伴委托端点、知识库摄入与检索、
提示词附录(进化)查看/编辑/重置、收件箱状态。仅绑定 127.0.0.1,纯标准库。"""
import io
import json
import os
import queue
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # ---------- 基础 ----------
        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _bytes(self, body, content_type, filename=None):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if filename:
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"' % filename)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                raise ValueError("请求体不是合法 JSON: %s" % e)

        # ---------- 安全校验(Host 防DNS重绑定 / Origin 防跨站 / 可选令牌) ----------
        def _security_check(self):
            sec = app["cfg"].get("security") or {}
            host = self.headers.get("Host") or ""
            allowed = ("127.0.0.1:%s" % app.get("port"), "localhost:%s" % app.get("port"))
            if host and host not in allowed:
                return "forbidden-host"
            origin = self.headers.get("Origin")
            if origin:
                try:
                    from urllib.parse import urlparse
                    if urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                        return "forbidden-origin"
                except Exception:
                    return "forbidden-origin"
            token = str(sec.get("token") or "")
            if token:
                provided = self.headers.get("X-HeartAgent-Token") or ""
                if not provided and "token=" in self.path:
                    provided = self.path.split("token=")[1].split("&")[0]
                if provided != token:
                    return "unauthorized"
            return None

        # ---------- GET ----------
        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path.startswith("/api/"):
                    chk = self._security_check()
                    if chk == "unauthorized":
                        return self._json({"error": "unauthorized", "need_token": True}, 401)
                    if chk:
                        return self._json({"error": chk}, 403)
                if path in ("/", "/index.html"):
                    return self._serve_index()
                if path == "/api/events":
                    return self._sse()
                if path == "/api/log":
                    n = 200
                    try:
                        n = int(self.path.split("count=")[1].split("&")[0])
                    except Exception:
                        pass
                    return self._json({"events": app["bus"].recent(n)})
                if path == "/api/state":
                    return self._json(self._state())
                if path == "/api/tasks":
                    return self._json({"tasks": app["tasks"].list()})
                if path == "/api/memory":
                    return self._json({"facts": app["memory"].facts(),
                                       "summaries": app["memory"].summaries()})
                if path == "/api/extensions":
                    return self._json({"extensions": app["extensions"].list(),
                                       "pending": app["extensions"].pending(),
                                       "usage": app["extensions"].usage(),
                                       "trust": app["extensions"].trust_all(),
                                       "audit": app["extensions"].audit_tail(20)})
                if path == "/api/goals":
                    return self._json({"goals": app["goals"].list()})
                if path == "/api/badges":
                    from agentcore.badges import render
                    return self._json({"summary": __import__("agentcore.badges", fromlist=["render"]).collect(app)})
                if path.startswith("/api/badges/"):
                    name = path.rsplit("/", 1)[-1]
                    from agentcore import badges as _badges
                    allowed = {"uptime", "tasks", "memory", "eval", "cost"}
                    if name == "summary.json":
                        return self._json(_badges.collect(app))
                    if name.replace(".svg", "") in allowed and name.endswith(".svg"):
                        files = _badges.render(_badges.collect(app))
                        content = files.get(name, "").encode("utf-8")
                        return self._bytes(content, "image/svg+xml")
                    return self._json({"error": "unknown badge"}, 404)
                if path == "/api/sessions":
                    return self._json({"sessions": app["memory"].list_sessions()})
                if path == "/api/usage":
                    return self._json({"usage": app["usage"].snapshot()})
                if path == "/api/metrics":
                    return self._json(self._metrics())
                if path == "/api/evals":
                    return self._json({"cases": app["evals"].list_cases(),
                                       "history": app["evals"].history(30),
                                       "last_score": app["evals"].last_score()})
                if path == "/api/replay":
                    return self._json({"groups": self._replay_groups(30)})
                if path == "/api/conflicts":
                    return self._json({"conflicts": app["memory"].list_conflicts("all")})
                if path == "/api/graph":
                    return self._json(app["memory"].graph())
                if path == "/api/prompt":
                    return self._json({"appendix": app["evolver"].appendix(),
                                       "history": app["evolver"].history()[-10:]})
                if path == "/api/inbox":
                    return self._json(app["inbox"].status())
                if path == "/api/export":
                    return self._export()
                if path == "/api/export/chat.md":
                    return self._export_chat_md()
                if path.startswith("/static/"):
                    return self._serve_static(path[len("/static/"):])
                if path.startswith("/api/shots/"):
                    return self._serve_shot(path.rsplit("/", 1)[-1])
                if path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)

        def _serve_index(self):
            try:
                with open(app["index_path"], "rb") as f:
                    body = f.read()
            except OSError:
                return self._json({"error": "web/index.html 缺失"}, 500)
            self._bytes(body, "text/html; charset=utf-8")

        def _serve_static(self, rel):
            """静态资源服务(/static/*):为前端模块化拆分 CSS/JS 提供通道,防路径穿越。"""
            web_dir = os.path.dirname(os.path.abspath(app["index_path"]))
            p = os.path.normpath(os.path.join(web_dir, rel))
            if os.path.dirname(p) != web_dir or not os.path.isfile(p):
                return self._json({"error": "not found"}, 404)
            ct = ("text/css" if p.endswith(".css") else
                  "application/javascript" if p.endswith(".js") else
                  "text/html" if p.endswith(".html") else "application/octet-stream")
            with open(p, "rb") as f:
                self._bytes(f.read(), ct)

        def _replay_groups(self, count=30):
            """把落盘事件按对话轮分组,供思考回放器使用。"""
            path = app.get("events_sink")
            groups = []
            if not path or not os.path.exists(path):
                return groups
            try:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()[-4000:]
            except OSError:
                return groups
            cur = None
            for line in lines:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                t = ev.get("type")
                if t in ("chat_user", "inbound"):
                    cur = {"id": len(groups), "ts": ev.get("ts", ""),
                           "label": ("📨 " if t == "inbound" else "") +
                                    str(ev.get("text", ""))[:60],
                           "events": [ev]}
                    groups.append(cur)
                elif cur is not None:
                    cur["events"].append(ev)
                    if len(cur["events"]) > 80:
                        cur["events"] = cur["events"][-80:]
            return groups[-count:]

        def _state(self):
            hb = app["heartbeat"]
            mem = app["memory"]
            return {
                "status": {"beats": hb.beats, "last_beat": hb.last_beat,
                           "uptime_s": round(time.time() - app["start_time"]),
                           "version": app["version"]},
                "model": app["model_labels"],
                "memory": mem.overview(),
                "chat_log": mem.chat_log(60),
                "session": {"current": mem.current_session(),
                            "sessions": mem.list_sessions()},
                "usage": app["usage"].snapshot(),
                "pending_extensions": len(app["extensions"].pending()),
                "inbox": app["inbox"].status() if app.get("inbox") else {},
                "peers": [p.get("name") for p in app.get("peers", [])],
                "autonomy": app["brain"].autonomy_level,
                "eval_score": (app["evals"].last_score() if app.get("evals") else None),
                "conflicts_open": app["memory"].overview().get("conflicts_open", 0),
                "wallet": self._wallet(),
            }

        def _wallet(self):
            wallet = (app["cfg"].get("wallet") or {})
            budget = float(wallet.get("monthly_budget_cny", 0) or 0)
            snap = app["usage"].snapshot()
            spent = app["usage"].cost_of(snap.get("month"), wallet.get("prices") or {})
            return {"budget": budget, "spent": spent,
                    "remaining": round(budget - spent, 2),
                    "by_model": snap.get("month", {})}

        def _metrics(self):
            """运行指标汇总:token 用量 / 任务成功率 / 健康分历史 / 花费 / 记忆规模。"""
            usage = app["usage"].snapshot()
            wallet = (app["cfg"].get("wallet") or {})
            hb = app["heartbeat"]
            runs = failed = 0
            for t in app["tasks"].list():
                for h in t.get("history", []):
                    runs += 1
                    failed += 0 if h.get("ok") else 1
            evals = app["evals"]
            return {
                "uptime_s": round(time.time() - app["start_time"]),
                "beats": hb.beats,
                "tokens": usage.get("total", {}),
                "tokens_by_model": usage.get("month", {}),
                "tasks": {
                    "runs": runs, "failed": failed,
                    "success_rate": round(100.0 * (runs - failed) / runs, 1) if runs else 100.0,
                    "count": len(app["tasks"].list()),
                },
                "memory": app["memory"].overview(),
                "wallet": {
                    "budget": float(wallet.get("monthly_budget_cny", 0) or 0),
                    "spent": app["usage"].cost_of(usage.get("month"), wallet.get("prices") or {}),
                },
                "extensions": len(app["extensions"].list()),
                "eval_score": evals.last_score(),
                "eval_history": evals.history(30),
            }

        def _export(self):
            from . import mindpack
            body, _manifest = mindpack.export_pack(app["base_dir"], app["data_dir"],
                                                   app["cfg"], memory=app["memory"])
            self._bytes(body, "application/zip",
                        "heartagent_mindpack_%s.zip" % time.strftime("%Y%m%d_%H%M%S"))

        def _export_chat_md(self):
            """当前会话导出为 Markdown(分享/存档)。"""
            mem = app["memory"]
            lines = ["# HeartAgent 对话记录", "",
                     "- 会话: %s" % mem.current_session(),
                     "- 导出时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"), ""]
            for m in mem.chat_log(400):
                who = "🧑 我" if m["role"] == "user" else "🤖 主控AI"
                lines += ["## %s(%s)" % (who, m.get("ts", "")), "", m["content"], ""]
            body = "\n".join(lines).encode("utf-8")
            self._bytes(body, "text/markdown; charset=utf-8",
                        "heartagent_chat_%s.md" % time.strftime("%Y%m%d_%H%M%S"))

        @staticmethod
        def _speak(text):
            """Windows SAPI 文字转语音(尽力而为)。"""
            import subprocess
            safe = text.replace("'", " ").replace('"', " ")[:600]
            ps = ("Add-Type -AssemblyName System.Speech;"
                  "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                  "$s.Speak('%s')" % safe)
            try:
                subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, timeout=60)
            except Exception:
                pass

        def _serve_shot(self, name):
            """提供截图文件(文件名白名单校验,防路径穿越)。"""
            shots_dir = os.path.normpath(os.path.abspath(
                os.path.join(app["data_dir"], "shots")))
            p = os.path.normpath(os.path.abspath(os.path.join(shots_dir, name)))
            if (os.path.dirname(p) != shots_dir or not name.endswith(".png")
                    or not os.path.exists(p)):
                return self._json({"error": "not found"}, 404)
            with open(p, "rb") as f:
                self._bytes(f.read(), "image/png")

        # ---------- SSE ----------
        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = app["bus"].subscribe()
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                        data = json.dumps(ev, ensure_ascii=False, default=str)
                        self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (ConnectionError, BrokenPipeError, OSError):
                pass
            finally:
                app["bus"].unsubscribe(q)

        # ---------- POST ----------
        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                if path.startswith("/api/"):
                    chk = self._security_check()
                    if chk == "unauthorized":
                        return self._json({"error": "unauthorized", "need_token": True}, 401)
                    if chk:
                        return self._json({"error": chk}, 403)
                body = self._body()
                if path == "/api/chat":
                    text = str(body.get("text", "")).strip()
                    if not text:
                        return self._json({"ok": False, "error": "消息为空"}, 400)
                    threading.Thread(target=app["brain"].handle_user, args=(text,),
                                     daemon=True).start()
                    return self._json({"ok": True})
                if path == "/api/chat/stop":
                    app["brain"].stop()
                    return self._json({"ok": True})
                if path == "/api/voice/speak":
                    text = str(body.get("text", "")).strip()[:1000]
                    if not text:
                        return self._json({"ok": False, "error": "text 为空"}, 400)
                    threading.Thread(target=_speak, args=(text,), daemon=True).start()
                    return self._json({"ok": True})

                # ---- A2A 同伴委托(同步返回,供其他 HeartAgent 实例调用) ----
                if path == "/api/a2a":
                    prompt = str(body.get("prompt", "")).strip()
                    if not prompt:
                        return self._json({"ok": False, "error": "prompt 为空"}, 400)
                    box = {}

                    def _run():
                        try:
                            box["r"] = app["brain"].autonomous(
                                prompt, source=str(body.get("source") or "A2A委托"))
                        except Exception as e:
                            box["r"] = "执行失败: %s" % e

                    t = threading.Thread(target=_run, daemon=True)
                    t.start()
                    t.join(110)
                    if t.is_alive():
                        return self._json({"ok": False,
                                           "error": "对端正忙(110s 未完成)"})
                    return self._json({"ok": True, "result": box.get("r", "")})

                # ---- 通用 webhook 入站:企业微信/钉钉/飞书等平台回调把消息转发进来 ----
                if path == "/api/webhook/in":
                    text = str(body.get("text") or body.get("content")
                               or body.get("message") or body.get("msg") or "").strip()
                    if not text:
                        return self._json({"ok": False, "error": "消息为空"}, 400)
                    sender = str(body.get("sender") or body.get("from")
                                 or body.get("user") or "webhook").strip()
                    wcfg = (app["cfg"].get("inbox") or {}).get("webhook") or {}
                    allow = [str(s).strip() for s in wcfg.get("allow_senders", []) if str(s).strip()]
                    if allow and sender not in allow:
                        return self._json({"ok": False, "error": "sender 不在白名单"}, 403)

                    def _run_wh():
                        app["bus"].publish("inbound", source="webhook:" + sender, text=text)
                        app["brain"].autonomous(
                            "收到一条 webhook 消息,请处理。发送者: %s\n消息:\n%s"
                            % (sender, text[:3000]), source="webhook: " + sender)
                    threading.Thread(target=_run_wh, daemon=True).start()
                    return self._json({"ok": True})

                if path == "/api/tasks":
                    t = app["tasks"].add(body.get("name"), body.get("schedule") or {},
                                         body.get("action") or {}, retry=body.get("retry"),
                                         chain=body.get("chain"), notify_policy=body.get("notify"))
                    return self._json({"ok": True, "id": t["id"]})
                if path == "/api/tasks/op":
                    op = body.get("op")
                    tid = str(body.get("id", ""))
                    if op == "delete":
                        return self._json({"ok": app["tasks"].delete(tid)})
                    if op == "toggle":
                        t = app["tasks"].toggle(tid)
                        return self._json({"ok": bool(t), "enabled": t and t["enabled"]})
                    if op == "run":
                        return self._json(app["tasks"].run_now(tid))
                    if op == "update":
                        return self._json({"ok": True,
                                           "task": app["tasks"].update(tid, body.get("patch") or {})})
                    return self._json({"ok": False, "error": "未知操作"}, 400)

                # ---- 目标 ----
                if path == "/api/goals":
                    g = app["goals"].add(body.get("title"), body.get("description", ""),
                                         body.get("check_every_hours", 24),
                                         auto_decompose=bool(body.get("auto_decompose", True)))
                    return self._json({"ok": True, "id": g["id"]})
                if path == "/api/goals/op":
                    op = body.get("op")
                    gid = str(body.get("id", ""))
                    if op == "delete":
                        return self._json({"ok": app["goals"].delete(gid)})
                    if op == "update":
                        g = app["goals"].update(gid, status=body.get("status"),
                                                note=body.get("note"))
                        return self._json({"ok": True, "goal": g})
                    if op == "check":
                        return self._json(app["goals"].run_check_now(gid))
                    return self._json({"ok": False, "error": "未知操作"}, 400)

                # ---- 知识库 ----
                if path == "/api/ingest":
                    from . import rag
                    return self._json(rag.ingest(body.get("target"), app["memory"],
                                                 app["bus"], tags=body.get("tags"),
                                                 force=bool(body.get("force"))))
                if path == "/api/knowledge/search":
                    return self._json({"hits": app["memory"].search_knowledge(
                        str(body.get("query", "")), top=8)})

                # ---- 提示词附录(进化) ----
                if path == "/api/prompt":
                    app["evolver"].set_appendix(str(body.get("appendix", "")))
                    return self._json({"ok": True})
                if path == "/api/prompt/reset":
                    app["evolver"].reset()
                    return self._json({"ok": True})

                # ---- v4 实验室:评估/回放/辩论/冲突/包/自治 ----
                if path == "/api/evals/run":
                    threading.Thread(target=app["evals"].run, args=(app["brain"],),
                                     daemon=True).start()
                    return self._json({"ok": True, "note": "评估已在后台运行,结果见事件流"})
                if path == "/api/evals/case":
                    c = app["evals"].add_case(body.get("name"), body.get("input"),
                                              body.get("expect_tools"),
                                              body.get("expect_keywords"),
                                              body.get("forbidden"))
                    return self._json({"ok": True, "id": c["id"]})
                if path == "/api/evals/op":
                    if body.get("op") == "delete":
                        return self._json({"ok": app["evals"].delete_case(str(body.get("id", "")))})
                    return self._json({"ok": False, "error": "未知操作"}, 400)
                if path == "/api/replay/fork":
                    text = str(body.get("text", "")).strip()
                    if not text:
                        return self._json({"ok": False, "error": "text 为空"}, 400)
                    model = str(body.get("model", "assist"))
                    llm = app["assist_llm"] if model == "assist" else app["main_llm"]
                    label = "反事实·%s" % ("协作模型" if model == "assist" else "主模型")
                    box = {}

                    def _cf():
                        try:
                            box["final"] = app["brain"].counterfactual(text, llm, label)
                        except Exception as e:
                            box["final"] = "执行失败: %s" % e

                    t = threading.Thread(target=_cf, daemon=True)
                    t.start()
                    t.join(110)
                    if t.is_alive():
                        return self._json({"ok": False, "error": "反事实重演超时"})
                    return self._json({"ok": True, "model": model, "final": box.get("final", "")})
                if path == "/api/debate":
                    topic = str(body.get("topic", "")).strip()
                    if not topic:
                        return self._json({"ok": False, "error": "topic 为空"}, 400)
                    threading.Thread(target=app["debate"].run,
                                     args=(topic, int(body.get("rounds", 2))),
                                     daemon=True).start()
                    return self._json({"ok": True})
                if path == "/api/conflicts/op":
                    op = body.get("op")
                    if op in ("keep_a", "keep_b", "dismiss"):
                        return self._json({"ok": app["memory"].resolve_conflict(
                            str(body.get("id", "")), op)})
                    return self._json({"ok": False, "error": "未知操作"}, 400)
                if path == "/api/sleep/run":
                    hb = app["heartbeat"]
                    hb.state["last_sleep"] = 0  # 立即触发一次睡眠整理
                    threading.Thread(target=hb._maybe_sleep,
                                     args=(app["cfg"].get("sleep_consolidation") or {},),
                                     daemon=True).start()
                    return self._json({"ok": True})
                if path == "/api/memory/archive":
                    return self._json({"ok": app["memory"].set_archived(
                        str(body.get("id", "")), 1 if body.get("flag", True) else 0)})
                if path == "/api/pack/install":
                    from packs import job_hunter
                    return self._json(job_hunter.install(
                        str(body.get("name", "")), app["memory"], app["extensions"],
                        app["goals"], app["bus"], app["data_dir"]))
                if path == "/api/import":
                    from . import mindpack
                    p = str(body.get("path", "")).strip()
                    if not os.path.exists(p):
                        return self._json({"ok": False, "error": "文件不存在: %s" % p}, 400)
                    return self._json(mindpack.import_pack(
                        p, app["memory"], app["extensions"], app["tasks"],
                        app["goals"], app["bus"], app["data_dir"]))
                if path == "/api/autonomy":
                    level = int(body.get("level", 2))
                    if level not in (0, 1, 2, 3):
                        return self._json({"ok": False, "error": "level 须为 0-3"}, 400)
                    app["brain"].autonomy_level = level
                    app["cfg"].setdefault("autonomy", {})["level"] = level
                    if app.get("save_config"):
                        app["save_config"]()
                    names = {0: "旁观", 1: "提案", 2: "常规自治", 3: "完全自治"}
                    app["bus"].publish("config", text="🛡️ 自治等级调整为 L%d(%s)"
                                       % (level, names[level]))
                    return self._json({"ok": True, "level": level})

                if path == "/api/session":
                    op = body.get("op")
                    if op == "new":
                        sid = app["memory"].new_session(body.get("title"))
                        return self._json({"ok": True, "id": sid})
                    if op == "switch":
                        return self._json({"ok": app["memory"].switch_session(str(body.get("id", "")))})
                    return self._json({"ok": False, "error": "未知操作"}, 400)

                if path == "/api/memory/add":
                    content = str(body.get("content", "")).strip()
                    if not content:
                        return self._json({"ok": False, "error": "内容为空"}, 400)
                    f = app["memory"].add_fact(content, tags=body.get("tags"), source="用户手动")
                    return self._json({"ok": True, "id": f["id"], "merged": f.get("merged", False)})
                if path == "/api/memory/update":
                    return self._json({"ok": app["memory"].update_fact(
                        str(body.get("id", "")), body.get("content"), body.get("tags"))})
                if path == "/api/memory/delete":
                    return self._json({"ok": app["memory"].delete_fact(str(body.get("id", "")))})
                if path == "/api/memory/organize":
                    added = app["assistant"].organize_memory(app["memory"].chat_log(30))
                    return self._json({"ok": True, "added": len(added), "items": added})

                if path == "/api/extensions/generate":
                    name = str(body.get("name", "")).strip()
                    if not name:
                        return self._json({"ok": False, "error": "缺少 name"}, 400)
                    if body.get("code") is not None and str(body.get("code")).strip():
                        r = app["extensions"].install(name, body["code"],
                                                      str(body.get("description", "")),
                                                      approve=body.get("approve"))
                    else:
                        r = app["extensions"].generate(
                            name, str(body.get("description", "")),
                            str(body.get("spec", "") or body.get("description", "")),
                            app["main_llm"], sample_args=body.get("sample_args"))
                    return self._json(r)
                if path == "/api/extensions/approve":
                    r = app["extensions"].approve(str(body.get("name", "")),
                                                  bool(body.get("decision")))
                    return self._json(r)
                if path == "/api/extensions/call":
                    r = app["extensions"].call(str(body.get("name", "")),
                                               body.get("args") or {})
                    return self._json(r)
                if path == "/api/extensions/delete":
                    return self._json({"ok": app["extensions"].delete(str(body.get("name", "")))})

                if path == "/api/notify/test":
                    from . import notify
                    r = notify.send(app["cfg"].get("notifications", {}),
                                    "[HeartAgent] 测试通知", "这是一条测试通知,配置生效。")
                    return self._json({"ok": bool(r), "detail": r})

                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                app["bus"].publish("error", text="接口异常 %s: %s" % (path, e))
                return self._json({"ok": False, "error": str(e)}, 500)

    return Handler


def start_server(app, port):
    app["port"] = port  # Host 校验需要

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True
        request_queue_size = 64  # 默认5在高频轮询+SSE长连接下会瞬时拒连

    srv = _Server(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=srv.serve_forever, name="web", daemon=True).start()
    return srv
