"""扩展执行器(子进程入口):
python runner.py <模块路径> <json参数|-> [--no-run]
- 有 META + run(args) 的扩展 → 调用 run 并返回 {"ok":true,"result":...}
- 普通脚本(任务代码) → 直接执行顶层代码,stdout 作为结果
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agentcore import guard as _guard  # noqa: E402

_caps, _roots = _guard.caps_from_env()
_guard.install(_caps, _roots)  # 能力围栏:先于扩展代码安装

if os.environ.get("HEARTAGENT_RESTRICTED") == "1":  # 增强围栏:受限令牌降权(尽力而为)
    from agentcore import sandbox  # noqa: E402
    sandbox.drop_privileges()


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "缺少模块路径参数"}))
        sys.exit(2)
    path = sys.argv[1]
    args = {}
    if len(sys.argv) > 2 and sys.argv[2] != "-":
        try:
            args = json.loads(sys.argv[2])
        except Exception as e:
            print(json.dumps({"ok": False, "error": "参数 JSON 解析失败: %s" % e}))
            sys.exit(2)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    spec = importlib.util.spec_from_file_location("dyn_module", path)
    if spec is None or spec.loader is None:
        print(json.dumps({"ok": False, "error": "无法加载模块: %s" % path}))
        sys.exit(1)
    mod = importlib.util.module_from_spec(spec)

    buf = io.StringIO()
    result = None
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(mod)  # 顶层代码在此执行(普通脚本即完成工作)
            if hasattr(mod, "run"):
                result = mod.run(args)
    except Exception:
        sys.stdout.write(json.dumps(
            {"ok": False, "error": traceback.format_exc(limit=10)}, ensure_ascii=False))
        sys.exit(1)

    printed = buf.getvalue()
    out = {"ok": True, "result": result}
    if result is None and printed.strip():
        out["result"] = printed[-4000:]
    elif printed.strip():
        out["stdout"] = printed[-2000:]
    sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
