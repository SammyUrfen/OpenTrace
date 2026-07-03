"""Diff demo (baseline). Allocates an 8 MB buffer each iteration and releases it
before the next — resident memory stays flat.

Compare with workload_mem_v2.py (which keeps every buffer) to see the diff
viewer show v2's higher peak RSS and a monotonic-memory-growth anomaly.
"""
import time

result = 0
for _ in range(14):
    chunk = bytearray(8 * 1024 * 1024)  # 8 MB
    result += len(chunk)
    chunk = None                        # released each iteration
    time.sleep(0.2)

print(f"v1 done — processed {result} bytes")
