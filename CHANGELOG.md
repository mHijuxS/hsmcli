# Changelog

All notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[semver](https://semver.org/), pre-1.0 (minor bumps may break behaviour).

## [0.3.0] — 2026-08-31

### Added

- **`hsmcli auth` — signing in is a first-class command, not a config
  key.** `auth login` is a *guided secure cookie import*: it opens the
  browser at HackSmarter (sign in with email or the GitHub button —
  `--github` points the guidance at it without starting an OAuth flow;
  the imported session is identical), then takes the `Cookie:` header
  behind a *hidden* prompt, so the token stays out of shell history,
  scrollback and `ps`. Only the Supabase auth chunks
  (`sb-auth-auth-token.N`, matched exactly) are stored — a header without
  them is rejected, and the candidate is verified against the API *before*
  it replaces the stored session, so a bad paste can't clobber a working
  login. hsmcli does not refresh the token; renewal is re-running
  `auth login`. `auth import-cookie -` is the
  piped fallback for scripts and secret managers; `auth status` reports
  who/until-when/stored-where (exit 1 when signed out, expired, or the
  stored cookie doesn't decode to a session);
  `auth logout` removes the stored session and warns if `$HSMCLI_COOKIE`
  still overrides it. `config set-cookie` remains as a deprecated alias.
  A real PKCE/device-code flow needs an endpoint HackSmarter would have to
  provide; until then the browser does the OAuth dance and hsmcli takes
  the session it produced — it never sees a password.
- **Overwrite protection on downloads.** `vpn`, `image` and `certificate`
  no longer clobber an existing file: at a terminal they ask, in a script
  they refuse (exit 2) unless `--force` is passed.
- `--debug` masks credential-bearing response fields (IAM secret keys,
  signed URLs, tokens) so a trace is safe to paste into a bug report;
  `HSMCLI_DEBUG_RAW=1` restores the unredacted payload.
- An autouse test fixture blocks real sockets, so a stray unmocked request
  in a future test fails instead of hanging CI.

### Changed

- **Structured output is now a contract.** `--json`/`--yaml` emit exactly
  one document on stdout; warnings, progress and next-step hints go to
  stderr everywhere (previously warnings and the launch-timeout prose
  landed on stdout, corrupting `--json > out.json`). `launch --json`
  honours `--wait` (the default) and emits the *final* live state — IP
  included — instead of returning the power-on ACK early; `--no-wait`
  emits the ACK. `whoami --json` exits 1 when the session is missing or
  the profile fetch failed, instead of embedding the error and exiting 0.
- **`vpn --print` prints only the profile on stdout** (confirmation moves
  to stderr), so `vpn --print > lab.ovpn` produces a working profile; it
  no longer also writes the default file unless `-o` asks.
- `reset` labels the immediate follow-up read honestly ("may still show
  the old machine") instead of presenting the pre-reset address as the
  "new IP".
- `config set-base-url` requires `https://` — an `http://` URL would send
  the whole Supabase session in cleartext. `--allow-insecure-http` exists
  for local development; URLs with embedded credentials or no hostname are
  rejected outright. With a loopback base URL the stored session is scoped
  to that host (it stays pinned to `.hacksmarter.org` for everything else,
  so a hostile base URL still can't exfiltrate it).
- Broken pipes (`hsmcli … | head`) exit 141 silently instead of reporting
  "[Errno 32] Broken pipe" as an API failure.
- `launch --timeout` rejects zero and negative values.

### Fixed

- **No API request could time out** — `requests` defaults to waiting
  forever, so a stalled server hung the CLI (behind a live spinner, during
  `launch --wait`). Every call now carries a connect/read timeout
  (5s/30s; 120s read for downloads).
- **VPN profiles were written world-readable.** They embed the client's
  private key; they're now created `0600` like the config file.
- `--config-dir` pointed at a pre-existing directory no longer chmods it
  to 0700 — tightening is reserved for directories hsmcli created and for
  the default `~/.hsmcli`. (A shared directory, or `/tmp`, could be
  silently privatised before.)
- Config writes are atomic (temp file + rename), so an interrupted write
  can't truncate the stored session; a corrupt config file now says so on
  stderr instead of silently signing you out.
- The release workflow no longer interpolates the dispatch input into
  shell code (textbook injection, even if only maintainers could reach
  it), and validates it against a version pattern.

### Packaging

- License metadata migrated to PEP 639 (`license = "MIT"` +
  `license-files`); the deprecated table form and classifier were on
  setuptools' removal path and would eventually have broken the release
  build.
- The AKIA-shaped AWS key id in a test fixture was replaced with AWS's
  documented placeholder, so secret scanners don't flag the first public
  push.

### Added (earlier in the 0.3.0 cycle)

- **`labs list -T/--topic` — the website's subject filter.** The catalog
  page's chips (AWS, Web, Windows, Linux, Active Directory, Blue Team,
  Guided Lab, Miscellaneous) are derived client-side from each card's
  `subtitle`; there is no topic field in the API. `--topic` reimplements
  that match verbatim — same keywords, same word-boundary rule, same
  ordering — and was diffed against the page's own JS bundle over all 88
  catalog items with zero divergence. Repeatable (`-T aws -T web` ORs),
  and it accepts what people type: `ad`, `active directory`, `web app`,
  `misc`, `guided`. Pairs with the existing `-d/--difficulty`, which the
  page derives the same way from the `(Hard)` suffix.
- A **Topic** column on `labs list` and a topic line on `lab <name> info`,
  both shown only when there's more than one topic to distinguish.
- `--sort topic`.

### Changed

- **Output is written for a person now.** The API's internal vocabulary is
  translated on the way out — `na` and `not_launched` read as `off`,
  `in_progress` as `in progress`, `unanswered` as `unsolved` — and lab names
  lose the `Challenge Lab:` prefix and `(Easy)` suffix that the difficulty
  and category columns already carry. `--json` / `--yaml` are untouched:
  they still emit HackSmarter's own spelling, so scripts keep matching on
  `running`.
- **Every command ends by naming the next one.** `enroll` points at
  `launch`, `launch` at `vpn` (with the `openvpn` line and an `nmap` at the
  IP it just got), `vpn` at `openvpn`, a solved flag at the remaining one.
  Suggestions quote the identifier you typed, not the resolved UUID.
- **Errors say what went wrong and what fixes it.** A 403 was printed as
  `HTTP 403 (forbidden) on GET /api/student/courses/<uuid>/take:
  {"error":"forbidden","message":"Forbidden"}. Not enrolled? Try: hsmcli lab
  <uuid> enroll`; it now reads `✗ You're not enrolled in dark` followed by
  `→ hsmcli lab dark enroll`. Expired cookies, rate limits, HackSmarter-side
  5xx and being offline each get their own wording. The technical string is
  still on the exception for `--debug` and bug reports.
- **Errors and their hints go to stderr**, so `--json > out.json` can't pick
  up prose.
- **Tables only show columns that carry information.** Machine UUIDs appear
  when there's more than one machine to tell apart, expiry when the lab sets
  one, points when the lab scores its flags; `match_type` (always `exact`)
  is gone. The lab card puts the name in its border instead of repeating it
  three ways, and `runtime: 80h·GB` reads as `80 GB-hours of runtime
  included`.
- **Panels and tables stop at 100 columns** instead of stretching across a
  200-column terminal.
- **`lab <name> vpn` writes `./dark.ovpn`**, not
  `./hsm-bb164cba-ddc9-4cb0-8e95-ad4853d0143c.ovpn`. `lab <name> image` is
  named after the lab too. `-o` still overrides.
- **`launch --wait` shows a spinner** carrying the current state and elapsed
  time, keeps one permanent line per state change, and reports how long the
  machine took.
- `notifications`, `events`, `subscriptions`, `orgs`, `bundles` and `exams`
  render a table instead of dumping raw JSON. `--json` for every field.
- `config show` reports whether the session is usable and for how much
  longer, instead of printing 40 characters of the cookie. `set-cookie`
  names who you just signed in as.
- `hsmcli` with no arguments prints a short getting-started card rather than
  the full argparse help; the sign-in step drops off once you're signed in.
- `enroll` no longer prints `{"success": true}` under the ✓.

### Added

- `--version`, and `--no-color` (`NO_COLOR` is honoured too — as is a
  non-terminal stdout, which now drops the styling automatically).
- Ctrl-C during a launch poll exits 130 with a one-line note instead of a
  traceback.
- Commands that need a session say so up front instead of letting the first
  call come back 401.
- `HttpError`, a shared base for `AuthError` / `ForbiddenError` /
  `APIError`, so every failed call carries `.status`, `.endpoint` and
  `.body`. `.server_message()` returns the API's own explanation with bare
  restatements of the status code ("Forbidden") filtered out.

- **`lab info` is a working view, not a dump.** It prints the header, the
  objective/scope, the flags and the live systems — the four things you
  need while on the box. The chapter table, lesson briefing, community
  walkthrough links (spoilers) and bundle pricing moved behind `--chapters`,
  `--briefing`, `--writeups` and `--bundles`; `--all` shows everything.
  `--full` still renders every lesson and now implies `--briefing`.
  `--no-briefing` is gone — that's the default.
- Walkthrough sections are stripped from the briefing too, so `--briefing`
  no longer smuggles the link dump back in.

### Fixed

- **`python -m hsmcli` always exited 0**, even on failure — it called
  `main()` without `sys.exit()`, so every error looked like a success to a
  script. The installed `hsmcli` entry point was never affected.
- **`launch` failed on a lab that was already up or still booting.** It
  powered on unconditionally; the server answers that with "System is
  already running", or a 400 while the instance is still provisioning, so a
  successful launch reported `✗ 400 Client Error` and exited non-zero.
  `launch` now reads the live state first — already running prints the
  status and exits 0, already booting skips straight to the poll — and a
  power call rejected right after `/launch` was accepted is treated as
  provisioning rather than failure.
- Power/launch/reset calls now raise the same typed errors as every other
  request, so a rejection shows the server's reason instead of just
  `400 Client Error: Bad Request for url: …`.

## [0.2.0] — 2026-08-05

### Fixed

- **`labs list` only ever showed part of your labs.** It read
  `/api/student/catalog`, which returns the current storefront cards — 41 of
  81 courses on a subscriber account. Labs bought outside that storefront
  were invisible, including in-progress ones. It now merges
  `/api/student/courses` (the complete set) with `/catalog`.
- **`lab <name> <action>` could target the wrong lab.** `resolve_course_id`
  tried the complete list, swallowed the ambiguity error, then re-resolved
  against the partial catalog — so `lab odyssey launch` silently picked
  *Hack With Me: Active Directory (Odyssey x Triathlon)*, the only Odyssey
  the catalog knows.
- `extract_lessons` crashed on a `{"course": null}` payload, cascading
  through `extract_aws_labs` into `_ensure_playthrough` and taking every
  lifecycle command with it.
- `heartbeat <name>` passed the identifier through unresolved and failed with
  `400 Invalid uuid`, despite the README documenting a name.
- `--config-dir a/b/c` raised a bare `FileNotFoundError`.
- `hsmcli labs` and `hsmcli config` printed help and exited **0**, so an
  incomplete command read as success to a script. Now exit 2.
- `detect_public_ip` validated with a character class that accepted
  `.......`, `::::::::` and `999.999.999.999`, any of which would be sent on
  as an AWS lab's `allowed_ip`. Now validated with `ipaddress`.
- The sdist was missing `tests/conftest.py`, so the published test suite
  could not run.

### Security

- The config file holds the whole Supabase session — access *and* refresh
  token — and was written `0644` inside a `0755` directory, readable by any
  local user. It is now created `0600` (via `os.open`, so there's no
  world-readable window between write and chmod) inside a `0700` directory,
  and a looser mode left by an earlier version is repaired on next run.
- `config set-cookie` validated nothing: a bare token pasted without its
  `name=` saved fine and then failed every call with "cookie may be
  expired". It now rejects input that isn't a Cookie header and warns when no
  `sb-auth-auth-token.N` chunk is present.

### Changed

- **`labs list` defaults to challenge labs** (60 of 81). `-c all` widens;
  the footer names the active narrowing so a filtered list never reads as
  complete.
- **`--debug` no longer exits after the first response.** It traces every
  request to stderr and continues, so `labs list` shows both endpoints it
  reads, `--debug --json` stays pipeable, and lifecycle calls through
  `_power_call` are traced too.
- **The `User-Agent` names this client** instead of posing as Firefox.
  HackSmarter doesn't filter on it — verified across eight endpoints
  including a POST. `HSMCLI_USER_AGENT` and `BROWSER_USER_AGENT` remain as a
  fallback.
- Name resolution gained a core-name tie-break (category prefix and
  difficulty suffix stripped), so `odyssey` picks the lab actually named
  Odyssey and `shadowgate` no longer collides with `ShadowGate2`.
- `-t owned` and `-t not_started` are now the same filter, since the two
  endpoints spell that state differently.
- **`requires-python` is now `>=3.9`** (was an untested `>=3.8`; 3.8 is EOL).
  CI verifies 3.9 through 3.14.

### Added

- Typed exceptions — `HsmcliError` and `AuthError`, `ForbiddenError`,
  `NotEnrolledError`, `APIError`, `TransportError` — so library callers can
  tell an expired cookie from a 404 without matching message text. All
  subclass `Exception`, so existing handlers are unaffected.
- `labs list --catalog` for the old storefront-only view.
- A LICENSE file. MIT was declared in metadata but the text was absent, so
  the wheel shipped only the claim.
- Test suite (215 cases) over the payload-shape layer, and CI on 3.9–3.14
  with `build` + `twine check` and a guard that LICENSE stays in the wheel.

### Removed

- `hsmcli.py` and `install.sh` — redundant with the console script and
  `python -m hsmcli`; install.sh also ran `sudo ln` when its own writability
  check had already passed, and `pip3 install -e .` into a system Python that
  PEP 668 blocks.
- `requirements.txt` and the hardcoded `__version__`; `pyproject.toml` is now
  the single source for both.

## [0.1.0]

Initial version: catalog/lab listing, lab info with briefing rendering,
systems and networks lifecycle (launch/stop/reset), AWS-lab lifecycle with
IAM credential export, VPN config download, flag submission, heartbeat.
