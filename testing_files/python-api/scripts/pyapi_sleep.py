# python-api test: runs for data["seconds"] -> longer than the request budget means JobTimeout, then RetriesExhausted
import time

time.sleep(float(data.get("seconds", 400)))
result = {"slept": data.get("seconds", 400)}
