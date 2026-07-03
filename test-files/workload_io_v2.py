"""Diff demo (regression). Same as workload_io_v1.py, but the file open was
moved INSIDE the loop — so it re-opens the file every iteration.

Run v1, then v2, then right-click v1 → "Compare with…" → v2 to see the diff
flag the extra openat() syscalls + the repeated-open anomaly.
"""
import time

path = "/tmp/ot_workload_data.txt"
with open(path, "w") as f:
    f.write("the quick brown fox\n" * 2000)

total = 0
for _ in range(30):
    with open(path) as f:       # <-- regression: re-opened every iteration
        data = f.read()
    total += data.count("fox")
    time.sleep(0.08)

print(f"v2 done — counted {total}")
