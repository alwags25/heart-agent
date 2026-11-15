"""HeartAgent 入口 v3:
  python main.py               启动双屏 Web UI(默认 http://127.0.0.1:8765)
  python main.py --cli         终端模式
新增装配:目标系统、技能日志、好奇心、提示词进化、混合路由、同伴智能体(A2A)、
外部收件箱(邮件/Telegram)。配置热重载覆盖所有新模块。"""
import argparse
import json
import logging
import os
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

# ---- 冻结模式(PyInstaller)--__runner__:子进程执行扩展/代码任务 ----
if "--__runner__" in sys.argv:
    _i = sys.argv.index("--__runner__")
    sys.argv = [sys.argv[0]] + sys.argv[_i + 1:]
    import runner as _runner_mod
    _runner_mod.main()
    sys.exit(0)

FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(os.path.abspath(__file__))          # 资源目录(冻结时= _MEIPASS)
APP_DIR = os.path.dirname(sys.executable) if FROZEN else BASE  # 可写目录(数据/配置/扩展)
sys.path.insert(0, BASE)

from agentcore import __version__
from agentcore.assistant import AssistantAI
from agentcore.brain import Brain
from agentcore.debate import Debate
from agentcore.events import EventBus
from agentcore.evals import EvalHarness
from agentcore.evolve import PromptEvolver
from agentcore.extensions import ExtensionManager
from agentcore.goals import GoalManager
from agentcore.heartbeat import Heartbeat
from agentcore.inbox import InboxManager
from agentcore.llm import OllamaEmbedder, ProxyLLM, make_llm, model_label
from agentcore.memory import MemoryStore
from agentcore.server import start_server
from agentcore.skills import SkillsLog
from agentcore.tasks import TaskManager
from agentcore.usage import UsageTracker

DEFAULT_CONFIG = {
    "_说明": "provider 可选 openai(OpenAI兼容:GLM/DeepSeek等) / ollama / mock。"
             "api_key 可用环境变量 HEARTAGENT_MAIN_API_KEY / HEARTAGENT_ASSIST_API_KEY 覆盖。"
             "改完保存即热重载,无需重启。",
    "main_model": {
        "provider": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4.6",
    },
    "assist_model": {
        "provider": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key": "",
        "model": "glm-4-flash",
    },
    "embeddings": {"enabled": True, "base_url": "http://localhost:11434",
                   "model": "nomic-embed-text"},
    "router": {
        "_说明": "混合路由:简单消息走本地小模型快答,省钱提速;对端不可用时自动回退主模型",
        "enabled": True, "max_len": 32,
        "local": {"provider": "ollama", "base_url": "http://localhost:11434",
                  "model": "qwen2.5:0.5b"},
    },
    "peer_agents": [
        # {"name": "研究员", "base_url": "http://127.0.0.1:8766"},
    ],
    "inbox": {
        "_说明": "外部接入渠道;只响应 allow 列表内的发件人",
        "poll_sec": 60,
        "email": {"enabled": False, "host": "", "port": 993, "user": "", "password": "",
                  "allow_from": []},
        "telegram": {"enabled": False, "token": "", "allow_chat_ids": []},
        "webhook": {"_说明": "通用 webhook 入站:POST /api/webhook/in {text,sender},可配白名单",
                    "enabled": True, "allow_senders": []},
    },
    "curiosity": {
        "enabled": True, "explores_per_day": 5,
        "sources": [
            # {"type": "rss", "url": "https://example.com/feed"},
            # {"type": "url", "url": "https://example.com"},
            # {"type": "folder", "path": "D:/资料/监控目录"},
        ],
    },
    "skill_harvest": {"enabled": True, "every_hours": 6, "min_new": 2},
    "prompt_evolution": {"enabled": False, "every_hours": 24, "sample_size": 4},
    "notifications": {
        "on_task_failure": True,
        "daily_report_push": False,
        "channels": {
            "serverchan": {"enabled": False, "sendkey": ""},
            "wecom": {"enabled": False, "webhook": ""},
            "dingtalk": {"enabled": False, "webhook": ""},
            "feishu": {"enabled": False, "webhook": ""},
            "email": {"enabled": False, "host": "", "smtp_host": "", "port": 465,
                      "user": "", "password": "", "to": ""},
            "desktop": {"enabled": False},
        },
    },
    "auto_approve_extensions": False,
    "second_opinion": {"_说明": "协作AI安全审查:高危操作(删任务/危代码/联网扩展)先经协作AI放行",
                       "enabled": True},
    "security": {"_说明": "token 非空时所有 API 需携带 X-HeartAgent-Token(UI 会提示输入)",
                 "token": "",
                 "restricted_token": False,
                 "_restricted_token说明": "true 时扩展/代码子进程附加受限令牌降权(增强围栏,尽力而为)"},
    "vision": {"_说明": "截图理解需主模型支持图像输入(如 glm-4.6v / glm-4.5v)", "model": ""},
    "autonomy": {"_说明": "自治等级:0旁观(AI动作全需批准) 1提案 2常规自治(默认) 3完全自治",
                 "level": 2},
    "wallet": {
        "_说明": "成本钱包:monthly_budget_cny 为月预算(0=不限);prices 为 元/百万token",
        "monthly_budget_cny": 50,
        "prices": {"glm-4.6": {"in": 2.0, "out": 8.0},
                   "glm-4-flash": {"in": 0.1, "out": 0.1},
                   "deepseek-chat": {"in": 2.0, "out": 8.0}},
    },
    "sleep_consolidation": {"enabled": True, "every_hours": 12, "decay_days": 30,
                            "min_hits": 2, "max_conflict_checks": 20},
    "evals": {"enabled": True, "every_hours": 24},
    "daily_report": {"enabled": True, "hour": 9},
    "reflection": {"enabled": True, "every_hours": 6},
    "max_concurrent_tasks": 2,
    "ext_mem_limit_mb": 512,
    "heartbeat_interval_sec": 30,
    "idle_threshold_sec": 120,
    "ui_port": 8765,
    "data_dir": "data",
}

