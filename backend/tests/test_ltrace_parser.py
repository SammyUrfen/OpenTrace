"""Unit tests for the ltrace parser: library-call vs @SYS-syscall mapping,
unfinished/resumed merges, <void>/hex returns, signals and exit lines. Line
shapes are taken verbatim from real `ltrace -S -f -ttt -T -o` output."""
from __future__ import annotations

from app.trace import ltrace_parser as lp
from app.trace.events import EXIT, LIBCALL, SIGNAL, SYSCALL


def parse(*lines: str):
    return list(lp.parse_lines(lines))


def test_library_call_malloc():
    (ev,) = parse("734474 1782133300.300000 malloc(65536)          = 0x3640b010 <0.000101>")
    assert ev.event_type == LIBCALL
    assert ev.source == "ltrace"
    assert ev.pid == 734474
    assert ev.syscall == "malloc"
    assert ev.args == "65536"
    assert ev.retval == "0x3640b010"
    assert abs(ev.latency_ms - 0.101) < 1e-9


def test_syscall_at_sys_suffix_stripped_and_typed():
    (ev,) = parse('734474 1782133300.268194 openat@SYS(AT_FDCWD, "/etc/ld.so.cache", 0x80000, 00) = 3 <0.000120>')
    assert ev.event_type == SYSCALL  # @SYS -> a syscall event
    assert ev.syscall == "openat"    # suffix stripped
    assert ev.path == "/etc/ld.so.cache"  # enrichment still works
    assert ev.retval == "3"


def test_free_returns_void():
    (ev,) = parse("734474 1782133300.310000 free(0x3640b010)        = <void> <0.000089>")
    assert ev.event_type == LIBCALL
    assert ev.syscall == "free"
    assert ev.args == "0x3640b010"
    assert ev.retval == "<void>"


def test_unfinished_resumed_merge_keeps_start_ts():
    evs = parse(
        "734474 1782133300.642578 malloc(65536 <unfinished ...>",
        "734474 1782133300.643612 brk@SYS(nil) = 0xec59000 <0.000040>",
        "734474 1782133300.643738 <... malloc resumed> ) = 0x3640b010 <0.000432>",
    )
    # the brk syscall in the middle + the resumed malloc => 2 events
    kinds = [(e.event_type, e.syscall) for e in evs]
    assert (SYSCALL, "brk") in kinds
    malloc_ev = next(e for e in evs if e.syscall == "malloc")
    assert malloc_ev.event_type == LIBCALL
    assert malloc_ev.retval == "0x3640b010"
    # timestamp is the START of the call, not the resume line
    assert abs(malloc_ev.timestamp_ms - 1782133300642.578) < 1.0
    assert abs(malloc_ev.latency_ms - 0.432) < 1e-9


def test_signal_and_exit_lines():
    sig, ex = parse(
        "734474 1782133300.700000 --- SIGCHLD (Child exited) ---",
        "734474 1782133300.800000 +++ exited (status 0) +++",
    )
    assert sig.event_type == SIGNAL and sig.syscall == "SIGCHLD"
    assert ex.event_type == EXIT and ex.retval == "0"


def test_killed_by_signal_exit():
    (ev,) = parse("9 1782133300.900000 +++ killed by SIGSEGV +++")
    assert ev.event_type == EXIT
    assert ev.error == "SIGSEGV"


def test_syscall_errno_description_stripped():
    # A failing syscall with an errno + parenthetical description must not leak
    # the description into args, and must capture retval/error cleanly.
    (ev,) = parse('9 1782133300.000000 access@SYS("/x", 04) = -1 ENOENT (No such file or directory) <0.000020>')
    assert ev.event_type == SYSCALL
    assert ev.syscall == "access"
    assert ev.retval == "-1"
    assert ev.error == "ENOENT"
    assert "No such file" not in ev.args
    assert ev.args == '"/x", 04'


def test_unrecognized_line_is_skipped():
    assert parse("garbage that is not a trace line") == []
