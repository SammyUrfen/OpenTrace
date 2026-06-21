"""Tests for reconstructing program stdout/stderr from strace -e write hex dumps."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from app.program_output import extract_output

_FIXTURE = (
    '100 1700000000.000001 write(1, "hi\\n", 3) = 3 <0.000010>\n'
    " | 00000  68 69 0a                                          hi.              |\n"
    '100 1700000000.000002 write(2, "oops\\n", 5) = 5 <0.000010>\n'
    " | 00000  6f 6f 70 73 0a                                    oops.            |\n"
    "100 1700000000.000003 close(3)          = 0 <0.000005>\n"
)


def test_extract_from_fixture(tmp_path):
    log = tmp_path / "s.log"
    log.write_text(_FIXTURE)
    chunks = extract_output(log)
    assert len(chunks) == 2
    assert chunks[0]["stream"] == "stdout" and chunks[0]["text"] == "hi\n"
    assert chunks[1]["stream"] == "stderr" and chunks[1]["text"] == "oops\n"
    assert chunks[0]["timestamp_ms"] == pytest.approx(1700000000000.001, abs=1)


def test_missing_file_is_empty(tmp_path):
    assert extract_output(tmp_path / "nope.log") == []


@pytest.mark.skipif(not shutil.which("strace"), reason="strace not available")
def test_extract_from_real_strace(tmp_path):
    log = tmp_path / "real.log"
    subprocess.run(
        ["strace", "-f", "-T", "-ttt", "-e", "write=1,2", "-o", str(log), "--",
         "python3", "-c",
         "import sys; print('line one'); print('with\\ttab'); "
         "sys.stderr.write('err!\\n'); sys.stdout.write('Z'*80+'\\n')"],
        capture_output=True,
    )
    chunks = extract_output(log)
    out = "".join(c["text"] for c in chunks if c["stream"] == "stdout")
    err = "".join(c["text"] for c in chunks if c["stream"] == "stderr")
    assert "line one\n" in out
    assert "with\ttab\n" in out
    assert "Z" * 80 in out  # multi-dump-line write reconstructs fully
    assert "err!\n" in err
