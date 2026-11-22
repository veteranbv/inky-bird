from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from PIL import Image

from inky_bird_frame.config import DiscoveryProvider, parse_config
from inky_bird_frame.errors import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]
FENCED_BLOCK = re.compile(r"^\s*```.*?^\s*```\s*$", re.MULTILINE | re.DOTALL)
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\s*\)")
HTML_LINK = re.compile(r"\b(?:href|src|srcset)\s*=\s*[\"'](?P<target>[^\"']+)[\"']")
REFERENCE_LINK = re.compile(r"^\s*\[[^\]]+\]:\s*(?P<target>\S+)", re.MULTILINE)
UI_TEXT = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"|`([^`]*)`|>([^<>]+)<")
HEADING = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*#*\s*$", re.MULTILINE)


def _markdown_files() -> list[Path]:
    root_files = sorted(ROOT.glob("*.md"))
    return root_files + sorted((ROOT / "docs").rglob("*.md"))


def _without_fenced_code(text: str) -> str:
    return FENCED_BLOCK.sub("", text)


def _targets(text: str) -> list[str]:
    content = _without_fenced_code(text)
    targets = [match.group("target") for match in MARKDOWN_LINK.finditer(content)]
    targets.extend(match.group("target") for match in HTML_LINK.finditer(content))
    targets.extend(match.group("target") for match in REFERENCE_LINK.finditer(content))
    return targets


def _slug(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("`", "").strip().lower()
    value = re.sub(r"[^\w\- ]", "", value)
    return value.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    content = _without_fenced_code(path.read_text(encoding="utf-8"))
    occurrences: defaultdict[str, int] = defaultdict(int)
    anchors: set[str] = set()
    for match in HEADING.finditer(content):
        base = _slug(match.group("title"))
        index = occurrences[base]
        occurrences[base] += 1
        anchors.add(base if index == 0 else f"{base}-{index}")
    return anchors


@pytest.mark.repository_catalog
def test_internal_documentation_links_and_anchors_resolve() -> None:
    problems: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}

    for source in _markdown_files():
        for raw_target in _targets(source.read_text(encoding="utf-8")):
            target = raw_target.removeprefix("<").removesuffix(">")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                continue

            target_path = source if not parsed.path else source.parent / unquote(parsed.path)
            target_path = target_path.resolve()
            try:
                target_path.relative_to(ROOT)
            except ValueError:
                problems.append(f"{source.relative_to(ROOT)}: link escapes repository: {target}")
                continue

            if not target_path.exists():
                problems.append(f"{source.relative_to(ROOT)}: missing target: {target}")
                continue

            if parsed.fragment and target_path.suffix.lower() == ".md":
                anchors = anchor_cache.setdefault(target_path, _anchors(target_path))
                fragment = unquote(parsed.fragment).lower()
                if fragment not in anchors:
                    problems.append(
                        f"{source.relative_to(ROOT)}: missing anchor #{fragment} in "
                        f"{target_path.relative_to(ROOT)}"
                    )

    assert not problems, "\n" + "\n".join(problems)


def test_installation_native_checks_use_the_managed_runtime() -> None:
    guide = (ROOT / "docs/installation.md").read_text(encoding="utf-8")
    section = guide.split("### Verify controller data", 1)[1].split("\n## ", 1)[0]

    assert 'IBF="$HOME/Services/inky-bird/.venv/bin/inky-bird"' in section
    assert re.search(r"^inky-bird-frame(?:\s|$)", section, re.MULTILINE) is None


def test_canonical_repository_links_and_clone_directory_agree() -> None:
    canonical = "https://github.com/veteranbv/inky-bird"
    contribution = (ROOT / "CONTRIBUTING.md").read_text()
    assert f"git clone {canonical}.git\ncd inky-bird\n" in contribution
    for path in ("README.md", "pyproject.toml", "src/inky_bird_frame/web/manage.js"):
        text = (ROOT / path).read_text()
        assert canonical in text
        assert canonical + "-frame" not in text


def test_status_labels_distinguish_rotation_from_the_approved_library() -> None:
    guide = (ROOT / "docs/presentation.md").read_text(encoding="utf-8")
    interface = (ROOT / "src/inky_bird_frame/web/presentation.js").read_text(encoding="utf-8")

    for text in (guide, interface):
        assert "Inky plates in rotation" in text
        assert "label: In rotation" in text
        assert "label: Approved" in text
        assert "data.view_ready_count" in text
        assert "data.approved_count" in text
        assert "Inky ready plates" not in text
    assert "Other approved plates remain in the library" in guide
    assert "without changing the entity ID or unique ID" in guide


def test_walkthrough_has_accessible_fallbacks_and_honest_scope() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    transcript = (ROOT / "docs/walkthrough.md").read_text(encoding="utf-8")

    assert "https://github.com/user-attachments/assets/" in readme
    assert "[Text transcript](docs/walkthrough.md)" in readme
    assert "[Still preview](docs/images/inky-demo-poster.jpg)" in readme
    assert "browser Play does not refresh the wall" in readme
    assert "automatic browser playback remains once a minute" in transcript
    assert "does not trigger or synchronize that physical display" in transcript
    assert "not an interactive controller demo" in transcript
    assert ".gif" not in readme


def test_readme_screenshots_are_centered_and_sources_are_prominent() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert '<table align="center">' in readme
    assert readme.index("## Supported sources") < readme.index("## Start with what you have")
    assert "[Supported sources](#supported-sources)" in readme
    sources = readme.split("## Supported sources", 1)[1].split("\n## ", 1)[0]
    for name in (
        "iNaturalist",
        "eBird",
        "eBird Archive",
        "BirdWeather",
        "BirdNET-Go",
        "BirdNET Analyzer",
        "Bird Buddy",
    ):
        assert f"[{name}]" in sources
    for caveat in (
        "no provider account",
        "not continuous account sync",
        "private API",
        "does not record sound",
        "not a claim that you personally saw a bird",
        "not published with the reusable artwork catalog",
    ):
        assert caveat in sources.replace("\n", " ")


