# python-api test: the subprocess dies without an answer -> WorkerCrashed, retried, then RetriesExhausted
import os

os._exit(9)