log = logging.getLogger("heartagent")


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and not k.startswith("_"):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    # 防路径穿越:只读加载,配置文件必须位于 heart-agent 目录内
    real_base = os.path.realpath(BASE)
    real_target = os.path.realpath(path or os.path.join(APP_DIR, "config.json"))
    if os.path.commonpath([real_base, real_target]) != real_base:
        print("拒绝: 配置文件路径必须在 heart-agent 目录内(%s)" % path)
        raise SystemExit(2)
    if not os.path.exists(real_target):
        print("未找到 %s,使用内置默认配置(mock 模式)。" % os.path.basename(real_target))
        print("提示: 复制 config.example.json 为 config.json 并填入 api_key 后重启/热重载。")
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(real_target, encoding="utf-8") as f:
        saved = json.load(f)
    merged = _deep_merge(DEFAULT_CONFIG, saved)
    return merged


def _try_embedder(cfg, memory):
    ecfg = (cfg or {}).get("embeddings") or {}
    if not ecfg.get("enabled"):
        return

    def _attach():
        try:
            emb = OllamaEmbedder(ecfg.get("base_url"), ecfg.get("model"))
            emb.embed("测试")
            memory.set_embedder(emb)
            log.info("语义检索已启用(%s)", ecfg.get("model"))
        except Exception as e:
            log.info("嵌入服务不可用,检索退化为全文匹配: %s", e)
    threading.Thread(target=_attach, daemon=True).start()


def _build_router(cfg, usage_sink=None):
    """混合路由:简单消息走本地小模型;本地模型不可用 → 返回 None(自动回退)。
    路由必须快:超时压到 15s,卡住立即回退主模型,绝不拖慢对话。"""
    rcfg = (cfg or {}).get("router") or {}
    if not rcfg.get("enabled"):
        return None
    inner = make_llm(rcfg.get("local") or {})
    if inner.is_mock:
        return None
    try:
        inner.timeout = min(getattr(inner, "timeout", 30), 15)
    except Exception:
        pass
    return ProxyLLM(inner, "router", on_usage=usage_sink)


