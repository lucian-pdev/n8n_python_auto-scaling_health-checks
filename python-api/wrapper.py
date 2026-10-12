#!/usr/bin/env python3
"""
Wrapper script executed in subprocess (inside venv or system Python).
Receives code and data via stdin, executes safely, returns result via stdout.

Error reporting (generic, every script gets it):
- ScriptError("msg")      -> error = "msg"  (script's own message, shown as-is in n8n)
- any other exception     -> error = "<Type>: <msg> (line N in <func>)", located in the script
- no `result` set         -> error = "script did not set `result`"
- result not JSON-able    -> error = "result is not JSON-serializable: ..."
Plus error_type and traceback (script frames only, with source lines).
"""

import json
import linecache
import sys
import traceback


class ScriptError(Exception):
    """Raise in a script for an expected failure; the message goes to n8n unchanged."""


def _emit(output: dict) -> None:
    body = json.dumps(output)                                       # serialize first: a failure prints nothing
    print("__RESULT_START__")
    print(body)
    print("__RESULT_END__")


def _describe(e: BaseException, script_name: str) -> dict:
    """Error fields for the response, pointing at the script, not at this wrapper."""
    tb = e.__traceback__.tb_next if e.__traceback__ else None      # drop the exec() frame
    frames = traceback.extract_tb(tb)
    in_script = [f for f in frames if f.filename == script_name]
    if type(e).__name__ == "ScriptError":                           # also matches a script's own ScriptError class
        error = str(e)
    else:
        where = in_script[-1] if in_script else (frames[-1] if frames else None)
        error = f"{type(e).__name__}: {e}" + (f" (line {where.lineno} in {where.name})" if where else "")
    return {
        "status": "error",
        "error": error,
        "error_type": type(e).__name__,
        "traceback": "".join(traceback.format_exception(type(e), e, tb)),
    }


def main():
    job_id, script_name = "unknown", "script.py"
    try:
        # Read configuration from stdin (avoids temp file injection issues)
        config = json.load(sys.stdin)
        code = config.get("code", "")
        data = config.get("data", {})
        job_id = config.get("job_id", "unknown")
        script_name = config.get("script_name") or "script.py"

        # Register the source so tracebacks show "File "<script>", line N" + the code line
        linecache.cache[script_name] = (len(code), None, code.splitlines(True), script_name)

        # Execute user code in isolated namespace
        user_namespace = {
            "data": data,
            "_n8n_data": data,                                      # backward compatibility
            "ScriptError": ScriptError,
            "__builtins__": __builtins__,
        }
        exec(compile(code, script_name, "exec"), user_namespace)
    except Exception as e:
        _emit({"job_id": job_id, **_describe(e, script_name)})
        return 1

    if "result" not in user_namespace:
        _emit({"job_id": job_id, "status": "error", "error_type": "MissingResult",
               "error": "script did not set `result`", "traceback": None})
        return 1

    try:
        _emit({"job_id": job_id, "status": "success", "result": user_namespace["result"]})
    except (TypeError, ValueError) as e:
        _emit({"job_id": job_id, "status": "error", "error_type": "ResultNotSerializable",
               "error": f"result is not JSON-serializable: {e}", "traceback": None})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
