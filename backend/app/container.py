"""Container awareness for attach targets (roadmap Phase E).

The backend runs on the HOST and sees host PIDs, so attaching to a containerized
process already works by its host PID — but the user needs to KNOW a target is in a
container (and which one), and sometimes only knows the in-container PID. This module
reads /proc alone (no root, no docker socket) to: (a) label a PID's container from
its cgroup, and (b) resolve a container-local PID → host PID via the NSpid map.
"""
from __future__ import annotations

import re
from pathlib import Path

# A 64-hex id identifies docker/containerd/cri-o containers; podman uses it too.
# Ordered specific → generic; first match wins. Patterns are matched against the
# cgroup path (works for both cgroup v1 per-controller lines and v2 unified).
_CGROUP_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"docker[-/]([0-9a-f]{64})"), "docker"),
    (re.compile(r"libpod[-/]([0-9a-f]{64})"), "podman"),
    (re.compile(r"cri-containerd[-/]([0-9a-f]{64})"), "containerd"),
    (re.compile(r"crio-([0-9a-f]{64})"), "cri-o"),
    (re.compile(r"kubepods\S*?[-/]([0-9a-f]{64})"), "kubernetes"),
    # generic: a 64-hex as a full path segment (other/older layouts)
    (re.compile(r"(?:^|/)([0-9a-f]{64})(?:\.scope)?(?:/|$)"), "container"),
]


_NOT_CONTAINER = {"container": False, "runtime": None, "id": None,
                  "container_id": None, "cgroup_path": None, "pod_uid": None}
_POD_UID_RE = re.compile(r"pod([0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12})")


def parse_cgroup(text: str) -> dict:
    """Pure parse of /proc/<pid>/cgroup content → {container, runtime, id (short),
    container_id (full 64-hex), cgroup_path, pod_uid (k8s)}."""
    for line in text.splitlines():
        path = line.split(":", 2)[-1]  # hierarchy-ID:controllers:PATH
        for rx, runtime in _CGROUP_PATTERNS:
            m = rx.search(path)
            if m:
                full = m.group(1)
                pod = _POD_UID_RE.search(path)
                return {
                    "container": True, "runtime": runtime, "id": full[:12],
                    "container_id": full, "cgroup_path": path.strip(),
                    "pod_uid": pod.group(1).replace("_", "-") if pod else None,
                }
    return dict(_NOT_CONTAINER)


def container_info(pid: int) -> dict:
    """Detect whether `pid` runs in a container (via /proc/<pid>/cgroup): its
    runtime + id + (k8s) pod uid, plus the NSpid map. All falsy when not
    containerized or on error (fail-open: a bare-metal process reads normally)."""
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        info = dict(_NOT_CONTAINER)
    else:
        info = parse_cgroup(text)
    ns = nspid_map(pid)
    info["ns_pids"] = ns
    info["container_pid"] = ns[-1] if len(ns) > 1 else None
    return info


def nspid_map(pid: int) -> list[int]:
    """The `NSpid:` line from /proc/<pid>/status → [host_pid, ns1_pid, …]; the LAST
    entry is the innermost (container-local) PID, the FIRST is the host PID. []
    on failure. A length-1 list means the process is in the root PID namespace."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("NSpid:"):
                return [int(x) for x in line.split()[1:]]
    except (OSError, ValueError):
        pass
    return []


def resolve_host_pid(local_pid: int, container_id: str | None = None) -> int | None:
    """Given a container-LOCAL pid (as seen inside the container), find the matching
    HOST pid by scanning /proc/*/status for an NSpid whose innermost value ==
    local_pid, optionally constrained to a container short id. None if not found."""
    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        host_pid = int(entry.name)
        ns = nspid_map(host_pid)
        if len(ns) >= 2 and ns[-1] == local_pid:
            if container_id is None or container_info(host_pid).get("id") == container_id:
                return host_pid
    return None
