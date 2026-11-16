# Installation

This guide starts with a blank controller and Raspberry Pi and ends with a frame
that returns after a reboot. Follow the sections in order. The first panel test
uses an included plate, so you can prove the hardware before setting up Codex or
bird observations.

If you only want the browser journal, use [Start with approved art](settings.md#start-with-approved-art)
or run the [Docker controller](docker.md) without a display node.

## Before you begin

The examples below use the names for a clean installation. Existing installs
keep their current configuration, data roots and native service names; setup
detects that identity instead of creating a second scheduler. Before updating
an existing install, read the [name migration guide](repository-name.md).

You do not need panel hardware, Codex authentication or a bird-service account
for the approved-art path. The requirements below describe the complete
observation-driven wall installation.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/installation-architecture-dark.png">
  <img src="images/installation-architecture.png" alt="Inky Bird runtime architecture showing observation and generation services; the controller; the Raspberry Pi display node and an optional trusted browser application reading the approved catalog over a private network; and the Inky Impression panel">
</picture>

### Choose the two computers

The controller and display node have different jobs. A capable Linux Raspberry
Pi can run both, but the recommended framed build puts a small Pi behind the
display and runs the controller elsewhere. The controller can also run on an
existing Docker host; follow the
[Docker controller guide](docker.md) instead of either native controller
section.

| Role | Recommended | Supported | Notes |
| --- | --- | --- | --- |
| Controller | Existing Apple silicon Mac or Ubuntu Server 24.04 LTS computer | macOS with launchd; 64-bit Ubuntu 24.04 with systemd; 64-bit Raspberry Pi OS Bookworm or later with systemd | A Raspberry Pi 4 with 4GB is the smallest recommended dedicated controller. Docker installation is documented separately. |
| Display | Raspberry Pi Zero 2 W with pre-soldered 40-pin header and Raspberry Pi OS Lite 64-bit | Raspberry Pi OS Bookworm or later on a 40-pin Raspberry Pi | Supports the 7.3-inch PIM773 at 800x480 and 13.3-inch PIM774 at 1600x1200. |

The setup command detects launchd or systemd, but detection does not mean every
operating system has been tested. Other systems may work. Contributions that
document and test them are welcome.

Pimoroni lists PIM773 and PIM774 as compatible with every 40-pin Raspberry Pi,
including Zero variants. A Zero without a header requires soldering. The Zero
2 W has a 64-bit processor and built-in 2.4 GHz Wi-Fi. See the
[Inky Impression product page](https://shop.pimoroni.com/products/inky-impression) and
[Raspberry Pi Zero 2 W specifications](https://www.raspberrypi.com/products/raspberry-pi-zero-2-w/).
The application reads the panel geometry from Pimoroni's EEPROM; no display
model or resolution setting belongs in `config.toml`.

The supported topology remains one active display node per controller. Multiple
nodes can read the same catalog, but the controller's fetch and success
heartbeats describe the display role as a whole rather than identifying each
physical panel independently.

### What you need

You need:

- the framed-display parts in the [hardware guide](hardware.md);
- one controller from the support table;
- a computer with a microSD reader;
- a GitHub connection to clone this public repository;
- a ChatGPT plan that includes Codex, or an OpenAI API key with separate API
  billing;
- coordinates, or a supported postal-code lookup, for location-based discovery; and
- administrative access on both computers.

The controller requires Python 3.11 or newer, `git`, `rsync`, `uv`, and Codex
CLI. The display requires `git`, `rsync`, and Pimoroni's Python environment.

### Network requirements

Every application connection is client-initiated. The display node connects
directly to the controller. A browser connects directly only from a trusted HTTP
origin; otherwise, its application proxy or TLS terminator connects to the
controller. The controller's configured port (8793 in the supplied example
configuration) must remain on a trusted network and must not be forwarded from
the public internet.

| From | To | Purpose |
| --- | --- | --- |
| Controller | Internet HTTPS (TCP 443) | Codex, internet-backed observation services and geocoder, licensed references, and configured research sources |
| Controller | BirdNET-Go HTTP(S) endpoint | Read-only summaries from an explicitly configured self-hosted station |
| Display node | Configured controller TCP port (8793 in the supplied example) | Health and catalog reads, image downloads, and narrow display-health reports |
| Trusted browser application | Same-origin proxy, HTTPS endpoint, or controller HTTP port for an HTTP application | Optional read-only catalog and image downloads; an exact allowed origin is required when the request is cross-origin |
| Application proxy or TLS terminator | Configured controller TCP port (8793 in the supplied example) | Server-side catalog and image requests; keep this hop on the trusted network |
| Setup computer | Display node SSH (TCP 22) | Installation, updates, and troubleshooting |

The two computers do not have to share a subnet. They must be routable to each
other, and Wi-Fi client isolation must not block the display from reaching the
controller. Give the controller a stable DNS name or DHCP reservation because
the display stores its URL. The display itself may use ordinary DHCP because it
initiates every application connection.

Do not expose the configured controller port to the public internet. Catalog,
asset, embed, and narrow display-health routes remain unauthenticated LAN
services. Owner access at `/app` has a separate secure boundary; see
[owner setup and recovery](browser-access.md). Use a VPN or a reviewed TLS
reverse proxy when traffic crosses an untrusted network. Do not assume that
adding owner sign-in protects the legacy routes.

For owner sign-in, also trust the other web services reachable on the same
hostname or IP, even on different ports. Browser cookies do not isolate ports.
If you host an untrusted service, follow the
[host-isolation requirements](browser-access.md#sessions-and-private-files)
before using that address for Inky.

The example configuration sets `controller.bind_host = "0.0.0.0"`, which
listens on every interface of the controller host. On a multi-homed
controller, set `bind_host` to the specific LAN address the display uses so
the service is not offered on other networks.

To let another web application or a Homepage dashboard read the catalog, see
the [reference page](reference.md#browser-applications).

## 1. Install the controller

Choose the macOS or Linux path below. Keep the source checkout. Native
installations use an explicit GitHub release tag; future updates fetch the
selected tag and run the same setup command again. Replace `vX.Y.Z` in the
examples with the exact release you intend to install.

### macOS controller

The standard installer creates jobs for your logged-in macOS user. It does not
create a system account or install administrator-managed services. If an operator
already runs Inky through system LaunchDaemons (jobs that can start before login),
use that deployment process for upgrades. The standard installer refuses to
compete with those jobs before it changes files. Run `doctor controller` as the
configured service account; it recognizes protected, administrator-owned system
plists for that account. This check confirms job registration, not the security
of a custom deployment or its process environment.

Install prerequisites with Homebrew:

```bash
brew install git rsync uv
brew install --cask codex
```

OpenAI also publishes npm packages and direct release binaries. See the
[official Codex CLI installation options](https://github.com/openai/codex#installing-and-running-codex-cli).

Clone and prepare the project:

```bash
git clone https://github.com/veteranbv/inky-bird.git
cd inky-bird
INKY_VERSION=vX.Y.Z
git checkout --detach "$INKY_VERSION"
uv sync --extra controller --locked
mkdir -p "$HOME/Library/Application Support/Inky Bird"
cp config.example.toml \
  "$HOME/Library/Application Support/Inky Bird/config.toml"
chmod 600 "$HOME/Library/Application Support/Inky Bird/config.toml"
```

Edit the private file:

```bash
open -t "$HOME/Library/Application Support/Inky Bird/config.toml"
```

At minimum, set:

- one discovery location from `zip_code`, `postal_code` plus `country_code`, or
  `latitude` plus `longitude` when iNaturalist or eBird is enabled;
- `controller.codex_path` to the output of `command -v codex`;
- optionally, `controller.codex_model` to a model you have validated for
  source-backed profile research, image generation, and review (omit it to use
  Codex's configured default, and run one foreground generation after changing
  the pin);
- `display_node.controller_url` to a name or address the Pi can reach, such as
  `http://bird-controller.local:8793`; and
- any paths that should live outside the configuration directory.

Sign in with the account that will run the LaunchAgents:

```bash
codex login
codex login status
```

OpenAI recommends ChatGPT sign-in to use Codex through a Plus, Pro, Business,
Edu, or Enterprise plan. API-key login is supported but billed separately. See
[Codex authentication](https://learn.chatgpt.com/docs/auth).

Validate, preview, install, and diagnose:

```bash
CONFIG="$HOME/Library/Application Support/Inky Bird/config.toml"
uv run inky-bird config validate --config "$CONFIG"
uv run inky-bird setup controller --config "$CONFIG"
uv run inky-bird setup controller --config "$CONFIG" --yes
uv run inky-bird doctor controller --config "$CONFIG"
```

The preview lists changes and performs no installation. `--yes` copies a managed
runtime to `~/Services/inky-bird`, installs its locked controller
environment, creates LaunchAgents, loads them, and verifies them with
`launchctl`.

At the end of `--yes`, setup asks for `Inky username:` and then a hidden
password when the controller has no browser owner yet. These create the owner
account you use to sign in at `/app`. Cancelling leaves the controller running
with owner controls locked; run `uv run inky-bird access setup --config "$CONFIG"`
later in a private terminal.

New artwork is off with `config.example.toml` (`generation_enabled = false`),
so a new controller shows the approved plates only. Setup output then includes
`"generation_enabled": false` and a `generation` line that says how to turn it
on. To make new plates, set `generation_enabled = true` under `[controller]` and
run setup again. Generation uses your own Codex sign-in, through your ChatGPT
plan's Codex allowance or a billed API key, and that provider's charges apply.
On macOS, the first scheduled generation then runs one
`schedule.generation_minutes` interval after setup (six hours with the example
value), then at that interval. A configuration file without a
`generation_enabled` line keeps generation on, unless it is app-managed, so an
existing installation keeps its current behaviour. With an app-managed
configuration, generation is off until you turn it on in Settings; then the
scheduler starts generation right after its next successful refresh.

The setup preview refuses, before `sudo` or any installer step, what the native
installers would refuse: provider keys read from environment variables
(`*_api_key_env`, `birdweather_token_env`), notification destinations that use
`url_env`, a missing OIDC client secret or an unsafe `native-access.env`, and an
unsafe `native-tls.env`. Store provider keys and notification URLs directly in
the private mode-`0600` configuration file. Supply the OIDC client secret in its
named environment variable when you run setup (see
[browser access](browser-access.md)). Keep `native-access.env` and
`native-tls.env` as private (mode `0600`) regular files.

LaunchAgents begin when this macOS user logs in. They survive application
updates and restart after logout/login or reboot. For unattended recovery, the
Mac must also power on after an outage and reach that user session. Newer
supported desktop Macs expose **System Settings > Energy > Start up when power
is connected**; Apple documents the hardware and OS requirements in
[Turn on a Mac without pressing its power button](https://support.apple.com/en-us/125517).

### Ubuntu or Raspberry Pi OS controller

The commands below are for Debian-family systemd hosts. Start with a 64-bit
Ubuntu Server 24.04 LTS or Raspberry Pi OS Bookworm-or-later installation.

```bash
sudo apt update
sudo apt install -y git rsync npm pipx
pipx ensurepath
source "$HOME/.profile"
pipx install uv
mkdir -p "$HOME/.local"
npm config set prefix "$HOME/.local"
npm install -g @openai/codex
export PATH="$HOME/.local/bin:$PATH"
```

Add `export PATH="$HOME/.local/bin:$PATH"` to the account's shell profile if it
is not already present. The installer stores the absolute Codex path in service
configuration, so later service starts do not depend on the interactive PATH.

Clone and prepare the project:

```bash
git clone https://github.com/veteranbv/inky-bird.git
cd inky-bird
INKY_VERSION=vX.Y.Z
git checkout --detach "$INKY_VERSION"
uv sync --extra controller --locked
mkdir -p "$HOME/.config/inky-bird"
cp config.example.toml "$HOME/.config/inky-bird/config.toml"
chmod 600 "$HOME/.config/inky-bird/config.toml"
nano "$HOME/.config/inky-bird/config.toml"
```

Set the same minimum fields listed in the macOS section. On a headless
controller, use OpenAI's device-code flow:

```bash
codex login --device-auth
codex login status
```

Device-code login must be permitted in the ChatGPT account or workspace. The
[official headless login guide](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)
documents that requirement and fallback methods.

Validate, preview, install, and diagnose:

```bash
CONFIG="$HOME/.config/inky-bird/config.toml"
uv run inky-bird config validate --config "$CONFIG"
uv run inky-bird setup controller --config "$CONFIG"
uv run inky-bird setup controller --config "$CONFIG" --yes
uv run inky-bird doctor controller --config "$CONFIG"
```

The installer creates a persistent `inky-bird-controller.service` plus
refresh, generation, and optional notification/publication timers. An
app-managed configuration (the default from `config init`) gets one persistent
`inky-bird-scheduler.service` instead of those timers. It enables them for boot
and verifies their state through systemd. Setup performs one successful refresh
before installing the units, then each timer schedules its first run two minutes
after activation and continues at the configured interval. Installer progress
and any `sudo` prompt remain visible. See the official
[`systemd.timer` manual](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html).

As on macOS, `--yes` ends by asking for `Inky username:` and a hidden password
when no browser owner exists yet.

New artwork is off with `config.example.toml`, as on macOS. To make new plates,
set `generation_enabled = true` under `[controller]` and run setup again; that
adds the generation timer. It first runs two minutes after setup, then every
`schedule.generation_minutes`. Generation uses your own Codex sign-in or API
key, and that provider's charges apply. With an app-managed configuration,
generation is off until you turn it on in Settings; then the scheduler starts
generation right after its next successful refresh.

The generation service's `TimeoutStartSec` follows the configuration:
`max_species_attempts_per_cycle` x (2 x `max_generation_attempts` + 2) x 20
minutes, plus 15 minutes. With the example values that is 48,900 seconds, about
13.6 hours. Run setup again after changing either setting. The other one-shot
services stop after 30 minutes, and the display service after 15. A first
publication of a large catalog over a slow upload link may stop at the 30-minute
limit; it starts again at the next timer run.

### Docker controller

Use the [Docker controller guide](docker.md) on an AMD64 or ARM64 Docker host.
The normal path pulls the published GHCR image; it does not build from source.
After the controller health check passes, continue at
[Prepare the display Pi](#2-prepare-the-display-pi).

### Verify controller data

Run a read-only discovery, then refresh the active catalog:

```bash
IBF="$HOME/Services/inky-bird/.venv/bin/inky-bird"
"$IBF" discover --config "$CONFIG"
"$IBF" refresh --config "$CONFIG"
curl --fail --silent "http://127.0.0.1:8793/health"
```

The health response must have `"ok": true`. `version` identifies the running
application. `approved_species` is the reusable catalog size. `active_species`
is the approved subset currently observed in the configured window and radius
or retained in the private collection. It may initially be zero in a region
whose birds have not been generated yet. The approved count comes from the last
built catalog index; the active count comes from current active-catalog state.
The endpoint does not rebuild or scan the catalog, so it stays fast as the
catalog grows. Run `"$IBF" --version` to identify the command-line installation
directly.

An existing controller can explicitly retain every currently approved plate
after upgrading by previewing and applying `collection import-approved`; see
the [operations guide](operations.md#seed-and-manage-a-private-collection).

### Open the journal

Open `http://CONTROLLER_ADDRESS:8793/app` in a browser on your home network,
using the controller's actual LAN address, and sign in with the username and
password you created at the end of setup. The plates are under **Birds → More
plates**. With nothing on your frame yet, Home shows a plate from the catalog
and **Choose birds**. See [browser access](browser-access.md) for allowed addresses and HTTPS.

### Import an approved catalog without changing parent permissions

For a controller that owns its catalog and state directories but cannot write
their parent, supply the existing controller state directory when importing:

```bash
inky-bird catalog sync \
  --source-catalog reviewed-catalog \
  --catalog data/catalog \
  --state-dir data/state
```

The command holds the catalog lock and keeps recoverable transactions under
`data/state/catalog-sync-work`. That location must be caller-owned, not writable
by other users, on the destination filesystem and outside both catalogs. Its
ancestors must be owned by the caller or root and prevent replacement by other
users; shared writable ancestors need the sticky bit. No
parent permission change or privileged import is required. Use the same
`--state-dir` when resuming an interrupted import. Existing sibling transaction
records must be recovered before changing the transaction location; the command
does not silently discard them. Imports without `--state-dir` retain sibling
transaction staging for offline catalogs.

Catalog import preserves the approved plates and their metadata. Putting a
plate on your frame is a separate owner action; it does not record the bird as
noted.

## 2. Prepare the display Pi

### Flash Raspberry Pi OS

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) to write
**Raspberry Pi OS Lite (64-bit)** to the microSD card. In Imager's OS
customization:

1. choose a unique hostname;
2. create a non-default username and password;
3. configure the wall location's Wi-Fi country, SSID, and password;
4. enable SSH with public-key authentication when possible; and
5. set the time zone.

SSH is disabled by default unless it is enabled during imaging or later on the
Pi. Raspberry Pi documents these settings in
[Remote access](https://www.raspberrypi.com/documentation/computers/remote-access.html)
and recommends Raspberry Pi OS for most Pi use in
[Operating systems](https://www.raspberrypi.com/documentation/computers/os.html).

Boot the Pi, find its hostname or DHCP lease, and connect:

```bash
ssh YOUR_USER@YOUR_PI_HOSTNAME.local
```

Confirm the network before attaching application behavior:

```bash
hostnamectl
ip address show
ip route
```

### Attach PIM773 or PIM774

Shut down and disconnect power before handling the boards:

```bash
sudo poweroff
```

![Raspberry Pi Zero 2 W mounted to the rear of PIM774](images/display-assembly.png)

*Conceptual assembly sequence, not a pinout or dimensional drawing. Follow the
Pimoroni hardware documentation supplied with the panel.*

Hold the Inky board by its edges. Align the Pi's complete 40-pin header with the
Inky connector, press it straight into place, and secure the Pi with the
included standoffs. Do not press on the glass panel. Pimoroni ships both
supported displays assembled with the required mounting hardware and documents
that no soldering is needed when the Pi already has a 40-pin header.

Reconnect power and SSH back in.

### Install the Pimoroni environment

Use Pimoroni's supported installer rather than modifying the system Python:

```bash
sudo apt update
sudo apt install -y git rsync
git clone https://github.com/pimoroni/inky.git "$HOME/inky"
cd "$HOME/inky"
./install.sh
```

The installer creates `~/.virtualenvs/pimoroni` and configures the required Pi
interfaces. Pimoroni recommends Raspberry Pi OS Bookworm or later and documents
the manual SPI, I2C, and `dtoverlay=spi0-0cs` requirements in the
[Inky library installation guide](https://github.com/pimoroni/inky#installation).
Reboot if the installer asks.

### Prove the panel without Codex

Clone this project and install it into the same Pimoroni environment:

```bash
git clone https://github.com/veteranbv/inky-bird.git "$HOME/inky-bird"
cd "$HOME/inky-bird"
INKY_VERSION=vX.Y.Z
git checkout --detach "$INKY_VERSION"
"$HOME/.virtualenvs/pimoroni/bin/python" -m pip install -e '.[inky]'
"$HOME/.virtualenvs/pimoroni/bin/inky-bird" display-image \
  catalog/species/12942-eastern-bluebird/display.png
```

The included Eastern Bluebird should appear in portrait orientation. PIM774
uses the canonical image unchanged. PIM773 automatically contains the complete
plate on its wider 800x480 aspect ratio with paper-colored margins, which appear
above and below the plate when the panel is portrait-mounted. It does not crop
or stretch the illustration or notes. A refresh normally takes tens of seconds
and may take longer when cold. Do not continue until this test succeeds; it
isolates the Pi, Python environment, 40-pin connection, and display from every
controller dependency.

## 3. Connect the display to the controller

Create a private display configuration. Do not copy the controller's private
file because it may contain credentials and a private discovery location.

When one Pi runs both roles, the controller already uses this same file. The
`if` below keeps an existing file; edit its `[display_node]` section instead of
creating a second configuration.

```bash
cd "$HOME/inky-bird"
mkdir -p "$HOME/.config/inky-bird"
if [ ! -e "$HOME/.config/inky-bird/config.toml" ]; then
  cp config.example.toml "$HOME/.config/inky-bird/config.toml"
fi
chmod 600 "$HOME/.config/inky-bird/config.toml"
nano "$HOME/.config/inky-bird/config.toml"
```

Set `display_node.controller_url` to the stable controller URL and choose
`display_node.rotation_mode`. Set `schedule.rotation_minutes` to the desired
cadence. The display process uses only `[display_node]` and display schedule
values; leave other example values non-sensitive and disabled.

From the Pi, verify the exact controller URL before installation:

```bash
curl --fail --silent "http://YOUR_CONTROLLER:8793/health"
```

Then validate, preview, install, and diagnose:

```bash
CONFIG="$HOME/.config/inky-bird/config.toml"
INKY="$HOME/.virtualenvs/pimoroni/bin/inky-bird"
"$INKY" config validate --config "$CONFIG"
"$INKY" setup display --config "$CONFIG" --source-dir "$PWD" \
  --venv "$HOME/.virtualenvs/pimoroni"
"$INKY" setup display --config "$CONFIG" --source-dir "$PWD" \
  --venv "$HOME/.virtualenvs/pimoroni" --yes
"$INKY" doctor display --config "$CONFIG"
```

Setup installs and enables a systemd timer but does not force an immediate
panel update. The timer takes over after `display_startup_delay_seconds`. Start
the first live rotation explicitly when the controller is ready:

```bash
"$INKY" display-cycle --config "$CONFIG" --force
```

If the controller reports `active_species: 0`, run a generation cycle on the
controller first:

```bash
# Run these on the controller with its private configuration.
"$HOME/Services/inky-bird/.venv/bin/inky-bird" generate \
  --config /path/to/controller/config.toml
"$HOME/Services/inky-bird/.venv/bin/inky-bird" refresh \
  --config /path/to/controller/config.toml
```

Use the controller's configuration path for those two controller commands, not
the Pi's display configuration.

## 4. Verify automatic recovery

Do not call installation complete until both doctor commands report
`"ready": true`.

On a systemd controller:

```bash
systemctl is-enabled inky-bird-controller.service
systemctl is-active inky-bird-controller.service
systemctl list-timers 'inky-bird-*'
```

On the display:

```bash
systemctl is-enabled inky-bird-display.timer
systemctl is-active inky-bird-display.timer
systemctl list-timers inky-bird-display.timer
```

On macOS:

```bash
launchctl print "gui/$(id -u)/com.inky-bird.serve"
launchctl print "gui/$(id -u)/com.inky-bird.refresh"
launchctl print "gui/$(id -u)/com.inky-bird.generate"
```

An app-managed configuration has `com.inky-bird.scheduler` instead of the
`refresh` and `generate` agents, and the generation agent exists only while
generation is on.

The Pi and Linux controller services return automatically after a reboot. The
macOS LaunchAgents return when their owning user logs in. The e-paper panel
keeps its last image without power, so a controller or network outage does not
blank the frame.

## Configuration map

| Section | Used by | Purpose |
| --- | --- | --- |
| `[discovery]` | Controller | Providers, credentials, location, radius, observation window, and species cap |
| `[controller]` | Controller | Persistent paths, Codex executable, HTTP bind, and generation bounds |
| `[research]` | Controller | Bounded fallback research policy and approved domains |
| `[notifications]` | Controller | Optional Apprise destinations, events, retries, and noise controls |
| `[public_catalog]` | Controller | Optional owner-only publication of reusable approved plates |
| `[display_node]` | Display | Controller URL, local state path, and selection policy |
| `[schedule]` | Both | Refresh, generation, publication, notification, and rotation intervals |

`config.example.toml` contains comments, supported values, and recommended
defaults. Configuration is TOML everywhere. Keep real files outside the Git
checkout, mode `0600`, and out of backups or support bundles that are shared
publicly.

## Update

Keep using your existing `$CONFIG`, runtime directory and service names during
an upgrade. Do not create the new default config directory beside an existing
one. If setup detects both native identities, it stops before making changes.
For every display runtime update, stop its timer, let the
current service finish, and preserve a recoverable runtime backup. The installer
refuses to replace either installed Python distribution while that role is running.
Display setup enables and restarts the timer when it finishes; a forced panel
cycle is not required. See [package upgrade and rollback](repository-name.md#python-environments).

The remote display wrapper transfers public source to a private staging
directory beside the application, not over the installed runtime. It checks
capacity for staging and a complete managed copy before transfer. The local
installer then checks identity, quiescence and administrator access before
copying live files. Refusal leaves the runtime and private config untouched;
the wrapper removes only its own staging directory. Later installation failures
still require your matched runtime/state backup. Initial panel refresh is
opt-in with `INKY_BIRD_RUN_INITIAL_DISPLAY=true`; normal updates can wait for
the configured timer instead.

Read the release notes and take a quiesced backup using the
[backup and restore guide](backup.md). Choose the exact target release, update
each source checkout to that tag, review the changes, and rerun the same setup
command. Setup is idempotent and replaces managed application code and native
service definitions without replacing the private configuration. Replace
`vX.Y.Z` below with that target tag.

If no browser owner exists, interactive controller setup finishes by asking for
a username and password. Password entry is hidden. Cancelling leaves the
installed controller running and owner controls locked. Run `access setup`
later in a private terminal. Unattended setup never waits for account entry;
display and scheduled work continue. Existing owners remain unchanged. See
[the migration guide](migration-v1.md) before this first browser-journal upgrade.

```bash
INKY_VERSION=vX.Y.Z
git fetch --tags --prune
git checkout --detach "$INKY_VERSION"
uv sync --extra controller --locked       # controller checkout
uv run inky-bird setup controller --config "$CONFIG" --yes
uv run inky-bird doctor controller --config "$CONFIG"
```

Before it changes anything, controller setup waits up to 30 minutes for a plate
that is being made. If one is still being made, setup stops with "A plate is
still being made. Run the upgrade again after it finishes. No changes made."
(`setup` then reports "Installer exited with status 3"). If the installed version
cannot be loaded, or cannot read your configuration (for example after you
edited it for the new release), setup says it cannot check for a plate and
continues without waiting; with no installed version, it continues without a
message. Setup refuses only if the check itself fails to run, for example when
it cannot open the lock in `state_dir`: "Could not check whether a plate is
being made. No changes made."

On a Linux controller, setup then stops the scheduler and job timers and waits
up to 30 more minutes for a running refresh, generate, catalog-publish or
notifications job to finish before it copies anything. If a job is still
running then, setup stops with "Timed out waiting for `UNIT` to finish." (naming
the job). Run setup again once the job has finished. If an upgrade fails after
it stopped services, it starts again only the services it stopped and says so,
or lists any it could not start with the command to start them. It does not
undo state or catalog changes the new version already made.

On the display, stop the timer and wait for a running panel update to finish
before setup. An install that began under the old name uses
`inky-bird-frame-display` instead of `inky-bird-display`:

```bash
cd "$HOME/inky-bird"
CONFIG="$HOME/.config/inky-bird/config.toml"
INKY="$HOME/.virtualenvs/pimoroni/bin/inky-bird"
INKY_VERSION=vX.Y.Z
git fetch --tags --prune
git checkout --detach "$INKY_VERSION"
sudo systemctl stop inky-bird-display.timer
while systemctl show --property=ActiveState --value inky-bird-display.service |
  grep -Eqv '^(inactive|failed)$'; do
  sleep 5
done
"$INKY" setup display --config "$CONFIG" --source-dir "$PWD" \
  --venv "$HOME/.virtualenvs/pimoroni" --yes
"$INKY" doctor display --config "$CONFIG"
```

`--source-dir "$PWD"` is required in the display update path because setup
points the Pimoroni environment at the managed runtime. The
explicit source directory ensures each update installs the checkout that was
just selected rather than the previous managed copy.

When the controller and display are updated separately, follow the release
notes for that upgrade. Ordinarily, update and validate the controller first.
When the release notes require a display-first update for a new catalog schema,
update display nodes before the controller. The active catalog wire format is
`schema_version: 1`, and a display node refuses a catalog with a newer schema
version: the cycle fails closed and the panel keeps its last image until the
display is updated. Private collection membership does not change this wire
schema.

Current display nodes report a parsed catalog fetch through `POST
/v1/display-fetch` and a verified panel update through `POST
/v1/display-success`. The reports contain only `{"schema_version": 1}`. If an
older controller rejects either POST, the display ignores the telemetry
failure and continues rotating plates. Controller and display releases before
v0.7.0 used state-changing GET telemetry that is no longer accepted. A direct
split upgrade from an older release cannot preserve heartbeat reporting: the
old role uses GET while the new role uses POST. Update the controller first,
then update the display promptly, and expect a temporary telemetry gap. If
uninterrupted heartbeat monitoring is required, update both roles to v0.7.0
first, verify POST telemetry, and then continue to the newer release.

Docker controller updates pull a published image and recreate the services. See
the [Docker update instructions](docker.md#update) for version pinning and
rollback.

## Uninstall

From the source checkout, run the uninstall script that matches the role and
platform. Each script removes the installed services and timers but preserves
private configuration, controller state, catalogs, and generated art:

```bash
./deploy/uninstall-controller.sh           # macOS controller
./deploy/uninstall-controller-systemd.sh   # Linux controller
./deploy/uninstall-display-local.sh        # display node
```

The equivalent manual removal follows. On an install that began under the old
name, replace `inky-bird` with `inky-bird-frame` in each name. On a systemd
controller:

```bash
for unit in \
  inky-bird-controller.service \
  inky-bird-scheduler.service \
  inky-bird-refresh.timer \
  inky-bird-generate.timer \
  inky-bird-catalog-publish.timer \
  inky-bird-notifications.timer; do
  sudo systemctl disable --now "$unit" 2>/dev/null || true
done
sudo rm -f /etc/systemd/system/inky-bird-{controller,scheduler,refresh,generate,catalog-publish,notifications}.{service,timer}
sudo systemctl daemon-reload
```

Missing optional units are harmless. On the display:

```bash
sudo systemctl disable --now inky-bird-display.timer
sudo rm -f /etc/systemd/system/inky-bird-display.{service,timer}
sudo systemctl daemon-reload
```

On macOS, unload each installed label and remove its matching plist from
`~/Library/LaunchAgents`:

```bash
for label in serve scheduler refresh generate catalog-publish notifications; do
  launchctl bootout "gui/$(id -u)/com.inky-bird.$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.inky-bird.$label.plist"
done
```

After service removal, delete `~/Services/inky-bird` only if its managed
runtime is no longer needed. Delete the private support/configuration directory
only after saving any catalog or state you want to keep.

## Browser applications

Recipes for another web application that reads the catalog, for framing the
featured plate, and for a Homepage dashboard are in the
[reference page](reference.md#browser-applications).

### Homepage dashboard

See [Homepage dashboard](reference.md#homepage-dashboard) in the reference page.

## Troubleshooting

Start with the role's doctor command. Its JSON output gives every check a stable
ID, status, summary, and remediation. Continue with the focused runbook in
[`troubleshooting.md`](troubleshooting.md).

## Authoritative references

- [Inky reference page](reference.md): HTTP errors, caching and security
  headers, external browser applications and Homepage.

- [OpenAI Codex CLI](https://github.com/openai/codex#installing-and-running-codex-cli)
  and [authentication](https://learn.chatgpt.com/docs/auth)
- [uv installation](https://docs.astral.sh/uv/getting-started/installation/) and
  [project environments](https://docs.astral.sh/uv/guides/projects/)
- [Raspberry Pi Imager](https://www.raspberrypi.com/software/),
  [Raspberry Pi OS](https://www.raspberrypi.com/documentation/computers/os.html),
  and [SSH setup](https://www.raspberrypi.com/documentation/computers/remote-access.html)
- [Pimoroni PIM773 and PIM774 hardware](https://shop.pimoroni.com/products/inky-impression)
  and [Inky Python installation](https://github.com/pimoroni/inky#installation)
- [Apple launchd jobs](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
- [systemd service control](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)
  and [timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)
- [Docker Compose](https://docs.docker.com/compose/) and
  [service startup ordering](https://docs.docker.com/compose/how-tos/startup-order/)
