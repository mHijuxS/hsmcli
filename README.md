# hsmcli

Command-line client for [HackSmarter Labs](https://www.hacksmarter.org) —
manage labs, systems, VPN, credits and lab lifecycle (launch / stop /
reset) from the terminal.

Modeled after `htbcli` and `hccli`: rich terminal output, name-based
identifier resolution, JSON/YAML output for scripting, cookie-based auth.

## Install

```bash
git clone <this-repo> hsmcli && cd hsmcli
./install.sh          # pip install -e . + /usr/local/bin symlink
# or, with uv (recommended, keeps deps isolated):
uv tool install .
```

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
hsmcli labs list                        # full catalog
hsmcli labs list -e                     # only enrolled labs
hsmcli labs list -d easy -d medium      # difficulty filter (repeatable)
hsmcli labs list -t in_progress         # state filter
hsmcli labs list -c challenge -c range  # category filter (challenge/guided/range/hackwith/foundations/other)
hsmcli labs list -s "active directory"  # substring filter on name/description
hsmcli labs list --sort difficulty      # sort by name | difficulty | state

# One lab
hsmcli lab info    <name>               # rich card + chapters + live systems
hsmcli lab take    <name>               # raw /take payload
hsmcli lab enroll  <name>               # POST /enroll
hsmcli lab systems <name>               # live status of all systems
hsmcli lab status  <name>               # compact "is it on?" summary

# Lifecycle
hsmcli lab launch <name> [--wait]       # /launch + /power on (heartbeat + poll if --wait)
hsmcli lab stop   <name>                # /power off
hsmcli lab reset  <name>                # /reset (new IP assigned)
hsmcli lab vpn    <name> -o me.ovpn     # download OpenVPN config

# Account
hsmcli whoami
hsmcli credits                          # PAYG top-up balance
hsmcli subscriptions | orgs | bundles
hsmcli notifications | events | exams

# Misc
hsmcli heartbeat <name>                 # POST /api/heartbeat (keeps session warm)
```

Every command accepts `--json` / `--yaml` for scripting. `--debug` dumps
the raw API response of any call and exits.

### Name resolution

Labs and systems accept either a UUID or a case-insensitive substring of
the name. Ambiguous matches list the candidates and exit non-zero:

```bash
hsmcli lab info implicit                     # matches "Challenge Lab: Implicit (Easy)"
hsmcli lab launch "Odyssey"                  # multi-word ok
hsmcli lab info 37e66768-0973-4a1b-9ae6-…    # UUID always works
```

## Endpoint map

| Command | Method | Path | Body |
|---|---|---|---|
| `whoami` | GET | `/api/student/profile` | — |
| `labs list` | GET | `/api/student/catalog` | — |
| `labs list -e` | GET | `/api/student/courses` | — |
| `lab info` | GET | `/api/student/courses/{id}` | — |
| `lab take` | GET | `/api/student/courses/{id}/take` | — |
| `lab enroll` | POST | `/api/student/courses/{id}/enroll` | — |
| `lab systems` / `status` | GET | `/api/student/courses/{playthrough}/systems?courseSystemIds=[…]` | — |
| `lab launch` | POST | `.../systems/{sys}/launch` then `.../power` | `{"power":"on"}` |
| `lab stop` | POST | `.../systems/{sys}/power` | `{"power":"off"}` |
| `lab reset` | POST | `.../systems/{sys}/reset` | `{}` |
| `lab vpn` | GET | `/api/student/courses/{playthrough}/vpn` | — |
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

## Config

Stored at `~/.hsmcli/config.json` (override with `--config-dir`).

Keys:

- `cookie` — the whole `Cookie:` header
- `base_url` — defaults to `https://www.hacksmarter.org`
- `output_format` — `table` (default) | `json` | `yaml`

Env vars: `HSMCLI_COOKIE` overrides the stored cookie.

## Requirements

Python 3.8+, `requests`, `PyYAML`, `rich`.

## License

MIT
