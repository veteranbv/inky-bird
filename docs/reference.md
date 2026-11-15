# Reference

Details for people who connect other software to Inky: the HTTP error format,
caching and security headers, and recipes for external browser applications
and Homepage. For everyday setup, start with the [installation guide](installation.md)
or the [Docker guide](docker.md).

## HTTP errors

Every JSON error has `"ok": false` and `"schema_version": 1`. The `error` value
takes one of two forms, depending on the address.

| Addresses | `error` value |
| --- | --- |
| `/`, `/app`, `/favicon.ico`, `/screen` and `/v1/access` | object with `code` and `message` |
| Everything under `/app/`, `/v1/owner/`, `/v1/screen/`, `/v1/integrations/`, `/v1/dashboard/` and `/wall/`, except the two display reports in the next row | object with `code` and `message` |
| `/wall/v1/display-fetch` and `/wall/v1/display-success` | plain string |
| `/health`, `/v1/catalog`, `/v1/embed/featured-plate`, `/v1/assets/…`, `/v1/display-fetch` and `/v1/display-success` | plain string |
| Any other address | plain string: 404 `not found` |

The `/wall/` display reports are checked by Inky's access gate before the
report itself runs, so a refusal from that gate still uses the object form:
403 `secure_access_required` when the request did not use the configured
address, and 405 `method_not_allowed` for any method other than `POST`.

An unexpected fault inside the server answers 500 with the plain text
`Internal Server Error` on any address, not JSON. The service log keeps the
cause.

### Owner, screen and integration routes

These addresses answer an error as:

```json
{"ok": false, "error": {"code": "not_found", "message": "This Inky address does not exist"}, "schema_version": 1}
```

`code` is one of the fixed values below; scripts should branch on it and on the
HTTP status, not on the message. The message is safe to show and never holds a
path or address; the private service log keeps the cause.

| `code` | Status | Meaning |
| --- | --- | --- |
| `sign_in_required` | 401 | Sign in first, or the sign-in has ended. |
| `sign_in_rate_limited` | 429 | Too many sign-in or pairing attempts; wait and try again. |
| `confirmation_required` | 403 | This change needs a recent sign-in confirmation. |
| `password_confirmation_failed` | 403 | The confirmation password was wrong. |
| `secure_access_required` | 403 | Open the configured address; use HTTPS where this installation requires it. |
| `origin_denied` | 403 | A change came from another site or address. Open Inky directly. |
| `feature_unavailable` | 403 | This installation has the feature turned off. |
| `access_error` | 400 | Another access or sign-in problem. |
| `birdbuddy_account` | 400 | A Bird Buddy account action did not finish. History is kept. |
| `journal_error` | 400 | The request does not fit the journal, for example an invalid edit. |
| `operation_error` | 400 | An owner operation could not run. |
| `request_incomplete` | 400 | The request ended before its body arrived. |
| `not_found` | 404 | No such address or action. |
| `method_not_allowed` | 405 | The address exists but not for this method. The `Allow` header lists the methods it takes. |
| `journal_changed` | 409 | The journal or settings changed since the page loaded. Reload and try again. |
| `operation_busy` | 409 | Another owner operation is running. |
| `data_source_unavailable` | 503 | A bird data source, such as retained eBird history, cannot be read right now. |
| `dependency_missing` | 503 | A component Inky needs is not installed. |
| `journal_unavailable` | 503 | Local state is damaged or unreadable. Check storage and the approved catalog; do not delete private history. |

### Catalog and display routes

These addresses keep their original form, with the error as a plain string:

```json
{"ok": false, "error": "not found", "schema_version": 1}
```

A wrong method answers 405 `method not allowed` with an `Allow` header:
`GET, HEAD` for the catalog reads, `POST` for `/v1/display-fetch` and
`/v1/display-success`. `/v1/catalog` answers 503 `active catalog unavailable`
when the active catalog cannot be read. An image under `/v1/assets/` that is
not part of the active catalog answers 404 `not found`; one that cannot be read
or no longer matches its catalog checksum answers 503 `active asset
unavailable`, or `active catalog unavailable` when the catalog itself cannot be
read. `/v1/embed/featured-plate` is an HTML page, so when the catalog cannot be
read it answers 503 with a page that says "Featured plate unavailable".

### Display reports

A display reports a fetch to `/v1/display-fetch` and a finished update to
`/v1/display-success`, or to the same paths under `/wall/` when its controller
address ends in `/wall`. The body must be exactly `{"schema_version": 1}`, sent as
`application/json`. Success answers 200 `{"ok": true, "schema_version": 1}`.
Both paths answer these errors with a plain string:

