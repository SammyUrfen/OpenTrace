import time

print("Starting memory growth anomaly test...")
memory_hog = []

# Memory needs to grow monotonically by >50MB and >1.4x over >8 samples (2 seconds)
for i in range(12): # 12 iterations * 0.25s = 3.0s
    # Append ~5MB string per iteration
    memory_hog.append("A" * (5 * 1024 * 1024))
    time.sleep(0.25)

print(f"Finished memory growth test. Allocated {len(memory_hog) * 5} MB.")
