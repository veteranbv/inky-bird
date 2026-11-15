from __future__ import annotations

import hashlib
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from inky_bird_frame.config import (
    AppConfig,
    NotificationDestination,
    NotificationEvent,
    NotificationsConfig,
    load_config,
)
from inky_bird_frame.errors import ConfigurationError
from inky_bird_frame.notifications import (
    NotificationItem,
    _deliver,
    check_display_heartbeat,
    controller_started_path,
    dispatch_notifications,
    enqueue_notification,
    notification_status,
    record_controller_start,
    record_degradation,
    record_recovery,
    requeue_dead_letters,
    safe_notify,
    send_notification_test,
    validate_notification_destinations,
)

CONFIG = """
[discovery]
zip_code = "12345"
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

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display"

[notifications]
enabled = true
degradation_failure_threshold = 2
degradation_window_minutes = 30
cooldown_minutes = 360
delivery_retry_minutes = 5
max_delivery_attempts = 3

[[notifications.destinations]]
name = "first"
url = "pover://user@token"
events = ["terminal_error", "degraded", "recovered"]

[[notifications.destinations]]
name = "second"
url = "ntfy://example-topic"
events = ["terminal_error"]
"""

DISPLAY_CONFIG = """
[discovery]
zip_code = "12345"
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

[display_node]
controller_url = "http://controller.test:8793"
state_dir = "display"

[schedule]
rotation_minutes = {rotation_minutes}

[notifications]
enabled = true
degradation_failure_threshold = 1
degradation_window_minutes = 30
cooldown_minutes = 360
delivery_retry_minutes = 5
max_delivery_attempts = 3

[[notifications.destinations]]
name = "first"
url = "pover://user@token"
events = ["display_stale", "display_recovered"]
"""


def _retry_config(temporary: str, retry_settings: str) -> AppConfig:
    path = Path(temporary) / "config.toml"
    path.write_text(
        CONFIG.replace("delivery_retry_minutes = 5\nmax_delivery_attempts = 3\n", retry_settings)
    )
    return load_config(path)


