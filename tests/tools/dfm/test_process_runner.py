from pathlib import Path
import sys
import threading

import pytest

from tools.dfm.analyzers.base import CancellationToken
from tools.dfm.errors import DFMError
from tools.dfm.runtime.process import ProcessRunner


FIXTURE = Path(__file__).parent / "fixtures" / "worker_fixture.py"


def test_process_runner_streams_events_and_keeps_stderr_separate(tmp_path):
    events = []

    stdout_log = tmp_path / "worker.stdout.log"
    stderr_log = tmp_path / "worker.stderr.log"
    result = ProcessRunner().run(
        [sys.executable, str(FIXTURE), "success"],
        tmp_path / "含 空格",
        5,
        CancellationToken(),
        events.append,
        stdout_log,
        stderr_log,
    )

    assert result.returncode == 0
    assert [event.type for event in events] == ["progress", "completed"]
    assert "ordinary worker output" in result.stdout
    assert "fixture diagnostic" in result.stderr
    assert '"type":"progress"' in stdout_log.read_text(encoding="utf-8")
    assert "fixture diagnostic" in stderr_log.read_text(encoding="utf-8")


def test_process_runner_streams_raw_geometry_jsonl_events(tmp_path):
    events = []

    result = ProcessRunner().run(
        [sys.executable, str(FIXTURE), "raw_geometry"],
        tmp_path,
        5,
        CancellationToken(),
        events.append,
    )

    assert result.returncode == 0
    assert [event.type for event in events] == ["progress", "completed"]
    assert all(
        event.contract_version == "dfm.geometry.event/v1" for event in events
    )


def test_process_runner_times_out_and_terminates_worker(tmp_path):
    with pytest.raises(DFMError) as exc_info:
        ProcessRunner().run(
            [sys.executable, str(FIXTURE), "hang"],
            tmp_path,
            0.2,
            CancellationToken(),
            lambda _event: None,
        )

    assert exc_info.value.code == "worker_timeout"


def test_process_runner_honors_cancellation(tmp_path):
    token = CancellationToken()
    timer = threading.Timer(0.2, token.cancel)
    timer.start()
    try:
        with pytest.raises(DFMError) as exc_info:
            ProcessRunner().run(
                [sys.executable, str(FIXTURE), "hang"],
                tmp_path,
                5,
                token,
                lambda _event: None,
            )
    finally:
        timer.cancel()

    assert exc_info.value.code == "run_cancelled"
