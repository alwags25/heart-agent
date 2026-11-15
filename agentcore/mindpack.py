"""心智包(Mind Pack):把整个"心智"打包成可分享的 zip ——
记忆 + 扩展 + 任务 + 目标 + 提示词附录 + 配置(密钥脱敏),带 manifest 完整性清单。
导入采用安全合并:记忆走去重合并,扩展一律进待审批队列。"""
import hashlib
import json
import os
import time
import zipfile

MANIFEST = "manifest.json"


def _sha(data):
    return hashlib.sha256(data).hexdigest()[:16]


def export_pack(base_dir, data_dir, cfg, memory=None):
    """返回 (zip字节, manifest)。密钥脱敏;记忆以 facts.json 形式携带(导入零句柄问题)。"""
    safe_cfg = json.loads(json.dumps(cfg))
    for key in ("main_model", "assist_model"):
        if isinstance(safe_cfg.get(key), dict):
            safe_cfg[key]["api_key"] = ""
    files = {}
    buf_time = time.strftime("%Y-%m-%d %H:%M:%S")

    def add(zpath, full):
        try:
            with open(full, "rb") as f:
                data = f.read()
            files[zpath] = data
        except OSError:
            pass

    if memory is not None:
        files["facts.json"] = json.dumps(
            memory.facts(include_archived=True), ensure_ascii=False, indent=1).encode("utf-8")
    for fn in ("tasks.json", "goals.json", "skills_log.json",
               "prompt_appendix.txt", "ext_trust.json", "eval_cases.json"):
        p = os.path.join(data_dir, fn)
        if os.path.exists(p):
            add(fn, p)
    ext_dir = os.path.join(base_dir, "extensions")
    if os.path.isdir(ext_dir):
        for fn in sorted(os.listdir(ext_dir)):
            if fn.startswith("ext_") and fn.endswith(".py"):
                add("extensions/" + fn, os.path.join(ext_dir, fn))
    manifest = {
        "pack": "heartagent-mind", "format": 1,
        "exported_at": buf_time,
        "counts": {"files": len(files)},
        "integrity": {name: _sha(data) for name, data in files.items()},
        "config_redacted": safe_cfg,
    }
    out = io_zip(files, manifest)
    return out, manifest


def io_zip(files, manifest):
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
        z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1))
    return buf.getvalue()


def read_manifest(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return json.loads(z.read(MANIFEST).decode("utf-8"))


def import_pack(zip_path, memory, extensions, tasks, goals, events, data_dir):
    """安全合并导入:记忆去重合并;扩展进待审批;任务/目标重新分配 id。返回摘要。"""
    manifest = read_manifest(zip_path)
    if manifest.get("pack") != "heartagent-mind":
        raise ValueError("不是有效的 HeartAgent 心智包")
    summary = {"facts_added": 0, "extensions_pending": 0, "tasks_added": 0,
               "goals_added": 0, "appendix": False, "archived_facts": 0}
    tmp_ext = os.path.join(data_dir, "_import_ext")
    os.makedirs(tmp_ext, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        # 1) 记忆:facts.json → 逐条 add_fact(自动去重合并)
        if "facts.json" in names:
            try:
                for f in json.loads(z.read("facts.json")):
                    if not f.get("content") or f.get("archived"):
                        continue
                    r = memory.add_fact(f["content"], tags=f.get("tags") or [],
                                        source=(f.get("source") or "导入") + "·包")
                    summary["facts_added"] += 0 if r.get("merged") else 1
            except Exception:
                pass
        # 2) 扩展 → 一律进待审批(不直接执行外来代码)
        for n in names:
            if n.startswith("extensions/ext_") and n.endswith(".py"):
                code = z.read(n).decode("utf-8", "replace")
                name = os.path.basename(n)[4:-3]
                try:
                    extensions.install(name, code, "来自心智包导入", approve=False)
                    summary["extensions_pending"] += 1
                except ValueError:
                    pass
        # 3) 任务/目标(重新分配 id,默认停用;按名称/标题去重避免重复导入累积)
        if "tasks.json" in names:
            try:
                existing = {t["name"] for t in tasks.tasks}
                for t in json.loads(z.read("tasks.json")):
                    if t.get("name") in existing:
                        continue
                    t["id"] = "t%d" % int(time.time() * 1000)
                    t["enabled"] = False
                    t["history"] = []
                    tasks.tasks.append(t)
                    existing.add(t["name"])
                    summary["tasks_added"] += 1
                    time.sleep(0.002)
                tasks._save()
            except Exception:
                pass
        if "goals.json" in names:
            try:
                existing = {g["title"] for g in goals.goals}
                for g in json.loads(z.read("goals.json")):
                    if g.get("title") in existing:
                        continue
                    g["id"] = "g%d" % int(time.time() * 1000)
                    g["status"] = "paused"
                    goals.goals.append(g)
                    existing.add(g["title"])
                    summary["goals_added"] += 1
                    time.sleep(0.002)
                goals._save()
            except Exception:
                pass
        # 4) 提示词附录(可直接看/重置)
        if "prompt_appendix.txt" in names:
            text = z.read("prompt_appendix.txt").decode("utf-8", "replace").strip()
            if text:
                try:
                    evolver_appendix = os.path.join(data_dir, "prompt_appendix.txt")
                    with open(evolver_appendix, "w", encoding="utf-8") as f:
                        f.write(text)
                    summary["appendix"] = True
                except OSError:
                    pass
    shutil_rmtree(tmp_ext)
    if events:
        events.publish("insight",
                       text="🧠 心智包导入完成:记忆+%d(去重后)/ 扩展待审批 %d / 任务 %d / 目标 %d%s"
                            % (summary["facts_added"], summary["extensions_pending"],
                               summary["tasks_added"], summary["goals_added"],
                               "(含提示词附录)" if summary["appendix"] else ""))
    return {"ok": True, "manifest_version": manifest.get("format"), **summary}


def shutil_rmtree(path):
    import shutil
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