def _delays_until_dead_letter(config: AppConfig, now: datetime) -> list[int]:
    """Attempt each delivery exactly when due and return the waits in minutes."""

    enqueue_notification(
        config, NotificationEvent.DEGRADED, dedupe_key="outage", title="Down", body="Down", now=now
    )
    delays: list[int] = []
    current = now
    with patch("inky_bird_frame.notifications._deliver", side_effect=RuntimeError("down")):
        while True:
            dispatch_notifications(config, now=current)
            state = json.loads((config.controller.state_dir / "notifications.json").read_text())
            if state["dead_letters"]:
                assert state["pending"] == []
                return delays
            following = datetime.fromisoformat(state["pending"][0]["next_attempt_at"])
            delays.append(int((following - current).total_seconds() // 60))
            current = following


def test_delivery_retries_double_up_to_a_six_hour_cap() -> None:
    now = datetime(2026, 7, 10, tzinfo=UTC)
    with TemporaryDirectory() as temporary:
        config = _retry_config(
            temporary, "delivery_retry_minutes = 5\nmax_delivery_attempts = 12\n"
        )
        delays = _delays_until_dead_letter(config, now)

    assert delays == [5, 10, 20, 40, 80, 160, 320, 360, 360, 360, 360]


def test_default_retries_last_at_least_seventy_two_hours_before_dead_letter() -> None:
    now = datetime(2026, 7, 10, tzinfo=UTC)
    with TemporaryDirectory() as temporary:
        config = _retry_config(temporary, "")
        delays = _delays_until_dead_letter(config, now)
        status = notification_status(config)

    assert config.notifications.delivery_retry_minutes == 5
    assert config.notifications.max_delivery_attempts == 20
    assert delays == [5, 10, 20, 40, 80, 160, 320, *([360] * 12)]
    assert sum(delays) >= 72 * 60
    assert status["dead_letters"] == 1
    assert status["retry"] == {
        "first_delay_minutes": 5,
        "max_delay_minutes": 360,
        "max_delivery_attempts": 20,
        "dead_letter_after_minutes": sum(delays),
    }


def test_a_retry_interval_above_the_cap_stays_constant() -> None:
    now = datetime(2026, 7, 10, tzinfo=UTC)
    with TemporaryDirectory() as temporary:
        config = _retry_config(
            temporary, "delivery_retry_minutes = 400\nmax_delivery_attempts = 4\n"
        )
        delays = _delays_until_dead_letter(config, now)

    assert delays == [400, 400, 400]


def test_status_reports_the_next_retry_and_dead_letters() -> None:
    now = datetime(2026, 7, 10, tzinfo=UTC)
    with TemporaryDirectory() as temporary:
        config = _retry_config(temporary, "delivery_retry_minutes = 5\nmax_delivery_attempts = 2\n")
        empty = notification_status(config)
        for key in ("first", "second"):
            enqueue_notification(
                config,
                NotificationEvent.DEGRADED,
                dedupe_key=key,
                title="Down",
                body="Down",
                now=now,
            )
        with patch("inky_bird_frame.notifications._deliver", side_effect=RuntimeError("down")):
            dispatch_notifications(config, now=now)
        retrying = notification_status(config)
        with patch("inky_bird_frame.notifications._deliver", side_effect=RuntimeError("down")):
            dispatch_notifications(config, now=now + timedelta(minutes=5))
        dead = notification_status(config)

    assert empty["next_attempt_at"] is None
    assert retrying["pending"] == 2
    assert retrying["dead_letters"] == 0
    assert retrying["next_attempt_at"] == (now + timedelta(minutes=5)).isoformat()
    assert dead["pending"] == 0
    assert dead["dead_letters"] == 2
    assert dead["next_attempt_at"] is None
    assert dead["retry"] == {
        "first_delay_minutes": 5,
        "max_delay_minutes": 360,
        "max_delivery_attempts": 2,
        "dead_letter_after_minutes": 5,
    }


class NotificationTests(unittest.TestCase):
    def _config(self, temporary: str) -> AppConfig:
        path = Path(temporary) / "config.toml"
        path.write_text(CONFIG)
        return load_config(path)

    def test_validation_and_status_redact_service_urls(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            validated = validate_notification_destinations(config)
            status = notification_status(config)

        serialized = json.dumps(status)
        self.assertEqual([item["scheme"] for item in validated], ["pover", "ntfy"])
        self.assertNotIn("user", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("example-topic", serialized)

    def test_manual_delivery_rejects_disabled_notifications(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            config = AppConfig(
                discovery=config.discovery,
                controller=config.controller,
                display_node=config.display_node,
                schedule=config.schedule,
                public_catalog=config.public_catalog,
                research=config.research,
                notifications=NotificationsConfig(),
            )

            with self.assertRaisesRegex(ConfigurationError, "disabled"):
                send_notification_test(config)

    def test_safe_notify_enqueues_without_provider_delivery(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch("inky_bird_frame.notifications._deliver") as deliver:
                result = safe_notify(
                    config,
                    NotificationEvent.TERMINAL_ERROR,
                    dedupe_key="queued-only",
                    title="Failure",
                    body="Something failed",
                )
            status = notification_status(config)

        deliver.assert_not_called()
        self.assertTrue(result["queued"])
        self.assertEqual(result["attempted"], 0)
        self.assertEqual(status["pending"], 1)

    def test_delivery_uses_provider_result_truthiness(self) -> None:
        class ProviderResult:
            def __init__(self, confirmed: bool) -> None:
                self.confirmed = confirmed

            def __bool__(self) -> bool:
                return self.confirmed

        notifier = MagicMock()
        notifier.add.return_value = True
        notifier.__len__.return_value = 1
        destination = NotificationDestination(
            name="test",
            url="json://localhost",
            events=(NotificationEvent.TERMINAL_ERROR,),
        )
        item = NotificationItem(
            item_id="test",
            event=NotificationEvent.TERMINAL_ERROR,
            title="Failure",
            body="Something failed",
            created_at=datetime(2026, 7, 10, tzinfo=UTC),
            attempts=0,
            next_attempt_at=datetime(2026, 7, 10, tzinfo=UTC),
            delivered_to=(),
        )

        with patch("inky_bird_frame.notifications._new_notifier", return_value=notifier):
            notifier.notify.return_value = ProviderResult(confirmed=True)
            _deliver(destination, item)
            notifier.notify.return_value = ProviderResult(confirmed=False)
            with self.assertRaisesRegex(RuntimeError, "did not confirm delivery"):
                _deliver(destination, item)

    def test_enqueue_ignores_events_without_a_subscribed_destination(self) -> None:
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            queued = enqueue_notification(
                config,
                NotificationEvent.DISCOVERY,
                dedupe_key="unsubscribed",
                title="Discovery",
                body="A bird was discovered",
            )
            status = notification_status(config)

        self.assertFalse(queued)
        self.assertEqual(status["pending"], 0)

    def test_partial_delivery_retries_only_failed_destination(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            enqueue_notification(
                config,
                NotificationEvent.TERMINAL_ERROR,
                dedupe_key="one",
                title="Failure",
                body="Something failed",
                now=now,
            )
            with patch(
                "inky_bird_frame.notifications._deliver",
                side_effect=[None, RuntimeError("down")],
            ) as deliver:
                first = dispatch_notifications(config, now=now)
            with patch("inky_bird_frame.notifications._deliver") as retry:
                second = dispatch_notifications(config, now=now + timedelta(minutes=5))
            duplicate = enqueue_notification(
                config,
                NotificationEvent.TERMINAL_ERROR,
                dedupe_key="one",
                title="Failure",
                body="Something failed",
                now=now,
            )

        self.assertEqual(deliver.call_count, 2)
        self.assertEqual(first["pending"], 1)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(retry.call_args.args[0].name, "second")
        self.assertEqual(second["pending"], 0)
        self.assertFalse(duplicate)

    def test_enqueue_during_delivery_is_preserved_for_next_dispatch(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            enqueue_notification(
                config,
                NotificationEvent.DEGRADED,
                dedupe_key="first",
                title="First",
                body="First",
                now=now,
            )

            def enqueue_another(*_args: object) -> None:
                enqueue_notification(
                    config,
                    NotificationEvent.DEGRADED,
                    dedupe_key="second",
                    title="Second",
                    body="Second",
                    now=now,
                )

            with patch("inky_bird_frame.notifications._deliver", side_effect=enqueue_another):
                result = dispatch_notifications(config, now=now)
            status = notification_status(config)

        self.assertEqual(result["delivered"], 1)
        self.assertEqual(status["pending"], 1)

    def test_dead_letters_can_be_requeued_after_provider_recovery(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            enqueue_notification(
                config,
                NotificationEvent.DEGRADED,
                dedupe_key="dead-letter",
                title="Down",
                body="Down",
                now=now,
            )
            with patch("inky_bird_frame.notifications._deliver", side_effect=RuntimeError("down")):
                for offset in (0, 5, 15):
                    dispatch_notifications(config, now=now + timedelta(minutes=offset))
            before = notification_status(config)
            requeued = requeue_dead_letters(config, now=now + timedelta(minutes=20))
            with patch("inky_bird_frame.notifications._deliver"):
                dispatch_notifications(config, now=now + timedelta(minutes=20))
            after = notification_status(config)

        self.assertEqual(before["dead_letters"], 1)
        self.assertEqual(requeued, 1)
        self.assertEqual(after["dead_letters"], 0)
        self.assertEqual(after["pending"], 0)

    def test_degradation_threshold_and_recovery_are_deduplicated(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                first = record_degradation(
                    config, key="refresh", title="down", body="down", now=now
                )
                second = record_degradation(
                    config,
                    key="refresh",
                    title="down",
                    body="down",
                    now=now + timedelta(minutes=5),
                )
                record_recovery(config, key="refresh", title="up", body="up")
                repeated = record_recovery(config, key="refresh", title="up", body="up")

        self.assertFalse(first["queued"])
        self.assertEqual(second, notify.return_value)
        self.assertEqual(notify.call_count, 2)
        self.assertFalse(repeated["queued"])

    def test_failed_degradation_enqueue_remains_eligible_for_notice(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": False, "failed": 1},
            ) as notify:
                record_degradation(config, key="refresh", title="down", body="down", now=now)
                record_degradation(
                    config,
                    key="refresh",
                    title="down",
                    body="down",
                    now=now + timedelta(minutes=5),
                )
                record_degradation(
                    config,
                    key="refresh",
                    title="down",
                    body="down",
                    now=now + timedelta(minutes=10),
                )
            health = json.loads(
                (config.controller.state_dir / "notification-health.json").read_text()
            )["services"]["refresh"]

        self.assertEqual(notify.call_count, 2)
        self.assertFalse(health["notified"])
        self.assertIsNone(health["last_notice"])

    def test_failed_recovery_enqueue_preserves_health_for_retry(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True, "failed": 0},
            ):
                record_degradation(config, key="refresh", title="down", body="down", now=now)
                record_degradation(
                    config,
                    key="refresh",
                    title="down",
                    body="down",
                    now=now + timedelta(minutes=5),
                )
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": False, "failed": 1},
            ):
                failed = record_recovery(config, key="refresh", title="up", body="up")
            retained = json.loads(
                (config.controller.state_dir / "notification-health.json").read_text()
            )["services"]
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True, "failed": 0},
            ):
                retried = record_recovery(config, key="refresh", title="up", body="up")
            cleared = json.loads(
                (config.controller.state_dir / "notification-health.json").read_text()
            )["services"]

        self.assertEqual(failed["failed"], 1)
        self.assertIn("refresh", retained)
        self.assertTrue(retried["queued"])
        self.assertNotIn("refresh", cleared)

    def test_distinct_incidents_send_distinct_recovery_notifications(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                for incident in range(2):
                    record_degradation(
                        config,
                        key="refresh",
                        title="down",
                        body="down",
                        now=now + timedelta(hours=incident, minutes=1),
                    )
                    record_degradation(
                        config,
                        key="refresh",
                        title="down",
                        body="down",
                        now=now + timedelta(hours=incident, minutes=2),
                    )
                    record_recovery(config, key="refresh", title="up", body="up")

        recovery_keys = [
            call.kwargs["dedupe_key"]
            for call in notify.call_args_list
            if call.args[1] is NotificationEvent.RECOVERED
        ]
        self.assertEqual(len(recovery_keys), 2)
        self.assertEqual(len(set(recovery_keys)), 2)


class DisplayHeartbeatTests(unittest.TestCase):
    def _config(self, temporary: str, *, rotation_minutes: int = 30) -> AppConfig:
        path = Path(temporary) / "config.toml"
        path.write_text(DISPLAY_CONFIG.format(rotation_minutes=rotation_minutes))
        return load_config(path)

    def _write_heartbeat(
        self, config: AppConfig, fetched_at: datetime, *, route: str | None = None
    ) -> None:
        """Write a fetch record; ``route=None`` is the format older controllers wrote."""

        record: dict[str, object] = {"schema_version": 1, "fetched_at": fetched_at.isoformat()}
        if route is not None:
            record["route"] = route
        config.controller.state_dir.mkdir(parents=True, exist_ok=True)
        (config.controller.state_dir / "display-last-fetch.json").write_text(json.dumps(record))

    def _write_wall(self, config: AppConfig, *, members: bool) -> None:
        """Approve one synthetic plate; ``members`` puts it in the default wall view."""

        image = b"synthetic plate"
        species = config.controller.catalog_dir / "species/1-example-bird"
        species.mkdir(parents=True, exist_ok=True)
        for kind in ("portrait", "display"):
            (species / f"{kind}.png").write_bytes(image)
        entry = {
            "taxon_id": 1,
            "common_name": "Example Bird",
            "scientific_name": "Avis exemplum",
            "slug": "example-bird",
            "portrait_path": "species/1-example-bird/portrait.png",
            "display_path": "species/1-example-bird/display.png",
            "portrait_sha256": hashlib.sha256(image).hexdigest(),
            "display_sha256": hashlib.sha256(image).hexdigest(),
            "approved_at": "2026-07-01T00:00:00+00:00",
        }
        catalog = {"schema_version": 1, "generated_at": "2026-07-01T00:00:00+00:00"}
        config.controller.state_dir.mkdir(parents=True, exist_ok=True)
        (config.controller.catalog_dir / "index.json").write_text(
            json.dumps({**catalog, "species": [entry]})
        )
        (config.controller.state_dir / "active-catalog.json").write_text(
            json.dumps({**catalog, "species": [entry] if members else []})
        )

    def _write_success(self, config: AppConfig, succeeded_at: datetime) -> None:
        config.controller.state_dir.mkdir(parents=True, exist_ok=True)
        (config.controller.state_dir / "display-last-success.json").write_text(
            json.dumps({"schema_version": 1, "succeeded_at": succeeded_at.isoformat()})
        )

    def test_stale_display_updates_alert_despite_fresh_fetches(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=True)
            self._write_heartbeat(config, now - timedelta(minutes=5))
            self._write_success(config, now - timedelta(minutes=200))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["stale"])
        self.assertEqual(result["signal"], "display-update")
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_fresh_display_update_overrides_stale_fetch_signal(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=200))
            self._write_success(config, now - timedelta(minutes=10))
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertFalse(result["stale"])
        self.assertEqual(result["signal"], "display-update")
        notify.assert_not_called()

    def test_corrupt_success_file_treats_display_as_never_succeeded(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=True)
            self._write_heartbeat(config, now - timedelta(minutes=10))
            config.controller.state_dir.mkdir(parents=True, exist_ok=True)
            (config.controller.state_dir / "display-last-success.json").write_bytes(
                b'{"schema_version": 1, "succeeded_at": "2026-07-10T\xff'
            )
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["stale"])
        self.assertEqual(result["signal"], "catalog-fetch")
        self.assertIn("warning", result)
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_non_utf8_heartbeat_is_reported_not_fatal(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            config.controller.state_dir.mkdir(parents=True, exist_ok=True)
            (config.controller.state_dir / "display-last-fetch.json").write_bytes(
                b'{"schema_version": 1, "fetched_at": "2026-\xff\xfe'
            )
            result = check_display_heartbeat(config, now=now)

        self.assertFalse(result["checked"])
        self.assertIn("warning", result)

    def test_fresh_heartbeat_does_not_alert(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=10))
            self._write_success(config, now - timedelta(minutes=10))
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["checked"])
        self.assertFalse(result["stale"])
        notify.assert_not_called()

    def test_fetching_without_any_completed_update_alerts(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=True)
            self._write_heartbeat(config, now - timedelta(minutes=5))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["stale"])
        self.assertEqual(result["signal"], "catalog-fetch")
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)
        self.assertIn("never completed an update", notify.call_args.kwargs["body"])

    def test_stale_heartbeat_alerts_once_across_dispatch_runs(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=200))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                first = check_display_heartbeat(config, now=now)
                second = check_display_heartbeat(config, now=now + timedelta(minutes=5))

        self.assertTrue(first["stale"])
        self.assertTrue(second["stale"])
        self.assertEqual(notify.call_count, 1)
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_stale_alert_enqueues_display_stale_event(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=200))
            check_display_heartbeat(config, now=now)
            check_display_heartbeat(config, now=now + timedelta(minutes=5))
            status = notification_status(config)
            state = json.loads((config.controller.state_dir / "notifications.json").read_text())

        self.assertEqual(status["pending"], 1)
        self.assertEqual(state["pending"][0]["event"], "display_stale")

    def test_recovery_after_stale_alert_notifies_once(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_heartbeat(config, now - timedelta(minutes=200))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                check_display_heartbeat(config, now=now)
                self._write_success(config, now + timedelta(minutes=10))
                recovered = check_display_heartbeat(config, now=now + timedelta(minutes=15))
                repeated = check_display_heartbeat(config, now=now + timedelta(minutes=20))

        self.assertFalse(recovered["stale"])
        self.assertFalse(repeated["stale"])
        events = [call.args[1] for call in notify.call_args_list]
        self.assertEqual(
            events, [NotificationEvent.DISPLAY_STALE, NotificationEvent.DISPLAY_RECOVERED]
        )

    def test_fresh_fetch_of_an_empty_wall_view_is_paused_not_stale(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=False)
            self._write_heartbeat(config, now - timedelta(minutes=5), route="wall")
            self._write_success(config, now - timedelta(minutes=200))
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)
            health = config.controller.state_dir / "notification-health.json"
            recorded = health.exists()

        self.assertTrue(result["checked"])
        self.assertFalse(result["stale"])
        self.assertTrue(result["paused"])
        self.assertEqual(result["signal"], "catalog-fetch")
        self.assertEqual(result["seen_at"], (now - timedelta(minutes=5)).isoformat())
        self.assertFalse(recorded)
        notify.assert_not_called()

    def test_empty_wall_view_with_a_display_that_stopped_fetching_is_stale(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=False)
            self._write_heartbeat(config, now - timedelta(minutes=200), route="wall")
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["stale"])
        self.assertFalse(result["paused"])
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_paused_wall_then_resumed_updates_recover_once(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=False)
            self._write_heartbeat(config, now - timedelta(minutes=200))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                dead = check_display_heartbeat(config, now=now)
                self._write_heartbeat(config, now + timedelta(minutes=5), route="wall")
                paused = check_display_heartbeat(config, now=now + timedelta(minutes=10))
                self._write_wall(config, members=True)
                self._write_success(config, now + timedelta(minutes=15))
                recovered = check_display_heartbeat(config, now=now + timedelta(minutes=20))

        self.assertTrue(dead["stale"])
        self.assertTrue(paused["paused"])
        self.assertFalse(paused["stale"])
        self.assertFalse(recovered["stale"])
        self.assertFalse(recovered["paused"])
        events = [call.args[1] for call in notify.call_args_list]
        self.assertEqual(
            events, [NotificationEvent.DISPLAY_STALE, NotificationEvent.DISPLAY_RECOVERED]
        )

    def test_unreadable_wall_selection_keeps_the_stale_alert(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=False)
            (config.controller.state_dir / "presentation-access.json").write_text("not json")
            self._write_heartbeat(config, now - timedelta(minutes=5), route="wall")
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["stale"])
        self.assertFalse(result["paused"])
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_empty_wall_view_does_not_pause_a_display_on_the_catalog_route(self) -> None:
        # Displays on /v1/catalog never pause for the wall view, so a fresh
        # fetch with a stale panel is still a failed update there.
        for route in ("catalog", None, "unexpected"):
            with self.subTest(route=route):
                now = datetime(2026, 7, 10, tzinfo=UTC)
                with TemporaryDirectory() as temporary:
                    config = self._config(temporary)
                    self._write_wall(config, members=False)
                    self._write_heartbeat(config, now - timedelta(minutes=5), route=route)
                    self._write_success(config, now - timedelta(minutes=200))
                    with patch(
                        "inky_bird_frame.notifications.safe_notify",
                        return_value={"queued": True},
                    ) as notify:
                        result = check_display_heartbeat(config, now=now)

                self.assertTrue(result["stale"])
                self.assertFalse(result["paused"])
                self.assertEqual(result["signal"], "display-update")
                self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_wall_route_fetch_with_a_populated_wall_view_is_stale(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=True)
            self._write_heartbeat(config, now - timedelta(minutes=5), route="wall")
            self._write_success(config, now - timedelta(minutes=200))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["stale"])
        self.assertFalse(result["paused"])
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_missing_heartbeat_stays_silent(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertEqual(result, {"checked": False, "stale": None})
        notify.assert_not_called()

    def test_corrupt_heartbeat_is_no_signal_with_warning(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            config.controller.state_dir.mkdir(parents=True, exist_ok=True)
            (config.controller.state_dir / "display-last-fetch.json").write_text("not json")
            with patch("inky_bird_frame.notifications.safe_notify") as notify:
                result = check_display_heartbeat(config, now=now)

        self.assertFalse(result["checked"])
        self.assertIn("Invalid display heartbeat", str(result["warning"]))
        notify.assert_not_called()

    def test_threshold_respects_sixty_minute_floor(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary, rotation_minutes=5)
            self._write_success(config, now - timedelta(minutes=50))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                within_floor = check_display_heartbeat(config, now=now)
                self._write_success(config, now - timedelta(minutes=61))
                past_floor = check_display_heartbeat(config, now=now)

        self.assertEqual(within_floor["threshold_minutes"], 60)
        self.assertFalse(within_floor["stale"])
        self.assertTrue(past_floor["stale"])
        self.assertEqual(notify.call_count, 1)

    def test_old_update_right_after_the_controller_starts_is_not_stale(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=True)
            self._write_success(config, now - timedelta(minutes=10))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                before = check_display_heartbeat(config, now=now)
                # The controller is off for four hours, so the display cannot report.
                back = now + timedelta(hours=4)
                record_controller_start(config, now=back)
                checks = [
                    check_display_heartbeat(config, now=back + timedelta(minutes=minutes))
                    for minutes in (0, 5, 10, 15, 20, 25)
                ]
                stale_alerts = notify.call_count
                # The display reports again within a rotation.
                self._write_success(config, back + timedelta(minutes=27))
                resumed = check_display_heartbeat(config, now=back + timedelta(minutes=30))

        self.assertFalse(before["stale"])
        self.assertEqual([check["stale"] for check in checks], [False] * 6)
        self.assertTrue(all(check["resuming"] for check in checks))
        self.assertEqual(stale_alerts, 0)
        self.assertFalse(resumed["stale"])
        self.assertEqual(notify.call_count, 0)

    def test_display_still_silent_after_the_grace_is_stale(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=True)
            self._write_success(config, now - timedelta(minutes=10))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                check_display_heartbeat(config, now=now)
                back = now + timedelta(hours=4)
                record_controller_start(config, now=back)
                check_display_heartbeat(config, now=back)
                check_display_heartbeat(config, now=back + timedelta(minutes=20))
                late = check_display_heartbeat(config, now=back + timedelta(minutes=40))

        self.assertTrue(late["stale"])
        self.assertFalse(late["resuming"])
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_a_long_gap_between_checks_alone_gives_no_grace(self) -> None:
        # A long job can hold up the checks while the controller keeps serving.
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=True)
            record_controller_start(config, now=now - timedelta(days=1))
            self._write_success(config, now - timedelta(minutes=10))
            with patch(
                "inky_bird_frame.notifications.safe_notify",
                return_value={"queued": True},
            ) as notify:
                check_display_heartbeat(config, now=now)
                late = check_display_heartbeat(config, now=now + timedelta(hours=4))

        self.assertTrue(late["stale"])
        self.assertFalse(late["resuming"])
        self.assertIs(notify.call_args.args[1], NotificationEvent.DISPLAY_STALE)

    def test_unreadable_start_record_gives_no_grace(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=True)
            self._write_success(config, now - timedelta(hours=5))
            controller_started_path(config).write_text("{not json")
            result = check_display_heartbeat(config, now=now)

        self.assertTrue(result["stale"])
        self.assertFalse(result["resuming"])

    def test_regular_checks_keep_reporting_a_stale_display(self) -> None:
        now = datetime(2026, 7, 10, tzinfo=UTC)
        with TemporaryDirectory() as temporary:
            config = self._config(temporary)
            self._write_wall(config, members=True)
            self._write_success(config, now - timedelta(hours=5))
            results = [
                check_display_heartbeat(config, now=now + timedelta(minutes=minutes))
                for minutes in (0, 5, 10)
            ]

        self.assertEqual([result["stale"] for result in results], [True, True, True])


class NotificationFailureLogTests(unittest.TestCase):
    def test_failed_queueing_is_logged_without_details(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG)
            config = load_config(path)
            with (
                patch(
                    "inky_bird_frame.notifications.enqueue_notification",
                    side_effect=OSError("/private/state/notifications.json"),
                ),
                self.assertLogs("inky_bird_frame.notifications", "WARNING") as logs,
            ):
                result = safe_notify(
                    config,
                    NotificationEvent.TERMINAL_ERROR,
                    dedupe_key="example",
                    title="Example",
                    body="Example",
                )

        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("terminal_error", logs.output[0])
        self.assertIn("OSError", logs.output[0])
        self.assertNotIn("/private", logs.output[0])

    def test_failed_health_records_are_logged(self) -> None:
        from inky_bird_frame.notifications import safe_record_degradation, safe_record_recovery

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(CONFIG)
            config = load_config(path)
            failure = OSError("disk full")
            with (
                patch("inky_bird_frame.notifications.record_degradation", side_effect=failure),
                patch("inky_bird_frame.notifications.record_recovery", side_effect=failure),
                self.assertLogs("inky_bird_frame.notifications", "WARNING") as logs,
            ):
                safe_record_degradation(config, key="refresh", title="Down", body="Down")
                safe_record_recovery(config, key="refresh", title="Up", body="Up")

        self.assertEqual(len(logs.output), 2)
        self.assertTrue(all("refresh" in line and "OSError" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
