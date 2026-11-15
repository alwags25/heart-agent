"""记忆库 v2:SQLite + FTS5 全文检索 + 可选向量语义检索 + 去重合并 + 多会话。
- facts:长期事实,新增时自动与已有记忆做相似度合并(向量余弦或 bigram Jaccard)
- summaries:协作AI/反思/日报产出的摘要
- chat_log:按会话分组的对话记录
- 旧版 data/memory.json 会在启动时自动迁移进 SQLite
纯标准库(sqlite3),嵌入向量由 OllamaEmbedder(见 llm.py)提供,不可用时退化为 FTS。"""
import json
import math
import os
import re
import sqlite3
import threading
import time
import zlib

FMT = "%Y-%m-%d %H:%M:%S"


def _tokens(text):
    text = text or ""
    toks = set(re.findall(r"[a-zA-Z0-9_]+", text.lower()))
    han = re.findall(r"[\u4e00-\u9fff]", text)
    toks.update(han)
    toks.update(han[i] + han[i + 1] for i in range(len(han) - 1))
    return toks


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _stable_hash(text):
    """跨进程稳定的字符串哈希(内置 hash() 带随机盐,不能用于落盘向量)。"""
    return zlib.crc32((text or "").encode("utf-8"))


class HashEmbedder:
    """零依赖的轻量语义向量:哈希词袋 + L2 归一化。

    作为 Ollama 嵌入不可用时的兜底,让"语义检索"在离线/未装嵌入服务时也能工作
    (质量略低于真实嵌入,但比纯字符串匹配覆盖面广)。维度固定,进程内/跨进程一致。"""

    def __init__(self, dim=256):
        self.dim = dim
        self.model = "local-hash"

    def embed(self, text):
        vec = [0.0] * self.dim
        for tok in _tokens(text):
            idx = _stable_hash(tok) % self.dim
            vec[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec


def _fts_text(s):
    """unicode61 分词器不切中文,给每个汉字前插空格变成单字 token,查询同理。"""
    return re.sub(r"([\u4e00-\u9fff])", r" \1", s or "")


class MemoryStore:
    def __init__(self, path, embedder=None, cap_facts=500, cap_chat=400, cap_summaries=100):
        self.path = path
        self.embedder = embedder  # 可后置注入(set_embedder)
        self.cap_facts, self.cap_chat, self.cap_summaries = cap_facts, cap_chat, cap_summaries
        self.lock = threading.RLock()
        self._vec_cache = {}      # fact_id -> 已反序列化向量,避免检索时反复 json.loads
        self._local_emb = HashEmbedder()  # 无 Ollama 时的零依赖语义兜底
        self._triple_cache = None  # 实体 -> 三元组 倒排索引(惰性构建)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self._fts_ok = True
        self._create_tables()
        self._migrate_json(path + ".legacy" if os.path.exists(path + ".legacy") else
                           os.path.join(os.path.dirname(path), "memory.json"))
        if not self._session_count():
            self._new_session("默认会话")

    # ---------- 基础 ----------
    def _create_tables(self):
        with self.lock, self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS facts(
                id TEXT PRIMARY KEY, content TEXT, tags TEXT, source TEXT,
                created_at TEXT, updated_at TEXT, hits INTEGER DEFAULT 0,
                emb BLOB, emb_model TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS summaries(
                id TEXT PRIMARY KEY, kind TEXT DEFAULT 'chat', summary TEXT, created_at TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS chat_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                role TEXT, content TEXT, ts TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY, title TEXT, created_at TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS meta(
                k TEXT PRIMARY KEY, v TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS knowledge(
                id TEXT PRIMARY KEY, source TEXT, tags TEXT,
                chunk TEXT, emb BLOB, created_at TEXT)""")
            # 睡眠整理:记忆归档/强度 + 矛盾登记 + 实体关系图谱
            for col in ("archived INTEGER DEFAULT 0", "strength REAL DEFAULT 1.0",
                        "extracted INTEGER DEFAULT 0"):
                try:
                    self.db.execute("ALTER TABLE facts ADD COLUMN %s" % col)
                except sqlite3.OperationalError:
                    pass
            self.db.execute("""CREATE TABLE IF NOT EXISTS conflicts(
                id TEXT PRIMARY KEY, fact_a TEXT, fact_b TEXT,
                question TEXT, status TEXT DEFAULT 'open',
                resolution TEXT, created_at TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS triples(
                id TEXT PRIMARY KEY, subj TEXT, rel TEXT, obj TEXT,
                fact_id TEXT, created_at TEXT)""")
            try:
                self.db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(content, fact_id UNINDEXED)")
            except sqlite3.OperationalError:
                self._fts_ok = False  # 无 FTS5 时退化为 LIKE

    def _migrate_json(self, legacy_path):
        if not os.path.exists(legacy_path):
            return
        try:
            with open(legacy_path, encoding="utf-8") as f:
                old = json.load(f)
        except Exception:
            return
        now = time.strftime(FMT)
        seq = int(time.time() * 1000)
        with self.lock, self.db:
            for fac in old.get("facts", []):
                if isinstance(fac, dict) and fac.get("content"):
                    seq += 1
                    fid = fac.get("id") or "f%d" % seq
                    self.db.execute(
                        "INSERT OR IGNORE INTO facts(id,content,tags,source,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (fid, fac["content"], json.dumps(fac.get("tags", []), ensure_ascii=False),
                         fac.get("source", "迁移"), fac.get("created_at", now), now))
                    self._fts_insert(fid, fac["content"])
            for s in old.get("summaries", []):
                if isinstance(s, dict) and s.get("summary"):
                    self.db.execute("INSERT OR IGNORE INTO summaries(id,kind,summary,created_at)"
                                    " VALUES(?,?,?,?)",
                                    (s.get("id") or "s%d" % int(time.time() * 1000),
                                     "chat", s["summary"], s.get("created_at", now)))
            sid = self.current_session()
            for m in old.get("chat_log", []):
                if isinstance(m, dict):
                    self.db.execute("INSERT INTO chat_log(session_id,role,content,ts) VALUES(?,?,?,?)",
                                    (sid, m.get("role", "user"), m.get("content", ""), m.get("ts", "")))
        os.replace(legacy_path, legacy_path + ".migrated")

    def _fts_insert(self, fid, content):
        if self._fts_ok:
            self.db.execute("INSERT INTO facts_fts(content,fact_id) VALUES(?,?)",
                            (_fts_text(content), fid))

    def _fts_delete(self, fid):
        if self._fts_ok:
            self.db.execute("DELETE FROM facts_fts WHERE fact_id=?", (fid,))

    def _fts_search(self, query, top=20):
        if not self._fts_ok:
            like = "%" + (query or "").strip() + "%"
            rows = self.db.execute(
                "SELECT id FROM facts WHERE content LIKE ? LIMIT ?", (like, top)).fetchall()
            return [r[0] for r in rows]
        toks = [t for t in re.findall(r"\S+", _fts_text(query)) if t][:8]
        if not toks:
            return []
        match = " AND ".join('"%s"' % t.replace('"', "") for t in toks)
        try:
            rows = self.db.execute(
                "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ? LIMIT ?", (match, top)).fetchall()
            hits = [r[0] for r in rows]
        except sqlite3.OperationalError:
            return []
        if not hits:  # AND 太严时退化为 OR
            match = " OR ".join('"%s"' % t.replace('"', "") for t in toks)
            try:
                rows = self.db.execute(
                    "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ? LIMIT ?", (match, top)).fetchall()
                hits = [r[0] for r in rows]
            except sqlite3.OperationalError:
                hits = []
        return hits

    # ---------- 嵌入 ----------
    def set_embedder(self, embedder):
        self.embedder = embedder
        self._vec_cache.clear()  # 嵌入源可能变化,向量维度不一致,清空缓存

    def _emb_of(self, text):
        if not self.embedder:
            return None
        try:
            return self.embedder.embed(text)
        except Exception:
            return None

    def _query_vec(self, text):
        """检索查询向量:优先 Ollama 嵌入,不可用时退回零依赖哈希词袋向量。"""
        if self.embedder:
            try:
                return self.embedder.embed(text)
            except Exception:
                pass
        return self._local_emb.embed(text)

    def _fact_vec(self, fid, emb_json):
        """反序列化事实/知识块的向量,带缓存,避免每次检索重复 json.loads。"""
        if not emb_json:
            return None
        v = self._vec_cache.get(fid)
        if v is None:
            try:
                v = json.loads(emb_json)
                self._vec_cache[fid] = v
            except Exception:
                return None
        return v

    def _row_fact(self, row):
        return {"id": row[0], "content": row[1],
                "tags": json.loads(row[2] or "[]"), "source": row[3],
                "created_at": row[4], "updated_at": row[5], "hits": row[6],
                "archived": bool(row[7]) if len(row) > 7 and row[7] is not None else False}

    # ---------- 长期事实 ----------
    def add_fact(self, content, tags=None, source="user"):
        content = str(content).strip()
        if not content:
            return {"id": None, "merged": False}
        tags = [str(t) for t in (tags or [])][:8]
        now = time.strftime(FMT)
        emb = self._emb_of(content)
        with self.lock, self.db:
            # 去重合并:与已有记忆高度相似则合并而不是新增
            for row in self.db.execute(
                    "SELECT id,content,tags,emb FROM facts WHERE archived=0"
                    " ORDER BY updated_at DESC LIMIT 400").fetchall():
                old_emb = json.loads(row[3]) if row[3] else None
                sim = (_cosine(emb, old_emb) if emb and old_emb else
                       _jaccard(_tokens(content), _tokens(row[1])))
                if sim >= (0.86 if emb and old_emb else 0.78):
                    new_tags = list(dict.fromkeys(json.loads(row[2] or "[]") + tags))
                    merged = content if len(content) >= len(row[1]) else row[1]
                    self.db.execute(
                        "UPDATE facts SET content=?, tags=?, updated_at=?, emb=?, emb_model=? WHERE id=?",
                        (merged, json.dumps(new_tags, ensure_ascii=False), now,
                         json.dumps(emb) if emb else row[3],
                         self.embedder.model if emb and self.embedder else None, row[0]))
                    self._fts_delete(row[0])
                    self._fts_insert(row[0], merged)
                    self._vec_cache.pop(row[0], None)
                    return {"id": row[0], "merged": True, "content": merged}
            fid = "f%d" % int(time.time() * 1000)
            self.db.execute(
                "INSERT INTO facts(id,content,tags,source,created_at,updated_at,emb,emb_model)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (fid, content, json.dumps(tags, ensure_ascii=False), source, now, now,
                 json.dumps(emb) if emb else None,
                 self.embedder.model if emb and self.embedder else None))
            self._fts_insert(fid, content)
            n = self.db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            if n > self.cap_facts:  # 淘汰最久未命中/未更新的
                old = self.db.execute(
                    "SELECT id FROM facts ORDER BY updated_at ASC LIMIT ?",
                    (n - self.cap_facts,)).fetchall()
                for (oid,) in old:
                    self.db.execute("DELETE FROM facts WHERE id=?", (oid,))
                    self._fts_delete(oid)
            return {"id": fid, "merged": False, "content": content}

    def update_fact(self, fid, content=None, tags=None):
        with self.lock, self.db:
            row = self.db.execute("SELECT id FROM facts WHERE id=?", (fid,)).fetchone()
            if not row:
                return False
            if content is not None:
                emb = self._emb_of(content)
                self.db.execute("UPDATE facts SET content=?, updated_at=?, emb=?, emb_model=? WHERE id=?",
                                (str(content).strip(), time.strftime(FMT),
                                 json.dumps(emb) if emb else None,
                                 self.embedder.model if emb and self.embedder else None, fid))
                self._fts_delete(fid)
                self._fts_insert(fid, str(content).strip())
                self._vec_cache.pop(fid, None)
            if tags is not None:
                self.db.execute("UPDATE facts SET tags=? WHERE id=?",
                                (json.dumps([str(t) for t in tags][:8], ensure_ascii=False), fid))
            return True

    def delete_fact(self, fid):
        with self.lock, self.db:
            cur = self.db.execute("DELETE FROM facts WHERE id=?", (fid,))
            self._fts_delete(fid)
            self._vec_cache.pop(fid, None)
            return cur.rowcount > 0

    def facts(self, include_archived=False):
        with self.lock:
            return [self._row_fact(r) for r in self.db.execute(
                "SELECT id,content,tags,source,created_at,updated_at,hits,archived FROM facts"
                + ("" if include_archived else " WHERE archived=0") +
                " ORDER BY updated_at DESC").fetchall()]

    def set_archived(self, fid, flag=1):
        with self.lock, self.db:
            cur = self.db.execute("UPDATE facts SET archived=? WHERE id=?",
                                  (1 if flag else 0, fid))
            return cur.rowcount > 0

    def sleep_decay(self, days=30, min_hits=2):
        """遗忘曲线:久未命中且过旧的弱记忆归档(可恢复)。返回归档数量。"""
        cutoff = time.strftime(FMT, time.localtime(time.time() - days * 86400))
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT id FROM facts WHERE archived=0 AND hits<=? AND updated_at<?",
                (min_hits, cutoff)).fetchall()
            for (fid,) in rows:
                self.db.execute("UPDATE facts SET archived=1 WHERE id=?", (fid,))
            return len(rows)

    def search_facts(self, query, top=5):
        """混合检索:FTS 命中 + 向量余弦综合排序(不含已归档)。
        无 Ollama 嵌入时退回零依赖哈希词袋向量,仍保留语义相关性而非纯字符串匹配。"""
        with self.lock:
            fts_ids = set(self._fts_search(query))
            rows = self.db.execute(
                "SELECT id,content,tags,source,created_at,updated_at,hits,emb FROM facts"
                " WHERE archived=0").fetchall()
        qvec = self._query_vec(query)
        has_embedder = bool(self.embedder)
        low_q = (query or "").strip().lower()
        scored = []
        for r in rows:
            fid = r[0]
            score = 0.5 if fid in fts_ids else 0.0
            if has_embedder:
                v = self._fact_vec(fid, r[7])
                if v is not None:
                    score += _cosine(qvec, v)
            else:
                score += _cosine(qvec, self._local_emb.embed(r[1]))
                if low_q and low_q in (r[1] or "").lower():
                    score += 0.15
            if score > 0.15:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        out = []
        with self.lock, self.db:
            for score, r in scored[:top]:
                self.db.execute("UPDATE facts SET hits=hits+1 WHERE id=?", (r[0],))
                f = self._row_fact(r[:7])
                f["score"] = round(score, 3)
                out.append(f)
        return out

    # ---------- 摘要 ----------
    def add_summary(self, text, kind="chat"):
        sid = "s%d" % int(time.time() * 1000)
        with self.lock, self.db:
            self.db.execute("INSERT INTO summaries(id,kind,summary,created_at) VALUES(?,?,?,?)",
                            (sid, kind, str(text).strip(), time.strftime(FMT)))
            n = self.db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
            if n > self.cap_summaries:
                for (oid,) in self.db.execute(
                        "SELECT id FROM summaries ORDER BY created_at ASC LIMIT ?",
                        (n - self.cap_summaries,)).fetchall():
                    self.db.execute("DELETE FROM summaries WHERE id=?", (oid,))
        return {"id": sid, "summary": text, "kind": kind}

    def summaries(self, kind=None):
        with self.lock:
            if kind:
                rows = self.db.execute(
                    "SELECT id,kind,summary,created_at FROM summaries WHERE kind=?"
                    " ORDER BY created_at DESC", (kind,)).fetchall()
            else:
                rows = self.db.execute(
                    "SELECT id,kind,summary,created_at FROM summaries"
                    " ORDER BY created_at DESC").fetchall()
        return [{"id": r[0], "kind": r[1], "summary": r[2], "created_at": r[3]} for r in rows]

    # ---------- 会话与对话 ----------
    def _session_count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def _new_session(self, title):
        sid = "sess%s%03d" % (time.strftime("%Y%m%d%H%M%S"), int(time.time() * 1000) % 1000)
        with self.lock, self.db:
            self.db.execute("INSERT INTO sessions(id,title,created_at) VALUES(?,?,?)",
                            (sid, title or "新会话", time.strftime(FMT)))
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('current_session',?)", (sid,))
        return sid

    def new_session(self, title=None):
        with self.lock:
            return self._new_session(title or "会话 " + time.strftime("%m-%d %H:%M"))

    def switch_session(self, sid):
        with self.lock:
            row = self.db.execute("SELECT id FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row:
                return False
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('current_session',?)", (sid,))
            return True

    def current_session(self):
        with self.lock:
            row = self.db.execute("SELECT v FROM meta WHERE k='current_session'").fetchone()
            return row[0] if row else self._new_session("默认会话")

    def list_sessions(self):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,title,created_at FROM sessions ORDER BY created_at DESC LIMIT 30").fetchall()
        cur = self.current_session()
        return [{"id": r[0], "title": r[1], "created_at": r[2], "current": r[0] == cur}
                for r in rows]

    def append_chat(self, role, content, session_id=None):
        sid = session_id or self.current_session()
        with self.lock, self.db:
            self.db.execute("INSERT INTO chat_log(session_id,role,content,ts) VALUES(?,?,?,?)",
                            (sid, role, str(content), time.strftime("%m-%d %H:%M:%S")))
            n = self.db.execute("SELECT COUNT(*) FROM chat_log WHERE session_id=?", (sid,)).fetchone()[0]
            if n > self.cap_chat:
                for (oid,) in self.db.execute(
                        "SELECT id FROM chat_log WHERE session_id=? ORDER BY id ASC LIMIT ?",
                        (sid, n - self.cap_chat)).fetchall():
                    self.db.execute("DELETE FROM chat_log WHERE id=?", (oid,))

    def chat_log(self, n=50, session_id=None):
        sid = session_id or self.current_session()
        with self.lock:
            rows = self.db.execute(
                "SELECT role,content,ts FROM chat_log WHERE session_id=?"
                " ORDER BY id DESC LIMIT ?", (sid, n)).fetchall()
        return [{"role": r[0], "content": r[1], "ts": r[2]} for r in reversed(rows)]

    def chat_len(self, session_id=None):
        sid = session_id or self.current_session()
        with self.lock:
            return self.db.execute(
                "SELECT COUNT(*) FROM chat_log WHERE session_id=?", (sid,)).fetchone()[0]

    def overview(self):
        with self.lock:
            return {"facts": self.db.execute(
                        "SELECT COUNT(*) FROM facts WHERE archived=0").fetchone()[0],
                    "archived": self.db.execute(
                        "SELECT COUNT(*) FROM facts WHERE archived=1").fetchone()[0],
                    "conflicts_open": self.db.execute(
                        "SELECT COUNT(*) FROM conflicts WHERE status='open'").fetchone()[0],
                    "summaries": self.db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0],
                    "knowledge": self.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0],
                    "chat_log": self.db.execute(
                        "SELECT COUNT(*) FROM chat_log WHERE session_id=?",
                        (self.current_session(),)).fetchone()[0],
                    "sessions": self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                    "fts": self._fts_ok, "semantic": bool(self.embedder)}

    def facts_x(self):
        """内部:取全部未归档事实(含嵌入),供矛盾候选检测。"""
        with self.lock:
            return self.db.execute(
                "SELECT id,content,emb FROM facts WHERE archived=0").fetchall()

    def conflict_candidates(self, min_sim=0.25, max_sim=0.95, limit=20):
        """找语义相近但文字不同的记忆对(矛盾/过时高发区)。"""
        rows = self.facts_x()
        vecs = []
        for fid, content, emb in rows:
            v = None
            if emb:
                try:
                    v = json.loads(emb)
                except Exception:
                    v = None
            vecs.append((fid, content, v, _tokens(content)))
        existing = {(c[1], c[2]) for c in self.list_conflicts_raw()}
        pairs, n = [], len(vecs)
        for i in range(n):
            for j in range(i + 1, n):
                a, b = vecs[i], vecs[j]
                sim = (_cosine(a[2], b[2]) if a[2] and b[2]
                       else _jaccard(a[3], b[3]))
                if min_sim <= sim <= max_sim and (a[0], b[0]) not in existing:
                    pairs.append({"a_id": a[0], "a": a[1], "b_id": b[0], "b": b[1],
                                  "sim": round(sim, 3)})
                if len(pairs) >= limit:
                    return pairs
        return pairs

    def list_conflicts_raw(self):
        with self.lock:
            return self.db.execute(
                "SELECT id,fact_a,fact_b FROM conflicts").fetchall()

    def add_conflict(self, fact_a, fact_b, question):
        cid = "c%d" % int(time.time() * 1000)
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO conflicts(id,fact_a,fact_b,question,status,created_at)"
                " VALUES(?,?,?,?, 'open', ?)",
                (cid, fact_a, fact_b, str(question)[:400], time.strftime(FMT)))
        return cid

    def list_conflicts(self, status="open"):
        with self.lock:
            rows = self.db.execute(
                """SELECT c.id,c.fact_a,c.fact_b,c.question,c.status,c.resolution,c.created_at,
                          fa.content, fb.content
                   FROM conflicts c
                   LEFT JOIN facts fa ON fa.id=c.fact_a
                   LEFT JOIN facts fb ON fb.id=c.fact_b
                   WHERE (? = 'all' OR c.status=?)
                   ORDER BY c.created_at DESC LIMIT 50""",
                (status or "all", status or "all")).fetchall()
        return [{"id": r[0], "fact_a": r[1], "fact_b": r[2], "question": r[3],
                 "status": r[4], "resolution": r[5], "created_at": r[6],
                 "a_text": r[7] or "(已删除)", "b_text": r[8] or "(已删除)"}
                for r in rows]

    def resolve_conflict(self, cid, action):
        """action: keep_a(归档b) / keep_b(归档a) / dismiss(误报)"""
        with self.lock:
            row = self.db.execute(
                "SELECT fact_a,fact_b FROM conflicts WHERE id=?", (cid,)).fetchone()
            if not row:
                return False
            fa, fb = row
        if action == "keep_a":
            self.set_archived(fb, 1)
        elif action == "keep_b":
            self.set_archived(fa, 1)
        with self.lock, self.db:
            self.db.execute(
                "UPDATE conflicts SET status='resolved', resolution=? WHERE id=?",
                (action, cid))
        return True

    def open_conflict_questions(self, top=3):
        return [{"id": c["id"], "question": c["question"],
                 "a": c["a_text"], "b": c["b_text"]}
                for c in self.list_conflicts("open")[:top]]

    # ---------- 向量回填 / 实体关系图谱 ----------
    def backfill_embeddings(self, batch=30):
        """给尚未生成向量的记忆/知识块补嵌入(嵌入服务就绪后调用)。"""
        if not self.embedder:
            return 0
        done = 0
        with self.lock, self.db:
            for fid, content in self.db.execute(
                    "SELECT id,content FROM facts WHERE emb IS NULL AND archived=0"
                    " LIMIT ?", (batch,)).fetchall():
                emb = self._emb_of(content)
                if emb:
                    self.db.execute("UPDATE facts SET emb=?, emb_model=? WHERE id=?",
                                    (json.dumps(emb), self.embedder.model, fid))
                    done += 1
            for kid, chunk in self.db.execute(
                    "SELECT id,chunk FROM knowledge WHERE emb IS NULL LIMIT ?",
                    (batch,)).fetchall():
                emb = self._emb_of(chunk)
                if emb:
                    self.db.execute("UPDATE knowledge SET emb=? WHERE id=?",
                                    (json.dumps(emb), kid))
                    done += 1
        return done

    def unextracted_facts(self, limit=10):
        with self.lock:
            return [{"id": r[0], "content": r[1]} for r in self.db.execute(
                "SELECT id,content FROM facts WHERE extracted=0 AND archived=0"
                " ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()]

    def mark_extracted(self, fids):
        with self.lock, self.db:
            for fid in fids:
                self.db.execute("UPDATE facts SET extracted=1 WHERE id=?", (fid,))

    def add_triples(self, items):
        """items: [{"s","r","o"}];同一条三元组去重。返回新增数。"""
        added = 0
        now = time.strftime(FMT)
        with self.lock, self.db:
            for it in items:
                s, r, o = str(it.get("s", "")).strip()[:60], \
                    str(it.get("r", "")).strip()[:40], str(it.get("o", "")).strip()[:60]
                if not (s and r and o):
                    continue
                dup = self.db.execute(
                    "SELECT 1 FROM triples WHERE subj=? AND rel=? AND obj=? LIMIT 1",
                    (s, r, o)).fetchone()
                if dup:
                    continue
                self.db.execute(
                    "INSERT INTO triples(id,subj,rel,obj,fact_id,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    ("tp%d" % int(time.time() * 1000000 % 1e10), s, r, o,
                     it.get("fact_id"), now))
                added += 1
        if added:
            self._triple_cache = None  # 新增后倒排索引失效
        return added

    def all_triples(self, limit=200):
        with self.lock:
            return [{"id": r[0], "s": r[1], "r": r[2], "o": r[3]}
                    for r in self.db.execute(
                        "SELECT id,subj,rel,obj FROM triples"
                        " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()]

    def graph(self, max_edges=40):
        rows = self.all_triples(max_edges)
        degree = {}
        for t in rows:
            degree[t["s"]] = degree.get(t["s"], 0) + 1
            degree[t["o"]] = degree.get(t["o"], 0) + 1
        nodes = [{"id": k, "label": k, "degree": v}
                 for k, v in sorted(degree.items(), key=lambda x: -x[1])][:30]
        ids = {n["id"] for n in nodes}
        return {"nodes": nodes,
                "edges": [t for t in rows if t["s"] in ids and t["o"] in ids]}

    def _build_triple_index(self):
        """实体 → 三元组 倒排索引(惰性构建并缓存),避免每次对话全量遍历。"""
        if self._triple_cache is None:
            idx = {}
            for t in self.all_triples(2000):
                for ent in (t["s"], t["o"]):
                    idx.setdefault(ent, []).append(t)
            self._triple_cache = idx
        return self._triple_cache

    def graph_context(self, query, max_items=8):
        """与查询相关的实体关系文本,注入系统提示(走实体倒排索引)。"""
        toks = _tokens(query or "")
        if not toks:
            return ""
        hits, seen = [], set()
        for ent, triples in self._build_triple_index().items():
            if _tokens(ent) & toks:
                for t in triples:
                    key = (t["s"], t["r"], t["o"])
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append("- %s —%s→ %s" % key)
                    if len(hits) >= max_items:
                        return "\n".join(hits)
        return "\n".join(hits)

    # ---------- 知识库(RAG) ----------
    def add_knowledge_chunks(self, source, chunks, tags=None):
        """批量写入知识块;同 source 下相同内容自动去重。返回新增数量。"""
        tags_json = json.dumps([str(t) for t in (tags or [])][:8], ensure_ascii=False)
        added = 0
        now = time.strftime(FMT)
        with self.lock, self.db:
            for ch in chunks:
                ch = (ch or "").strip()
                if len(ch) < 20:
                    continue
                kid = "k%d" % (time.time_ns() + added)
                dup = self.db.execute(
                    "SELECT 1 FROM knowledge WHERE source=? AND chunk=? LIMIT 1",
                    (source, ch[:4000])).fetchone()
                if dup:
                    continue
                emb = self._emb_of(ch)
                self.db.execute(
                    "INSERT INTO knowledge(id,source,tags,chunk,emb,created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (kid, source, tags_json, ch[:4000],
                     json.dumps(emb) if emb else None, now))
                added += 1
                if added >= 200:
                    break
        return added

    def search_knowledge(self, query, top=5):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,source,chunk,emb FROM knowledge").fetchall()
        qvec = self._query_vec(query)
        has_embedder = bool(self.embedder)
        qtok = _tokens(query or "")
        scored = []
        for r in rows:
            score = 0.0
            if has_embedder:
                v = self._fact_vec(r[0], r[3])
                if v is not None:
                    score = _cosine(qvec, v)
            else:
                # 无嵌入时:词元重叠 + 零依赖哈希向量余弦,覆盖面更广
                ktok = _tokens(r[2] or "")
                overlap = len(qtok & ktok)
                if qtok and overlap:
                    score = min(0.85, 0.25 + 0.1 * overlap)
                score = max(score, _cosine(qvec, self._local_emb.embed(r[2])))
            if score > 0.12:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        return [{"id": r[0], "source": r[1], "chunk": r[2], "score": round(s, 3)}
                for s, r in scored[:top]]

    def knowledge_count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]

    def clear_knowledge(self, source=None):
        with self.lock, self.db:
            if source:
                cur = self.db.execute("DELETE FROM knowledge WHERE source=?", (source,))
            else:
                cur = self.db.execute("DELETE FROM knowledge")
            return cur.rowcount

    # ---------- meta(上下文摘要等内部状态) ----------
    def get_meta(self, key):
        with self.lock:
            row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, str(value)))

    def close(self):
        with self.lock:
            self.db.close()
