# Browser journal

The browser uses your controller's plates and private selections. It does not
run another discovery service or keep a second copy of your history in browser
storage. Open `/app` and [set up your owner account](browser-access.md).

This guide describes the app on `main`. Start with [approved art](settings.md#start-with-approved-art)
if you want to try it without generation, connected sources, or a physical display.

## Find your way around

The tabs sit at the bottom on phones and at the top on wide screens. If you
already run Inky, follow the [migration guide](migration-v1.md) to keep your
configuration and data.

- **Home** shows **Your journal**: a plate from your frame, the birds noted
  **Lately** and a count of your birds and plates. With nothing on your frame
  yet, it shows a plate from the catalog and **Choose birds**. When a new plate
  is ready, Home says so.
- **Birds** has two lists. **Your birds** holds every bird in your journal, with
  a plate or still waiting for one. **More plates** holds the plates you could
  add to your frame. Search with **Find a bird**, sort by name, scientific name,
  when a bird was last noted or when its plate was added, and show only
  **Favorites**, **On my frame**, birds **Noted by you** or birds with **No
  plate yet**. Open a bird to read its plate's reviewed facts and sources.
  Older plates with incomplete profiles say so.
- **Collections** keeps **On my frame**, **On my frame · added by you**, your
  **Favorites**, the collections you make and **All birds**, each with its
  plates first.
- **Play** shows plates full screen in your browser, one a minute. For a shared
  screen, [pair a read-only view](presentation.md#pair-a-read-only-screen).
- **Manage** keeps the machinery, in five groups:
  - **Birds and sources**: **Sources**, where your birds come from and when
    each was last checked.
  - **Plates**: **In the works**, which plates are being made, what is waiting,
    new plates to look at and plates that need a hand (**Needs a hand**). See
    [owner actions](owner-operations.md).
  - **Frame and displays**: **Wall frame**, what your wall frame shows, when it
    last changed and the screens you have paired.
  - **Integrations**: Home Assistant and Homepage, and notifications.
  - **System**: settings, health, backups and upgrades, and advanced tools.

### What you keep and what you saw

A plate in **More plates** is yours to browse. It is not a record of a bird you
noted. Each source keeps its own counts: eBird checklists and station
detections stay separate. Retained eBird, Analyzer and selected-feeder Bird
Buddy history can go back beyond the live window and species limit. Unknown
or ambiguous names stay private and unmatched.

**On my frame** is the set of birds your wall frame and Play show by default:
the birds your sources noted recently, plus the birds you added. The birds you
added are also listed on their own as **On my frame · added by you**, the list
the CLI collection commands manage.

To add a bird with a plate, open it and choose **Add to my frame**. The change
happens at once and offers **Undo**. **Remove from my frame** works the same
way. Inky asks you to confirm first only when Undo could not put things back:
when the bird has no plate yet, so it could not be added again, or when the
change would also move other birds. **Add every bird with a plate**, on
**On my frame · added by you**, also shows what will change and waits for
**Change my frame**. A change reaches the wall frame at its next refresh while
it shows **On my frame** or **On my frame · added by you**; a
[selected wall view](presentation.md#choose-the-physical-wall-view) does not
change. If
another tab or device made the same change first, the page says so and sends
nothing.

Choose **Add to Favorites** on a bird's page to mark it, or save birds to a
collection for Play. Neither changes your frame, writes `collection.json` or
asks for a plate. The optional [selected wall view](presentation.md#choose-the-physical-wall-view)
is set separately. Birds without a plate stay visible in a collection, and
asking for their plates has its own confirmation. If another tab saved first,
Inky refuses your older edit; reload to see what the controller saved.

Portable collections carry labels, dates, species and declared origins.
Private checklist links and credentials stay out. Import previews matches and
leaves unknown names unresolved. For personal records, [import an eBird
archive or Analyzer results](history-imports.md) through the separate history
flow. Saving a trip selects complete checklists and changes neither plate
making nor your frame.

For a place you want to explore, use **Birds reported near a place**. The
search sends the location and dates to iNaturalist. Review the species before
saving; if the search finds over 500 species, narrow it and try again. Species
without an English common name are left out, and the preview says how many.
These community reports stay separate from the birds you noted.

### Playing plates and managing your setup

In Play, **Details** pauses playback. **Zoom** lets you pan around the image
without changing the plate. In fullscreen the title and notes disappear.
Controls fade between interactions, and **Bird name** toggles a small placard.
Fullscreen and wake lock depend on the browser. See [Play and screen
setup](presentation.md#play) for display options.

[App-managed settings](settings.md) let you preview source and policy changes
and keep a private backup. Existing file-managed installations keep their
configuration. Follow [discovery setup](discovery.md) for Bird Buddy permission
and login requirements. In Manage, **Wall frame** pairs screens and chooses
what your wall frame shows. **Home Assistant and Homepage** has dashboard plates
and separate status credentials. **Health and help** shows redacted checks.
**Backups and upgrades** and **Advanced tools** explain what to do on the
controller; host commands stay with the operator.

Play never records a successful wall frame update. It checks its view and
authority once a minute without renewing owner idle time. A successful empty
view stays empty. A network failure keeps the last plate with an offline
message. When the owner session ends, Play clears private labels and controls,
freezes the last visible plate and asks for sign-in. Paired screens have
separate expiry and revocation; they never renew owner authority.

The first time the journal opens after an upgrade or a change to your history,
Inky reads the whole history again before it can show your birds. Opening the
first trip after a restart does the same. With many years of checklists that
can take noticeably longer on a small computer. Later visits use the saved
summary.

## Private state and recovery

Named selections live in `state_dir/journal.json`, schema version 2. The file
contains a revision and groups with opaque IDs, names, kinds, inclusive dates
and typed members. Personal trip members keep checklist links as unsalted
SHA-256 hashes of eBird submission IDs, plus their original source scientific
names. Submission IDs are short sequential identifiers, so the hashes can be reversed by trying
every ID: they keep raw IDs out of the file, but the file is private owner data,
not anonymous. Source corrections can mark evidence
as changed without silently editing your curated membership.
Its mode must be `0600`. Inky rejects a symlink, insecure permissions, invalid
state or a file larger than 1 MiB. Individual edit requests retain the existing
8 KiB owner JSON bound. Portable import and trip selection allow up to 1 MiB
plus the small request envelope. Collection names accept 1–128 visible Unicode characters.
These are storage and request bounds, not truncation rules. Oversized changes
fail without committing a partial selection.

Writes use the existing catalog-state lock and atomic private replacement.
Two tabs cannot silently replace each other's edits. Removing a group does not
remove source history, plates or the birds on your frame. Group names can reveal
travel or personal interests. Keep the file and its backups private.

Version 1 selections remain manual selections, not inferred personal trips.
Reading them does not migrate or write state. The first edit creates an exact
private `journal-v1-backup.json`, then atomically writes version 2. Do not feed
that new schema to an older candidate. Use its matching backup and runtime.

When a file the journal reads cannot be read, the page stays up. That part is
left out and a notice explains it: the birds on your frame
(`active-catalog.json`), the list of birds waiting for plates, new plates
waiting for you, or the plates in the works (Manage then says they cannot be
read). An
unreadable species-match file marks only its own source unavailable. A damaged
`journal.json` still stops the page. Bookmarks and saved selections keep
working while an unrelated part is unreadable. A change that names a bird
missing because of the unreadable part is refused with its notice, and
collection import preview and apply are refused while a history or bird-listing
part is unreadable (never because of the plates in the works). Fix or restore the file, then
reload. The next refresh rewrites `active-catalog.json`.

The journal reads each retained history through its summary file
(`*-summary.json` beside the history; species names, counts and dates, no
places). Summaries are rebuilt automatically when missing or out of date, so
they are safe to delete. See [history imports](history-imports.md#private-files-retention-and-backup).

The owner journal response (`/v1/owner/journal`) has optional owner-only fields:

- each `retained_evidence` row has `first_noted` and `last_noted`
  (`YYYY-MM-DD`, or `null` when unknown). Analyzer dates come from dated
  segments only. Bird Buddy dates are the controller's local calendar dates when
  the history was last written, and `first_noted` is `null` once a species has
  postcards older than the 366-day window;
- `recent_species` lists at most 10 birds (`taxon_id`, `last_noted`,
  `source`), one per bird, newest first, from retained histories only, never
  from live sources;
- `notices` lists `{part, message}` for each part that could not be read; and
- `management` may be `{"available": false, "reason": "unreadable"}` when the
  plates in the works cannot be read, which differs from plate making not
  being set up.

Paired screens, `/v1/screen/view` and `/wall` responses do not include these
fields.

Use a stopped, consistent [controller backup](backup.md), not a live ZIP of
the state folder. An older release ignores this new file, but that does not
prove every newer state file is backward compatible. Roll back with the old
runtime and its matching complete backup.
