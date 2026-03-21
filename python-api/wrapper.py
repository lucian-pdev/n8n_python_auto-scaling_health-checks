#!/usr/bin/env python3
"""
Wrapper script executed in subprocess (inside venv or system Python).
Receives code and data via stdin, executes safely, returns result via stdout.
"""

import json
import sys
import os
import traceback

def main():
    try:
        # Read configuration from stdin (avoids temp file injection issues)
        config = json.load(sys.stdin)
        
        code = config.get("code", "")
        data = config.get("data", {})
        job_id = config.get("job_id", "unknown")
        
        # Make data available to user script via global
        globals()["_n8n_data"] = data
        globals()["data"] = data  # Convenience alias
        
        # Execute user code in isolated namespace
        user_namespace = {
            "data": data,
            "_n8n_data": data,
            "__builtins__": __builtins__,
        }
        
        exec(code, user_namespace)
        
        # Extract result (user code must define 'result')
        result = user_namespace.get("result")
        
        # Output with markers for parsing
        output = {
            "job_id": job_id,
            "status": "success",
            "result": result
        }
        
        print("__RESULT_START__")
        print(json.dumps(output))
        print("__RESULT_END__")
        
        return 0
        
    except Exception as e:
        # Capture full traceback
        error_output = {
            "job_id": config.get("job_id", "unknown") if 'config' in dir() else "unknown",
            "status": "error",
            "error": str(e),
            "traceback": traceback.format_exc()
        }
        
        print("__RESULT_START__")
        print(json.dumps(error_output))
        print("__RESULT_END__")
        
        return 1


if __name__ == "__main__":
    sys.exit(main())