def test_cinematic_walkthrough_opens_and_ends_without_blank_fades() -> None:
    renderer = (ROOT / "tests/web/render-walkthrough.cjs").read_text(encoding="utf-8")
    cinematic = renderer.split("async function renderCinematic()", 1)[1]
    assert "fade=t=in" not in cinematic
    assert "fade=t=out" not in cinematic
    assert "[3:v]trim=duration=2.9" in cinematic
    assert "'v2-title.png', 'v2-end.png'" in cinematic
    assert "'-map', '[cap3]'" in cinematic
    assert "verifyOpeningFrame(ffmpeg" in cinematic


def test_historical_homepage_images_are_not_presented_as_current() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for name, expected_size in (
        ("homepage-dashboard-compact.png", (900, 300)),
        ("homepage-dashboard-featured-plate.png", (900, 920)),
    ):
        path = ROOT / "docs/images" / name
        assert name not in readme
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            assert image.size == expected_size
            assert image.mode == "RGB"
            assert image.info == {}
            assert not image.getexif()


def test_journal_screenshots_are_generic_demo_images_without_metadata() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "public plates" in readme
    assert "a sample collection" in readme
    for name, size in (
        ("journal-home.png", (430, 1200)),
        ("journal-collection.png", (430, 1200)),
    ):
        assert name in readme
        with Image.open(ROOT / "docs/images" / name) as image:
            image.verify()
        with Image.open(ROOT / "docs/images" / name) as image:
            assert image.size == size
            assert image.info == {}
            assert not image.getexif()


def test_plate_problem_form_requires_supporting_sources() -> None:
    form = (ROOT / ".github/ISSUE_TEMPLATE/plate_problem.yml").read_text(encoding="utf-8")
    evidence = form.split("    id: evidence", 1)[1].split("\n  - type:", 1)[0]

    assert "    validations:\n      required: true" in evidence


def test_private_history_removal_documents_retained_state_and_refresh() -> None:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    section = guide.split("## Private-data lifecycle and removal", 1)[1]

    for retained_path in (
        "generation-queue.json",
        "collection.json",
        "generation-retries.json",
        "runs/",
        "workspace_dir",
    ):
        assert retained_path in section
    assert '"$IBF" refresh --config "$CONFIG"' in section
    assert (
        "docker compose run --rm --no-deps scheduler refresh --config /data/config.toml" in section
    )
    assert "clearing all controller observation and generation state" in section
    for retained_store in ("config.toml", "GitHub authentication", "service logs", "backups"):
        assert retained_store in section


FAKE_SYSTEMCTL = r"""#!/usr/bin/env bash
# Synthetic systemctl: "$FAKE/units" holds "<unit> <state>" for installed units.
units="$FAKE/units"
state_of() { awk -v unit="$1" '$1 == unit { print $2 }' "$units"; }
set_state() {
  awk -v unit="$1" -v state="$2" '$1 == unit { $2 = state } { print }' "$units" > "$units.new"
  mv "$units.new" "$units"
}
case "$1" in
  show)
    if [ -n "$(state_of "$4")" ]; then echo loaded; else echo not-found; fi ;;
  is-active)
    state=$(state_of "$2")
    echo "${state:-inactive}"
    [ "$state" = active ] ;;
  stop | start)
    if [ -z "$(state_of "$2")" ]; then
      echo "Unit $2 not loaded." >&2
      exit 5
    fi
    echo "$1 $2" >> "$FAKE/log"
    if [ "$1" = stop ]; then set_state "$2" inactive; else set_state "$2" active; fi ;;
  *) echo "unexpected systemctl call: $*" >&2; exit 64 ;;
esac
"""

FAKE_LAUNCHCTL = r"""#!/usr/bin/env bash
# Synthetic launchctl: "$FAKE/loaded" lists loaded labels.
touch "$FAKE/loaded"
case "$1" in
  print)
    grep -Fxq "${2##*/}" "$FAKE/loaded" || exit 113
    if grep -Fxq "${2##*/}" "$FAKE/running" 2>/dev/null; then echo "state = running"; fi ;;
  bootout)
    label=${2##*/}
    grep -Fxq "$label" "$FAKE/loaded" || exit 3
    grep -Fxv "$label" "$FAKE/loaded" > "$FAKE/loaded.new" || true
    mv "$FAKE/loaded.new" "$FAKE/loaded"
    echo "bootout $label" >> "$FAKE/log" ;;
  bootstrap)
    label=$(basename "$3" .plist)
    echo "$label" >> "$FAKE/loaded"
    echo "bootstrap $label" >> "$FAKE/log" ;;
  *) echo "unexpected launchctl call: $*" >&2; exit 64 ;;
esac
"""

FAKE_SUDO = '#!/usr/bin/env bash\nexec "$@"\n'

FAKE_DOCKER = r"""#!/usr/bin/env bash
# Synthetic docker: "$FAKE/running", "$FAKE/containers" and "$FAKE/volumes" list names.
echo "docker $*" >> "$FAKE/log"
[ "$1" = compose ] && shift
case "$*" in
  *"ps --status running --services"*) cat "$FAKE/running" 2>/dev/null ;;
  *"ps --all --quiet"*) cat "$FAKE/containers" 2>/dev/null ;;
  "stop "*) : > "$FAKE/running" ;;
  "volume ls"*) cat "$FAKE/volumes" 2>/dev/null ;;
  "volume inspect "*) grep -Fxq "$3" "$FAKE/volumes" 2>/dev/null ;;
esac
"""


def _bash_blocks(section: str) -> list[str]:
    blocks = re.findall(r"^( *)```bash\n(.*?)^\1```$", section, re.MULTILINE | re.DOTALL)
    return [
        "".join(line.removeprefix(indent) for line in block.splitlines(keepends=True))
        for indent, block in blocks
    ]


