# python-api test: holds data["mb"] MB long enough for the RSS watchdog to see it -> MemoryCapExceeded, retried
import time

block = b"x" * (int(data.get("mb", 1000)) * 1024 * 1024)  # written, so it is resident
time.sleep(float(data.get("hold", 10)))
result = len(block)
