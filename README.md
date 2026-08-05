# hsmcli

Command-line client for [HackSmarter Labs](https://www.hacksmarter.org) —
manage labs, systems, VPN, credits and lab lifecycle (launch / stop /
reset) from the terminal.

Modeled after `htbcli` and `hccli`: rich terminal output, name-based
identifier resolution, JSON/YAML output for scripting, cookie-based auth.

> **Unofficial.** Not affiliated with, endorsed by, or supported by Hack
> Smarter. It drives the same private JSON API the web app uses — which is
> undocumented and can change without notice, so expect breakage. Check
> Hack Smarter's terms of service before using it, and use it only with
> your own account and your own labs.

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

HackSmarter uses a Supabase session split across two cookies
(`sb-auth-auth-token.0` and `.1`). Log in on
<https://www.hacksmarter.org>, open devtools → Application → Cookies, copy
the whole `Cookie:` header for `www.hacksmarter.org`, then:

```bash
hsmcli config set-cookie 'sb-auth-auth-token.0=base64-…; sb-auth-auth-token.1=…'
# or paste from stdin
xclip -selection clipboard -o | hsmcli config set-cookie -
```

Override via env var: `export HSMCLI_COOKIE='…'`.

Verify:

```bash
hsmcli whoami
```

## Cheat sheet

```bash
# Discover
hsmcli labs list                        # challenge labs on the account (the default)
hsmcli labs list -c all                 # every category: + guided/range/hackwith/foundations/courses
hsmcli labs list -c range -c guided     # category filter (repeatable)
hsmcli labs list -e                     # only /courses (the labs on your account)
hsmcli labs list --catalog              # only /catalog (storefront cards, incl. bundles)
hsmcli labs list -d easy -d medium      # difficulty filter (repeatable)
hsmcli labs list -t in_progress         # state filter
hsmcli labs list -s "active directory"  # substring filter on name/description
hsmcli labs list --sort difficulty      # sort by name | difficulty | state

# One lab — identifier comes first, then the action
hsmcli lab <name> info                  # rich card + chapters + lesson briefing + live systems
hsmcli lab <name> info --full           # render every lesson's content (default: first 3)
hsmcli lab <name> info --no-briefing    # metadata only, skip lesson content
hsmcli lab <name> take                  # raw /take payload
hsmcli lab <name> enroll                # POST /enroll
hsmcli lab <name> systems               # live status of all systems
hsmcli lab <name> status                # compact "is it on?" summary

# Lifecycle
hsmcli lab <name> launch                # /launch + /power on, then poll until running (default)
hsmcli lab <name> launch --no-wait      # return immediately after /power ACKs
hsmcli lab <name> stop                  # /power off
hsmcli lab <name> reset                 # /reset (new IP assigned)
hsmcli lab <name> vpn -o me.ovpn        # download OpenVPN config
hsmcli lab <name> image                 # download the lab thumbnail (--url-only to just print the URL)

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
```

Every command accepts `--json` / `--yaml` for scripting. `--debug` traces
every request and response to **stderr** — one line per call plus the
payload — so it composes with the normal output:

```bash
hsmcli --debug labs list --json > labs.json 2> trace.log
```

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
| `lab info` | GET | `/api/student/courses/{id}` + `/take` (briefing) | — |
| `lab take` | GET | `/api/student/courses/{id}/take` | — |
| `lab enroll` | POST | `/api/student/courses/{id}/enroll` | — |
| `lab systems` / `status` | GET | `/api/student/courses/{playthrough}/systems?courseSystemIds=[…]` | — |
| `lab launch` | POST | `.../systems/{sys}/launch` then `.../power` | `{"power":"on"}` |
| `lab stop` | POST | `.../systems/{sys}/power` | `{"power":"off"}` |
| `lab reset` | POST | `.../systems/{sys}/reset` | `{}` |
| `lab systems` / `status` / `creds` (AWS) | GET | `/api/student/content/{playthrough}/aws-labs/{lab}` | — |
| `lab launch` / `stop` / `reset` / `extend` (AWS) | POST | `/api/student/content/{playthrough}/aws-labs/{lab}/power` | `{"action":"start\|stop\|reset\|extend","inputs":{…}}` |
| `lab submit` | POST | `/api/student/content/{playthrough}/lessons/{lesson}/submit-question` | `{questionId, submission}` |
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

- `cookie` — the whole `Cookie:` header
- `base_url` — defaults to `https://www.hacksmarter.org`
- `output_format` — `table` (default) | `json` | `yaml`

Env vars:

- `HSMCLI_COOKIE` — overrides the stored cookie
- `HSMCLI_USER_AGENT` — overrides the request `User-Agent`

The config file holds your whole Supabase session — access token *and*
refresh token — so it is created `0600` inside a `0700` directory, and a
looser mode left by an earlier version is tightened on next run. Treat it
like an SSH key.

## What it talks to

Everything goes to `base_url` (`https://www.hacksmarter.org`) except:

- **`images.coursestack.com`** — lab thumbnails, for `lab <name> image`.
  Public CDN, no cookies sent.
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

## License

MIT
