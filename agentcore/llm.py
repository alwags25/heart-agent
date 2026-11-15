"""LLM 抽象层 v2:原生 Function Calling + 流式输出 + 用量统计 + 环境变量密钥覆盖。
- OpenAICompatLLM:/chat/completions,tools 参数 + SSE 流式(tool_calls 分片重组)
- OllamaLLM:/api/chat,tools + NDJSON 流式
- MockLLM:离线演示,内置 time_now / remember / write_extension 脚本化工具调用,可演示完整 FC 链路与审批流
- ProxyLLM:热重载代理,包住当前实例并统计 token 用量
纯标准库实现。"""
import json
import os
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class ToolUnsupported(LLMError):
    """服务端不支持 tools 时抛出,调用方应回退到提示词协议"""


class ChatResult:
    __slots__ = ("content", "tool_calls", "usage")

    def __init__(self, content="", tool_calls=None, usage=None):
        self.content = content or ""
        self.tool_calls = tool_calls or []  # [{id,name,args}]
        self.usage = usage or {}


def _openai_tools(tools):
    """兼容两种工具定义格式:字典 {name,description,parameters} 或 (name,description,parameters) 元组"""
    out = []
    for t in (tools or []):
        if isinstance(t, dict):
            out.append({"type": "function",
                        "function": {"name": t.get("name", ""),
                                     "description": t.get("description", ""),
                                     "parameters": t.get("parameters") or
                                                   {"type": "object", "properties": {}}}})
        else:  # (name, description, parameters) 元组
            params = t[2] if len(t) > 2 and t[2] else {"type": "object", "properties": {}}
            out.append({"type": "function",
                        "function": {"name": t[0], "description": t[1],
                                     "parameters": params}})
    return out


def _parse_tool_calls(raw):
    calls = []
    for tc in raw or []:
        try:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                args = json.loads(args) if args.strip() else {}
            calls.append({"id": tc.get("id") or "call_%d" % len(calls),
                          "name": fn.get("name", ""), "args": args or {}})
        except Exception:
            continue
    return calls


class BaseLLM:
    is_mock = False
    supports_tools = True

    def chat(self, messages, tools=None, temperature=0.7, on_delta=None, on_reason=None):
        """on_delta(text):正文增量;on_reason(text):推理模型思考增量(节流由调用方负责)。"""
        raise NotImplementedError


