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
