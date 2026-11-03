# requires: tabulate==0.9.0
# python-api test: tabulate is not in the base image, so python-api must build a venv and run this script in it
import sys

from tabulate import tabulate

rows = data.get("rows", [["a", 1], ["b", 2]])
result = {"python": sys.prefix, "table": tabulate(rows, headers=["name", "value"])}