def _startup_ping(app):
    """启动自检:逐个 ping 主/协作模型,第一时间暴露配置错误(密钥/模型名/网络)。"""

    def _run():
        for role, proxy in (("主模型", app["main_llm"]), ("协作模型", app["assist_llm"])):
            t0 = time.time()
            label = app["model_labels"]["main" if role == "主模型" else "assist"]
            try:
                proxy.chat([{"role": "user", "content": "连接测试,回复 OK 两个字即可"}],
                           temperature=0)
                if proxy.is_mock:
                    app["bus"].publish("config", text="ℹ️ %s 当前为 mock 模式(%s)"
                                       % (role, label))
                else:
                    app["bus"].publish("config", text="✅ %s 连通正常:%s(延迟 %.1fs)"
                                       % (role, label, time.time() - t0))
            except Exception as e:
                app["bus"].publish("error", text="❌ %s 连通失败(%s): %s —— 请检查 "
                                   "config.json 的 api_key / model / base_url,保存后自动热重载重试"
                                   % (role, label, str(e)[:160]))
    threading.Thread(target=_run, daemon=True).start()


def build_app(config_path):
    cfg = load_config(config_path)
    bus = EventBus()
    data_dir = os.path.join(APP_DIR, cfg.get("data_dir", "data"))
    os.makedirs(data_dir, exist_ok=True)

    usage = UsageTracker(os.path.join(data_dir, "usage.json"))
    main_proxy = ProxyLLM(make_llm(cfg.get("main_model"), "MAIN"), "main",
                          on_usage=lambda label, u: usage.record(
                              model_label(cfg.get("main_model"), "MAIN"), u))
    assist_proxy = ProxyLLM(make_llm(cfg.get("assist_model"), "ASSIST"), "assist",
                            on_usage=lambda label, u: usage.record(
                                model_label(cfg.get("assist_model"), "ASSIST"), u))

    memory = MemoryStore(os.path.join(data_dir, "memory.db"))
    _try_embedder(cfg, memory)
    assistant = AssistantAI(assist_proxy, memory, bus)
    extensions = ExtensionManager(os.path.join(APP_DIR, "extensions"), bus, data_dir,
                                  auto_approve=cfg.get("auto_approve_extensions", False),
                                  mem_mb=cfg.get("ext_mem_limit_mb", 512),
                                  restricted=bool(cfg.get("security", {}).get("restricted_token", False)))
    tasks = TaskManager(os.path.join(data_dir, "tasks.json"), bus, data_dir,
                        notify_cfg=cfg.get("notifications"),
                        max_concurrent=cfg.get("max_concurrent_tasks", 2),
                        ext_mem_mb=cfg.get("ext_mem_limit_mb", 512))
    goals = GoalManager(os.path.join(data_dir, "goals.json"), bus, data_dir)
    skills = SkillsLog(os.path.join(data_dir, "skills_log.json"))
    evolver = PromptEvolver(main_proxy, memory, bus, data_dir)
    router = _build_router(cfg, lambda label, u: usage.record("router(本地)", u))
    peers = [p for p in (cfg.get("peer_agents") or []) if p.get("name") and p.get("base_url")]

    brain = Brain(main_proxy, memory, assistant, extensions, tasks, bus, data_dir,
                  goals=goals, skills=skills, router_llm=router, peers=peers,
                  assist_llm=assist_proxy)
    debate = Debate(main_proxy, bus, memory)
    brain.debate = debate
    evals = EvalHarness(os.path.join(data_dir, "eval_cases.json"),
                        os.path.join(data_dir, "eval_history.json"), bus,
                        judge_llm=main_proxy)
    brain.autonomy_level = int(cfg.get("autonomy", {}).get("level", 2))
    brain.second_opinion_enabled = bool(cfg.get("second_opinion", {}).get("enabled", True))
    tasks.set_brain(brain)
    goals.set_brain(brain)

    app = {
        "bus": bus, "memory": memory, "assistant": assistant, "extensions": extensions,
        "tasks": tasks, "brain": brain, "usage": usage, "cfg": cfg, "cfg_path": config_path,
        "main_llm": main_proxy, "assist_llm": assist_proxy, "router": router,
        "goals": goals, "skills": skills, "evolver": evolver, "peers": peers,
        "evals": evals, "debate": debate,
        "index_path": os.path.join(BASE, "web", "index.html"),
        "base_dir": APP_DIR, "data_dir": data_dir,
        "start_time": time.time(), "version": __version__,
    }
    heartbeat = Heartbeat(
        cfg.get("heartbeat_interval_sec", 30), cfg.get("idle_threshold_sec", 120),
        brain, assistant, memory, bus,
        config_path=config_path, tasks=tasks, extensions=extensions, main_llm=main_proxy,
        daily_report=cfg.get("daily_report"), reflection=cfg.get("reflection"),
        skills=skills, goals=goals, evolver=evolver,
        curiosity=cfg.get("curiosity"), skill_harvest=cfg.get("skill_harvest"),
        evals=evals, usage=usage, cfg_getter=lambda: app["cfg"],
        on_config_reload=lambda: reload_config(app),
        state_path=os.path.join(data_dir, "heartbeat_state.json"))
    heartbeat.set_evolve_cfg(cfg.get("prompt_evolution"))
    app["heartbeat"] = heartbeat
    app["save_config"] = lambda: persist_config(app)
    app["events_sink"] = os.path.join(data_dir, "events_log.jsonl")
    bus.set_sink(app["events_sink"])
    inbox = InboxManager(brain, bus, lambda: app["cfg"].get("inbox") or {})
    app["inbox"] = inbox
    app["model_labels"] = {"main": model_label(cfg.get("main_model"), "MAIN"),
                           "assist": model_label(cfg.get("assist_model"), "ASSIST")}
    _announce_models(app)
    return app


