"""The production scheduler wiring: job table, signals, waiting and activity."""

from __future__ import annotations

import fcntl
import io
import json
import signal
import threading
from argparse import Namespace
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from inky_bird_frame.activity import read_activity
from inky_bird_frame.cli import controller_jobs, main, scheduler_command
from inky_bird_frame.config import load_config
from inky_bird_frame.errors import BusyError
from inky_bird_frame.retry_requests import RETRY_WAKE_LISTENER
from inky_bird_frame.runtime_state import state_lock
from inky_bird_frame.scheduler import ScheduledJob
from inky_bird_frame.scheduler import run_scheduler as real_run_scheduler

CONFIG = """
[discovery]
latitude = 1.0
longitude = 2.0
radius_km = 8
species_limit = 12
window = "last-week"

[controller]
workspace_dir = "."
catalog_dir = "catalog"
state_dir = "state"
codex_path = "/usr/bin/false"
bind_host = "127.0.0.1"
port = 8793
references_per_species = 4
generations_per_cycle = 1
max_generation_attempts = 3
generation_enabled = {generation}

[display_node]
controller_url = "http://controller.example.test:8793"
state_dir = "display"

[schedule]
refresh_minutes = 15
generation_minutes = 360
catalog_publish_minutes = 5

[public_catalog]
enabled = {publish}
checkout_dir = "var/public-catalog"
repository = "example/inky-bird-frame"

[notifications]
enabled = {notify}
delivery_retry_minutes = 5

[[notifications.destinations]]
name = "first"
url = "ntfy://example-topic"
"""


def _write_config(
    path: Path,
    *,
    generation: bool = True,
    publish: bool = True,
    notify: bool = True,
    radius: int = 8,
) -> Path:
    path.write_text(
        CONFIG.format(
            generation=str(generation).lower(),
            publish=str(publish).lower(),
            notify=str(notify).lower(),
        ).replace("radius_km = 8", f"radius_km = {radius}")
    )
    return path