class _NativeSteps:
    """The numbered backup steps for each service manager, in the order the guide gives them."""

    def __init__(self) -> None:
        guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
        backup = guide.split("## Native controller backup", 1)[1].split(
            "\n## Native controller restore", 1
        )[0]
        quiesce = backup.split("### 3. Quiesce scheduled work", 1)[1].split("\n### 4.", 1)[0]
        restart = backup.split("### 5. Restart and verify", 1)[1]
        self.systemd_stop = self._platform(quiesce, "systemd Linux")
        self.launchd_stop = self._platform(quiesce, "macOS")
        self.systemd_start = self._platform(restart, "systemd Linux")
        self.launchd_start = self._platform(restart, "macOS")

    @staticmethod
    def _platform(section: str, name: str) -> list[str]:
        blocks = _bash_blocks(section.split(f"#### On {name}", 1)[1].split("\n#### ", 1)[0])
        assert blocks, name
        return blocks

    @staticmethod
    def step(blocks: list[str], command: str) -> str:
        return next(block for block in blocks if command in block)


class _NativeHost:
    """Run documented snippets against synthetic service managers, never the real ones."""

    def __init__(self, root: Path, family: str) -> None:
        self.fake = root / "fake"
        self.home = root / "home"
        self.backup = root / "backup"
        for directory in (self.fake / "bin", self.home, self.backup):
            directory.mkdir(parents=True)
        for name, script in (
            ("systemctl", FAKE_SYSTEMCTL),
            ("launchctl", FAKE_LAUNCHCTL),
            ("sudo", FAKE_SUDO),
            ("docker", FAKE_DOCKER),
        ):
            path = self.fake / "bin" / name
            path.write_text(script)
            path.chmod(0o755)
        (self.fake / "log").write_text("")
        self.family = family

    def install_units(self, **states: str) -> None:
        lines = [f"{self.family}-{name} {state}\n" for name, state in states.items()]
        (self.fake / "units").write_text("".join(lines))

    def install_agents(self, *labels: str, loaded: tuple[str, ...]) -> None:
        agents = self.home / "Library/LaunchAgents"
        agents.mkdir(parents=True)
        for label in labels:
            (agents / f"com.{self.family}.{label}.plist").write_text("synthetic")
        (self.fake / "loaded").write_text(
            "".join(f"com.{self.family}.{label}\n" for label in loaded)
        )

    def run(
        self, block: str, *, family: str | None = None, **variables: str
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.fake / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "FAKE": str(self.fake),
            "BACKUP": str(self.backup),
            "FAMILY": family or self.family,
            **variables,
        }
        return subprocess.run(
            ["bash", "-c", f"{block}\nprintf 'block-status=%s\\n' \"$?\""],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
            timeout=30,
        )

    def run_steps(
        self, blocks: list[str], *, family: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        # An operator runs the steps in order and stops at the first one that fails.
        assert blocks
        for block in blocks:
            result = self.run(block, family=family)
            if self.status(result) != 0:
                break
        return result

    def paste(
        self, blocks: list[str], *, family: str | None = None, **variables: str
    ) -> subprocess.CompletedProcess[str]:
        # An operator who pastes a whole section into one shell runs every block,
        # including the ones after a check that failed.
        assert blocks
        return self.run("\n".join(blocks), family=family, **variables)

    @staticmethod
    def status(result: subprocess.CompletedProcess[str]) -> int:
        # The shell that pasted the block must survive to report its status.
        match = re.search(r"^block-status=(\d+)$", result.stdout, re.MULTILINE)
        assert match, result.stdout
        return int(match.group(1))

    def calls(self) -> list[str]:
        return (self.fake / "log").read_text().splitlines()

    def recorded(self, name: str) -> list[str]:
        return (self.backup / name).read_text().splitlines()


BASH = pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")


@BASH
@pytest.mark.parametrize("family", ["inky-bird", "inky-bird-frame"])
def test_native_backup_quiesces_and_restores_an_app_managed_systemd_install(
    tmp_path: Path, family: str
) -> None:
    steps = _NativeSteps()
    host = _NativeHost(tmp_path, family)
    host.install_units(**{"controller.service": "active", "scheduler.service": "active"})

    result = host.run_steps(steps.systemd_stop)
    assert host.status(result) == 0, result.stderr

    assert host.recorded("native-active-systemd-units.txt") == [
        f"{family}-controller.service",
        f"{family}-scheduler.service",
    ]
    assert host.calls() == [
        f"stop {family}-scheduler.service",
        f"stop {family}-controller.service",
    ]

    result = host.run_steps(steps.systemd_start)
    assert host.status(result) == 0, result.stderr
    assert host.calls()[2:] == [
        f"start {family}-controller.service",
        f"start {family}-scheduler.service",
    ]


@BASH
def test_native_backup_stops_timers_before_the_controller_and_skips_missing_units(
    tmp_path: Path,
) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_units(
        **{
            "controller.service": "active",
            "refresh.timer": "active",
            "refresh.service": "inactive",
            "generate.timer": "inactive",
            "generate.service": "inactive",
        }
    )

    result = host.run_steps(_NativeSteps().systemd_stop)
    assert host.status(result) == 0, result.stderr

    assert host.recorded("native-active-systemd-units.txt") == [
        "inky-bird-controller.service",
        "inky-bird-refresh.timer",
    ]
    assert host.calls() == ["stop inky-bird-refresh.timer", "stop inky-bird-controller.service"]


@BASH
def test_native_backup_refuses_a_family_that_is_not_installed(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_units(**{"controller.service": "active"})

    result = host.run_steps(_NativeSteps().systemd_stop, family="inky-bird-frame")

    assert host.status(result) != 0
    assert "No installed inky-bird-frame-controller.service" in result.stderr
    assert host.calls() == []


@BASH
def test_native_backup_leaves_services_running_while_a_job_is_active(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_units(
        **{
            "controller.service": "active",
            "generate.timer": "active",
            "generate.service": "active",
        }
    )

    result = host.run_steps(_NativeSteps().systemd_stop)

    assert host.status(result) != 0
    assert "A job is still running" in result.stderr
    assert "inky-bird-generate.service" in result.stderr
    assert host.calls() == ["stop inky-bird-generate.timer"]


@BASH
@pytest.mark.parametrize("family", ["inky-bird", "inky-bird-frame"])
def test_native_backup_quiesces_and_restores_app_managed_launch_agents(
    tmp_path: Path, family: str
) -> None:
    steps = _NativeSteps()
    host = _NativeHost(tmp_path, family)
    host.install_agents("serve", "scheduler", loaded=("serve", "scheduler"))

    result = host.run_steps(steps.launchd_stop)
    assert host.status(result) == 0, result.stderr

    assert host.recorded("native-loaded-launchagents.txt") == ["serve", "scheduler"]
    assert host.calls() == [f"bootout com.{family}.serve", f"bootout com.{family}.scheduler"]

    result = host.run_steps(steps.launchd_start)
    assert host.status(result) == 0, result.stderr
    assert host.calls()[2:] == [
        f"bootstrap com.{family}.serve",
        f"bootstrap com.{family}.scheduler",
    ]


@BASH
def test_native_backup_refuses_launch_agents_of_another_family(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_agents("serve", loaded=("serve",))

    result = host.run_steps(_NativeSteps().launchd_stop, family="inky-bird-frame")

    assert host.status(result) != 0
    assert "No installed com.inky-bird-frame.serve LaunchAgent" in result.stderr
    assert host.calls() == []


@BASH
def test_native_backup_bootout_stops_at_the_first_agent_that_fails(tmp_path: Path) -> None:
    steps = _NativeSteps()
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_agents("serve", "scheduler", loaded=("scheduler",))
    (host.backup / "native-loaded-launchagents.txt").write_text("serve\nscheduler\n")

    result = host.run(steps.step(steps.launchd_stop, "launchctl bootout"))

    assert host.status(result) != 0
    assert host.calls() == []


@BASH
def test_native_restart_refuses_a_foreign_unit_and_starts_nothing(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_units(**{"controller.service": "inactive", "other.service": "inactive"})
    (host.backup / "native-active-systemd-units.txt").write_text(
        "inky-bird-controller.service\ninky-bird-other.service\n"
    )

    result = host.run_steps(_NativeSteps().systemd_start)

    assert host.status(result) != 0
    assert "Unexpected unit in" in result.stderr
    assert host.calls() == []


@BASH
def test_native_restart_refuses_a_unit_without_the_family_prefix(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_units(**{"controller.service": "inactive"})
    (host.backup / "native-active-systemd-units.txt").write_text("controller.service\n")

    result = host.run_steps(_NativeSteps().systemd_start)

    assert host.status(result) != 0
    assert "Unexpected unit in" in result.stderr
    assert host.calls() == []


@BASH
def test_native_restart_refuses_a_foreign_label_and_bootstraps_nothing(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_agents("serve", "other", loaded=())
    (host.backup / "native-loaded-launchagents.txt").write_text("serve\nother\n")

    result = host.run_steps(_NativeSteps().launchd_start)

    assert host.status(result) != 0
    assert "Unexpected label in" in result.stderr
    assert host.calls() == []


@BASH
def test_native_restart_refuses_a_missing_launch_agent_and_bootstraps_nothing(
    tmp_path: Path,
) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_agents("serve", loaded=())
    (host.backup / "native-loaded-launchagents.txt").write_text("serve\nscheduler\n")

    result = host.run_steps(_NativeSteps().launchd_start)

    assert host.status(result) != 0
    assert "Recorded LaunchAgent is missing" in result.stderr
    assert host.calls() == []


def _guide_part(start: str, end: str) -> str:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    return guide.split(start, 1)[1].split(end, 1)[0]


@BASH
def test_pasted_systemd_quiesce_stops_nothing_more_while_a_job_is_active(
    tmp_path: Path,
) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_units(
        **{
            "controller.service": "active",
            "generate.timer": "active",
            "generate.service": "active",
        }
    )

    result = host.paste(_NativeSteps().systemd_stop)

    assert host.status(result) != 0
    assert "A job is still running: inky-bird-generate.service" in result.stderr
    assert host.calls() == ["stop inky-bird-generate.timer"]


@BASH
def test_pasted_launch_agent_quiesce_unloads_nothing_while_a_job_is_active(
    tmp_path: Path,
) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_agents("serve", "generate", loaded=("serve", "generate"))
    (host.fake / "running").write_text("com.inky-bird.generate\n")

    result = host.paste(_NativeSteps().launchd_stop)

    assert host.status(result) != 0
    assert "A job is still running: com.inky-bird.generate" in result.stderr
    assert host.calls() == []


@BASH
def test_pasted_restart_starts_nothing_from_a_record_with_a_foreign_entry(
    tmp_path: Path,
) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_units(**{"controller.service": "inactive", "other.service": "inactive"})
    (host.backup / "native-active-systemd-units.txt").write_text(
        "inky-bird-controller.service\ninky-bird-other.service\n"
    )
    host.install_agents("serve", "other", loaded=())
    (host.backup / "native-loaded-launchagents.txt").write_text("serve\nother\n")

    for blocks in (_NativeSteps().systemd_start, _NativeSteps().launchd_start):
        result = host.paste(blocks)
        assert host.status(result) != 0
        assert "Unexpected " in result.stderr
    assert host.calls() == []


def _native_restore_blocks() -> list[str]:
    return _bash_blocks(_guide_part("3. Move any existing files", "4. Confirm that the controller"))


def _restore_paths(root: Path) -> dict[str, str]:
    return {
        "CONFIG": str(root / "live/config/config.toml"),
        "WORKSPACE": str(root / "live/workspace"),
        "CATALOG": str(root / "live/catalog"),
        "STATE": str(root / "live/state"),
    }


def _saved_backup(backup: Path) -> None:
    for part in ("config", "workspace", "catalog", "state"):
        (backup / part).mkdir()
    (backup / "config/config.toml").write_text("saved config\n")
    (backup / "state/journal.json").write_text("old\n")


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("rsync") is None, reason="requires bash and rsync"
)
def test_pasted_native_restore_copies_nothing_over_an_existing_path(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    _saved_backup(host.backup)
    paths = _restore_paths(tmp_path)
    live_journal = Path(paths["STATE"]) / "journal.json"
    live_journal.parent.mkdir(parents=True)
    live_journal.write_text("LIVE\n")

    result = host.paste(_native_restore_blocks(), **paths)

    assert host.status(result) != 0
    assert "Move this aside before restoring" in result.stderr
    assert live_journal.read_text() == "LIVE\n"
    assert not Path(paths["CONFIG"]).exists()


@pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("rsync") is None, reason="requires bash and rsync"
)
def test_pasted_native_restore_fills_empty_paths_privately(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    _saved_backup(host.backup)
    paths = _restore_paths(tmp_path)

    result = host.paste(_native_restore_blocks(), **paths)

    assert host.status(result) == 0, result.stderr
    assert (Path(paths["STATE"]) / "journal.json").read_text() == "old\n"
    assert Path(paths["CONFIG"]).read_text() == "saved config\n"
    assert Path(paths["CONFIG"]).stat().st_mode & 0o777 == 0o600


def _docker_blocks(start: str, end: str, backup: Path) -> list[str]:
    part = _guide_part(start, end).replace("/path/to/private-backup/inky-bird-docker", str(backup))
    return _bash_blocks(part)


def _docker_writes(host: _NativeHost) -> list[str]:
    return [call for call in host.calls() if re.search(r" (stop|create|start|up|run) ", call + " ")]


@BASH
def test_pasted_docker_stop_leaves_a_running_bootstrap_alone(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    (host.fake / "running").write_text("controller\nbootstrap\n")
    blocks = _docker_blocks("### 1. Stop writers", "\n### 2.", host.backup)

    result = host.paste(blocks)

    assert host.status(result) != 0
    assert "Bootstrap is still running" in result.stderr
    assert _docker_writes(host) == []


@BASH
def test_pasted_docker_stop_stops_the_writers_and_confirms_it(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    (host.fake / "running").write_text("controller\nscheduler\n")
    blocks = _docker_blocks("### 1. Stop writers", "\n### 2.", host.backup)

    result = host.paste(blocks)

    assert host.status(result) == 0, result.stderr
    assert _docker_writes(host) == ["docker compose stop scheduler controller bootstrap"]


@BASH
def test_pasted_docker_restore_creates_nothing_in_a_project_that_exists(tmp_path: Path) -> None:
    host = _NativeHost(tmp_path, "inky-bird")
    (host.fake / "volumes").write_text("inky-bird-frame-restore_controller-data\n")
    blocks = _docker_blocks(
        "2. Choose a restore project name", "3. Restore the controller archive", host.backup
    )

    result = host.paste(blocks)

    assert host.status(result) != 0
    assert "inky-bird-frame-restore" in result.stderr
    assert _docker_writes(host) == []


def test_backup_guide_gives_short_numbered_steps() -> None:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    blocks = _bash_blocks(guide)
    assert blocks
    longest = max(len(block.splitlines()) for block in blocks)
    assert longest <= 16, longest


def test_backup_job_check_names_only_the_separate_job_services() -> None:
    # An app-managed install runs every job inside the scheduler, which the
    # pasted check cannot see, so the step must send the owner to its log.
    guide = " ".join((ROOT / "docs/backup.md").read_text(encoding="utf-8").split())
    assert "first checks that no job" not in guide
    assert guide.count("This checks the separate job services") == 2
    assert guide.count("on an app-managed install, first check the scheduler log") == 2
    assert "and the scheduler log shows no unfinished job" in guide


def test_owner_guides_use_plain_grammar_and_wall_view_limits() -> None:
    def text(name: str) -> str:
        return " ".join((ROOT / name).read_text(encoding="utf-8").split())

    readme = text("README.md")
    assert "A plate Inky made that is waiting for you to look at it." in readme
    journal, operations = text("docs/journal.md"), text("docs/owner-operations.md")
    for guide in (journal, operations):
        assert "plates that **Needs a hand**" not in guide
        assert "at its next refresh while it shows **On my frame**" in guide
    assert "Start only what step 3 recorded." not in text("docs/backup.md")


def test_backup_snippets_that_can_fail_run_in_a_subshell() -> None:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    blocks = re.findall(r"^( *)```bash\n(.*?)^\1```$", guide, re.MULTILINE | re.DOTALL)
    checked = 0
    for indent, block in blocks:
        depth = 0
        for line in block.splitlines():
            stripped = line.removeprefix(indent).rstrip()
            if stripped == "(":
                depth += 1
            elif stripped == ")":
                depth -= 1
            elif re.search(r"\bexit\b", stripped):
                checked += 1
                assert depth > 0, f"exit outside a subshell would close the shell: {stripped}"
        assert depth == 0
    assert checked


def test_docker_backup_restores_only_the_previously_running_services() -> None:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    section = guide.split("## Docker backup", 1)[1].split("\n## Docker restore", 1)[0]

    capture = 'docker compose ps --status running --services > "$DOCKER_STATE"'
    stop = "docker compose stop scheduler controller bootstrap"
    restart = section.split("Validate the complete saved service set", 1)[1]
    validate = 'done < "$DOCKER_STATE"'
    controller_start = "docker compose start controller"
    scheduler_start = "docker compose start scheduler"
    assert section.index(capture) < section.index(stop)
    assert restart.index(validate) < restart.index(controller_start)
    assert section.index('STOPPED_STATE="$BACKUP/docker-running-after-stop.txt"') < section.index(
        "docker compose run --rm --no-deps -T --entrypoint tar controller"
    )
    assert "Bootstrap is still running; wait for it to exit before backup" in section
    assert "docker compose start bootstrap" not in section
    assert "docker compose up --detach" not in section
    assert 'if grep -Fxq controller "$DOCKER_STATE"; then' in section
    assert '"$HEALTH_URL" || exit 1' in section
    assert restart.index(controller_start) < restart.index(scheduler_start)
    assert "Compose writer is still running" in section
    assert "Recorded Compose service did not restart" in section


def test_sole_provider_removal_uses_the_empty_source_list_the_parser_accepts(
    tmp_path: Path,
) -> None:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    section = " ".join(guide.split("## Private-data lifecycle and removal", 1)[1].split())

    assert "set `sources = []` rather than deleting the setting" in section
    assert "rejects an empty" not in section

    raw = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))
    raw["discovery"]["sources"] = []
    raw["discovery"].pop("zip_code")
    assert (
        parse_config(raw, path=tmp_path / "config.toml", load_secrets=False).discovery.sources == ()
    )
    del raw["discovery"]["sources"]
    raw["discovery"]["zip_code"] = "00000"
    default = parse_config(raw, path=tmp_path / "config.toml", load_secrets=False)
    assert default.discovery.sources == (DiscoveryProvider.INATURALIST,)


# Legacy trusted-network routes that the owner proxy recipes must keep closed.
LEGACY_PROXY_SAMPLES = (
    "/",
    "/health",
    "/v1/catalog",
    "/v1/assets/species/1-example/portrait.png",
    "/v1/embed/featured-plate",
    "/v1/display-fetch",
    "/v1/display-success",
)


def _served_owner_paths() -> list[str]:
    """Every explicit route in web.py; the legacy routes all go through the catch-all."""
    source = (ROOT / "src/inky_bird_frame/web.py").read_text(encoding="utf-8")
    routes = re.findall(r'Route\(\s*"([^"]+)"', source)
    assert "/{path:path}" in routes
    explicit = [route for route in routes if route not in {"/", "/favicon.ico", "/{path:path}"}]
    samples = {"int": "1", "path": "species/1-example/portrait.png"}

    def sample(match: re.Match[str]) -> str:
        return samples.get(match.group(2) or "", "example")

    paths = [re.sub(r"\{(\w+)(?::(\w+))?\}", sample, route) for route in explicit]
    assert {"/app/ui.js", "/app/work-status.js", "/app/icon-32.png", "/wall/v1/catalog"} <= set(
        paths
    )
    return paths


def _proxy_matchers() -> dict[str, Callable[[str], bool]]:
    guide = (ROOT / "docs/browser-access.md").read_text(encoding="utf-8")

    rules = re.findall(r'^\s+rule: "(.+)"$', guide, re.MULTILINE)
    assert len(rules) == 2
    expressions = []
    for rule in rules:
        expression = re.sub(r"Host\(`[^`]+`\)|Method\(`[^`]+`\)", "True", rule)
        expression = re.sub(r"PathPrefix\(`([^`]+)`\)", r"path.startswith('\1')", expression)
        expression = re.sub(r"Path\(`([^`]+)`\)", r"(path == '\1')", expression)
        expression = expression.replace("&&", " and ").replace("||", " or ").replace("!", " not ")
        assert re.fullmatch(r"[\w\s()'/.=-]+", expression), expression
        expressions.append(compile(expression, "traefik-rule", "eval"))

    def traefik(path: str) -> bool:
        return any(eval(code, {"__builtins__": {}}, {"path": path}) for code in expressions)

    caddy_patterns = re.search(r"^\s+@owner path (.+)$", guide, re.MULTILINE)
    assert caddy_patterns
    patterns = caddy_patterns.group(1).split()

    def caddy(path: str) -> bool:
        return any(
            path.startswith(pattern[:-1]) if pattern.endswith("*") else path == pattern
            for pattern in patterns
        )

    nginx_location = re.search(r"^\s+location ~ (\S+) \{$", guide, re.MULTILINE)
    assert nginx_location
    nginx_pattern = re.compile(nginx_location.group(1))

    def nginx(path: str) -> bool:
        return nginx_pattern.fullmatch(path) is not None

    return {"Traefik": traefik, "Caddy": caddy, "Nginx": nginx}


@pytest.mark.parametrize("proxy", ["Traefik", "Caddy", "Nginx"])
def test_proxy_recipes_route_every_owner_and_wall_path_and_no_legacy_path(proxy: str) -> None:
    routes = _proxy_matchers()[proxy]

    missing = [path for path in _served_owner_paths() if not routes(path)]
    exposed = [path for path in LEGACY_PROXY_SAMPLES if routes(path)]

    assert not missing, f"{proxy} recipe does not route: {missing}"
    assert not exposed, f"{proxy} recipe exposes legacy routes: {exposed}"


TOML_EXAMPLE = re.compile(
    r"^(?P<indent>[ ]*)(?P<fence>```|~~~)toml\n(?P<body>.*?)^(?P=indent)(?P=fence)[ ]*$",
    re.MULTILINE | re.DOTALL,
)
LOCATION_KEYS = ("zip_code", "postal_code", "country_code", "latitude", "longitude")


def _merge(target: dict[str, object], update: dict[str, object]) -> None:
    for key, value in update.items():
        current = target.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _merge(current, value)
        else:
            target[key] = value


def _documented_toml_examples() -> list[tuple[str, str, dict[str, object]]]:
    examples = []
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        text = path.read_text(encoding="utf-8")
        for match in TOML_EXAMPLE.finditer(text):
            indent = match.group("indent")
            body = "\n".join(line.removeprefix(indent) for line in match.group("body").splitlines())
            lead_in = text[: match.start()].rstrip().rsplit("\n\n", 1)[-1]
            line = text.count("\n", 0, match.start()) + 1
            examples.append((f"{path.relative_to(ROOT)}:{line}", lead_in, tomllib.loads(body)))
    return examples


def test_documented_configuration_examples_load_or_say_they_are_fragments(
    tmp_path: Path,
) -> None:
    """Load each example into config.example.toml the way an owner would apply it.

    A [discovery] table that selects sources replaces the whole table; anything
    else is merged, and a new location form replaces the old one.
    """
    examples = _documented_toml_examples()
    assert len(examples) > 20
    failures = []
    for where, lead_in, example in examples:
        if "fragment" in lead_in.lower():
            continue
        raw: dict[str, object] = tomllib.loads(
            (ROOT / "config.example.toml").read_text(encoding="utf-8")
        )
        discovery = example.get("discovery")
        base_discovery = raw["discovery"]
        assert isinstance(base_discovery, dict)
        if isinstance(discovery, dict) and "sources" in discovery:
            raw["discovery"] = discovery
            example = {key: value for key, value in example.items() if key != "discovery"}
        elif isinstance(discovery, dict) and any(key in discovery for key in LOCATION_KEYS):
            for key in LOCATION_KEYS:
                base_discovery.pop(key, None)
        _merge(raw, example)
        try:
            parse_config(raw, path=tmp_path / "config.toml", load_secrets=False)
        except ConfigurationError as exc:
            failures.append(f"{where}: {exc}")
    assert not failures, "\n" + "\n".join(failures)


def test_architecture_describes_the_fields_the_request_log_actually_writes() -> None:
    server = (ROOT / "src/inky_bird_frame/server.py").read_text(encoding="utf-8")
    dispatch = server.split("async def dispatch(", 1)[1].split("\n    def ", 1)[0]
    logged = re.search(r'"event": "http_request",(.*?)\}', dispatch, re.DOTALL)
    assert logged
    assert set(re.findall(r'"(\w+)":', logged.group(1))) == {"method", "route", "status"}

    guide = " ".join((ROOT / "docs/architecture.md").read_text(encoding="utf-8").split())
    assert "with source IP address, path and query" not in guide
    assert "It never logs the source address, query, cookies" in guide
    assert "Owner, screen, wall and status routes are not logged per request" in guide


def _markdown_table(section: str, header: str) -> list[list[str]]:
    lines = section.split(header, 1)[1].lstrip("\n").splitlines()
    rows = []
    for line in lines[1:]:  # skip the separator row
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def test_cli_exit_codes_and_error_types_match_the_code() -> None:
    from inky_bird_frame.cli import ERROR_CODES, EXIT_CODES

    guide = (ROOT / "docs/operations.md").read_text(encoding="utf-8")
    section = guide.split("### Command results and exit codes", 1)[1].split("\n### ", 1)[0]

    documented_exits = {
        int(row[0].strip("`")): row[1]
        for row in _markdown_table(section, "| Exit code | Meaning |")
    }
    assert set(documented_exits) == {0, 1, *EXIT_CODES.values()}

    types = _markdown_table(section, "| `error.type` | Meaning |")
    assert [row[0].strip("`") for row in types] == list(ERROR_CODES)
    for row in types:
        code = row[0].strip("`")
        stated = re.search(r"exit code `(\d)`", row[1])
        expected = EXIT_CODES.get(code, 1)
        if stated is not None:
            assert int(stated.group(1)) == expected, code
        else:
            assert expected == 1, f"{code} exits {expected}; the table must say so"


def test_http_error_codes_and_statuses_match_the_code() -> None:
    from inky_bird_frame.web import HTTP_ERROR_CODES

    source = (ROOT / "src/inky_bird_frame/web.py").read_text(encoding="utf-8")
    returned: defaultdict[str, set[int]] = defaultdict(set)
    for status, code in re.findall(r'_error\(\s*(\d{3}),\s*"([a-z_]+)"', source):
        returned[code].add(int(status))
    assert set(returned) == set(HTTP_ERROR_CODES)

    guide = (ROOT / "docs/reference.md").read_text(encoding="utf-8")
    section = guide.split("### Owner, screen and integration routes", 1)[1].split("\n### ", 1)[0]
    documented = {
        row[0].strip("`"): {int(row[1])}
        for row in _markdown_table(section, "| `code` | Status | Meaning |")
    }
    assert documented == dict(returned)


def _documented_error_forms() -> list[tuple[str, set[str]]]:
    guide = (ROOT / "docs/reference.md").read_text(encoding="utf-8")
    section = guide.split("## HTTP errors", 1)[1].split("\n### ", 1)[0]
    rows = _markdown_table(section, "| Addresses | `error` value |")
    assert rows
    forms = []
    for addresses, value in rows:
        assert value.startswith(("object", "plain string")), value
        form = "object" if value.startswith("object") else "string"
        forms.append((form, set(re.findall(r"`(/[^`]*)`", addresses))))
    return forms


def _is_prefix(address: str) -> bool:
    return address != "/" and address.endswith("/")


def _documented_error_form(route: str, forms: list[tuple[str, set[str]]]) -> str | None:
    # An exact address beats a prefix; "…" stands for the rest of the path.
    for form, addresses in forms:
        if route in addresses or any(
            a.endswith("…") and route.startswith(a[:-1]) for a in addresses
        ):
            return form
    for form, addresses in forms:
        if any(_is_prefix(a) and route.startswith(a) for a in addresses):
            return form
    return None


def test_documented_error_form_matches_every_route() -> None:
    from test_route_table import LEGACY_ENVELOPE_ROUTES, UNAUTHENTICATED

    from inky_bird_frame.server import _READ_PATHS, _TELEMETRY_PATHS
    from inky_bird_frame.web import APP_PREFIXES

    forms = _documented_error_forms()
    prefixes = {a for _form, addresses in forms for a in addresses if _is_prefix(a)}
    assert prefixes == set(APP_PREFIXES)
    for _method, route in UNAUTHENTICATED:
        expected = "string" if route in LEGACY_ENVELOPE_ROUTES else "object"
        assert _documented_error_form(route, forms) == expected, route
    for route in (*_READ_PATHS, *_TELEMETRY_PATHS, "/v1/assets/species/1/portrait.png"):
        assert _documented_error_form(route, forms) == "string", route
    plain = {
        a for form, addresses in forms if form == "string" for a in addresses if not _is_prefix(a)
    }
    assert plain == LEGACY_ENVELOPE_ROUTES | _READ_PATHS | _TELEMETRY_PATHS | {"/v1/assets/…"}


def test_display_report_errors_match_the_code() -> None:
    import inspect

    from inky_bird_frame.server import MAX_TELEMETRY_REQUEST_BYTES, CatalogService

    source = inspect.getsource(CatalogService.telemetry_response)
    returned = {
        (int(status), error)
        for status, error in re.findall(r'(\d{3}),\s*\{"ok": False, "error": "([^"]+)"', source)
    }
    assert returned
    guide = (ROOT / "docs/reference.md").read_text(encoding="utf-8")
    section = guide.split("### Display reports", 1)[1].split("\n## ", 1)[0]
    documented = {
        (int(status), error.strip("`"))
        for status, error, _when in _markdown_table(section, "| Status | `error` | When |")
    }
    assert documented == returned
    assert f"over {MAX_TELEMETRY_REQUEST_BYTES:,} bytes" in section


def test_docs_say_head_answers_like_get() -> None:
    for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert "returns 404 to `HEAD`" not in text, path.name
        assert "`HEAD /health` returns 404" not in text, path.name
    reference = (ROOT / "docs/reference.md").read_text(encoding="utf-8")
    assert "Every `GET` route answers `HEAD`" in reference


def _owner_page_literals() -> list[str]:
    web = ROOT / "src/inky_bird_frame/web"
    return [
        next(group for group in match.groups() if group is not None).strip()
        for path in sorted(web.iterdir())
        if path.suffix in {".js", ".html"}
        for match in UI_TEXT.finditer(path.read_text(encoding="utf-8"))
    ]


def test_readme_glossary_uses_words_the_owner_page_shows() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    literals = _owner_page_literals()
    # Text a person reads starts with a capital; identifiers, classes and routes do not.
    shown = [text for text in literals if text[:1].isupper() and "/" not in text]
    rows = _markdown_table(readme, "| Word | Meaning |")
    assert rows
    for word, _meaning in rows:
        label = re.compile(rf"\b{re.escape(word.strip('*'))}s?\b", re.IGNORECASE)
        assert any(label.search(text) for text in shown), word
    words = {word.strip("*") for word, _meaning in rows}
    for name in (
        "Your journal",
        "Plate",
        "New plate",
        "On my frame",
        "Favorite",
        "Collections",
        "More plates",
        "Play",
        "Wall frame",
        "Sources",
        "In the works",
        "Needs a hand",
        "Noted",
    ):
        assert name in words, name


def test_operations_guide_says_upgraded_birds_get_twelve_counted_tries() -> None:
    guide = " ".join((ROOT / "docs/operations.md").read_text(encoding="utf-8").split())
    assert "past 12 tries before an upgrade stops at its next failure" not in guide
    assert "Failures from before this release are not counted" in guide


def test_installation_guide_describes_when_upgrade_skips_the_plate_check() -> None:
    guide = " ".join((ROOT / "docs/installation.md").read_text(encoding="utf-8").split())
    assert "If the installed version cannot run at all, setup refuses" not in guide
    assert "If the installed version cannot be loaded" in guide
    assert "the new version, which has already checked the configuration, waits" in guide
    assert "Setup also refuses if the check itself fails to run" in guide


def test_example_config_matches_retry_cap_and_generation_default() -> None:
    example = " ".join((ROOT / "config.example.toml").read_text(encoding="utf-8").split())
    assert "up to 6 hours, or delivery_retry_minutes if that is longer" in example
    assert "Older files without this line keep generation on, unless app-managed" in example


def test_notifications_guide_says_the_new_species_record_fails_quietly() -> None:
    guide = " ".join((ROOT / "docs/notifications.md").read_text(encoding="utf-8").split())
    assert "A notice or health record that cannot be saved" not in guide
    assert "A queued notice or health record that cannot be saved leaves a warning" in guide


RETIRED_SCREEN_NAMES = (
    "Saved birds",
    "Current and saved",
    "current selection",
    "Plate work",
    "Available artwork",
    "Recent owner operations",
    "Refresh observations",
    "Queue selected for the next scheduled check",
    "Retry selected as soon as possible",
    "Review missing-plate requests",
    "Manage → Displays",
    "Physical wall panel",
    "Import or review history",
    "Use retained history without an upload",
    "Frame mode",
    "Owner Frame",
)


def test_docs_use_the_names_the_owner_pages_show_now() -> None:
    # The walkthrough transcribes a film recorded before the names changed.
    for path in [path for path in _markdown_files() if path.name != "walkthrough.md"]:
        text = " ".join(_without_fenced_code(path.read_text(encoding="utf-8")).split())
        for retired in RETIRED_SCREEN_NAMES:
            assert retired.lower() not in text.lower(), (path.name, retired)


def test_documented_manage_paths_name_controls_the_pages_show() -> None:
    literals = set(_owner_page_literals())
    checked = 0
    for path in _markdown_files():
        text = " ".join(path.read_text(encoding="utf-8").split())
        for match in re.finditer(r"\*\*(Manage|Collections|Birds) → (.+?)\*\*", text):
            for part in (match.group(1), *match.group(2).split(" → ")):
                checked += 1
                assert part in literals, (path.name, match.group(0), part)
    assert checked


def test_journal_guide_names_every_area_and_the_one_tap_frame_change() -> None:
    guide = " ".join((ROOT / "docs/journal.md").read_text(encoding="utf-8").split())
    literals = set(_owner_page_literals())
    for name in (
        "Home",
        "Birds",
        "Collections",
        "Play",
        "Manage",
        "Your birds",
        "More plates",
        "On my frame",
        "Favorites",
        "Add to Favorites",
        "Add to my frame",
        "Remove from my frame",
        "Undo",
        "Change my frame",
        "In the works",
        "Needs a hand",
        "Birds and sources",
        "Plates",
        "Frame and displays",
        "Integrations",
        "System",
        "Wall frame",
    ):
        assert f"**{name}**" in guide, name
        assert name in literals, name
    assert "Preview changes to" not in guide


def test_guides_state_the_accepted_limits() -> None:
    def text(name: str) -> str:
        return " ".join((ROOT / name).read_text(encoding="utf-8").split())

    assert "may stop at the 30-minute limit" in text("docs/installation.md")
    assert "the no-refetch check runs only in Chromium" in text("CONTRIBUTING.md")
    assert "reads the whole history" in text("docs/journal.md")
    presentation = text("docs/presentation.md")
    assert "`panel_health` is `recent`, `stale` or `unknown`" in presentation
    assert "can read `stale` while an empty wall view has paused" in presentation
    assert "two refreshes finish at the same moment" in text("docs/notifications.md")
    assert "Linux does not promise" in text("docs/discovery.md")