class OpenAICompatLLM(BaseLLM):
    def __init__(self, base_url, api_key, model, timeout=120, label=None):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        self.label = label or model

    def _request(self, body, stream):
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.api_key},
            method="POST")
        return urllib.request.urlopen(req, timeout=self.timeout)

    def chat(self, messages, tools=None, temperature=0.7, on_delta=None, on_reason=None):
        body = {"model": self.model, "messages": messages, "temperature": temperature,
                "stream": bool(on_delta)}
        if tools:
            body["tools"] = _openai_tools(tools)
        if on_delta:
            body["stream_options"] = {"include_usage": True}
        # 降级链:stream+usage → stream(无 usage)→ 非流式;连接阶段 429/5xx 自动重试
        for drop in (None, "stream_options", "stream_and_tools"):
            b = dict(body)
            if drop == "stream_options":
                b.pop("stream_options", None)
            elif drop == "stream_and_tools":
                b.pop("stream", None)
                b.pop("stream_options", None)
            streaming = bool(b.get("stream"))
            try:
                resp = self._open_with_retry(b, streaming,
                                             allow_stream_fallback=drop is not None)
            except ToolUnsupported:
                raise
            except LLMError as e:
                if drop is not None and "stream" in str(e).lower():
                    continue  # 服务端拒绝流式 → 降级重试
                raise
            try:
                with resp:
                    if streaming:
                        return self._read_stream(resp, on_delta, on_reason)
                    payload = json.loads(resp.read().decode("utf-8"))
            except ToolUnsupported:
                raise
            except LLMError as e:
                if drop is not None and "stream" in str(e).lower():
                    continue
                raise
            except Exception as e:
                raise LLMError("读取模型响应失败(%s): %s" % (type(e).__name__, e))
            try:
                msg = payload["choices"][0]["message"]
                return ChatResult(msg.get("content") or "",
                                  _parse_tool_calls(msg.get("tool_calls")),
                                  payload.get("usage") or {})
            except Exception:
                raise LLMError("模型返回格式异常: " + json.dumps(payload, ensure_ascii=False)[:300])
        raise LLMError("模型调用失败")

    def _open_with_retry(self, b, streaming, allow_stream_fallback):
        """打开连接(连接阶段失败可重试;429/5xx 退避重试;流式被拒可降级)。"""
        last = None
        for attempt in range(3):
            try:
                return self._request(b, streaming)
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                low = detail.lower()
                if any(k in low for k in ("tool", "function call", "not support",
                                          "unsupported")):
                    raise ToolUnsupported("服务端拒绝 tools 参数: %s" % detail)
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep([2, 5][attempt])
                    last = LLMError("HTTP %s" % e.code)
                    continue
                if streaming and allow_stream_fallback and "stream" in low:
                    raise LLMError("服务端拒绝 stream: %s" % detail)
                raise LLMError("模型 API 返回 HTTP %s: %s" % (e.code, detail))
            except Exception as e:
                if attempt < 2:
                    time.sleep([2, 5][attempt])
                    last = e
                    continue
                raise LLMError("调用模型失败(%s): %s" % (type(e).__name__, e))
        raise LLMError("调用失败: %s" % last)

    def _read_stream(self, resp, on_delta, on_reason=None):
        content, usage = [], {}
        frags = {}  # index -> {id, name, args}
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            usage.update(chunk.get("usage") or {})
            for ch in chunk.get("choices", []):
                delta = ch.get("delta") or {}
                text = delta.get("content")
                if text:
                    content.append(text)
                    try:
                        on_delta(text)
                    except Exception:
                        pass
                reason = delta.get("reasoning_content") or delta.get("reasoning")
                if reason and on_reason:
                    try:
                        on_reason(reason)
                    except Exception:
                        pass
                for tc in delta.get("tool_calls") or []:
                    f = frags.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    if tc.get("id"):
                        f["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        f["name"] += fn["name"]
                    if fn.get("arguments"):
                        f["args"] += fn["arguments"]
        calls = []
        for i in sorted(frags):
            f = frags[i]
            try:
                args = json.loads(f["args"]) if f["args"].strip() else {}
            except Exception:
                args = {"_raw": f["args"][:300]}
            calls.append({"id": f["id"] or "call_%d" % i, "name": f["name"], "args": args})
        return ChatResult("".join(content), calls, usage)


def _ollama_messages(messages):
    """OpenAI 风格消息 → Ollama 格式:图像从 content 列表抽出放 images 字段。"""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            text = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
            imgs = []
            for p in c:
                if p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        imgs.append(url.split(",", 1)[-1])
            nm = {"role": m.get("role", "user"), "content": text}
            if imgs:
                nm["images"] = imgs
            out.append(nm)
        else:
            out.append(m)
    return out


class OllamaLLM(BaseLLM):
    def __init__(self, base_url, model, timeout=180, label=None):
        self.base_url = (base_url or "http://localhost:11434").rstrip("/")
        self.model = model
        self.timeout = timeout
        self.label = label or model

    def chat(self, messages, tools=None, temperature=0.7, on_delta=None, on_reason=None):
        messages = _ollama_messages(messages)
        body = {"model": self.model, "messages": messages, "stream": bool(on_delta),
                "options": {"temperature": temperature}}
        if tools:
            body["tools"] = _openai_tools(tools)
        req = urllib.request.Request(
            self.base_url + "/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        content, calls, usage = [], [], {}
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if on_delta:
                    for raw in resp:
                        try:
                            chunk = json.loads(raw.decode("utf-8", "replace"))
                        except Exception:
                            continue
                        msg = chunk.get("message") or {}
                        text = msg.get("content") or ""
                        if text:
                            content.append(text)
                            try:
                                on_delta(text)
                            except Exception:
                                pass
                        calls += _parse_tool_calls_ollama(msg.get("tool_calls"))
                        if chunk.get("done"):
                            usage = {"prompt_tokens": chunk.get("prompt_eval_count", 0),
                                     "completion_tokens": chunk.get("eval_count", 0)}
                else:
                    data = json.loads(resp.read().decode("utf-8"))
                    msg = data.get("message") or {}
                    content.append(msg.get("content") or "")
                    calls = _parse_tool_calls_ollama(msg.get("tool_calls"))
                    usage = {"prompt_tokens": data.get("prompt_eval_count", 0),
                             "completion_tokens": data.get("eval_count", 0)}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if tools and "tool" in detail.lower():
                raise ToolUnsupported("Ollama 拒绝 tools 参数: %s" % detail)
            raise LLMError("调用 Ollama 返回 HTTP %s: %s" % (e.code, detail))
        except Exception as e:
            raise LLMError("调用 Ollama 失败(确认已运行 ollama serve): %s" % e)
        return ChatResult("".join(content), calls, usage)


def _parse_tool_calls_ollama(raw):
    calls = []
    for tc in raw or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                args = {}
        calls.append({"id": tc.get("id") or "call_%d" % len(calls),
                      "name": fn.get("name", ""), "args": args or {}})
    return calls


class MockLLM(BaseLLM):
    """离线模式:演示原生 FC、流式输出与审批流。"""
    is_mock = True
    supports_tools = True

    def chat(self, messages, tools=None, temperature=0.7, on_delta=None, on_reason=None):
        last_user = ""
        tool_results = []
        is_engineer = False
        for m in messages:
            if m.get("role") == "user":
                last_user = str(m.get("content", ""))
            elif m.get("role") == "tool":
                tool_results.append(str(m.get("content", "")))
            elif m.get("role") == "system" and "扩展工程师" in str(m.get("content", "")):
                is_engineer = True
        if is_engineer:
            name = "greet"
            for part in last_user.split():
                if part.startswith("扩展名:"):
                    name = part[4:].strip() or "greet"
            code = ('META = {"name": "' + name + '", "description": "生成一句问候"}\n\n'
                    'def run(args):\n'
                    '    who = (args or {}).get("name") or "朋友"\n'
                    '    return {"ok": True, "message": "你好, " + who + "!"}\n')
            return ChatResult(code)
        # 睡眠整理的矛盾判定 → 返回固定冲突,演示完整链路
        if "矛盾" in last_user and "JSON" in last_user:
            return ChatResult(json.dumps(
                {"conflict": True,
                 "question": "这两条记忆不一致,请确认哪条是最新情况?"},
                ensure_ascii=False))
        # 第二意见(安全审查)→ 放行
        if "审查" in last_user and "JSON" in last_user:
            return ChatResult(json.dumps(
                {"verdict": "approve", "reason": "mock 审查:未发现风险"}, ensure_ascii=False))
        # 睡眠整理的三元组抽取 → 返回示例图谱
        if "三元组" in last_user and "JSON" in last_user:
            return ChatResult(json.dumps(
                [{"s": "用户", "r": "主力语言", "o": "Python"},
                 {"s": "用户", "r": "正在开发", "o": "HeartAgent"}], ensure_ascii=False))
        if tool_results:
            text = ("(mock)工具执行完毕:%s。基于以上结果给出回答。"
                    % tool_results[-1][:120].replace("\n", " "))
        elif tools and "任务" in last_user and "删除" in last_user:
            return ChatResult(tool_calls=[{"id": "c1", "name": "delete_task",
                                           "args": {"id": "nonexistent"}}])
        elif tools and ("屏幕" in last_user or "截图" in last_user):
            return ChatResult(tool_calls=[{"id": "c1", "name": "take_screenshot",
                                           "args": {}}])
        elif tools and "任务" in last_user and ("创建" in last_user or "建一个" in last_user):
            return ChatResult(tool_calls=[{"id": "c1", "name": "add_task",
                                           "args": {"name": "定时打印时间",
                                                    "schedule": {"kind": "loop",
                                                                 "interval_sec": 60},
                                                    "action": {"kind": "code",
                                                               "code": "import time; print(time.strftime('%H:%M:%S'))"}}}])
        elif tools and ("几点" in last_user or "时间" in last_user or "今天" in last_user):
            return ChatResult(tool_calls=[{"id": "c1", "name": "call_extension",
                                           "args": {"name": "time_now", "args": {}}}])
        elif tools and "记住" in last_user:
            return ChatResult(tool_calls=[{"id": "c1", "name": "remember",
                                           "args": {"content": last_user.split("记住", 1)[-1].strip()
                                                    or last_user}}])
        elif tools and "扩展" in last_user and ("写" in last_user or "安装" in last_user):
            return ChatResult(tool_calls=[{"id": "c1", "name": "write_extension",
                                           "args": {"name": "greet",
                                                    "description": "生成一句问候",
                                                    "spec": "参数 name 可选;返回 {ok, message}"}}])
        else:
            text = ("(mock 模式)已收到:「%s」。请编辑 config.json 填入 API Key 或切换 ollama 后重启,"
                    "我将具备真实的工具调用、扩展编写与记忆管理能力。" % last_user[:60])
        if on_delta:
            for i in range(0, len(text), 6):
                on_delta(text[i:i + 6])
                time.sleep(0.02)
        return ChatResult(text)


class OllamaEmbedder:
    """本地向量嵌入(Ollama /api/embed),失败时静默返回 None 由调用方退化。"""

    def __init__(self, base_url, model, timeout=30):
        self.base_url = (base_url or "http://localhost:11434").rstrip("/")
        self.model = model or "nomic-embed-text"
        self.timeout = timeout
        self._cache = {}

    def embed(self, text):
        key = (text or "")[:500]
        if key in self._cache:
            return self._cache[key]
        req = urllib.request.Request(
            self.base_url + "/api/embed",
            data=json.dumps({"model": self.model, "input": key}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vec = data["embeddings"][0]
        if len(self._cache) > 2000:
            self._cache.clear()
        self._cache[key] = vec
        return vec


class ProxyLLM:
    """热重载代理:包住当前 LLM 实例;swap() 切换底层模型,统计 token 用量。"""

    def __init__(self, inner, label, on_usage=None):
        self.inner = inner
        self.label = label
        self.on_usage = on_usage

    def swap(self, inner, label=None):
        self.inner = inner
        if label:
            self.label = label

    def chat(self, messages, tools=None, temperature=0.7, on_delta=None, on_reason=None):
        res = self.inner.chat(messages, tools=tools, temperature=temperature,
                              on_delta=on_delta, on_reason=on_reason)
        if self.on_usage and res.usage:
            try:
                self.on_usage(self.label, res.usage)
            except Exception:
                pass
        return res

    @property
    def is_mock(self):
        return getattr(self.inner, "is_mock", False)

    @property
    def supports_tools(self):
        return getattr(self.inner, "supports_tools", True)

    @supports_tools.setter
    def supports_tools(self, v):
        try:
            self.inner.supports_tools = v
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self.inner, name)


def make_llm(cfg, env_prefix=None):
    cfg = cfg or {}
    if env_prefix:  # 环境变量覆盖 API Key,避免明文落盘
        env_key = "HEARTAGENT_%s_API_KEY" % env_prefix.upper()
        env_url = "HEARTAGENT_%s_BASE_URL" % env_prefix.upper()
        if os.environ.get(env_key):
            cfg = dict(cfg)
            cfg["api_key"] = os.environ[env_key]
            if os.environ.get(env_url):
                cfg["base_url"] = os.environ[env_url]
    provider = str(cfg.get("provider", "mock")).lower()
    if provider in ("openai", "openai_compatible", "compatible", "zhipu", "glm", "deepseek"):
        if not cfg.get("api_key"):
            m = MockLLM()
            m.mock_reason = "api_key 未配置"
            return m
        return OpenAICompatLLM(
            cfg.get("base_url", "https://api.openai.com/v1"),
            cfg["api_key"], cfg.get("model", "gpt-4o-mini"),
            cfg.get("timeout_sec", 120),
            label="%s(%s)" % (cfg.get("model", "?"), provider))
    if provider == "ollama":
        return OllamaLLM(cfg.get("base_url", "http://localhost:11434"),
                         cfg.get("model", "qwen2.5:7b"), cfg.get("timeout_sec", 180),
                         label="%s(ollama)" % cfg.get("model", "?"))
    m = MockLLM()
    m.mock_reason = "provider=mock"
    return m


def model_label(cfg, env_prefix=None):
    cfg2 = dict(cfg or {})
    if env_prefix and os.environ.get("HEARTAGENT_%s_API_KEY" % env_prefix.upper()):
        cfg2["api_key"] = "env"
    llm = make_llm(cfg2)
    label = getattr(llm, "label", "%s(%s)" % (cfg2.get("model", "?"), cfg2.get("provider", "?")))
    if llm.is_mock:
        label += " [mock]"
    return label
