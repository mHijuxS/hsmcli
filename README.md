# hsmcli

A command-line client for [HackSmarter Labs](https://www.hacksmarter.org):
browse labs, start and stop machines, pull the VPN profile, submit flags,
and manage AWS labs without leaving the terminal.

> **Unofficial.** Not affiliated with or endorsed by Hack Smarter or
> CourseStack, who run the backend. It drives the same private JSON API the
> web app uses, with your own session, so it can break whenever that API
> changes. Use it with your own account only. Requests identify themselves
> as `hsmcli` — no browser spoofing — so the operators can see and throttle
> it.

## Install

```bash
git clone https://github.com/mHijuxS/hsmcli && cd hsmcli
uv tool install .        # recommended — keeps deps isolated
pipx install .           # or
pip install .            # or, into the current environment
```

It also runs uninstalled from a checkout: `python -m hsmcli`.

Requires Python 3.9+ and `requests`, `PyYAML`, `rich`, `websocket-client`.
CI covers 3.9–3.14.

## Quickstart

```bash
hsmcli auth login                    # sign in; the session is captured automatically
hsmcli labs list                     # see what's on the account
hsmcli lab dark launch               # boot it, wait, print the IP
hsmcli lab dark vpn                  # ./dark.ovpn, mode 0600
sudo openvpn dark.ovpn
hsmcli lab dark submit user '<flag>'
hsmcli lab dark stop
```

## Auth

`hsmcli auth login` first looks for an existing signed-in Firefox profile and
imports its HackSmarter session immediately. If Firefox is the default but no
session exists yet, it opens the normal default profile and watches for login.
Otherwise it opens an isolated window in the default Chromium-family browser
(or another installed Chromium browser); sign in as usual and hsmcli captures
the session, closes the window, and deletes its temporary profile.

- Cookie capture uses Chromium's built-in DevTools connection, bound to a
  random port on `127.0.0.1`; no extension is installed.
- Firefox import queries a temporary SQLite/WAL snapshot and works while
  Firefox is open. Set `HSMCLI_FIREFOX_PROFILE` only when profile
  auto-detection misses a nonstandard location.
- Only the Supabase auth chunks (`sb-auth-auth-token.N`) are kept. A header
  without them is rejected and nothing is written.
- The captured session is verified against the API before it replaces the
  stored session, so a bad login can't clobber a working one.

`--github` points the instructions at the GitHub button; the imported session
is identical either way. `--no-browser` keeps the hidden manual Cookie prompt
as a fallback for SSH or machines without a supported browser.

There is no official CLI auth endpoint, so a PKCE or device-code flow isn't
available to an unofficial client. The browser handles login, OAuth and
MFA; hsmcli only imports the result. Nothing here sees your password.

**Renewal.** hsmcli does not refresh the Supabase token. When it expires,
run `hsmcli auth login` again. `hsmcli auth status` shows how long is left.

For scripts and secret managers:

```bash
secret-tool lookup service hsmcli | hsmcli auth import-cookie -
export HSMCLI_COOKIE='…'          # env override; mind your shell history
```

Session management:

```bash
hsmcli auth status                # who, until when, stored where (exit 1 if signed out)
hsmcli auth logout                # remove the stored session
hsmcli whoami                     # full profile, from the API
```

`config set-cookie` still works but is deprecated in favour of `auth`.

## Commands

```bash
# Discover
hsmcli labs list                        # challenge labs on the account (default)
hsmcli labs list -c all                 # every category: + guided/range/hackwith/foundations/other
hsmcli labs list -c range -c guided     # category filter (repeatable)
hsmcli labs list -e                     # only /courses (the labs on your account)
hsmcli labs list --catalog              # only /catalog (storefront cards, incl. bundles)
hsmcli labs list -T ad                  # topic filter — the website's chips (repeatable)
hsmcli labs list -T aws -T web -d easy  # topic + difficulty, as on the catalog page
hsmcli labs list -d easy -d medium      # difficulty filter (repeatable)
hsmcli labs list -t in_progress         # state filter
hsmcli labs list -s "active directory"  # substring filter on name/description
hsmcli labs list --sort difficulty      # sort by name | difficulty | state | topic

# One lab — identifier first, then the action
hsmcli lab <name> info                  # objective/scope + flags + live systems
hsmcli lab <name> info --briefing       # + lesson content (--full for all, default: first 3)
hsmcli lab <name> info --chapters       # + the chapter/lesson table
hsmcli lab <name> info --writeups       # + community walkthroughs (hidden by default: spoilers)
hsmcli lab <name> info --bundles        # + the subscription bundles this lab is in
hsmcli lab <name> info --all            # every optional section
hsmcli lab <name> take                  # raw /take payload
hsmcli lab <name> enroll                # claim it (free / covered by your plan)
hsmcli lab <name> systems               # live status of all systems
hsmcli lab <name> status                # compact "is it on?" summary

# Lifecycle
hsmcli lab <name> launch                # /launch + /power on, then poll until running
hsmcli lab <name> launch --no-wait      # return as soon as /power ACKs
hsmcli lab <name> stop                  # /power off
hsmcli lab <name> reset                 # /reset (new IP assigned)
hsmcli lab <name> vpn                   # OpenVPN config to ./<lab>.ovpn (0600)
hsmcli lab <name> vpn -o me.ovpn        # …or wherever you want it
hsmcli lab <name> vpn --print           # profile on stdout, nothing else
hsmcli lab <name> image                 # lab thumbnail (--url-only to just print the URL)

# Flags
hsmcli lab <name> flags                 # the lab's flags and their state
hsmcli lab <name> submit user '<flag>'  # by role, 1-based index, UUID or prompt substring
hsmcli lab <name> submit 2 '<flag>' --force   # resubmit an already-solved one

# Finishing a lab
hsmcli lab <name> complete              # mark its lessons complete (in progress → done)
hsmcli lab <name> certificate           # completion PDF to ./<lab>-certificate.pdf
hsmcli lab <name> cert -o me.pdf        # …or wherever you want it (alias: cert)
hsmcli lab <name> certificate --url-only  # print the one-hour signed URL instead

# Account
hsmcli whoami
hsmcli credits                          # PAYG top-up balance
hsmcli subscriptions | orgs | bundles
hsmcli notifications | events | exams

# Misc
hsmcli heartbeat <name>                 # POST /api/heartbeat (keeps the session warm)
hsmcli config show                      # session state, base URL, output format, config path
hsmcli config set-format json           # default output format (also: set-base-url, reset)
```

`vpn`, `image` and `certificate` never overwrite an existing file silently:
at a terminal you're asked, in a script they fail unless you pass `--force`.

### AWS labs

Some labs (Second, Beanstalk, Rotation, …) hand out IAM keys instead of an
IP. The verbs are the same:

```bash
hsmcli lab second launch                 # ~2 min of terraform, then keys
hsmcli lab second launch --allowed-ip 1.2.3.4   # override the detected source IP
hsmcli lab second creds                  # show the keys again
eval "$(hsmcli lab second creds --export)"
aws sts get-caller-identity
hsmcli lab second extend                 # add another time_limit_minutes window
hsmcli lab second stop                   # tear it down
hsmcli lab second reset                  # tear down + re-provision (fresh keys)
```

Each lab runs terraform against a throwaway AWS account and scopes its
security group to a single `allowed_ip`. `launch` fills that from the
`suggested_ip` the API reports. Pass `--allowed-ip` if your traffic comes
from elsewhere, or `--input KEY=VALUE` for any other input the lab declares
in `student_inputs`. Labs are time-boxed (`time_limit_minutes`, usually 60);
`extend` buys another window.

## Output

Human output names what you'd plausibly do next:

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

Failures name the fix, quoting the name you typed:

```console
$ hsmcli lab dark launch
✗ You're not enrolled in dark, so HackSmarter won't share it yet.
  → hsmcli lab dark enroll  free, and takes a second
```

### Scripting

Every listing and lifecycle command takes `--json` / `--yaml`. The two
file-download commands don't; `vpn --print` and `image --url-only` are their
pipeable forms.

- Errors, warnings, progress and follow-up hints go to **stderr**, so
  `--json > out.json` never picks up prose.
- In structured mode a command emits **exactly one document** on stdout.
  `launch --json` waits by default and emits the final live state, not the
  power-on ACK; `--no-wait` gives you the ACK.
- Structured output is verbatim API vocabulary. The translation of `na`,
  `in_progress` and `not_launched` into readable words happens in human
  output only, so scripts keep matching on `running`.
- `--debug` traces every request and response to stderr, one line per call
  plus the payload:

  ```bash
  hsmcli --debug labs list --json > labs.json 2> trace.log
  ```

- `--no-color` (or `NO_COLOR=1`) drops ANSI styling; colour is off
  automatically when stdout isn't a terminal. `--version` prints the version.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | the request failed (auth, HTTP error, network) |
| 2 | bad invocation: unknown action, ambiguous name, no match, not enrolled, refused overwrite |
| 130 | interrupted (Ctrl-C) |
| 141 | downstream closed the pipe (`hsmcli … \| head`) — silent, like any Unix tool |

`launch --wait` also returns 2 on timeout: the machine may still be coming
up, so it isn't treated as a failure.

## Name resolution

Labs and systems accept a UUID or a case-insensitive substring of the name.
Ambiguous matches list the candidates and exit 2.

```bash
hsmcli lab implicit info                     # matches "Challenge Lab: Implicit (Easy)"
hsmcli lab "nova forge" launch               # multi-word ok; punctuation/spaces ignored
hsmcli lab 37e66768-0973-4a1b-9ae6-… info    # UUID always works
```

Precedence: UUID → exact name → exact **core** name → unique substring. The
core name is the name minus its category prefix and difficulty suffix, so
`odyssey` picks `Challenge Lab: Odyssey (Hard)` over `Hack With Me: Active
Directory (Odyssey x Triathlon)`, which merely contains the word.

## Filtering

### Categories (`-c`)

`labs list` shows challenge labs only by default — 60 of 81 on a subscriber
account. Guided labs, ranges, Hack-With-Me sessions, Foundations tracks and
standalone courses are a different kind of thing and make the list harder to
scan. `-c all` widens to everything, `-c range -c guided` picks specific
kinds. The footer always names the active narrowing, so a filtered list
never reads as complete.

### Topics (`-T`)

The catalog page filters by subject (AWS, Active Directory, Web, Windows,
Linux, Blue Team, Guided Lab, Miscellaneous) and difficulty. Neither is a
field in the API: the page derives the topic by keyword-matching the card's
`subtitle` ("This is a Medium Active Directory challenge lab.") and the
difficulty from the `(Hard)` suffix on the title.

`-T/--topic` reproduces that match, including the site's word-boundary rule
(`(^|[^a-z0-9])web([^a-z0-9]|$)`, which is why "Web App" matches `web` and
"Webhooks" doesn't) and its ordering, so `-T ad` selects what clicking the
chip selects. It was diffed against the page's JS bundle across all 88
catalog items with no divergence.

Three behaviours inherited from the site:

- **A lab can have several topics.** "Windows & Linux" matches both chips
  and shows as `Windows/Linux` in the Topic column.
- **`miscellaneous` means "names no subject"**, not "other". `Challenge Lab:
  SQL Basics (Easy)` lands there because its subtitle says only "This is an
  Easy challenge lab."
- **`guided_lab` matches on the title**, not the subtitle, since "This is an
  Easy Guided Lab." names no subject. A guided lab that does name one
  ("This is an Easy AWS Guided Lab.") matches both `-T guided` and `-T aws`.

Spellings are forgiving: `ad`, `active directory`, `active-directory` and
`active_directory` are the same, as are `web`/`web app`,
`misc`/`miscellaneous`, `guided`/`guided_lab`.

Topics are orthogonal to `-c`, which asks what *kind* of thing a lab is
rather than what it's about. The Topic column appears only when a list spans
more than one topic.

## Configuration

Stored at `~/.hsmcli/config.json`; override with `--config-dir`.

| Key | Meaning |
|---|---|
| `cookie` | the Supabase auth chunks only (`sb-auth-auth-token.N`); the rest of a pasted header is discarded at sign-in |
| `base_url` | defaults to `https://www.hacksmarter.org`; must be `https://` (`--allow-insecure-http` exists for local development only) |
| `output_format` | `table` (default), `json` or `yaml` |

| Env var | Meaning |
|---|---|
| `HSMCLI_COOKIE` | overrides the stored cookie; env vars land in shell history and are inherited by child processes, so the config file is the safer default |
| `HSMCLI_USER_AGENT` | overrides the request `User-Agent` |
| `HSMCLI_DEBUG_RAW=1` | disables `--debug`'s masking of credential-bearing response fields (IAM keys, signed URLs) |

The config file holds your Supabase access *and* refresh token. It is
created `0600` inside a `0700` directory, written atomically, and a looser
mode left by an earlier version is tightened on the next run. A pre-existing
custom `--config-dir` keeps whatever permissions it has. Treat the file like
an SSH key. Downloaded VPN profiles contain a private key and are written
`0600` too.

Every request carries a connect/read timeout (5s/30s, 120s read for
downloads), so a stalled server fails loudly instead of hanging.

## Hosts it contacts

Everything goes to `base_url` except:

- **`images.coursestack.com`** — lab thumbnails for `lab <name> image`.
  Public CDN, no cookies sent.
- **`certificates-*.s3.amazonaws.com`** — certificate PDFs for `lab <name>
  certificate`. The API issues a one-hour pre-signed URL per request; the
  download carries no cookies.
- **`api.ipify.org`, `ifconfig.me`, `icanhazip.com`** — only when starting
  an AWS lab, and only as a fallback when the API's own `suggested_ip` field
  is missing. `--allowed-ip <ip>` skips the lookup entirely.

Requests identify themselves as `hsmcli/<version> (+<repo url>)`.
HackSmarter doesn't filter on `User-Agent` — its same-origin check is the
`Referer` header, which hsmcli sets where needed — so there's nothing to
work around. If that changes, `HSMCLI_USER_AGENT` takes any string and
`hsmcli.api_client.BROWSER_USER_AGENT` holds a browser one.

## Using it as a library

The client is importable and its errors are typed, so you don't have to
match on message text:

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

`HsmcliError` is the base, and everything subclasses `Exception`.
`HttpError` carries `.status`, `.endpoint`, `.body` and `.server_message()`
(the API's own explanation, with bare restatements of the status code
filtered out); `AuthError` (401), `ForbiddenError` (403) and `APIError`
(everything else) derive from it. `NotEnrolledError` and `TransportError`
come straight off `HsmcliError`.

## Development

```bash
uv run --with pytest --with rich --with requests --with pyyaml pytest
# or, in an env with the package installed:
pip install -e '.[dev]' && pytest
```

The suite covers the payload-shape layer: id/name/state extractors, the
`/take` scrapers, cookie and session decoding, identifier resolution, the
two-endpoint merge and config file permissions. Those are the parts that
break silently when HackSmarter changes a response.

### Releasing

Not published to any index yet. The tool only reaches what the browser
reaches, and Hack Smarter's creator has said informally that it's fine; a
formal nod from CourseStack is the last box before a public index. The path
is prepared:

1. Bump `version` in `pyproject.toml` and add a `CHANGELOG.md` entry.
2. `git tag -a vX.Y.Z -m "…" && git push --tags`
3. Run the **release** workflow (manual dispatch only) with the same
   version. It builds, runs `twine check`, unpacks the sdist and runs the
   test suite from inside it, then attaches the artifacts.

The workflow has no PyPI step, so nothing can be uploaded by accident.
Adding one later means a job with Trusted Publishing — see the comment at
the top of `.github/workflows/release.yml`. The name `hsmcli` was unclaimed
on PyPI as of 2026-08-05.

## API notes

Reverse-engineering notes, useful if you're extending hsmcli or hitting the
API yourself.

### Endpoint map

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

### There are three different ids

The public `course.id` is **not** what the lifecycle endpoints accept.
`/systems`, `/launch`, `/power`, `/reset` and `/vpn` all use
`course.course_playthrough.id`, a separate handle created when you enroll.
hsmcli resolves it via `/take` transparently. The
`/api/student/content/{id}/…` routes (AWS labs, flag submission) want that
same playthrough id despite the `content` segment; a lesson's own
`content.id` gets a 403 there.

Enrolling uses a third id. There is no `/courses/{id}/enroll` route: the web
app claims a lab by "buying" its storefront card, `POST
/catalog/{catalog_item_id}/buy` with a null purchase option that the server
resolves itself. Free labs and anything covered by your subscription or a
bundle come back `{"state":"bought"}` with nothing charged; anything else
comes back `{"state":"checkout","session_url":…}`, and hsmcli prints that
link and exits 2 rather than claiming you're in. The card id is
`catalog_item_id` on a `/courses` entry, or the top-level `id` of a
`/catalog` entry (whose `item.id` is the course id).

### Three lab shapes

| Shape | Example | Lifecycle endpoint | What you get |
|---|---|---|---|
| systems | Implicit | `/courses/{playthrough}/systems/{id}` | one VM + IP (VPN) |
| networks | NovaForge | `/courses/{playthrough}/networks/{id}` | a subnet of VMs (VPN) |
| aws | Second | `/content/{playthrough}/aws-labs/{id}` | IAM keys, no VPN |

hsmcli sniffs the shape from `/take` and dispatches automatically, so
`launch` / `stop` / `reset` / `status` behave the same on all three.

### `/catalog` is not the full list

`/api/student/catalog` returns only the current storefront cards (41 vs 81
on a subscriber account), so labs bought outside it — including in-progress
ones — never appeared in `labs list`. `/api/student/courses` is the complete
set; `labs list` and name resolution use the two merged. `/catalog` is still
worth merging in: it carries `item.content_state`, and its `course_bundle`
and `event` cards are dropped from the lab list (see `hsmcli bundles` and
`hsmcli events`).

The two endpoints disagree on state vocabulary — `/courses` says `owned`
where `/catalog` says `not_started` — so `-t owned` and `-t not_started` are
treated as the same filter.

## License

MIT — see [LICENSE](LICENSE).
