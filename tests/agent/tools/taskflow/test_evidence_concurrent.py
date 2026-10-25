"""Concurrent ledger writers cannot break the Gate C reader.

Several step children of one flow declare evidence (auto-recorded terminal /
python_repl rows land in threads and async tasks alike) while the parent reads
the aggregate: ``collect_evidence_summary(session_keys=…)`` must never raise on
a line another writer is halfway through, and a row it renders is always one
that was appended WHOLE (the write takes the shared per-path file lock, and the
read skips an unparsable line instead of crashing on it).
"""

import threading
from pathlib import Path

import pytest

from agent.tools.taskflow.evidence_collector import collect_evidence_summary
from agent.tools.pub_base.evidence_ledger import EvidenceLedger

pytestmark = [pytest.mark.unit]

_KEYS = [f"agent:main:subagent:ev-{index}" for index in range(4)]
_ROWS_PER_KEY = 25


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "evidence.jsonl"
    monkeypatch.setattr(EvidenceLedger, "LEDGER_PATH", str(path))
    return path


def test_concurrent_writers_never_break_the_aggregate_reader(ledger_path: Path):
    commands = {key: {f"pytest {key}-{i}.py" for i in range(_ROWS_PER_KEY)} for key in _KEYS}
    errors: list[BaseException] = []
    stop = threading.Event()
    seen_lines: list[str] = []

    def _writer(key: str) -> None:
        try:
            ledger = EvidenceLedger.for_session(key)
            for command in sorted(commands[key]):
                ledger.append(kind="test", command=command, status="passed")
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    def _reader() -> None:
        # Interleave reads with the writes: a torn line must be skipped, never raised.
        while not stop.is_set():
            summary = collect_evidence_summary(session_keys=_KEYS)
            if summary:
                seen_lines.extend(summary.splitlines())

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    writers = [threading.Thread(target=_writer, args=(key,)) for key in _KEYS]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    stop.set()
    reader.join(timeout=5)

    assert errors == []
    # Every row is intact: the aggregate sees all of them, and nothing partial.
    known = {command for per_key in commands.values() for command in per_key}
    summary = collect_evidence_summary(session_keys=_KEYS)
    assert summary is not None
    rendered = {line.split("`")[1] for line in summary.splitlines() if "`" in line}
    assert rendered == known
    # A mid-flight read only ever rendered a WHOLE command (never a fragment).
    for line in seen_lines:
        if "`" in line:
            assert line.split("`")[1] in known, line


def test_the_aggregate_spans_every_declared_key(ledger_path: Path):
    for key in _KEYS:
        EvidenceLedger.for_session(key).append(
            kind="test", command=f"pytest {key}", status="passed"
        )

    summary = collect_evidence_summary(session_keys=_KEYS + ["agent:main:subagent:absent"])

    assert summary is not None
    assert summary.count("[pass]") == len(_KEYS)
    for key in _KEYS:
        assert f"pytest {key}" in summary