| Status | `error` | When |
| --- | --- | --- |
| 400 | `invalid content length` | The `Content-Length` header is not a number. |
| 400 | `request incomplete` | The request ended before its body arrived. |
| 400 | `invalid JSON` | The body is not JSON. |
| 400 | `invalid telemetry payload` | The body is JSON but not exactly `{"schema_version": 1}`. |
| 403 | `browser telemetry is not accepted` | The request has an `Origin` header, so it came from a browser. |
| 411 | `content length required` | There is not exactly one `Content-Length` header. |
| 413 | `request body too large` | The body is over 1,024 bytes. |
| 415 | `expected application/json` | The content type is not `application/json`. |
| 503 | `display telemetry unavailable` | Inky could not save the report. |

### `HEAD` requests

Every `GET` route answers `HEAD` with the same status and headers and no body,
so `HEAD`-based uptime monitors work on `/health` and `/v1/catalog`. The
single-sign-on callback (`/v1/owner/oidc/callback`) refuses `HEAD` with 405, so
a probe never uses up a sign-in.

## Caching and security headers

Plate images whose address names the current plate revision (portraits, grid
thumbnails and paired-screen plates) are kept in the browser's private cache
for up to a year. The app's own files (pages, scripts, styles and icons) are
`no-cache`, so the browser checks them on each load. Every other owner and
private response is `no-store`.
Signing out and **Forget this screen** send `Clear-Site-Data: "cache"`, so the
browser drops those cached plates. Browsers apply that header only over HTTPS
or on the controller's own loopback address; over plain LAN HTTP, clear the
browser's cache yourself.

Those app files carry an `ETag` made from the installed file's time and size,
and an unchanged file answers 304. Installing a release normally resets those
times, so browsers fetch the new files.

Inky does not send `Strict-Transport-Security`. Browsers would otherwise refuse
plain HTTP on that hostname for a long time and stop letting you click through
a certificate error. If you want it, set it at your reverse proxy.

The app serves `/app/manifest.webmanifest` for **Add to Home Screen**. The
proxy recipes in the [access guide](browser-access.md) already route `/app/*`.

## Browser applications

For Inky's own bundled journal, follow [first setup](browser-access.md) and
[the journal guide](journal.md). For paired screens, selected wall views and
scoped dashboard recipes, use [presentation setup](presentation.md). The
compatible external catalog interface below remains separate from owner access.

The [owner access page](browser-access.md) is separate from the read-only
integration routes below. This section is for an operator-supplied application
that consumes the catalog API or frames its narrow featured-plate embed.
Both permissions are disabled by default. To let a trusted web application
fetch the active catalog and its images directly, list its exact origin under
`[controller]`:

```toml
[controller]
cors_allowed_origins = ["https://frame.example.test"]
```

An origin contains only the scheme, host, and optional port. Inky rejects
wildcards, credentials, paths, queries, and fragments. Use an ASCII hostname
or the canonical form of an IP address. Internationalized and punycode
hostnames are not supported. A matching origin can read `GET /v1/catalog` and
the active catalog's portrait and display PNGs under `GET /v1/assets/`. Other
catalog files, inactive plates, health, the featured-plate embed, and display
telemetry do not receive cross-origin access. Requests from other origins
receive the normal response without an `Access-Control-Allow-Origin` header.

Framing is a separate permission. List each trusted parent origin explicitly:

```toml
[controller]
embed_allowed_origins = ["https://home.example.test"]
```

The controller adds those origins to the embed's Content Security Policy
`frame-ancestors` directive. CSP does not reliably match IP-address sources, so
framing origins must use DNS hostnames. Underscores and trailing dots are also
rejected. Unlisted sites cannot frame the embed. Keeping CORS and framing
separate means an existing catalog reader does not silently gain permission to
present controller content inside its pages.

The browser catalog includes active species identity, approval time, image
paths and checksums, and any available observation count and latest-detection
time. It does not include provider names, raw observations, locations,
credentials, or private provider state. Browser reads never refresh the
physical display's health signals. The server also verifies an active image's
catalog checksum before it sends the file.