def test_controller_jobs_table_with_every_job_enabled(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.toml")
    config = load_config(path, load_secrets=False)
    arguments = ("--config", str(path))

    assert controller_jobs(config, path) == [
        ScheduledJob(
            "refresh",
            ("refresh", *arguments),
            900,
            requires_refresh=False,
            policy_revision=config.discovery_revision,
        ),
        ScheduledJob(
            "generate",
            ("generate", *arguments),
            21600,
            requires_refresh=True,
            policy_revision=config.discovery_revision,
        ),
        ScheduledJob("catalog-publish", ("catalog-publish", *arguments), 300),
        ScheduledJob("notifications", ("notifications", "dispatch", *arguments), 300),
    ]
    assert config.discovery_revision


def test_controller_jobs_omit_disabled_work(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.toml", generation=False, publish=False, notify=False)
    config = load_config(path, load_secrets=False)

    assert [job.name for job in controller_jobs(config, path)] == ["refresh"]


def test_discovery_policy_change_rebinds_refresh_and_generation_only(tmp_path: Path) -> None:
    first = load_config(_write_config(tmp_path / "first.toml"), load_secrets=False)
    second = load_config(_write_config(tmp_path / "second.toml", radius=9), load_secrets=False)
    path = tmp_path / "config.toml"

    before = {job.name: job for job in controller_jobs(first, path)}
    after = {job.name: job for job in controller_jobs(second, path)}

    assert first.discovery_revision != second.discovery_revision
    for name in ("refresh", "generate"):
        assert after[name] == replace(before[name], policy_revision=second.discovery_revision)
    for name in ("catalog-publish", "notifications"):
        assert after[name] == before[name]
        assert after[name].policy_revision is None


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class FakeEvent:
    """Advance the fake clock instead of sleeping."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.flag = False
        self.waits: list[float] = []

    def set(self) -> None:
        self.flag = True

    def is_set(self) -> bool:
        return self.flag

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.clock.now += seconds
        return self.flag


class FakeRunner:
    instances: list[FakeRunner] = []

    def __init__(self, prefix: tuple[str, ...]) -> None:
        self.prefix = prefix
        self.calls: list[tuple[tuple[str, ...], float]] = []
        self.terminated: list[int] = []
        self.on_call: Callable[[FakeRunner], int] = lambda _runner: 0
        FakeRunner.instances.append(self)

    def __call__(self, arguments: tuple[str, ...]) -> int:
        self.calls.append((arguments, CLOCK.now))
        return self.on_call(self)

    def terminate(self, signum: int) -> None:
        self.terminated.append(signum)


CLOCK = FakeClock()


def _run_scheduler_command(
    path: Path, on_call: Callable[[FakeRunner], int]
) -> tuple[FakeRunner, FakeEvent]:
    CLOCK.now = 0.0
    FakeRunner.instances.clear()
    event = FakeEvent(CLOCK)

    def runner_factory(prefix: tuple[str, ...]) -> FakeRunner:
        runner = FakeRunner(prefix)
        runner.on_call = on_call
        return runner

    def scheduled(*args: Any, **kwargs: Any) -> None:
        real_run_scheduler(*args, monotonic=CLOCK.monotonic, **kwargs)

    with (
        patch("inky_bird_frame.cli.threading", SimpleNamespace(Event=lambda: event)),
        patch("inky_bird_frame.cli.SubprocessCommandRunner", side_effect=runner_factory),
        patch("inky_bird_frame.cli.run_scheduler", side_effect=scheduled),
        patch("builtins.print"),
    ):
        assert scheduler_command(Namespace(config=path)) == 0
    return FakeRunner.instances[0], event


def test_scheduler_command_waits_records_activity_and_stops_on_signal(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.toml", generation=False, publish=False, notify=False)
    state_dir = load_config(path, load_secrets=False).controller.state_dir
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)

    def on_call(runner: FakeRunner) -> int:
        if len(runner.calls) == 2:
            signal.raise_signal(signal.SIGTERM)
            return 1
        return 0

    runner, event = _run_scheduler_command(path, on_call)

    assert runner.prefix[1:] == ("-m", "inky_bird_frame.cli")
    assert runner.calls == [
        (("refresh", "--config", str(path)), 0.0),
        (("refresh", "--config", str(path)), 900.0),
    ]
    assert runner.terminated == [signal.SIGTERM]
    assert event.is_set()
    assert sum(event.waits) == 900.0
    assert all(0 < seconds <= 1.0 for seconds in event.waits)
    entries = read_activity(state_dir)["entries"]
    assert isinstance(entries, dict)
    assert entries["job-refresh"]["status"] == "failed"
    assert entries["job-refresh"]["last_success_at"] is not None
    assert entries["job-refresh"]["next_run_at"] is not None
    assert signal.getsignal(signal.SIGTERM) == previous_sigterm
    assert signal.getsignal(signal.SIGINT) == previous_sigint


def test_scheduler_command_keeps_last_good_jobs_when_config_breaks(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.toml", generation=False, publish=False)
    good = path.read_bytes()
    state_dir = load_config(path, load_secrets=False).controller.state_dir

    def on_call(runner: FakeRunner) -> int:
        refreshes = [call for call in runner.calls if call[0][0] == "refresh"]
        if len(refreshes) == 1 and len(runner.calls) == 1:
            path.write_text("[discovery\nbroken = ")
        elif len(refreshes) == 2:
            path.write_bytes(good)
        elif len(refreshes) == 3:
            signal.raise_signal(signal.SIGTERM)
        return 0

    with (
        patch("inky_bird_frame.cli.safe_record_degradation") as degrade,
        patch("inky_bird_frame.cli.safe_record_recovery") as recover,
        patch("inky_bird_frame.cli.dispatch_notifications") as dispatch,
    ):
        runner, _event = _run_scheduler_command(path, on_call)

    refreshes = [stamp for arguments, stamp in runner.calls if arguments[0] == "refresh"]
    assert refreshes == [0.0, 900.0, 1800.0]
    # Reported at 0, 300, 600 and 900 seconds, then the file is fixed.
    assert degrade.call_count == 4
    first = degrade.call_args_list[0]
    assert first.args[0].controller.state_dir == state_dir
    assert first.kwargs["key"] == "scheduler-configuration"
    assert dispatch.call_count == 4
    sent_with = dispatch.call_args_list[0].args[0]
    assert sent_with.notifications.destinations[0].url == "ntfy://example-topic"
    assert recover.call_count == 1
    assert recover.call_args.kwargs["key"] == "scheduler-configuration"


def test_second_scheduler_reports_busy_with_exit_three(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.toml", generation=False, publish=False, notify=False)
    listener = load_config(path, load_secrets=False).controller.state_dir / RETRY_WAKE_LISTENER
    output = io.StringIO()

    with (
        state_lock(listener, nonblocking=True),
        patch("inky_bird_frame.cli.run_scheduler") as run,
        patch("inky_bird_frame.cli.SCHEDULER_LOCK_WAIT_SECONDS", 0.05),
        redirect_stdout(output),
    ):
        assert main(["scheduler", "--config", str(path)]) == 3

    run.assert_not_called()
    payload = json.loads(output.getvalue())
    assert payload["error"] == {
        "type": "busy",
        "message": "Another scheduler is already running for this installation",
    }
    with (
        state_lock(listener, nonblocking=True),
        patch("inky_bird_frame.cli.SCHEDULER_LOCK_WAIT_SECONDS", 0.05),
        pytest.raises(BusyError),
    ):
        scheduler_command(Namespace(config=path))


def test_a_brief_status_probe_does_not_stop_the_only_scheduler(tmp_path: Path) -> None:
    path = _write_config(tmp_path / "config.toml", generation=False, publish=False, notify=False)
    listener = load_config(path, load_secrets=False).controller.state_dir / RETRY_WAKE_LISTENER
    listener.parent.mkdir(parents=True, exist_ok=True)
    listener.touch()
    probe = listener.open("rb")
    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
    release = threading.Timer(0.2, probe.close)
    release.start()
    try:
        with patch("inky_bird_frame.cli.run_scheduler") as run:
            assert scheduler_command(Namespace(config=path)) == 0
    finally:
        release.cancel()
        probe.close()

    run.assert_called_once()
