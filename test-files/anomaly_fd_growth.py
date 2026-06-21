import time

print("Starting FD growth anomaly test...")
fds = []
# We need to open more than 30 FDs and keep them open, over at least 8 ticks (2 seconds)
for i in range(40):
    f = open("/dev/null", "r")
    fds.append(f)
    time.sleep(0.1) # Total ~4 seconds

print(f"Finished opening {len(fds)} FDs.")
