"""RAG 知识摄入:本地文件/文件夹/网页 → 分块 → 嵌入向量 → SQLite(knowledge 表)。
嵌入服务不可用时自动退化为 LIKE 全文检索。
支持 .txt/.md/代码文件,以及简历常用的 .docx(zip+XML 解析)与 .pdf(尽力提取,
未压缩/Flate 流可解,扫描件与加密 PDF 不支持)。纯标准库。"""
import hashlib
import html as _html
import os
import re
import urllib.request
import zipfile
import zlib

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
MAX_FILES = 50
TEXT_EXTS = {".txt", ".md", ".py", ".json", ".csv", ".html", ".htm", ".log",
             ".yml", ".yaml", ".xml", ".js", ".ts", ".bat", ".sh", ".ini", ".cfg"}


def _strip_html(t):
    t = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = _html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _read_docx(path):
    """docx = zip 包:抽 word/document.xml 文本,段落转换行。"""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception:
        return ""
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return _html.unescape(xml).strip()


def _read_pdf(path):
    """尽力而为的 PDF 文本提取:解压 Flate 流,抽取 Tj/TJ 字面量字符串。
    扫描件、加密 PDF、内嵌字体子集编码会得到乱码或空 —— 调用方按空处理。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    pieces = []
    for m in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        data = m.group(1).strip(b"\r\n")
        try:
            data = zlib.decompress(data)
        except Exception:
            pass
        for t in re.findall(rb"\(((?:[^()\\]|\\.)*)\)\s*T[jJ*]", data):
            s = t.decode("latin-1", "replace")
            s = re.sub(r"\\([()\\])", r"\1", s)
            s = re.sub(r"\\(\d{1,3})", lambda x: chr(int(x.group(1), 8) & 0xFF), s)
            if s.strip():
                pieces.append(s)
    return " ".join(pieces)


def fetch_url_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "HeartAgent/3.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(2 * 1024 * 1024).decode("utf-8", "replace")
    if "<html" in raw[:500].lower() or "<body" in raw[:2000].lower():
        return _strip_html(raw)
    return raw


def _read_text_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return _read_docx(path)
    if ext == ".pdf":
        return _read_pdf(path)
    if ext not in TEXT_EXTS:
        return None
    try:
        if os.path.getsize(path) > 2 * 1024 * 1024:
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _chunk(text):
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    out, i = [], 0
    while i < len(text) and len(out) < 400:
        out.append(text[i:i + CHUNK_SIZE])
        i += CHUNK_SIZE - CHUNK_OVERLAP
    return out


def collect_sources(target):
    """返回 [(source标签, text)];target 可以是 URL、文件或文件夹。"""
    target = str(target).strip()
    if target.startswith("http://") or target.startswith("https://"):
        return [(target, fetch_url_text(target))]
    path = os.path.abspath(target)
    if not os.path.exists(path):
        raise ValueError("路径不存在: %s" % path)
    if os.path.isfile(path):
        return [(path, _read_text_file(path) or "")]
    files = []
    for root, _dirs, names in os.walk(path):
        for n in sorted(names):
            full = os.path.join(root, n)
            if _read_text_file(full) is not None:
                files.append(full)
            if len(files) >= MAX_FILES:
                break
        if len(files) >= MAX_FILES:
            break
    return [(f, _read_text_file(f) or "") for f in files]


def _content_hash(text):
    """源内容指纹:内容未变则跳过重复摄入(增量)。"""
    return hashlib.sha256((text or "")[:5000].encode("utf-8")).hexdigest()[:16]


def ingest(target, memory, events=None, tags=None, force=False):
    """摄入知识库;每个源按内容指纹做增量,未变化则跳过(force=True 强制重摄)。"""
    sources = collect_sources(target)
    total, done, skipped = 0, [], 0
    for src, text in sources:
        if not text or len(text.strip()) < 40:
            continue
        h = _content_hash(text)
        key = "kb:" + src
        if not force and memory.get_meta(key) == h:
            skipped += 1
            continue
        n = memory.add_knowledge_chunks(src, _chunk(text), tags=tags)
        memory.set_meta(key, h)
        total += n
        done.append("%s(%d块)" % (os.path.basename(src) if not src.startswith("http") else src[:60], n))
    if events and total:
        events.publish("insight", text="📚 知识摄入完成:新增 %d 块(跳过 %d 个未变化源)← %s"
                       % (total, skipped, "; ".join(done)[:200]))
    return {"ok": True, "chunks": total, "skipped": skipped,
            "sources": [s for s, _ in sources]}
