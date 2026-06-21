"""CRUD + storage roundtrip tests against a temp OPENTRACE_HOME."""
from __future__ import annotations

from pathlib import Path

from app import paths, run_views, runs, sessions, storage, terminals
from app.trace.events import Anomaly, MetricSample, TraceEvent


def test_session_create_slug_and_dir(ot_home):
    s = sessions.create(sessions.SessionCreate(display_name="My Compiler App"))
    assert s.slug == "My-Compiler-App"
    assert paths.session_dir(s.slug).is_dir()
    assert paths.session_json(s.slug).exists()
    # duplicate display name -> distinct slug
    s2 = sessions.create(sessions.SessionCreate(display_name="My Compiler App"))
    assert s2.slug == "My-Compiler-App-2"


def test_session_default_and_list(ot_home):
    d = sessions.get_or_create_default()
    assert d.display_name == "Default"
    again = sessions.get_or_create_default()
    assert again.id == d.id  # does not create a second default


def test_terminal_lifecycle(ot_home):
    s = sessions.create(sessions.SessionCreate(display_name="Proj"))
    t = terminals.create(terminals.TerminalCreate(
        session_id=s.id, shell="/bin/bash", cwd="/home/u"))
    assert t.is_active is True
    assert Path(t.histfile_path).exists()
    assert terminals.list_for_session(s.id)[0].id == t.id
    closed = terminals.close(t.id)
    assert closed.is_active is False


def test_run_create_and_finalize(ot_home):
    s = sessions.create(sessions.SessionCreate(display_name="Proj"))
    r = runs.create(runs.RunCreate(command="python app.py --x", cwd="/work", session_id=s.id))
    assert r.command_basename == "python"
    assert r.status == runs.RUNNING
    assert Path(r.run_dir).is_dir()
    assert r.display_name.startswith("python_")
    fin = runs.finalize(r.id, exit_code=0, status=runs.COMPLETED, max_severity="high")
    assert fin.status == runs.COMPLETED
    assert fin.exit_code == 0
    assert fin.duration_ms is not None and fin.duration_ms >= 0
    assert fin.max_severity == "high"


def test_run_dir_unique_on_collision(ot_home, monkeypatch):
    s = sessions.create(sessions.SessionCreate(display_name="Proj"))
    r1 = runs.create(runs.RunCreate(command="a.out", cwd="/", session_id=s.id))
    r2 = runs.create(runs.RunCreate(command="a.out", cwd="/", session_id=s.id))
    assert r1.run_dir != r2.run_dir
    assert Path(r1.run_dir).is_dir() and Path(r2.run_dir).is_dir()


def test_run_delete_removes_row_and_dir(ot_home):
    s = sessions.create(sessions.SessionCreate(display_name="Proj"))
    r = runs.create(runs.RunCreate(command="x", cwd="/", session_id=s.id))
    run_dir = Path(r.run_dir)
    assert run_dir.is_dir()
    assert runs.delete(r.id) is True
    assert runs.get(r.id) is None
    assert not run_dir.exists()
    assert runs.delete("nope") is False
    # deleting a run leaves its session intact
    assert sessions.get(s.id) is not None


def test_run_views_upsert(ot_home):
    s = sessions.create(sessions.SessionCreate(display_name="Proj"))
    r = runs.create(runs.RunCreate(command="x", cwd="/", session_id=s.id))
    run_views.upsert(r.id, "timeline", {"zoom": 2})
    run_views.upsert(r.id, "timeline", {"zoom": 5})
    v = run_views.get(r.id, "timeline")
    assert v.state == {"zoom": 5}
    assert len(run_views.list_for_run(r.id)) == 1


def test_delete_session_cascades(ot_home):
    s = sessions.create(sessions.SessionCreate(display_name="Proj"))
    r = runs.create(runs.RunCreate(command="x", cwd="/", session_id=s.id))
    assert runs.get(r.id) is not None
    assert sessions.delete(s.id) is True
    assert runs.get(r.id) is None  # cascade
    assert not paths.session_dir(s.slug).exists()


def test_storage_metrics_events_anomalies_roundtrip(ot_home):
    s = sessions.create(sessions.SessionCreate(display_name="Proj"))
    r = runs.create(runs.RunCreate(command="x", cwd="/", session_id=s.id))

    storage.insert_metrics(r.id, [
        MetricSample(timestamp_ms=1000.0, cpu_pct=12.0, rss_mb=50.0, open_fds=5),
        MetricSample(timestamp_ms=1250.0, cpu_pct=15.0, rss_mb=55.0, open_fds=6),
    ])
    rows = storage.read_metrics(r.id)
    assert len(rows) == 2 and rows[0]["cpu_pct"] == 12.0

    evs = [TraceEvent(timestamp_ms=1100.0, pid=9, syscall="openat", error="ENOENT", path="/x")]
    ids = storage.insert_events(r.id, evs)
    assert len(ids) == 1
    read = storage.read_events(r.id)
    assert read[0]["syscall"] == "openat" and read[0]["error"] == "ENOENT"

    storage.insert_anomalies(r.id, [Anomaly(
        rule_id="t", severity="high", severity_score=70.0,
        title="T", description="d", evidence_ids=ids)])
    an = storage.read_anomalies(r.id)
    assert an[0]["rule_id"] == "t" and an[0]["evidence_ids"] == ids


def test_ndjson_zst_roundtrip(ot_home, tmp_path):
    path = tmp_path / "x.ndjson.zst"
    rows = [{"a": i, "b": "héllo"} for i in range(100)]
    n = storage.write_ndjson_zst(path, rows)
    assert n == 100
    back = list(storage.read_ndjson_zst(path))
    assert back == rows


def test_max_severity_ordering():
    assert storage.max_severity(["low", "critical", "high"]) == "critical"
    assert storage.max_severity(["low", "medium"]) == "medium"
    assert storage.max_severity([]) == "clean"
