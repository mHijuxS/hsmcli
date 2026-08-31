# hsmcli

Command-line client for [HackSmarter Labs](https://www.hacksmarter.org) —
manage labs, systems, VPN, credits and lab lifecycle (launch / stop /
reset) from the terminal.

Modeled after `htbcli` and `hccli`: rich terminal output, name-based
identifier resolution, JSON/YAML output for scripting, cookie-based auth.

> **Unofficial.** Not affiliated with, endorsed by, or supported by Hack
> Smarter or CourseStack (who manage the backend platform). It drives
> exactly the endpoints a logged-in browser session uses — the same
> private JSON API the web app calls, with your own session — which is
> undocumented and can change without notice, so expect breakage. Hack
> Smarter's creator has said informally this is fine; the backend is
> CourseStack's, so their terms apply too. Use it only with your own
> account and your own labs. Requests identify themselves as `hsmcli`
> (no browser spoofing), so the operators can always see and throttle it.

## Install

```bash
git clone https://github.com/mHijuxS/hsmcli && cd hsmcli
uv tool install .        # recommended — keeps deps isolated
# or
pipx install .
# or, into the current environment
pip install .
```

`hsmcli` is also runnable without installing: `python -m hsmcli`.

## Auth

```bash
hsmcli auth login
```

is a **guided secure cookie import** — honest name: it opens HackSmarter
in your browser and you sign in as usual (email, or the GitHub button;
`--github` points the guidance at that button, it does *not* start an
OAuth flow itself — the imported session is identical either way), but
you still copy the `Cookie:` header from devtools → Network → any request
→ Request Headers → Cookie yourself. The paste goes into a **hidden
prompt**, so the token never lands in your shell history, scrollback or
`ps`. The CLI keeps only the Supabase auth chunks (`sb-auth-auth-token.N`
— a header without them is rejected, nothing saved), **verifies the paste
against the API first**, and only then replaces the stored session — a
bad paste can't clobber one that still works.

HackSmarter has no official CLI auth endpoint, so a proper PKCE /
device-code flow isn't possible for an unofficial client — the browser
does the login (including OAuth and MFA) and hsmcli imports the session
it produced. Nothing here ever sees your password.

**Renewal:** hsmcli does not refresh the Supabase token itself. When the
session expires (`hsmcli auth status` shows how long is left), the
current renewal path is running `hsmcli auth login` again.

For scripts and secret managers:

```bash
secret-tool lookup service hsmcli | hsmcli auth import-cookie -
export HSMCLI_COOKIE='…'          # env override (mind your shell history)
```

Manage the session:

```bash
hsmcli auth status                # who, until when, stored where (exit 1 if not signed in)
hsmcli auth logout                # remove the stored session
hsmcli whoami                     # full profile, via the API
```

`config set-cookie` still works but is deprecated in favour of the above.

## Cheat sheet

```bash
# Discover
hsmcli labs list                        # challenge labs on the account (the default)
hsmcli labs list -c all                 # every category: + guided/range/hackwith/foundations/courses
hsmcli labs list -c range -c guided     # category filter (repeatable)
hsmcli labs list -e                     # only /courses (the labs on your account)
hsmcli labs list --catalog              # only /catalog (storefront cards, incl. bundles)
hsmcli labs list -T ad                  # topic filter — the website's chips (repeatable)
hsmcli labs list -T aws -T web -d easy  # topic + difficulty, as on the catalog page
hsmcli labs list -d easy -d medium      # difficulty filter (repeatable)
hsmcli labs list -t in_progress         # state filter
hsmcli labs list -s "active directory"  # substring filter on name/description
hsmcli labs list --sort difficulty      # sort by name | difficulty | state | topic

# One lab — identifier comes first, then the action
hsmcli lab <name> info                  # objective/scope + flags + live systems
hsmcli lab <name> info --briefing       # + lesson content (--full for every lesson, default: first 3)
hsmcli lab <name> info --chapters       # + the chapter/lesson table
hsmcli lab <name> info --writeups       # + community walkthrough links (hidden by default: spoilers)
hsmcli lab <name> info --bundles        # + the subscription bundles this lab is in
hsmcli lab <name> info --all            # every optional section
hsmcli lab <name> take                  # raw /take payload
hsmcli lab <name> enroll                # claim it (free / covered by your plan)
hsmcli lab <name> systems               # live status of all systems
hsmcli lab <name> status                # compact "is it on?" summary

# Lifecycle
hsmcli lab <name> launch                # /launch + /power on, then poll until running (default)
hsmcli lab <name> launch --no-wait      # return immediately after /power ACKs
hsmcli lab <name> stop                  # /power off
hsmcli lab <name> reset                 # /reset (new IP assigned)
hsmcli lab <name> vpn                   # download OpenVPN config to ./<lab>.ovpn (0600)
hsmcli lab <name> vpn -o me.ovpn        # …or wherever you want it
hsmcli lab <name> vpn --print           # profile on stdout, nothing else — pipe it anywhere
hsmcli lab <name> image                 # download the lab thumbnail (--url-only to just print the URL)

# Flags
hsmcli lab <name> flags                 # list the lab's flags and their state
hsmcli lab <name> submit user '<flag>'  # submit by role, 1-based index, UUID or prompt substring
hsmcli lab <name> submit 2 '<flag>' --force   # resubmit an already-solved one

# Finishing a lab
hsmcli lab <name> complete              # mark its lessons complete (in progress → done)
hsmcli lab <name> certificate           # download the completion PDF to ./<lab>-certificate.pdf
hsmcli lab <name> cert -o me.pdf        # …or wherever you want it (alias: cert)
hsmcli lab <name> certificate --url-only  # print the one-hour signed download URL instead

# AWS labs (Second, Beanstalk, Rotation, …) — same verbs, IAM keys instead of an IP
hsmcli lab <name> launch                # start + poll until ready, then print credentials
hsmcli lab <name> launch --allowed-ip 1.2.3.4   # override the auto-detected source IP
hsmcli lab <name> creds                 # show the IAM keys again
eval "$(hsmcli lab <name> creds --export)"      # load them into the shell for `aws`
hsmcli lab <name> extend                # add another time_limit_minutes window
hsmcli lab <name> stop                  # tear the environment down
hsmcli lab <name> reset                 # tear down + re-provision (fresh keys)

# Account
hsmcli whoami
hsmcli credits                          # PAYG top-up balance
hsmcli subscriptions | orgs | bundles
hsmcli notifications | events | exams

# Misc
hsmcli heartbeat <name>                 # POST /api/heartbeat (keeps session warm)
hsmcli config show                      # session state, base URL, output format, config path
hsmcli config set-format json           # default output format (also: set-base-url, reset)
```

Downloads (`vpn`, `image`, `certificate`) never overwrite an existing file
silently: at a terminal you're asked, in a script they fail unless you pass
`--force`.

Every listing/lifecycle command accepts `--json` / `--yaml` for scripting
(the file-download commands `vpn` and `image` don't — `vpn --print` and
`image --url-only` are their pipeable forms). `--debug` traces
every request and response to **stderr** — one line per call plus the
payload — so it composes with the normal output:

```bash
hsmcli --debug labs list --json > labs.json 2> trace.log
```

`--no-color` (or `NO_COLOR=1`) drops the ANSI styling; colour is off
automatically when output isn't a terminal. `--version` prints the version.

### What it looks like

Human output is written for the middle of an engagement, not for a demo.
Every command ends by naming what you'd plausibly do next:

```console
$ hsmcli lab dark launch
✓ Starting Dark

  08:23:18  booting
  08:23:25  running  10.0.23.197
✓ Dark is up at 10.0.23.197  (23s)
Next:
  → hsmcli lab dark vpn        download the VPN profile
  → sudo openvpn dark.ovpn     connect to the lab network
  → nmap -sVC -T4 10.0.23.197  start looking
```

Failures name the fix rather than the plumbing, and the fix quotes the
name you typed:

```console
$ hsmcli lab dark launch
✗ You're not enrolled in dark, so HackSmarter won't share it yet.
  → hsmcli lab dark enroll  free, and takes a second
```

Errors, warnings, progress and their follow-up commands go to **stderr**,
so `--json > out.json` never picks up prose. In structured mode a command
emits **exactly one document** on stdout — `launch --json` waits (by
default) and emits the final live state, not the power-on ACK; add
`--no-wait` for the ACK. The API's own vocabulary (`na`, `in_progress`,
`not_launched`) is translated for human output only: `--json` always
emits exactly what HackSmarter said, so scripts keep matching on
`running`.

