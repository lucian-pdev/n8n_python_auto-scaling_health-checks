# python-api test: the script reports its own error -> fatal, one attempt
raise ScriptError(data.get("message", "test error from pyapi_raise"))