This setting does not add authentication, TLS, or public-internet safety. Keep
the controller private. Prefer a same-origin application proxy, which needs no
CORS permission and can authenticate the browser before making a server-side
request to Inky. Direct public-HTTPS-to-local-network behavior varies by
browser: the browser may block mixed content, ask for local-network permission,
or allow only specific local address forms. A VPN does not make those browser
rules consistent. Terminate HTTPS for the controller or use the same-origin
proxy when you need a portable path.

Direct cross-origin access through cookie, session, client-certificate, or HTTP
Authorization proxies is not compatible with this interface. Inky does not
permit credentialed CORS requests and does not implement CORS or Private
Network Access preflight. Standard catalog `GET` requests use no custom request
headers. Routine application request logs use a fixed route label, method and
status, not source addresses, arbitrary paths, queries, cookies or credentials.
Do not enable proxy request logging for authentication callbacks. Normal systemd
journal or bounded private macOS log retention applies.

### Homepage dashboard

[Homepage](https://gethomepage.dev/) can show Inky with its stock service
widgets; no Homepage fork, custom JavaScript, or Inky-specific plugin is
required. Its `customapi` widget fetches through the Homepage server, while its
`iframe` widget loads in the browser.

For a compact status card, add one `customapi` widget:

```yaml
- Birds:
    - Inky Bird:
        icon: mdi-bird
        href: https://github.com/veteranbv/inky-bird
        description: Observation-driven field-journal plates
        siteMonitor: http://inky-controller:8793/v1/catalog
        widget:
          type: customapi
          url: http://inky-controller:8793/health
          refreshInterval: 300000
          mappings:
            - field: active_species
              label: Active
              format: number
            - field: approved_species
              label: Plates
              format: number
            - field: version
              label: Version
              format: text
```

For the featured-plate card, use Homepage's multiple-widget form and add the
`iframe` after the same status widget:

```yaml
- Birds:
    - Inky Bird:
        icon: mdi-bird
        href: https://github.com/veteranbv/inky-bird
        description: Observation-driven field-journal plates
        siteMonitor: http://inky-controller:8793/v1/catalog
        widgets:
          - type: customapi
            url: http://inky-controller:8793/health
            refreshInterval: 300000
            mappings:
              - field: active_species
                label: Active
                format: number
              - field: approved_species
                label: Plates
                format: number
              - field: version
                label: Version
                format: text
          - type: iframe
            src: https://inky.example.test/v1/embed/featured-plate
            classes: h-60
            allowScrolling: no
            referrerPolicy: no-referrer
```

Replace the names and URLs with addresses reachable from the Homepage server
and browser. `siteMonitor` uses `/v1/catalog`, not `/health`, because the
catalog route fails closed when active catalog state is unavailable. Inky
answers Homepage's `HEAD` readiness probe without transferring the catalog.
Every `GET` route, `/health` included, answers `HEAD` the same way.
The stats request is server-side and needs no CORS permission. Polling every
five minutes avoids Homepage's much noisier default while keeping the counts
useful.

The optional image needs a browser-reachable HTTPS route for the controller. A
true same-origin proxy beneath the Homepage origin works with the default
`frame-ancestors 'self'` policy. For a separate controller origin, as shown
above, add the Homepage origin to `embed_allowed_origins`. Make sure a reverse
proxy forwards both `/v1/embed/featured-plate` and `/v1/assets/`, and does not
replace the controller's CSP with a conflicting frame-denial header. The embed
uses a relative asset URL so those routes can share a direct origin or the same
proxy path prefix. Keep the route private; use authentication or a VPN before
traffic crosses an untrusted boundary. Allowing the origin does not secure the
controller. The iframe refreshes every 15 minutes and displays the upright
portrait, never the rotated hardware image. The embed refreshes itself, so do
not add Homepage's `refreshInterval` option. Homepage 2.1.2 accumulates iframe
timers when that option is enabled. That release also drops its documented
iframe `name` setting before rendering, so the example omits an ineffective
setting. This is an upstream accessibility limitation: Homepage 2.1.2 renders
the stock iframe without an accessible frame name. The embedded document itself
still has a descriptive title, and its image has alternative text.

Whenever any active entry has a valid detection time, the featured plate comes
from that set: newest detection wins, with approval time and taxon ID breaking
ties. Only when no active entry has a detection time does newest approval become
the fallback. BirdWeather, BirdNET-Go, and Bird Buddy are currently the only
sources that contribute those times, so this is not a universal
latest-observation view and does not claim to match the plate currently on the
frame. The embed exposes only the selected approved bird identity and image. A
valid empty catalog shows a quiet empty state; invalid or unavailable catalog
state returns `503`.