### Name resolution

Labs and systems accept either a UUID or a case-insensitive substring of
the name. Ambiguous matches list the candidates and exit non-zero:

```bash
hsmcli lab implicit info                     # matches "Challenge Lab: Implicit (Easy)"
hsmcli lab "nova forge" launch               # multi-word ok; punctuation/spaces ignored
hsmcli lab 37e66768-0973-4a1b-9ae6-… info    # UUID always works
```

Precedence: UUID → exact name → exact **core** name → unique substring.
The core name is the name with its category prefix and difficulty suffix
stripped, so `odyssey` picks `Challenge Lab: Odyssey (Hard)` over
`Hack With Me: Active Directory (Odyssey x Triathlon)`, which merely
contains the word.

### Categories

`labs list` shows **challenge labs only** by default — 60 of 81 on a
subscriber account. Guided labs, ranges, Hack-With-Me sessions, the
Foundations tracks and the standalone courses are a different kind of
thing, and lumping them in makes the list harder to scan. `-c all` widens
to every category; `-c range -c guided` picks specific ones. The footer
always names the active narrowing, so a filtered list never reads as
complete.

### Topics — the website's chips, reimplemented

The catalog page filters by subject (AWS, Active Directory, Web, Windows,
Linux, Blue Team, Guided Lab, Miscellaneous) and by difficulty. Neither is
a field: **there is no topic or difficulty anywhere in the API payload**.
The page derives both client-side — the topic by keyword-matching the
card's `subtitle` ("This is a Medium Active Directory challenge lab."), the
difficulty by reading the `(Hard)` suffix off the title.

