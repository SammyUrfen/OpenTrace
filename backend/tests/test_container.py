"""Tests for container awareness + NSpid PID resolution (app.container, Phase E).

Pure /proc parsing — deterministic and testable without root or a real container.
"""
from __future__ import annotations

import os

from app import container


def test_parse_cgroup_layouts():
    cases = {
        "12:pids:/docker/" + "a" * 64: ("docker", "a" * 12),
        "0::/system.slice/docker-" + "b" * 64 + ".scope": ("docker", "b" * 12),
        "0::/machine.slice/libpod-" + "c" * 64 + ".scope": ("podman", "c" * 12),
        "0::/system.slice/cri-containerd-" + "d" * 64 + ".scope": ("containerd", "d" * 12),
        "11:memory:/kubepods/besteffort/pod123/" + "e" * 64: ("kubernetes", "e" * 12),
    }
    for cgroup_line, (runtime, short) in cases.items():
        info = container.parse_cgroup(cgroup_line + "\n")
        assert info["container"] is True, cgroup_line
        assert info["runtime"] == runtime and info["id"] == short
    # a normal host process cgroup → not a container
    assert container.parse_cgroup("0::/user.slice/user-1000.slice/session-2.scope")["container"] is False


def test_container_info_bare_metal_is_false():
    # this test process is not in a container
    info = container.container_info(os.getpid())
    assert info["container"] is False and info["id"] is None


def test_container_info_missing_pid_fail_open():
    info = container.container_info(2**31 - 1)
    assert info["container"] is False and info["id"] is None and info["ns_pids"] == []


def test_nspid_map_self_is_host_namespace():
    ns = container.nspid_map(os.getpid())
    assert ns and ns[0] == os.getpid()  # first entry is the host pid


def test_resolve_host_pid_finds_self_by_innermost():
    # in the root namespace NSpid is length-1, so resolve returns None (needs a
    # nested ns); a missing local pid also yields None — both must not raise.
    assert container.resolve_host_pid(2**31 - 2) is None


# --- cgroup resource-limit parsing (R7) -------------------------------------

def test_parse_cpu_max_v2():
    assert container.parse_cpu_max_v2("50000 100000") == 0.5   # half a core
    assert container.parse_cpu_max_v2("200000 100000") == 2.0  # two cores
    assert container.parse_cpu_max_v2("max 100000") is None    # unlimited
    assert container.parse_cpu_max_v2("50000") == 0.5          # default 100ms period
    assert container.parse_cpu_max_v2("") is None
    assert container.parse_cpu_max_v2("garbage") is None


def test_parse_cpu_quota_v1():
    assert container.parse_cpu_quota_v1("25000", "100000") == 0.25
    assert container.parse_cpu_quota_v1("-1", "100000") is None  # unlimited sentinel
    assert container.parse_cpu_quota_v1("x", "100000") is None


def test_parse_mem_limit():
    assert container.parse_mem_limit("536870912") == 536870912
    assert container.parse_mem_limit("max") is None
    assert container.parse_mem_limit("") is None
    # v1 "unlimited" sentinel (near 2^63) reads as no limit
    assert container.parse_mem_limit("9223372036854771712") is None
    assert container.parse_mem_limit("0") is None


def test_cgroup_limits_v2_unified(tmp_path):
    # synthetic cgroup v2: single unified line + files under fs_root/<path>
    cg_path = "/system.slice/docker-" + "a" * 64 + ".scope"
    base = tmp_path / cg_path.lstrip("/")
    base.mkdir(parents=True)
    (base / "cpu.max").write_text("50000 100000\n")
    (base / "memory.max").write_text("268435456\n")
    limits = container.cgroup_limits(
        0, cgroup_text=f"0::{cg_path}\n", fs_root=str(tmp_path))
    assert limits == {"cpu_quota_cores": 0.5, "mem_limit_bytes": 268435456}


def test_cgroup_limits_v1_per_controller(tmp_path):
    rel = "/docker/" + "b" * 64
    cpu_dir = tmp_path / "cpu,cpuacct" / rel.lstrip("/")
    mem_dir = tmp_path / "memory" / rel.lstrip("/")
    cpu_dir.mkdir(parents=True)
    mem_dir.mkdir(parents=True)
    (cpu_dir / "cpu.cfs_quota_us").write_text("25000\n")
    (cpu_dir / "cpu.cfs_period_us").write_text("100000\n")
    (mem_dir / "memory.limit_in_bytes").write_text("134217728\n")
    text = f"4:cpu,cpuacct:{rel}\n9:memory:{rel}\n"
    limits = container.cgroup_limits(0, cgroup_text=text, fs_root=str(tmp_path))
    assert limits == {"cpu_quota_cores": 0.25, "mem_limit_bytes": 134217728}


def test_cgroup_limits_bare_metal_fail_open(tmp_path):
    # no cgroup files at all → all-None, never raises
    limits = container.cgroup_limits(
        0, cgroup_text="0::/user.slice/user-1000.slice\n", fs_root=str(tmp_path))
    assert limits == {"cpu_quota_cores": None, "mem_limit_bytes": None}
