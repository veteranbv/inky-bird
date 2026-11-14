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
    for path in ("README.md", "pyproject.toml", "src/inky_bird_frame/web/journal.js"):
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
    assert "[Still preview](docs/images/inky-walkthrough-poster.jpg)" in readme
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
    assert "public approved artwork" in readme
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
  print) grep -Fxq "${2##*/}" "$FAKE/loaded" ;;
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


def _bash_blocks(section: str) -> list[str]:
    return re.findall(r"^```bash\n(.*?)^```$", section, re.MULTILINE | re.DOTALL)


def _native_backup_blocks() -> tuple[list[str], list[str]]:
    guide = (ROOT / "docs/backup.md").read_text(encoding="utf-8")
    backup = guide.split("## Native controller backup", 1)[1].split(
        "\n## Native controller restore", 1
    )[0]
    quiesce = backup.split("### 3. Quiesce scheduled work", 1)[1].split("\n### 4.", 1)[0]
    restart = backup.split("### 5. Restart and verify", 1)[1]
    return _bash_blocks(quiesce), _bash_blocks(restart)


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

    def run(self, block: str, *, family: str | None = None) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.fake / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "FAKE": str(self.fake),
            "BACKUP": str(self.backup),
            "FAMILY": family or self.family,
        }
        return subprocess.run(
            ["bash", "-c", f"{block}\nprintf 'block-status=%s\\n' \"$?\""],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
            timeout=30,
        )

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


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
@pytest.mark.parametrize("family", ["inky-bird", "inky-bird-frame"])
def test_native_backup_quiesces_and_restores_an_app_managed_systemd_install(
    tmp_path: Path, family: str
) -> None:
    (record, quiesce, *_), (start, _) = _native_backup_blocks()
    host = _NativeHost(tmp_path, family)
    host.install_units(**{"controller.service": "active", "scheduler.service": "active"})

    for block in (record, quiesce):
        result = host.run(block)
        assert host.status(result) == 0, result.stderr

    assert host.recorded("native-active-systemd-units.txt") == [
        f"{family}-controller.service",
        f"{family}-scheduler.service",
    ]
    assert host.calls() == [
        f"stop {family}-scheduler.service",
        f"stop {family}-controller.service",
    ]

    result = host.run(start)
    assert host.status(result) == 0, result.stderr
    assert host.calls()[2:] == [
        f"start {family}-controller.service",
        f"start {family}-scheduler.service",
    ]


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_native_backup_stops_timers_before_the_controller_and_skips_missing_units(
    tmp_path: Path,
) -> None:
    (record, quiesce, *_), _ = _native_backup_blocks()
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

    for block in (record, quiesce):
        result = host.run(block)
        assert host.status(result) == 0, result.stderr

    assert host.recorded("native-active-systemd-units.txt") == [
        "inky-bird-controller.service",
        "inky-bird-refresh.timer",
    ]
    assert host.calls() == ["stop inky-bird-refresh.timer", "stop inky-bird-controller.service"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_native_backup_refuses_a_family_that_is_not_installed(tmp_path: Path) -> None:
    (record, *_), _ = _native_backup_blocks()
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_units(**{"controller.service": "active"})

    result = host.run(record, family="inky-bird-frame")

    assert host.status(result) != 0
    assert "No installed inky-bird-frame-controller.service" in result.stderr
    assert host.calls() == []


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_native_backup_leaves_services_running_while_a_job_is_active(tmp_path: Path) -> None:
    (record, quiesce, *_), _ = _native_backup_blocks()
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_units(
        **{
            "controller.service": "active",
            "generate.timer": "active",
            "generate.service": "active",
        }
    )

    assert host.status(host.run(record)) == 0
    result = host.run(quiesce)

    assert host.status(result) != 0
    assert "One-shot service is not quiesced: inky-bird-generate.service" in result.stderr
    assert host.calls() == ["stop inky-bird-generate.timer"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
@pytest.mark.parametrize("family", ["inky-bird", "inky-bird-frame"])
def test_native_backup_quiesces_and_restores_app_managed_launch_agents(
    tmp_path: Path, family: str
) -> None:
    (_, _, inspect, bootout), (_, bootstrap) = _native_backup_blocks()
    host = _NativeHost(tmp_path, family)
    host.install_agents("serve", "scheduler", loaded=("serve", "scheduler"))

    for block in (inspect, bootout):
        result = host.run(block)
        assert host.status(result) == 0, result.stderr

    assert host.recorded("native-loaded-launchagents.txt") == ["serve", "scheduler"]
    assert host.calls() == [f"bootout com.{family}.serve", f"bootout com.{family}.scheduler"]

    result = host.run(bootstrap)
    assert host.status(result) == 0, result.stderr
    assert host.calls()[2:] == [
        f"bootstrap com.{family}.serve",
        f"bootstrap com.{family}.scheduler",
    ]


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_native_backup_refuses_launch_agents_of_another_family(tmp_path: Path) -> None:
    (_, _, inspect, _), _ = _native_backup_blocks()
    host = _NativeHost(tmp_path, "inky-bird")
    host.install_agents("serve", loaded=("serve",))

    result = host.run(inspect, family="inky-bird-frame")

    assert host.status(result) != 0
    assert "No installed com.inky-bird-frame.serve LaunchAgent" in result.stderr


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
