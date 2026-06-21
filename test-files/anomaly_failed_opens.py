import os

print("Starting failed opens anomaly test...")

# Rule requires > 5 failed opens with ENOENT/EACCES on non-library paths
for i in range(20):
    path = f"/tmp/app_missing_config_{i}.json"
    try:
        with open(path, "r") as f:
            pass
    except FileNotFoundError:
        pass # Expected

print("Finished failed opens.")