`-T/--topic` reimplements that match verbatim, down to the site's own
word-boundary rule (`(^|[^a-z0-9])web([^a-z0-9]|$)`, which is why "Web App"
matches `web` and "Webhooks" doesn't) and its ordering, so `-T ad` selects
exactly what clicking the chip does. The port was diffed against the
page's own JS bundle over all 88 catalog items — topics and difficulty —
with zero divergence.

Three details worth knowing, all inherited from the site:

- **A lab can have several topics.** "Windows & Linux" and "Web and Linux"
  match both chips, and the Topic column shows `Windows/Linux`.
- **`miscellaneous` means "names no subject"**, not "other" — `Challenge
  Lab: SQL Basics (Easy)` lands there because its subtitle says only "This
  is an Easy challenge lab."
- **`guided_lab` is matched on the title, not the subtitle**, because "This
  is an Easy Guided Lab." names no subject. A guided lab that *does* name
  one (`This is an Easy AWS Guided Lab.`) matches both `-T guided` and
  `-T aws`.

The flag takes what you'd actually type: `ad`, `active directory`,
`active-directory` and `active_directory` are the same thing, as are
`web`/`web app`, `misc`/`miscellaneous`, `guided`/`guided_lab`.

This is orthogonal to `-c/--category`, which asks what *kind* of thing a
lab is (challenge / guided / range / …) rather than what it's about. The
Topic column only appears when a list actually spans more than one, so
`-T ad` doesn't waste a column repeating "AD".

### `/catalog` is not the full list

`/api/student/catalog` returns only the current storefront cards (41 vs 81
on a subscriber account), so labs bought outside it never appeared in
`labs list` — including in-progress ones. `/api/student/courses` is the
complete set; `labs list` and name resolution both use the two merged.
`/catalog` is still worth merging in: it carries `item.content_state`, and
its `course_bundle` / `event` cards are dropped from the lab list (see
`hsmcli bundles` / `hsmcli events`).

The two endpoints also disagree on state vocabulary — `/courses` says
`owned` where `/catalog` says `not_started` — so `-t owned` and
`-t not_started` are treated as the same filter.

## Endpoint map

| Command | Method | Path | Body |
|---|---|---|---|
| `whoami` | GET | `/api/student/profile` | — |
| `labs list` | GET | `/api/student/courses` + `/api/student/catalog` (merged) | — |
| `labs list -e` | GET | `/api/student/courses` | — |
| `labs list --catalog` | GET | `/api/student/catalog` | — |
| `lab info` | GET | `/api/student/courses/{id}` + `/take` (flags, briefing) | — |
| `lab take` | GET | `/api/student/courses/{id}/take` | — |
| `lab enroll` | POST | `/api/student/catalog/{catalog_item_id}/buy` | `{"purchase_option_id":null,"promo_code":null,"pwyc_price_cents":null}` |
| `lab systems` / `status` | GET | `/api/student/courses/{playthrough}/systems?courseSystemIds=[…]` | — |
| `lab launch` | POST | `.../systems/{sys}/launch` then `.../power` | `{"power":"on"}` |
| `lab stop` | POST | `.../systems/{sys}/power` | `{"power":"off"}` |
| `lab reset` | POST | `.../systems/{sys}/reset` | `{}` |
| `lab systems` / `status` / `creds` (AWS) | GET | `/api/student/content/{playthrough}/aws-labs/{lab}` | — |
| `lab launch` / `stop` / `reset` / `extend` (AWS) | POST | `/api/student/content/{playthrough}/aws-labs/{lab}/power` | `{"action":"start\|stop\|reset\|extend","inputs":{…}}` |
| `lab submit` | POST | `/api/student/content/{playthrough}/lessons/{lesson}/submit-question` | `{questionId, submission}` |
| `lab complete` | POST | `/api/student/content/{playthrough}/lessons/{lesson}/complete` | *(empty body, per lesson)* |
| `lab certificate` | GET | `/api/student/completion/course/{completion_id}/certificate` → `{url}` | — |
| `lab vpn` | GET | `/api/student/courses/{playthrough}/vpn` | — |
| `lab image` | GET | `https://images.coursestack.com/{image_path}` | — |
| `credits` | GET | `/api/student/credits/{customer_id}` | — |
| `heartbeat` | POST | `/api/heartbeat` | `{lessonId, courseId, coursePlaythroughId}` |
| `subscriptions` | GET | `/api/student/subscriptions` | — |
| `orgs` | GET | `/api/student/orgs` | — |
| `bundles` | GET | `/api/student/bundles` | — |
| `notifications` | GET | `/api/student/notifications` | — |
| `events` | GET | `/api/student/events` | — |
| `exams` | GET | `/api/exams/owned` | — |

### Two id sleight-of-hand

The public `course.id` is **not** the id the lifecycle endpoints accept.
`/systems`, `/launch`, `/power`, `/reset`, and `/vpn` all use
`course.course_playthrough.id` — a separate handle created when you
enroll. `hsmcli` auto-resolves it via `/take` transparently.

Enrolling uses a *third* id. There is no `/courses/{id}/enroll` route;
the web app claims a lab by "buying" its storefront card —
`POST /catalog/{catalog_item_id}/buy` with a null purchase option, which
the server resolves itself. Free labs and anything your subscription or
a bundle covers come back `{"state":"bought"}` with nothing charged;
anything else comes back `{"state":"checkout","session_url":…}` and
`hsmcli` prints that link and exits 2 rather than claiming you're in.
The card id is `catalog_item_id` on a `/courses` entry, or the top-level
`id` of a `/catalog` entry (whose `item.id` is the course id).

The `/api/student/content/{id}/…` routes (AWS labs, flag submission) want
that **same playthrough id**, despite the `content` segment — a lesson's
own `content.id` gets a 403 there.

### Three lab shapes

| Shape | Example | Lifecycle endpoint | What you get |
|---|---|---|---|
| systems | Implicit | `/courses/{playthrough}/systems/{id}` | one VM + IP (VPN) |
| networks | NovaForge | `/courses/{playthrough}/networks/{id}` | a subnet of VMs (VPN) |
| aws | Second | `/content/{playthrough}/aws-labs/{id}` | IAM keys, no VPN |

`hsmcli` sniffs the shape from `/take` and dispatches automatically, so
`launch` / `stop` / `reset` / `status` work the same on all three.

### AWS labs

An AWS lab runs terraform against a throwaway AWS account and scopes its
security group to a single `allowed_ip`. `launch` fills that in from the
`suggested_ip` the API itself reports (the address HackSmarter sees you
from) — pass `--allowed-ip` if the traffic will come from somewhere else,
or `--input KEY=VALUE` for any other input a lab declares in
`student_inputs`. Labs are time-boxed (`time_limit_minutes`, typically
60); `extend` buys another window.

```bash
hsmcli lab second launch                 # ~2 min of terraform, then keys
eval "$(hsmcli lab second creds --export)"
aws sts get-caller-identity
hsmcli lab second stop                   # done — stop paying for runtime
```

## Config

Stored at `~/.hsmcli/config.json` (override with `--config-dir`).

Keys:

- `cookie` — the Supabase auth chunks only (`sb-auth-auth-token.N`);
  the rest of a pasted browser header is discarded at sign-in
- `base_url` — defaults to `https://www.hacksmarter.org`; must be
  `https://` (an `http://` URL would send the session in cleartext —
  `--allow-insecure-http` exists for local development only)
- `output_format` — `table` (default) | `json` | `yaml`

Env vars:

- `HSMCLI_COOKIE` — overrides the stored cookie (note: env vars can end up
  in shell history and are inherited by child processes; the config file
  is the safer default)
- `HSMCLI_USER_AGENT` — overrides the request `User-Agent`
- `HSMCLI_DEBUG_RAW=1` — disables the `--debug` trace's masking of
  credential-bearing response fields (IAM keys, signed URLs)

The config file holds your Supabase session — access token *and* refresh
token — so it is created `0600` inside a `0700` directory (writes are
atomic), and a looser mode left by an earlier version is tightened on next
run. A pre-existing custom `--config-dir` is left with the permissions it
has. Treat the file like an SSH key. Downloaded VPN profiles carry a
private key too, and are written `0600` as well.

Every API request carries a connect/read timeout (5s/30s; 120s read for
downloads), so a stalled server fails loudly instead of hanging the CLI.

## What it talks to

Everything goes to `base_url` (`https://www.hacksmarter.org`) except:

- **`images.coursestack.com`** — lab thumbnails, for `lab <name> image`.
  Public CDN, no cookies sent.
- **`certificates-*.s3.amazonaws.com`** — completion certificate PDFs, for
  `lab <name> certificate`. The API hands out a one-hour pre-signed URL per
  request (there's no stable link); the download itself carries no cookies.
- **`api.ipify.org`, `ifconfig.me`, `icanhazip.com`** — only when starting
  an AWS lab, and only as a *fallback*. AWS labs scope their security group
  to one `allowed_ip`; hsmcli prefers the `suggested_ip` the HackSmarter
  API itself reports, and asks a third party for your egress address only
  when that field is absent. Pass `--allowed-ip <ip>` to skip the lookup
  entirely.

Requests identify themselves as `hsmcli/<version> (+<repo url>)` — no
browser spoofing. HackSmarter doesn't filter on `User-Agent` (its
same-origin check is the `Referer` header, which hsmcli sets on the calls
that need it), so there's nothing to work around. If that ever changes,
`HSMCLI_USER_AGENT` takes any string, and `hsmcli.api_client
.BROWSER_USER_AGENT` holds a browser one.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the request failed (auth, HTTP error, network) |
| 2 | your invocation was wrong — bad/missing action, ambiguous name, no match, not enrolled, refused overwrite |
| 130 | interrupted (Ctrl-C) |
| 141 | downstream closed the pipe (`hsmcli … \| head`) — silent, like any Unix tool |

`launch --wait` returns 2 on timeout as well: the machine may still be
coming up, so it isn't a failure.

## Using it as a library

`hsmcli` is a CLI first, but the client is importable. Errors are typed, so
you don't have to match on message text:

```python
from hsmcli import AuthError, HsmcliError
from hsmcli.api_client import HackSmarterAPI
from hsmcli.config import Config
from hsmcli.resolvers import all_lab_items, resolve_course_id

api = HackSmarterAPI(Config())
try:
    labs = all_lab_items(api)
    cid = resolve_course_id(api, "odyssey")
except AuthError:
    ...   # cookie expired
except HsmcliError:
    ...   # anything else this client raises
```

`HsmcliError` is the base. `HttpError` carries `.status`, `.endpoint`,
`.body` and `.server_message()` (the API's own explanation, with bare
restatements of the status code filtered out); `AuthError` (401),
`ForbiddenError` (403) and `APIError` (everything else) derive from it.
`NotEnrolledError` and `TransportError` come straight off `HsmcliError`.
All subclass `Exception`.

## Tests

```bash
uv run --with pytest --with rich --with requests --with pyyaml pytest
# or, in an env with the package installed:
pip install -e '.[dev]' && pytest
```

The suite covers the payload-shape layer — the id/name/state extractors,
the `/take` scrapers, cookie and session decoding, identifier resolution,
the two-endpoint merge, and config file permissions. Those are the parts
that break silently when HackSmarter changes a response, so they're worth
keeping green.

## Requirements

Python 3.9+, `requests`, `PyYAML`, `rich`. CI tests 3.9 through 3.14.

## Releasing

Not published to any index yet. The tool only reaches what the browser
reaches and Hack Smarter's creator has said it's fine; a formal nod from
CourseStack (who run the backend) is the last box before a public index.
The path is prepared, though:

1. Bump `version` in `pyproject.toml` and add a `CHANGELOG.md` entry.
2. `git tag -a vX.Y.Z -m "…" && git push --tags`
3. Run the **release** workflow (manual dispatch only) with the same version.
   It builds, runs `twine check`, unpacks the sdist and runs the test suite
   from *inside* it, then attaches the artifacts.

The workflow deliberately has no PyPI step, so nothing can be uploaded by
accident. Adding one later means a job with Trusted Publishing — see the
comment at the top of `.github/workflows/release.yml`. The name `hsmcli` was
unclaimed on PyPI as of 2026-08-05.

## License

MIT — see [LICENSE](LICENSE).