def _announce_models(app):
    m, a = app["main_llm"], app["assist_llm"]
    modes = []
    if m.is_mock:
        modes.append("主模型(%s)" % getattr(m, "mock_reason", "mock"))
    if a.is_mock:
        modes.append("协作模型(%s)" % getattr(a, "mock_reason", "mock"))
    if modes:
        app["bus"].publish("status",
                           text="当前为 mock 模式:%s。编辑 config.json 填入 api_key"
                                "或改用 ollama 后保存(热重载)即可启用真实模型。" % ", ".join(modes))
    else:
        app["bus"].publish("status", text="模型就绪:主 %s / 协作 %s"
                           % (app["model_labels"]["main"], app["model_labels"]["assist"]))


def persist_config(app):
    """把内存中的 cfg 写回 config.json(自治等级等运行时调整用)。"""
    try:
        with open(app["cfg_path"], "w", encoding="utf-8") as f:
            json.dump(app["cfg"], f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning("配置写回失败: %s", e)


def reload_config(app):
    cfg = load_config(app["cfg_path"])
    app["cfg"] = cfg
    was_mock = (app["main_llm"].is_mock, app["assist_llm"].is_mock)
    app["main_llm"].swap(make_llm(cfg.get("main_model"), "MAIN"))
    app["assist_llm"].swap(make_llm(cfg.get("assist_model"), "ASSIST"))
    app["model_labels"] = {"main": model_label(cfg.get("main_model"), "MAIN"),
                           "assist": model_label(cfg.get("assist_model"), "ASSIST")}
    now_mock = (app["main_llm"].is_mock, app["assist_llm"].is_mock)
    if all(was_mock) and not any(now_mock):  # mock → 真实模型的切换值得点名
        app["bus"].publish("config",
                           text="🔑 已切换到真实模型:%s / %s —— 现在起对话将真正调用 API"
                                % (app["model_labels"]["main"], app["model_labels"]["assist"]))
    app["tasks"].update_notify_cfg(cfg.get("notifications"))
    app["extensions"].auto_approve = cfg.get("auto_approve_extensions", False)
    app["extensions"].mem_mb = cfg.get("ext_mem_limit_mb", 512)
    app["extensions"].restricted = bool(cfg.get("security", {}).get("restricted_token", False))
    app["heartbeat"].reconfigure(
        interval=cfg.get("heartbeat_interval_sec"),
        idle_threshold=cfg.get("idle_threshold_sec"),
        daily_report=cfg.get("daily_report"), reflection=cfg.get("reflection"),
        curiosity=cfg.get("curiosity"), skill_harvest=cfg.get("skill_harvest"))
    app["heartbeat"].set_evolve_cfg(cfg.get("prompt_evolution"))
    rcfg = (cfg.get("router") or {})
    if app.get("router"):
        inner = make_llm(rcfg.get("local") or {}) if rcfg.get("enabled") else None
        if inner and not inner.is_mock:
            app["router"].swap(inner, "router")
            app["brain"].router_llm = app["router"]
        else:
            app["brain"].router_llm = None
    elif rcfg.get("enabled"):
        r = _build_router(cfg)
        if r:
            app["router"] = r
            app["brain"].router_llm = r
    app["brain"].peers = [p for p in (cfg.get("peer_agents") or [])
                          if p.get("name") and p.get("base_url")]
    app["brain"].autonomy_level = int(cfg.get("autonomy", {}).get("level", 2))
    app["brain"].second_opinion_enabled = bool(cfg.get("second_opinion", {}).get("enabled", True))
    _announce_models(app)


def start_background(app):
    stop = threading.Event()
    threading.Thread(target=app["tasks"].worker, args=(stop,), name="tasks",
                     daemon=True).start()
    app["heartbeat"].start()
    app["inbox"].start()
    _startup_ping(app)
    return stop


EVENT_ICONS = {
    "chat_user": "说", "chat_assistant": "答", "chat_stream": None, "chat_retract": "转",
    "thought": "想", "tool": "具", "tool_result": "果", "assistant_ai": "协",
    "heartbeat": "跳", "task": "务", "extension": "扩", "autonomous": "主",
    "error": "错", "status": None, "insight": "思", "report": "报", "config": "配",
    "inbound": "信", "goal": "标",
}


def run_cli(app):
    bus = app["bus"]
    stop = threading.Event()

    def printer():
        q = bus.subscribe()
        while not stop.is_set():
            try:
                ev = q.get(timeout=0.5)
            except Exception:
                continue
            icon = EVENT_ICONS.get(ev["type"], "·")
            if icon is None:
                continue
            text = ev.get("text") or "%s %s" % (
                ev.get("name", ""), json.dumps(ev.get("args", {}), ensure_ascii=False))
            print("  [%s|%s] %s" % (icon, ev.get("ts", ""), text.replace("\n", " ")[:200]))

    threading.Thread(target=printer, daemon=True).start()
    start_background(app)
    print("HeartAgent CLI(心跳/任务/收件箱后台运行,Ctrl+C 退出)\n")
    try:
        while True:
            try:
                text = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            app["brain"].handle_user(text)
            print()
    finally:
        stop.set()
        app["heartbeat"].stop()
        app["inbox"].stop()
        print("再见")


def run_web(app, port, open_browser):
    start_background(app)
    try:
        start_server(app, port)
    except OSError as e:
        print("启动失败: 端口 %d 被占用(%s)。" % (port, e))
        print("换一个端口再试: python main.py --port 9000")
        return
    url = "http://127.0.0.1:%d" % port
    print("HeartAgent v%s 已启动: %s" % (__version__, url))
    print("  左屏 = 对话/任务/记忆/目标/扩展   右屏 = 思考过程实时事件流")
    print("  收件箱: %s | 配置热重载已启用 | 日志: data/heartagent.log"
          % ("启用" if (app["inbox"].status()["email"]
                       or app["inbox"].status()["telegram"]) else "未配置"))
    print("  Ctrl+C 退出\n")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        app["heartbeat"].stop()
        app["inbox"].stop()
        print("\n已停止")


def setup_logging(data_dir):
    os.makedirs(data_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(os.path.join(data_dir, "heartagent.log"),
                             maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    root.addHandler(ch)


def main():
    if os.name == "nt":
        os.system("")
    parser = argparse.ArgumentParser(description="HeartAgent 双模型协作智能体")
    parser.add_argument("--cli", action="store_true", help="终端模式(默认 Web 双屏)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", default=os.path.join(APP_DIR, "config.json"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--heartbeat", type=int, default=None, help="覆盖心跳间隔秒数(调试)")
    parser.add_argument("--idle", type=int, default=None, help="覆盖空闲阈值秒数(调试)")
    args = parser.parse_args()

    app = build_app(args.config)
    setup_logging(app["data_dir"])
    if args.heartbeat:
        app["heartbeat"].interval = max(5, args.heartbeat)
    if args.idle:
        app["heartbeat"].idle_threshold = max(10, args.idle)

    if args.cli:
        run_cli(app)
    else:
        run_web(app, args.port or app["cfg"].get("ui_port", 8765),
                open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
