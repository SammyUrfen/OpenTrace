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


# --- cgroup resource limits (R7: container-limit-aware rules) ----------------
#
# A container's cgroup caps CPU (a fractional-core quota) and memory (a hard byte
# limit). Without knowing those, the rule engine's "90% of a core" / RSS-growth
# gates never fire for a 0.5-core-quota'd or memory-boxed container. We read the
# quota + limit from /proc + /sys/fs/cgroup alone (no root, no runtime socket) and
# hand them to the rules. Handles cgroup v2 (unified) and v1 (per-controller).

# memory.limit_in_bytes (v1) uses a near-2^63 sentinel to mean "unlimited"; treat
# anything at/above this as no limit.
_MEM_UNLIMITED = 1 << 62


def parse_cpu_max_v2(text: str) -> float | None:
    """cgroup v2 `cpu.max` ('<quota_us> <period_us>' | 'max <period>') → the allowed
    CPU as a core count (quota/period), or None when unlimited/unparseable."""
    parts = text.split()
    if not parts or parts[0] == "max":
        return None
    try:
        quota = int(parts[0])
        period = int(parts[1]) if len(parts) > 1 else 100_000
    except (ValueError, IndexError):
        return None
    return quota / period if quota > 0 and period > 0 else None


def parse_cpu_quota_v1(quota_text: str, period_text: str) -> float | None:
    """cgroup v1 `cpu.cfs_quota_us` / `cpu.cfs_period_us` → core count, or None when
    unlimited (quota == -1) or unparseable."""
    try:
        quota = int(quota_text.strip())
        period = int(period_text.strip())
    except (ValueError, AttributeError):
        return None
    return quota / period if quota > 0 and period > 0 else None


def parse_mem_limit(text: str) -> int | None:
    """cgroup memory limit (`memory.max` v2 | `memory.limit_in_bytes` v1) → bytes,
    or None when unlimited ('max', ≤0, or the v1 near-2^63 sentinel)."""
    t = (text or "").strip()
    if not t or t == "max":
        return None
    try:
        val = int(t)
    except ValueError:
        return None
    return val if 0 < val < _MEM_UNLIMITED else None


def _cgroup_paths(text: str) -> dict[str, str]:
    """Map controller → relative cgroup path from /proc/<pid>/cgroup content. A v2
    unified line ('0::/path') has no controllers, so it's stored under key ''."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _hid, controllers, path = parts
        for ctrl in (controllers.split(",") if controllers else [""]):
            out[ctrl] = path
    return out


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def cgroup_limits(pid: int, *, cgroup_text: str | None = None,
                  fs_root: str = "/sys/fs/cgroup") -> dict:
    """The CPU quota (cores) + memory limit (bytes) the target's cgroup enforces,
    read from /proc + /sys/fs/cgroup (no root). Returns
    {cpu_quota_cores, mem_limit_bytes}, each None when unlimited/unavailable.
    Handles cgroup v2 (unified) and v1 (per-controller mounts). Fail-open: all-None
    on any error, so a bare-metal target reads as unconstrained (rules behave as
    they always have). `cgroup_text`/`fs_root` are injectable for tests."""
    if cgroup_text is None:
        cgroup_text = _read_text(Path(f"/proc/{pid}/cgroup")) or ""
    paths = _cgroup_paths(cgroup_text)
    fs = Path(fs_root)
    cpu_cores: float | None = None
    mem_bytes: int | None = None

    # cgroup v2 unified: both files live under fs_root + the unified path.
    unified = paths.get("")
    if unified is not None:
        base = fs / unified.lstrip("/")
        t = _read_text(base / "cpu.max")
        if t is not None:
            cpu_cores = parse_cpu_max_v2(t)
        t = _read_text(base / "memory.max")
        if t is not None:
            mem_bytes = parse_mem_limit(t)

    # cgroup v1 fallback: per-controller mounts (cpu,cpuacct / memory).
    if cpu_cores is None and "cpu" in paths:
        rel = paths["cpu"].lstrip("/")
        for mount in ("cpu,cpuacct", "cpu"):
            q = _read_text(fs / mount / rel / "cpu.cfs_quota_us")
            p = _read_text(fs / mount / rel / "cpu.cfs_period_us")
            if q is not None and p is not None:
                cpu_cores = parse_cpu_quota_v1(q, p)
                break
    if mem_bytes is None and "memory" in paths:
        rel = paths["memory"].lstrip("/")
        t = _read_text(fs / "memory" / rel / "memory.limit_in_bytes")
        if t is not None:
            mem_bytes = parse_mem_limit(t)

    return {"cpu_quota_cores": cpu_cores, "mem_limit_bytes": mem_bytes}


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
