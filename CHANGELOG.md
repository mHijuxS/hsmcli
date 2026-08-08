# Changelog

All notable changes to this project. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[semver](https://semver.org/), pre-1.0 (minor bumps may break behaviour).

## [Unreleased]

### Changed

- **`lab info` is now a working view, not a dump.** It prints the header,
  the objective/scope, the flags and the live systems — the four things you
  need while on the box. The chapter table, lesson briefing, community
  walkthrough links (spoilers) and bundle pricing moved behind `--chapters`,
  `--briefing`, `--writeups` and `--bundles`; `--all` shows everything.
  `--full` still renders every lesson and now implies `--briefing`.
  `--no-briefing` is gone — that's the default.
- Walkthrough sections are stripped from the briefing too, so `--briefing`
  no longer smuggles the link dump back in.

### Fixed

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
