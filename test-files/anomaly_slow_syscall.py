import socket
import time

print("Starting slow syscall anomaly test...")
# Rule requires a non-blocking syscall (like connect) taking > 1.0s.
# sleep, poll, select, etc. are in _BLOCKING_SYSCALLS and won't trigger it.
# We connect to a black-hole IP to force a slow `connect()` syscall.

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2.5) # Force the connect syscall to take 2.5s before throwing

print("Attempting to connect to 192.0.2.1 (TEST-NET-1, drops packets)...")
try:
    s.connect(("192.0.2.1", 12345))
except Exception as e:
    print(f"Connection failed as expected: {e}")

print("Finished slow syscall.")
