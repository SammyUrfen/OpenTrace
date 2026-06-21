import time

print("Starting CPU-bound loop anomaly test...")
# Rule requires > 90% CPU and < 50 syscalls/sec for >= 8 ticks (2 seconds)
start = time.time()
counter = 0

while time.time() - start < 3.0:
    counter += 1
    # pure compute, no syscalls except the time check (which is vDSO and fast)
    if counter % 1000000 == 0:
        pass

print("Finished CPU-bound loop.")
