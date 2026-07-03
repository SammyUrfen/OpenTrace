"""Diff demo (regression). Same as workload_mem_v1.py, but every 8 MB buffer is
appended to a list instead of released — resident memory climbs (a leak).

Run v1, then v2, then compare: the diff viewer should show v2's RSS rising and a
monotonic-memory-growth anomaly that v1 doesn't have.
"""
import time

chunks = []
result = 0
for _ in range(14):
    chunk = bytearray(8 * 1024 * 1024)  # 8 MB
    result += len(chunk)
    chunks.append(chunk)                # <-- regression: kept, never released
    time.sleep(0.2)

print(f"v2 done — processed {result} bytes, holding {len(chunks)} buffers")
