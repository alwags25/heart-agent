"""扩展系统 v2(能力自我进化 + 可控):
- 审批门:LLM 生成的扩展默认进入 pending/ 等待用户在 UI 批准(可配置 auto_approve 直通)
- 版本管理:覆盖安装前自动备份旧版到 versions/
- 审计日志:安装/批准/拒绝/调用失败/删除 全部落 data/ext_audit.jsonl
- 用量统计:每个扩展的调用次数与最近使用时间(供反思机制分析)
- 资源限制:子进程挂进 Windows Job Object,限内存 + 超时连子树一起杀
"""
import ast
import json
import os
import re
import subprocess
import sys
import time

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    BASE = os.path.dirname(sys.executable)  # 可写根:数据/扩展在 exe 旁
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(BASE, "runner.py")


def _runner_cmd(path, args_json):
    """扩展子进程命令:源码模式用 runner.py;冻结模式用主程序 --__runner__。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--__runner__", path, args_json]
    return [sys.executable, RUNNER, path, args_json]

GEN_SYS = """你是 HeartAgent 的扩展工程师,负责为智能体编写 Python 扩展模块。硬性要求:
1. 必须定义 META = {"name": "扩展名", "description": "一句话描述"}
2. 必须定义 def run(args: dict) -> dict,入参从 args 字典取,返回 dict(含 "ok" 字段)
3. 只使用 Python 标准库;禁止 input()、死循环、联网下载大文件;执行要快(60 秒内)
4. 代码健壮:参数缺失给出清晰错误;异常要 try/except 后返回 {"ok": False, "error": ...}
5. 只输出完整 Python 代码,不要任何解释和 markdown 围栏"""

BANNED_PATTERNS = [r"\binput\s*\(", r"\bwhile\s+True\s*:(?![^\n]*sleep)",
                   r"shutil\.rmtree\s*\(\s*['\"]/", r"os\.system\s*\("]


def _job_limit(proc, mem_mb):
    """Windows:把子进程放进 Job Object,限制内存、进程树随超时一起终止。尽力而为。"""
    if os.name != "nt" or not mem_mb:
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(f, ctypes.c_ulonglong) for f in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wt.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.windll.kernel32
        h = k32.CreateJobObjectW(None, None)
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = 0x100 | 0x2000  # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
        info.ProcessMemoryLimit = int(mem_mb) * 1024 * 1024
        if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        if not k32.AssignProcessToJobObject(h, int(proc._handle)):
            return None
        return h
    except Exception:
        return None


def detect_caps(code):
    """静态分析扩展代码,推断所需能力令牌(可被 META.capabilities 补充)。"""
    caps = set()
    NET = {"socket", "urllib", "http", "ftplib", "smtplib", "requests", "ssl"}
    PROC = {"subprocess", "multiprocessing", "pty"}
    try:
        tree = ast.parse(code or "")
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for n in names:
                root = n.split(".")[0]
                if root in NET:
                    caps.add("net")
                if root in PROC:
                    caps.add("proc")
    except Exception:
        pass
    if re.search(r"os\.(system|popen)\s*\(", code or ""):
        caps.add("proc")
    return sorted(caps)


def run_module(path, args, timeout=60, mem_mb=512, caps=None, roots=None, restricted=False):
    """隔离子进程执行扩展/代码,按能力令牌注入围栏,返回 {"ok":..., "result":...}。
    restricted=True 时附加受限令牌降权(增强围栏,尽力而为)。"""
    env = dict(os.environ)
    env["HEARTAGENT_CAPS"] = json.dumps(sorted(caps or []))
    env["HEARTAGENT_ROOTS"] = json.dumps([str(r) for r in (roots or [])])
    if restricted:
        env["HEARTAGENT_RESTRICTED"] = "1"
    try:
        proc = subprocess.Popen(
            _runner_cmd(path, json.dumps(args or {}, ensure_ascii=False)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    except Exception as e:
        return {"ok": False, "error": "启动子进程失败: %s" % e}
    job = _job_limit(proc, mem_mb)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        if job:  # 杀掉整个进程树
            try:
                import ctypes
                ctypes.windll.kernel32.TerminateJobObject(job, 1)
            except Exception:
                pass
        return {"ok": False, "error": "执行超时(>%ss)" % timeout}
    out_txt = (out or b"").decode("utf-8", "replace").strip()
    err_txt = (err or b"").decode("utf-8", "replace")
    try:
        return json.loads(out_txt)
    except Exception:
        return {"ok": proc.returncode == 0, "stdout": out_txt[-2000:], "stderr": err_txt[-1000:]}


class ExtensionManager:
    def __init__(self, ext_dir, events=None, data_dir=None, auto_approve=False, mem_mb=512,
                 restricted=False):
        self.dir = ext_dir
        self.events = events
        self.data_dir = data_dir or os.path.join(BASE, "data")
        self.auto_approve = auto_approve
        self.mem_mb = mem_mb
        self.restricted = restricted  # 受限令牌降权(增强围栏,默认关闭)
        self.pending_dir = os.path.join(ext_dir, "pending")
        self.versions_dir = os.path.join(ext_dir, "versions")
        self.usage_path = os.path.join(self.data_dir, "ext_usage.json")
        self.trust_path = os.path.join(self.data_dir, "ext_trust.json")
        self.audit_path = os.path.join(self.data_dir, "ext_audit.jsonl")
        self.fs_roots = [self.data_dir, ext_dir, os.path.join(BASE, "extensions"),
                         os.environ.get("TEMP", os.path.join(BASE, "data"))]
        for d in (ext_dir, self.pending_dir, self.versions_dir, self.data_dir):
            os.makedirs(d, exist_ok=True)

    # ---------- 路径 ----------
    def _path(self, name):
        return os.path.join(self.dir, "ext_%s.py" % name)

    def _pending_path(self, name):
        return os.path.join(self.pending_dir, "ext_%s.py" % name)

    @staticmethod
    def safe_name(name):
        return re.sub(r"\W", "_", str(name or "").strip())

    # ---------- 审计/用量 ----------
    def audit(self, action, name, detail=""):
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action,
               "name": name, "detail": str(detail)[:500]}
        try:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def audit_tail(self, n=30):
        try:
            with open(self.audit_path, encoding="utf-8") as f:
                return [json.loads(x) for x in f.readlines()[-n:] if x.strip()]
        except OSError:
            return []

    def _load_usage(self):
        try:
            with open(self.usage_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_usage(self, u):
        try:
            with open(self.usage_path, "w", encoding="utf-8") as f:
                json.dump(u, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def usage(self):
        return self._load_usage()

    # ---------- 信任分 / 能力令牌 ----------
    # L0(<40): AI 不可调用,需用户在 UI 手动调用积累信任
    # L1(40-69): AI 可调用  L2(70-89): 可参与技能收割
    # L3(>=90): 可被无人值守任务自由使用
    def _load_trust(self):
        try:
            with open(self.trust_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_trust(self, t):
        try:
            with open(self.trust_path, "w", encoding="utf-8") as f:
                json.dump(t, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    @staticmethod
    def trust_level(score):
        return 3 if score >= 90 else 2 if score >= 70 else 1 if score >= 40 else 0

    def trust(self, name):
        t = self._load_trust().get(name) or {}
        score = t.get("score", 55)
        return {"name": name, "score": score, "level": self.trust_level(score),
                "calls": t.get("calls", 0), "failures": t.get("failures", 0),
                "caps": t.get("caps", [])}

    def trust_all(self):
        return [self.trust(e["name"]) for e in self.list()]

    def _trust_update(self, name, ok, caps=None):
        t = self._load_trust()
        e = t.setdefault(name, {"score": 55, "calls": 0, "failures": 0,
                                "caps": caps or []})
        if caps:
            e["caps"] = sorted(set(e.get("caps", [])) | set(caps))
        e["calls"] += 1
        e["score"] = max(0, min(100, e["score"] + (4 if ok else -25)))
        if not ok:
            e["failures"] += 1
        self._save_trust(t)
        return self.trust(name)

    def _caps_of(self, name):
        """生效能力令牌 = META.capabilities ∪ 安装时静态检测。"""
        meta_caps = self._read_meta(self._path(name)).get("capabilities") or []
        stored = self._load_trust().get(name, {}).get("caps", [])
        return sorted(set(str(c) for c in meta_caps) | set(stored))

    def _touch_usage(self, name, ok):
        u = self._load_usage()
        e = u.setdefault(name, {"calls": 0, "failures": 0, "last": None})
        e["calls"] += 1
        e["failures"] += 0 if ok else 1
        e["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_usage(u)

    # ---------- 元信息 ----------
    def _read_meta(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                        getattr(t, "id", None) == "META" for t in node.targets):
                    meta = ast.literal_eval(node.value)
                    if isinstance(meta, dict):
                        return meta
        except Exception:
            pass
        return {}

    def list(self):
        items = []
        for fn in sorted(os.listdir(self.dir)):
            if fn.startswith("ext_") and fn.endswith(".py"):
                meta = self._read_meta(os.path.join(self.dir, fn))
                items.append({"name": meta.get("name") or fn[4:-3],
                              "description": meta.get("description", ""),
                              "version": meta.get("version", 1),
                              "file": fn})
        return items

    def pending(self):
        items = []
        if os.path.isdir(self.pending_dir):
            for fn in sorted(os.listdir(self.pending_dir)):
                if fn.startswith("ext_") and fn.endswith(".py"):
                    path = os.path.join(self.pending_dir, fn)
                    try:
                        with open(path, encoding="utf-8") as f:
                            code = f.read()
                    except OSError:
                        code = ""
                    items.append({"name": fn[4:-3],
                                  "description": self._read_meta(path).get("description", ""),
                                  "code": code})
        return items

    # ---------- 安装 ----------
    def _validate(self, name, code):
        if not name or not name.isidentifier():
            raise ValueError("扩展名不合法(字母/数字/下划线,不以数字开头): %r" % name)
        code = (code or "").strip()
        if not code:
            raise ValueError("扩展代码为空")
        try:
            compile(code, "ext_%s.py" % name, "exec")
        except SyntaxError as e:
            raise ValueError("扩展代码语法错误: %s" % e)
        if "def run(" not in code:
            raise ValueError("扩展缺少 def run(args) 函数")
        warnings = [p for p in BANNED_PATTERNS if re.search(p, code)]
        return code, warnings

    def _import_test(self, path):
        check = ("import importlib.util,sys;"
                 "sp=importlib.util.spec_from_file_location('e',sys.argv[1]);"
                 "m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m);"
                 "assert hasattr(m,'run') and hasattr(m,'META')")
        return subprocess.run([sys.executable, "-c", check, path],
                              capture_output=True, timeout=30)

    def install(self, name, code, description="", approve=None):
        """校验并安装。approve=None 时按 self.auto_approve 决定是否直通。"""
        name = self.safe_name(name)
        code, warnings = self._validate(name, code)
        caps = sorted(set(detect_caps(code))
                      | set(str(c) for c in self._read_meta_from_code(code)
                            .get("capabilities", [])))
        direct = self.auto_approve if approve is None else approve
        if not direct:
            with open(self._pending_path(name), "w", encoding="utf-8") as f:
                f.write(code)
            self.audit("pending", name, description)
            if self.events:
                self.events.publish("extension",
                                    text="⏳ 扩展待审批: %s(%s)%s —— 请在「扩展」页查看代码并批准"
                                         % (name, description or "无描述",
                                            "(需要能力: %s)" % ", ".join(caps) if caps else ""))
            return {"ok": True, "status": "pending", "name": name,
                    "warnings": warnings, "caps": caps,
                    "file": "pending/ext_%s.py" % name}
        return self._finalize(name, code, description, warnings, caps)

    def _finalize(self, name, code, description, warnings, caps=None):
        path = self._path(name)
        backup = None
        if os.path.exists(path):  # 版本备份
            os.makedirs(self.versions_dir, exist_ok=True)
            backup = "ext_%s_%s.py" % (name, time.strftime("%Y%m%d%H%M%S"))
            os.replace(path, os.path.join(self.versions_dir, backup))
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        proc = self._import_test(path)
        if proc.returncode != 0:
            if backup:
                os.replace(os.path.join(self.versions_dir, backup), path)
            else:
                os.remove(path)
            self.audit("install_failed", name, proc.stderr.decode("utf-8", "replace")[-400:])
            raise ValueError("扩展导入测试失败:\n%s" % proc.stderr.decode("utf-8", "replace")[-800:])
        self.audit("installed", name, description)
        t = self._load_trust()
        e = t.setdefault(name, {"score": 55, "calls": 0, "failures": 0, "caps": []})
        e["caps"] = sorted(set(e.get("caps", [])) | set(caps or []))
        self._save_trust(t)
        if self.events:
            self.events.publish("extension",
                                text="🔧 扩展已安装: %s%s%s" % (
                                    name, "(%s)" % description if description else "",
                                    (" ⚠️注意代码含高危模式" if warnings else "")))
        return {"ok": True, "status": "approved", "name": name, "caps": caps or [],
                "warnings": warnings, "backup": backup, "file": "ext_%s.py" % name}

    def approve(self, name, decision):
        name = self.safe_name(name)
        ppath = self._pending_path(name)
        if not os.path.exists(ppath):
            return {"ok": False, "error": "没有待审批的扩展 %s" % name}
        if not decision:
            os.remove(ppath)
            self.audit("rejected", name)
            if self.events:
                self.events.publish("extension", text="❌ 已拒绝扩展: %s" % name)
            return {"ok": True, "status": "rejected"}
        with open(ppath, encoding="utf-8") as f:
            code = f.read()
        meta = self._read_meta_from_code(code)
        try:
            r = self._finalize(name, code, meta.get("description", ""),
                               [], sorted(set(detect_caps(code))
                                          | set(str(c) for c in meta.get("capabilities", []))))
        except ValueError as e:
            self.audit("approve_failed", name, e)
            return {"ok": False, "error": str(e)}  # 待审批文件保留,可修复后重试
        os.remove(ppath)
        return r

    @staticmethod
    def _read_meta_from_code(code):
        try:
            tree = ast.parse(code)
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                        getattr(t, "id", None) == "META" for t in node.targets):
                    m = ast.literal_eval(node.value)
                    if isinstance(m, dict):
                        return m
        except Exception:
            pass
        return {}

    # ---------- 生成(LLM 自写扩展) ----------
    def generate(self, name, description, spec, llm, sample_args=None):
        user = ("扩展名: %s\n功能: %s\n详细要求: %s\n请输出完整代码。"
                % (name, description or name, spec or description or name))
        raw = llm.chat([{"role": "system", "content": GEN_SYS},
                        {"role": "user", "content": user}], temperature=0.2)
        raw_text = raw.content if hasattr(raw, "content") else str(raw)
        code = self._extract_code(raw_text)
        r = self.install(name, code, description)
        if r.get("status") == "approved" and sample_args is not None:
            r["test"] = self.call(name, sample_args)
        return r

    @staticmethod
    def _extract_code(raw):
        if "```" in raw:
            m = re.search(r"```(?:python|py)?\s*\n(.*?)```", raw, re.S)
            if m:
                return m.group(1).strip()
        return raw.strip()

    # ---------- 调用/删除 ----------
    def call(self, name, args=None, timeout=60, ai=False):
        """调用扩展。ai=True 表示主控AI发起:受信任等级门禁(用户手动调用不受限)。"""
        name = self.safe_name(name)
        path = self._path(name)
        if not os.path.exists(path):
            return {"ok": False, "error": "扩展 %s 不存在" % name}
        trust = self.trust(name)
        if ai and trust["level"] == 0:
            return {"ok": False,
                    "error": "扩展信任分不足(L0, %d分):AI 暂被禁止调用。"
                             "请在扩展页手动调用验证可靠后,信任分会自动恢复。" % trust["score"]}
        caps = self._caps_of(name)
        r = run_module(path, args or {}, timeout=timeout,
                       mem_mb=self.mem_mb, caps=caps, roots=self.fs_roots,
                       restricted=self.restricted)
        self._trust_update(name, bool(r.get("ok")), caps=caps)
        self._touch_usage(name, bool(r.get("ok")))
        if not r.get("ok"):
            self.audit("call_failed", name,
                       r.get("error") or r.get("stderr") or "")
        return r

    def delete(self, name):
        name = self.safe_name(name)
        path = self._path(name)
        if os.path.exists(path):
            os.remove(path)
            self.audit("deleted", name)
            return True
        return False
