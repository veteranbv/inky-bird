# Names, upgrades and compatibility

`inky-bird` is the preferred name for the repository, Python distribution,
command and controller image. An existing installation does not need to move
its data or replace its service identities to use the updated application.

| Surface | Preferred for new installations | Existing installations |
| --- | --- | --- |
| Repository | `veteranbv/inky-bird` | Old GitHub URLs redirect after the rename; keep the old repository name unused. |
| Python distribution | `inky-bird` | Remove the old distribution before installing the new one; see below. |
| Command | `inky-bird` | `inky-bird-frame` remains a supported alias, including JSON output. |
| Controller image | `ghcr.io/veteranbv/inky-bird` | `ghcr.io/veteranbv/inky-bird-frame` receives the same builds and tags. No sunset is scheduled. |
| Native runtime, config and services | `inky-bird` / `com.inky-bird.*` | Setup retains the detected `inky-bird-frame` family and its paths. |
| Compose project and volumes | Stable `inky-bird-frame` project | Unchanged. The image name does not select a new data volume. |

The Python import namespace stays `inky_bird_frame`. Status API fields,
`INKY_*` environment variables, Home Assistant entity/unique IDs, the AppArmor
profile and container license paths stay compatible. Do not reset owner access,
provider credentials, collections, retry records or budgets for a name change.

## Native installations

Clean installs use `~/Services/inky-bird`, `~/Library/Application Support/Inky Bird`
on macOS or `~/.config/inky-bird` on Linux, and the `inky-bird` service family.
Existing installs keep their detected configuration/runtime/service family.
Explicit custom paths remain supported.

Setup, doctor and uninstall find the installation the same way, without
changing anything. A support folder (even with a lost config), managed
environment, installed unit or plist, or loaded service or timer identifies an
installation. When only one name has services, or the configuration you pass
to setup is in one name's support folder, that installation is used and
leftover data under the other name does not block it. When both names still
look installed, setup and uninstall stop and list what they found, and doctor
reports the conflict; see
[Troubleshooting](troubleshooting.md#find-your-installations-names-and-paths).
Do not remove state or start a second scheduler to get past it. The user
LaunchAgent installer also refuses to compete with administrator-managed system
LaunchDaemons under either name.

Uninstall removes only the selected family's jobs, including its dynamic
scheduler. It leaves configuration, artwork, history, authentication and the
runtime in place. Keeping old names on an existing install is supported, not a
partially completed migration.

## Python environments

The two distribution names own the same `inky_bird_frame/` files. **Do not
install them side by side**, and do not uninstall the old distribution after
installing the new one: that can remove files from the working package.

The locked native controller installer uses `uv sync`, which reconciles its
environment to the selected project. The display installer removes the old
distribution before installing the renamed one. For every display runtime upgrade,
it requires the timer and service to be inactive before it changes any files.
Use the existing service family, preserve the last panel image, and record the
original enabled/active state for restoration.

If you install the wheel yourself with pip:

1. Download the new wheel and its dependencies, and keep the old wheel so you
   can go back.
2. Stop the role's services and timers, then take a backup with the
   [backup guide](backup.md). A source checkout is not a backup.
3. In that virtual environment, uninstall `inky-bird-frame` **first**, then
   install the `inky-bird` wheel. Do not upgrade unrelated hardware packages or
   move state at the same time.
4. Check that both commands run and that `doctor` passes, then start the
   services again and wait for the next display update to succeed.
5. If something fails, stop the role and restore the old runtime and your
   backup. To go back by wheel, uninstall `inky-bird` before reinstalling the
   old wheel.

The name change does not alter the catalog format or the private state files.

## Containers and release bundles

Every build is published under both image names with the same digest, so an
old image name does not mean old code. To check what you run, compare the image
digest or source revision, not the name.

New releases provide `inky-bird-docker.tar.gz`. The old
`inky-bird-frame-docker.tar.gz` filename remains a byte-identical download alias;
both contain the canonical `inky-bird-docker/` directory. Use the supplied
checksum and the documented `--strip-components=1` extraction. Keep existing
`.env`, private configuration and Compose volumes when updating the image.
