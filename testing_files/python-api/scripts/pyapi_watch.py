# requires: tabulate==0.9.0
# python-api watch: the hourly request from the 48h watch (watch.py tick), random data in, summary out, in a venv
import statistics
import sys

from tabulate import tabulate

rows = data["rows"]                     # [{"name": str, "value": float, "group": str}, ...]
groups: dict[str, list[float]] = {}
for row in rows:
    groups.setdefault(row["group"], []).append(float(row["value"]))
summary = [[g, len(v), round(statistics.fmean(v), 3), round(max(v), 3)] for g, v in sorted(groups.items())]

result = {
    "tick": data.get("tick"),
    "rows": len(rows),
    "groups": len(groups),
    "python": sys.prefix,
    "table": tabulate(summary, headers=["group", "n", "mean", "max"]),
}
