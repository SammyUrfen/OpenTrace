"""Diff demo (baseline). Reads a file ONCE and reuses the data in a loop.

Compare with workload_io_v2.py: the only change is that v2 re-opens the file on
every iteration — the diff viewer should show many more openat() syscalls and a
"repeated open" anomaly in v2.
"""
import time

path = "/tmp/ot_workload_data.txt"
with open(path, "w") as f:
    f.write("the quick brown fox\n" * 2000)

total = 0
with open(path) as f:           # opened once
    data = f.read()
for _ in range(30):
    total += data.count("fox")
    time.sleep(0.08)

print(f"v1 done — counted {total}")
