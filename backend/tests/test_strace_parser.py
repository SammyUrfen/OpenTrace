"""Unit tests for the strace parser, including edge cases that the regexes
must get right (errno, hex returns, unfinished/resumed merges, signals, exit)."""
from __future__ import annotations

from app.trace import strace_parser as sp
from app.trace.events import EXIT, SIGNAL, SYSCALL


def parse(*lines: str):
    return list(sp.parse_lines(lines))


def test_simple_openat_with_errno_and_path():
    line = (
        "971123 1782014232.330994 access(\"/etc/ld.so.preload\", R_OK) "
        "= -1 ENOENT (No such file or directory) <0.000020>"
    )
    (ev,) = parse(line)
    assert ev.event_type == SYSCALL
    assert ev.pid == 971123
    assert ev.syscall == "access"
    assert ev.retval == "-1"
    assert ev.error == "ENOENT"
    assert ev.path == "/etc/ld.so.preload"
    assert abs(ev.latency_ms - 0.020) < 1e-9
    # epoch seconds -> ms
    assert abs(ev.timestamp_ms - 1782014232330.994) < 1.0


def test_openat_success_path_extraction():
    line = (
        "12 1700000000.000001 openat(AT_FDCWD, \"/usr/lib/foo.so\", "
        "O_RDONLY|O_CLOEXEC) = 3 <0.000045>"
    )
    (ev,) = parse(line)
    assert ev.syscall == "openat"
    assert ev.retval == "3"
    assert ev.error is None
    assert ev.path == "/usr/lib/foo.so"


def test_hex_return_value():
    line = (
        "12 1700000000.000002 mmap(NULL, 8192, PROT_READ|PROT_WRITE, "
        "MAP_PRIVATE|MAP_ANONYMOUS, -1, 0) = 0x7f657b4c0000 <0.000022>"
    )
    (ev,) = parse(line)
    assert ev.syscall == "mmap"
    assert ev.retval == "0x7f657b4c0000"
    assert ev.error is None


def test_read_fd_extraction():
    line = "12 1700000000.000003 read(3, \"hello\", 4096) = 5 <0.000010>"
    (ev,) = parse(line)
    assert ev.syscall == "read"
    assert ev.fd == 3
    assert ev.retval == "5"


def test_signal_line():
    line = "12 1700000000.5 --- SIGSEGV {si_signo=SIGSEGV, si_code=SEGV_MAPERR} ---"
    (ev,) = parse(line)
    assert ev.event_type == SIGNAL
    assert ev.syscall == "SIGSEGV"


def test_exit_line():
    (ev,) = parse("12 1700000000.6 +++ exited with 0 +++")
    assert ev.event_type == EXIT
    assert ev.retval == "0"


def test_killed_by_signal():
    (ev,) = parse("12 1700000000.7 +++ killed by SIGKILL +++")
    assert ev.event_type == EXIT
    assert ev.error == "SIGKILL"


def test_unfinished_resumed_merge():
    lines = [
        "100 1700000000.000000 read(3,  <unfinished ...>",
        "100 1700000000.001000 <... read resumed> \"data\", 4096) = 100 <0.000500>",
    ]
    (ev,) = parse(*lines)
    assert ev.syscall == "read"
    assert ev.retval == "100"
    assert ev.fd == 3
    # event time is the START (unfinished) timestamp
    assert abs(ev.timestamp_ms - 1700000000000.0) < 1.0
    # latency comes from the resumed line's <...>
    assert abs(ev.latency_ms - 0.5) < 1e-9


def test_interleaved_unfinished_across_pids():
    lines = [
        "100 1700000000.000000 read(3,  <unfinished ...>",
        "200 1700000000.000100 write(4, \"x\", 1) = 1 <0.000005>",
        "100 1700000000.001000 <... read resumed> \"data\", 64) = 4 <0.000200>",
    ]
    evs = parse(*lines)
    assert len(evs) == 2
    syscalls = {e.pid: e.syscall for e in evs}
    assert syscalls == {100: "read", 200: "write"}


def test_garbage_line_is_skipped():
    assert parse("not a real strace line") == []
    assert parse("") == []


def test_no_pid_prefix_defaults_to_zero():
    (ev,) = parse("1700000000.0 brk(NULL) = 0x55990d11d000 <0.000015>")
    assert ev.pid == 0
    assert ev.syscall == "brk"


def test_real_strace_log_smoke(tmp_path):
    """Parse a freshly captured real strace log if present (best-effort)."""
    import subprocess
    import shutil

    if not shutil.which("strace"):
        return
    log = tmp_path / "s.log"
    subprocess.run(
        ["strace", "-f", "-T", "-ttt", "-o", str(log), "--",
         "python3", "-c", "import os; fd=os.open('/etc/hostname', os.O_RDONLY); os.close(fd)"],
        capture_output=True,
    )
    events = list(sp.parse_file(log))
    assert len(events) > 10
    assert any(e.syscall == "openat" and e.path == "/etc/hostname" for e in events)
    assert any(e.event_type == EXIT for e in events)
    # every parsed event has a positive epoch-ms timestamp
    assert all(e.timestamp_ms > 1_000_000_000_000 for e in events)
