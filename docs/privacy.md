# Privacy and data flow

Inky keeps your journal on your controller. You can browse the plates
without connecting an observation source or signing in to Codex. A phone or
tablet uses the controller through its browser; it does not need its own copy
of your journal.

## What Inky stores

Your observations, imported history, collections, work records, and owner
access state are private controller data. Your configuration and some state
files can also contain credentials. The browser can retain session and view
state. Keep [controller backups](backup.md) and exports private. Removing an
import does not necessarily remove birds on your frame, generated work, or old backups;
follow the [private-data removal guide](backup.md#private-data-lifecycle-and-removal).

Uploaded import files are kept only for the preview period you set. An upload
that stopped part-way (still receiving after an hour), or a raw file without its
preview record, is removed at startup and by the regular cleanup instead of
waiting for the full period.

Imported eBird checklists are linked by an unsalted SHA-256 of each submission
ID. That keeps raw IDs out of the files, but submission IDs are short
sequential identifiers and can be recovered by trying every one. Treat those files as private, not
anonymous.

For Bird Buddy, a dedicated guest account is the recommended least-privilege
choice. An operator can explicitly accept the broader permissions of an owner
account instead. Its refresh token retains that account's permissions; choosing
one feeder in Inky does not make the token read-only or reduce its scope. See
[Bird Buddy setup](discovery.md#bird-buddy-setup) for authorization and token custody.

The output of scheduled `refresh` and `generate` runs keeps the `place_name`
and `state` fields but leaves them empty, so service logs do not record where
you live. The place stays only in private controller state. `discover`, which
you run by hand, still prints the location so you can check it. Service logs
are private; an unexpected provider error can still write its own text there.

A plate whose text contains your postal code, coordinates, discovered place
with its state, your home directory or the controller's own paths (paths at
least two folders deep, so not `/root`) cannot be approved from the app, even
with publication off.

## What can leave the controller

| When you use… | What to expect |
| --- | --- |
| [Observation sources](discovery.md) | Inky makes requests to the sources you enable. A nearby-observation request can include the configured search area. Account-backed sources use the credentials you supply. Imported species names are sent to iNaturalist to match them to its taxonomy. |
| [Plate generation](architecture.md) | Inky fetches licensed references and uses Codex for species research, illustration, and review. Generation is optional and off in new configurations. When you turn it on, it uses your own Codex account and that provider's charges apply. |
| [Notifications](notifications.md) | Inky sends subscribed events to the destinations you configure through Apprise. Some messages include species names. |
| [Single sign-on](browser-access.md) | Your browser and controller communicate with the identity provider you configure. Ordinary local sign-in does not require an identity provider. |
| [Catalog publication](operations.md) | If you enable it, the publisher sends approved plates and their metadata to your configured GitHub repository. |

## Shared art and private screens

The reusable catalog contains location-neutral plates, species facts, sources,
and review and provenance records. It excludes your observations, locations,
account data, and private selections.

Your controller's active catalog is a different surface. Its species list and
available observation counts or detection times can reveal what your installation
has seen. Owner sign-in does not protect the legacy catalog and display routes.
Keep the controller on a trusted network. Do not port-forward it to the
internet. Use the [access guide](browser-access.md) for private access and the
[presentation guide](presentation.md) for scoped screen and dashboard access.
