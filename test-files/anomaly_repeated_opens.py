import os
import time

print("Starting repeated open anomaly test...")

path = "/tmp/dummy_file_for_repeated_open.txt"
with open(path, "w") as f:
    f.write("test content")

# Rule requires > 10 opens on the same file to trigger high severity
for _ in range(50):
    with open(path, "r") as f:
        data = f.read()
    time.sleep(0.01) # Sleep to spread out the events

print("Finished repeated opens.")
