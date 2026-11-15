"""心跳 v3:巡检 + 学习 + 反思 + 日报 + 配置热重载 + 好奇心引擎 + 技能收割 + 目标检查。
空闲时的自主行动队列(按拍依次尝试,互不阻塞):
- 整理记忆(原有):空闲且新对话 ≥4 条 → 协作AI提炼
- 好奇心:每日预算内探索配置的信息源(RSS/网页/文件夹),发现存入记忆并播报
- 技能收割:把 run_code 成功片段提炼为可复用扩展候选(进审批队列)
- 目标检查:到期的长期目标交主控AI回顾推进
- 反思 / 日报 / 提示词进化(周期性)"""
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import urllib.request


class Heartbeat:
    def __init__(self, interval_sec, idle_threshold_sec, brain, assistant, memory, events,
                 config_path=None, tasks=None, extensions=None, main_llm=None,
                 daily_report=None, reflection=None, on_config_reload=None,
                 skills=None, goals=None, evolver=None, curiosity=None,
                 skill_harvest=None, evals=None, usage=None, cfg_getter=None,
                 on_daily_report=None,
                 state_path="data/heartbeat_state.json"):
        self.interval = max(5, int(interval_sec))
        self.idle_threshold = max(10, int(idle_threshold_sec))
        self.brain = brain
        self.assistant = assistant
        self.memory = memory
        self.events = events
        self.config_path = config_path
        self.tasks = tasks
        self.extensions = extensions
        self.main_llm = main_llm
        self.skills = skills
        self.goals = goals
        self.evolver = evolver
        self.evals = evals          # EvalHarness
        self.usage = usage          # UsageTracker(钱包)
        self.cfg_getter = cfg_getter or (lambda: {})
        self.on_daily_report = on_daily_report  # 日报后钩子(徽章刷新等)
        self.curiosity_cfg = curiosity or {}
        self.harvest_cfg = skill_harvest or {}
        self.daily_report_cfg = daily_report or {}
        self.reflection_cfg = reflection or {}
        self._evolve_cfg = {}
        self.on_config_reload = on_config_reload
        self.state_path = state_path
        self.beats = 0
        self.last_beat = "-"
        self.organized_up_to = memory.chat_len()
        self._cfg_mtime = self._mtime()
        self.state = self._load_state()
        self._stop = None
        self._thread = None

    # ---------- 状态 ----------
    def _mtime(self):
        try:
            return os.path.getmtime(self.config_path)
        except OSError:
            return None

    def _load_state(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"last_reflection": 0, "last_report_date": "",
                    "explore": {"date": "", "count": 0, "idx": 0, "sources": {}},
                    "last_harvest": 0, "last_evolution": 0}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
        except OSError:
            pass

    def start(self):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="heartbeat", daemon=True)
        self._thread.start()

    def stop(self):
        if self._stop:
            self._stop.set()

    def reconfigure(self, interval=None, idle_threshold=None, daily_report=None,
                    reflection=None, curiosity=None, skill_harvest=None):
        if interval:
            self.interval = max(5, int(interval))
        if idle_threshold:
            self.idle_threshold = max(10, int(idle_threshold))
        if daily_report is not None:
            self.daily_report_cfg = daily_report
        if reflection is not None:
            self.reflection_cfg = reflection
        if curiosity is not None:
            self.curiosity_cfg = curiosity
        if skill_harvest is not None:
            self.harvest_cfg = skill_harvest

    # ---------- 主循环 ----------
    def _loop(self):
        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception as e:
                self.events.publish("error", text="心跳异常: %s" % e)

    def _cfg(self):
        return self.cfg_getter() or {}

    def _autonomy_level(self):
        return int(self._cfg().get("autonomy", {}).get("level", 2))

    def _budget_ok(self, need_cny=0.0):
        """钱包闸门:本月剩余预算不足时暂停高消耗自主活动。"""
        wallet = self._cfg().get("wallet") or {}
        budget = float(wallet.get("monthly_budget_cny", 0) or 0)
        if budget <= 0 and not wallet.get("prices"):
            return True  # 未配置钱包 → 不限制
        if not self.usage:
            return True
        snap = self.usage.snapshot()
        spent = self.usage.cost_of(snap.get("month"), wallet.get("prices") or {})
        remaining = budget - spent
        if remaining <= need_cny:
            today = time.strftime("%Y-%m-%d")
            if self.state.get("wallet_warn_date") != today:
                self.state["wallet_warn_date"] = today
                self._save_state()
                self.events.publish("insight",
                                    text="💰 预算闸门:本月已花 ¥%.2f / 预算 ¥%.2f,"
                                         "高消耗自主活动(睡眠整理/好奇/收割/目标检查)暂停,"
                                         "对话不受影响。" % (spent, budget))
            return False
        return True

    def _tick(self):
        self.beats += 1
        self.last_beat = time.strftime("%H:%M:%S")
        idle_for = time.time() - self.brain.last_activity
        self.events.publish("status", beats=self.beats, last_beat=self.last_beat,
                            idle_for=round(idle_for))
        self._check_config_reload()
        if self.daily_report_cfg.get("enabled"):
            self._maybe_daily_report()
        if self.reflection_cfg.get("enabled"):
            self._maybe_reflect()
        if self.evolver and self.evolver_cfg_enabled():
            self._maybe_evolve()
        # 睡眠整理与评估回归:自治等级>=2 且预算允许
        level = self._autonomy_level()
        if level >= 2 and self._budget_ok():
            scfg = self._cfg().get("sleep_consolidation") or {}
            if scfg.get("enabled", True):
                self._maybe_sleep(scfg)
            if self.evals and (self._cfg().get("evals") or {}).get("enabled", True):
                self._maybe_evals(self._cfg().get("evals") or {})
        if self.harvest_cfg.get("enabled", True) and level >= 2 and self._budget_ok():
            self._maybe_harvest_skills()
        if self.goals and level >= 2 and self._budget_ok():
            self._maybe_check_goals()
        if idle_for >= self.idle_threshold:
            self._idle_actions(idle_for)

    def evolver_cfg_enabled(self):
        return bool(getattr(self, "_evolve_cfg", {}).get("enabled"))

    def set_evolve_cfg(self, cfg):
        self._evolve_cfg = cfg or {}

    # ---------- 空闲行动 ----------
    def _idle_actions(self, idle_for):
        # 1) 整理记忆
        new_chats = self.memory.chat_len() - self.organized_up_to
        if new_chats >= 4:
            self.events.publish("heartbeat",
                                text="空闲检测:%d 条新对话,让协作AI整理记忆…" % new_chats)
            self.assistant.organize_memory(self.memory.chat_log(min(new_chats + 6, 40)))
            self.organized_up_to = self.memory.chat_len()
        # 2) 好奇心引擎(整理之后仍空闲才探索)
        elif self.curiosity_cfg.get("enabled") and self.curiosity_cfg.get("sources"):
            self._maybe_explore()
        elif self.beats % 6 == 0:
            self.events.publish("heartbeat",
                                text="空闲巡检正常(已空闲 %ds;记忆无新增,任务由调度线程盯守)"
                                     % int(idle_for))

    # ---------- 配置热重载 ----------
    def _check_config_reload(self):
        if not self.config_path or not self.on_config_reload:
            return
        mtime = self._mtime()
        if mtime and mtime != self._cfg_mtime:
            self._cfg_mtime = mtime
            try:
                self.on_config_reload()
                self.events.publish("config", text="配置已热重载(模型/参数/通知已生效)")
            except Exception as e:
                self.events.publish("error", text="配置热重载失败: %s" % e)

    # ---------- 好奇心引擎 ----------
    def _maybe_explore(self):
        sources = self.curiosity_cfg.get("sources") or []
        if not sources:
            return
        today = time.strftime("%Y-%m-%d")
        ex = self.state.setdefault("explore", {"date": "", "count": 0, "idx": 0, "sources": {}})
        if ex.get("date") != today:
            ex["date"], ex["count"] = today, 0
        budget = max(1, int(self.curiosity_cfg.get("explores_per_day", 5)))
        if ex["count"] >= budget:
            return
        idx = int(ex.get("idx", 0)) % len(sources)
        ex["idx"] = idx + 1
        src = sources[idx]
        try:
            findings = self._explore_source(src, ex.setdefault("sources", {}))
        except Exception as e:
            self.events.publish("error", text=" curiosity 探索失败(%s): %s"
                                % (src.get("type"), e))
            findings = None
        ex["count"] += 1
        self._save_state()
        if not findings:
            return
        try:
            res = self.main_llm.chat(
                [{"role": "user",
                  "content": "你是 HeartAgent 的好奇心引擎。以下是刚探索到的内容,提炼最多3条值得记住的"
                             "发现(每条一句话,具体、有信息量;没有值得记的就输出:无):\n%s"
                             % findings[:4000]}], temperature=0.4)
            text = (res.content or "").strip()
            if text and "无" != text:
                self.memory.add_fact(text[:600], tags=["好奇心"], source="好奇心引擎")
                self.events.publish("insight", text="🔭 好奇心发现(%s):%s"
                                    % (src.get("type"), text[:400]))
        except Exception as e:
            self.events.publish("error", text="好奇心总结失败: %s" % e)

    def _explore_source(self, src, state_store):
        key = str(src.get("url") or src.get("path") or "")
        st = state_store.setdefault(key, {})
        kind = src.get("type")
        if kind == "rss":
            req = urllib.request.Request(src["url"], headers={"User-Agent": "HeartAgent/3.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                root = ET.fromstring(r.read(1024 * 1024))
            items = []
            for item in root.iter("item"):
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if title:
                    items.append((title, link))
            if not items:  # Atom
                for entry in root.iter("entry"):
                    title = (entry.findtext("title") or "").strip()
                    link = next((e.get("href") for e in entry.iter("link")
                                 if e.get("href")), "")
                    if title:
                        items.append((title, link))
            seen = set(st.get("links", []))
            fresh = [(t, l) for t, l in items[:30] if l and l not in seen]
            st["links"] = [l for _t, l in items[:30]]
            if not fresh:
                return None
            return "RSS %s 新内容:\n%s" % (src["url"], "\n".join(
                "- %s" % t for t, _l in fresh[:8]))
        if kind == "url":
            req = urllib.request.Request(src["url"], headers={"User-Agent": "HeartAgent/3.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read(512 * 1024).decode("utf-8", "replace")
            text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", raw, flags=re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            h = hash(text[:20000])
            if st.get("hash") == h:
                return None
            st["hash"] = h
            return "网页 %s 当前内容(节选):\n%s" % (src["url"], text[:2500])
        if kind == "folder":
            path = src.get("path", "")
            snap = {}
            for root, _d, names in os.walk(path):
                for n in names:
                    p = os.path.join(root, n)
                    try:
                        snap[p] = os.path.getmtime(p)
                    except OSError:
                        pass
            old = st.get("files", {})
            changed = [p for p, m in snap.items() if old.get(p) != m]
            st["files"] = {p: m for p, m in list(snap.items())[:500]}
            if not changed or (old and not changed):
                if not old:
                    return None
            if not changed:
                return None
            return "文件夹 %s 有 %d 个文件新增/变动:\n%s" % (
                path, len(changed), "\n".join(os.path.basename(p) for p in changed[:10]))
        raise ValueError("未知探索源类型: %r(支持 rss/url/folder)" % kind)

    # ---------- 技能收割 ----------
    def _maybe_harvest_skills(self):
        if not self.skills:
            return
        every = max(1, float(self.harvest_cfg.get("every_hours", 6))) * 3600
        if time.time() - self.state.get("last_harvest", 0) < every:
            return
        pending = self.skills.unharvested()
        min_new = int(self.harvest_cfg.get("min_new", 2))
        if len(pending) < min_new:
            return
        self.state["last_harvest"] = time.time()
        self._save_state()
        try:
            listing = "\n\n".join("片段%d 意图:%s\n代码:\n%s" % (i + 1, e["intent"], e["code"][:600])
                                  for i, e in enumerate(pending[:6]))
            res = self.main_llm.chat(
                [{"role": "user",
                  "content": "以下是智能体近期用 run_code 成功完成的代码片段。挑出值得固化为可复用扩展的"
                             "(最多2个),输出 JSON 数组:"
                             '[{"name": "snake_case名", "description": "一句话", "spec": "输入输出要求", "idx": 片段序号}]\n'
                             "都不值得就输出 []。只输出 JSON。\n\n%s" % listing}],
                temperature=0.2)
            import json as _json
            arr = None
            m = re.search(r"\[[\s\S]*\]", res.content or "")
            if m:
                try:
                    arr = _json.loads(m.group(0))
                except Exception:
                    arr = None
            picked = [x for x in (arr or []) if isinstance(x, dict) and x.get("name")][:2]
            for x in picked:
                try:
                    self.extensions.generate(x["name"], x.get("description", ""),
                                             x.get("spec", x.get("description", "")),
                                             self.main_llm)
                    self.events.publish("insight",
                                        text="✨ 技能收割:把常用代码沉淀为扩展候选「%s」(待审批)"
                                             % x["name"])
                except Exception as e:
                    self.events.publish("error", text="技能收割生成失败(%s): %s" % (x.get("name"), e))
            self.skills.mark_harvested(len(pending))
        except Exception as e:
            self.events.publish("error", text="技能收割异常: %s" % e)

    # ---------- 目标检查 ----------
    def _maybe_check_goals(self):
        due = self.goals.due_checks()
        if not due:
            return
        g = due[0]  # 每拍最多检查一个,避免突发
        self.goals.touch(g["id"])
        try:
            self.goals._check_one(g)
        except Exception as e:
            self.events.publish("error", text="目标检查失败: %s" % e)

    # ---------- 睡眠整理(做梦) ----------
    def _maybe_sleep(self, scfg):
        every = max(1, float(scfg.get("every_hours", 12))) * 3600
        if time.time() - self.state.get("last_sleep", 0) < every:
            return
        self.state["last_sleep"] = time.time()
        self._save_state()
        # 1) 遗忘曲线
        decay_days = int(scfg.get("decay_days", 30))
        archived = self.memory.sleep_decay(days=decay_days,
                                           min_hits=int(scfg.get("min_hits", 2)))
        # 2) 矛盾检测:候选对 → 主模型判定
        candidates = self.memory.conflict_candidates(
            limit=int(scfg.get("max_conflict_checks", 20)))
        found = 0
        for pair in candidates:
            try:
                res = self.main_llm.chat(
                    [{"role": "user",
                      "content": "判断以下两条关于同一主体的记忆是否互相矛盾或一新一旧需要合并。"
                                 "只输出 JSON:{\"conflict\": true/false, \"question\": \"若矛盾,向用户澄清的问题\"}\n"
                                 "记忆A: %s\n记忆B: %s" % (pair["a"][:200], pair["b"][:200])}],
                    temperature=0.1)
                obj = self._extract_json(res.content or "")
                if obj and obj.get("conflict"):
                    self.memory.add_conflict(pair["a_id"], pair["b_id"],
                                             obj.get("question") or "两条记忆不一致")
                    found += 1
            except Exception:
                continue
        if archived or found:
            self.events.publish("insight",
                                text="💤 睡眠整理完成:归档 %d 条弱记忆,发现 %d 处记忆矛盾"
                                     "(待澄清,见记忆页)。" % (archived, found))
        elif self.beats % 24 == 0:
            self.events.publish("insight",
                                text="💤 睡眠整理巡检:记忆无矛盾、无弱记忆需要归档。")
        # 3) 向量回填(嵌入服务就绪前的旧记忆)
        try:
            filled = self.memory.backfill_embeddings()
            if filled:
                self.events.publish("insight",
                                    text="💤 睡眠整理:为 %d 条记忆/知识补齐了语义向量。" % filled)
        except Exception as e:
            self.events.publish("error", text="向量回填失败: %s" % e)
        # 4) 实体关系抽取(记忆图谱)
        try:
            self._extract_triples()
        except Exception as e:
            self.events.publish("error", text="图谱抽取失败: %s" % e)

    def _extract_triples(self):
        facts = self.memory.unextracted_facts(limit=10)
        if not facts or not self.main_llm:
            return
        lines = "\n".join("- %s" % f["content"][:150] for f in facts)
        res = self.main_llm.chat(
            [{"role": "user",
              "content": "从以下记忆中抽取(主体,关系,客体)三元组,主体/客体为简短实体名,"
                         "关系为动词短语。只输出 JSON 数组:"
                         "[{\"s\":\"主体\",\"r\":\"关系\",\"o\":\"客体\"}],没有可抽取的就输出 []\n%s"
                         % lines}], temperature=0.2)
        m = re.search(r"\[[\s\S]*\]", res.content or "")
        if not m:
            self.memory.mark_extracted([f["id"] for f in facts])
            return
        try:
            arr = json.loads(m.group(0))
        except Exception:
            return
        items = [{"s": t.get("s"), "r": t.get("r"), "o": t.get("o")}
                 for t in (arr or []) if isinstance(t, dict)][:15]
        added = self.memory.add_triples(items) if items else 0
        self.memory.mark_extracted([f["id"] for f in facts])
        if added:
            self.events.publish("insight",
                                text="🕸️ 睡眠整理:从记忆中抽取 %d 条实体关系,图谱已更新(记忆页可查看)。"
                                     % added)

    @staticmethod
    def _extract_json(text):
        import json as _json
        m = re.search(r"\{[\s\S]*\}", text or "")
        if not m:
            return None
        try:
            return _json.loads(m.group(0))
        except Exception:
            return None

    # ---------- 评估回归 ----------
    def _maybe_evals(self, ecfg):
        every = max(1, float(ecfg.get("every_hours", 24))) * 3600
        if time.time() - self.state.get("last_eval", 0) < every:
            return
        self.state["last_eval"] = time.time()
        self._save_state()
        try:
            self.evals.run(self.brain)
        except Exception as e:
            self.events.publish("error", text="评估回归异常: %s" % e)

    # ---------- 反思 / 日报 / 进化 ----------
    def _maybe_reflect(self):
        every = max(0.5, float(self.reflection_cfg.get("every_hours", 6))) * 3600
        if time.time() - self.state.get("last_reflection", 0) < every:
            return
        self.state["last_reflection"] = time.time()
        self._save_state()
        try:
            stats = self._gather_stats()
            prompt = (
                "你是 HeartAgent 的自我反思模块。根据以下运行数据,输出 3 条以内可执行的改进建议"
                "(比如失败任务怎么修、闲置扩展是否删除、缺什么记忆),直接给建议,不要客套:\n%s"
                % json.dumps(stats, ensure_ascii=False))
            res = self.main_llm.chat([{"role": "user", "content": prompt}], temperature=0.4)
            text = res.content.strip()
            if text:
                self.memory.add_summary(text, kind="reflection")
                self.events.publish("insight", text="反思建议:%s" % text[:600])
        except Exception as e:
            self.events.publish("error", text="反思失败: %s" % e)

    def _maybe_evolve(self):
        every = max(1, float(self._evolve_cfg.get("every_hours", 24))) * 3600
        if time.time() - self.state.get("last_evolution", 0) < every:
            return
        self.state["last_evolution"] = time.time()
        self._save_state()
        try:
            self.evolver.maybe_run(
                self.state, sample_size=int(self._evolve_cfg.get("sample_size", 4)))
        except Exception as e:
            self.events.publish("error", text="提示词进化异常: %s" % e)

    def _maybe_daily_report(self):
        hour = int(self.daily_report_cfg.get("hour", 9))
        today = time.strftime("%Y-%m-%d")
        if self.state.get("last_report_date") == today or int(time.strftime("%H")) < hour:
            return
        self.state["last_report_date"] = today
        self._save_state()
        try:
            stats = self._gather_stats(since_hours=24)
            prompt = (
                "你是 HeartAgent 的日报生成器。根据以下 24 小时运行数据写一份简短日报"
                "(Markdown,包含:目标进度、任务执行情况、记忆沉淀、扩展使用、一句话建议):\n%s"
                % json.dumps(stats, ensure_ascii=False))
            res = self.main_llm.chat([{"role": "user", "content": prompt}], temperature=0.4)
            text = res.content.strip() or "(无内容)"
            self.memory.add_summary("日报 %s\n%s" % (today, text), kind="report")
            self.events.publish("report", text="HeartAgent 日报 %s\n%s" % (today, text))
            if self.tasks and self.tasks.notify_cfg.get("daily_report_push"):
                from . import notify
                notify.send(self.tasks.notify_cfg, "[HeartAgent] 日报 %s" % today, text[:2500])
            if self.on_daily_report:
                try:
                    self.on_daily_report()
                except Exception as e:
                    self.events.publish("error", text="日报钩子失败: %s" % e)
        except Exception as e:
            self.events.publish("error", text="日报生成失败: %s" % e)

    # ---------- 统计 ----------
    def _gather_stats(self, since_hours=None):
        stats = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "memory": self.memory.overview()}
        if self.goals:
            stats["goals"] = self.goals.stats_snapshot()
        if self.skills:
            stats["skills_pending"] = len(self.skills.unharvested())
        if self.tasks:
            runs = {"total": 0, "failed": 0, "by_task": {}}
            for t in self.tasks.list():
                hist = t.get("history", [])
                if since_hours:
                    cutoff = time.time() - since_hours * 3600
                    hist = [h for h in hist
                            if self._ts_to_epoch(h.get("ts", "")) >= cutoff]
                fails = sum(1 for h in hist if not h.get("ok"))
                if hist:
                    runs["by_task"][t["name"]] = {"runs": len(hist), "failed": fails,
                                                  "enabled": t.get("enabled")}
                runs["total"] += len(hist)
                runs["failed"] += fails
            stats["tasks"] = runs
        if self.extensions:
            stats["extensions"] = self.extensions.usage()
        return stats

    @staticmethod
    def _ts_to_epoch(s):
        import calendar
        try:
            return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return 0
