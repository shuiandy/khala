<p>
  <img src="khala/static/khala-icon.png" width="128" height="128" alt="Khala icon">
</p>

# Khala

[简体中文](README.zh-CN.md)

Khala is a self-hosted, shared long-term memory for the AI agents you and your team use. Claude, Codex, ChatGPT,
local models and bots all read and write the same memory through one remote MCP server, and every agent only
sees what its person is allowed to see.

Memories are plain Markdown files in a Git repository. Git is the source of truth and the history; a small
SQLite database holds accounts, grants and agent tokens. A web admin lets people review what agents wrote,
undo it, and decide who can see what.

The name comes from the Khala of StarCraft's protoss, the sacred link that joins every mind.

## What it does

- **One memory for every agent.** Any MCP client that supports remote servers and OAuth can connect. Clients
  without OAuth get a long-lived bearer token from the web admin.
- **Scopes and grants.** Every record belongs to a scope (`personal`, `work`, a project). Scopes are owned by a
  person and shared with others as viewer, editor or maintainer. What you cannot read does not exist for you:
  search, history and error messages never reveal it.
- **Agents with ceilings.** Each connected client is an agent of one person. Its access is the person's access
  narrowed by the agent's own ceiling, and it can be revoked or have its changes undone in one place.
- **Notes first, records after review.** Agents leave notes in an inbox at the end of a task. Any agent can later
  consolidate notes into records; the result becomes a proposal a person approves (or a scope can opt into
  automatic approval). Contradictions become conflicts for a person to settle.
- **Strong sign-in without passwords.** Passkeys first, TOTP and recovery codes as backup, email codes to get
  started. OAuth consent is bound to the browser that signed in.
- **Git all the way down.** Every change is a commit with the agent and its reason in the trailers. A local
  clone of the repository works as a mirror; a pre-receive hook refuses secrets and oversized records.

## Try it locally

Requires Python 3.12+ and Git.

```sh
pipx install khala
khala init --admin you@example.com
khala serve
```

Or from a clone: `python3 -m venv .venv && .venv/bin/pip install -e .`, then the same commands from `.venv/bin`.

`khala init` creates `./khala-data` (the record repository, the state database and the key that encrypts TOTP
secrets), makes you the admin and writes the settings to `./khala.env`, which `khala serve` reads. Open
<http://localhost:8100/app>, sign in as `you@example.com`, and read the sign-in code from the server log (a local
instance prints mail instead of sending it). Then add a passkey on the Security page and connect an agent to
`http://localhost:8100/mcp`.

## Run it with Docker

On a machine with Docker, whose domain's DNS points at it and with ports 80 and 443 open:

```sh
git clone https://github.com/shuiandy/khala && cd khala
cp .env.example .env        # set KHALA_DOMAIN, KHALA_OWNERS and the SMTP settings
docker compose up -d
```

`compose.yaml` runs Khala behind Caddy, which gets and renews the TLS certificate. Everything the server keeps
(the record repository, the state database and the key that encrypts TOTP secrets) is in the `khala-data` volume;
back it up, or use the backup scripts below. Open `https://<your domain>/app` and sign in as the owner.

## Run it on a server

The scripts in `deploy/` install Khala on a Linux host with systemd, in this layout (every name can be changed
in `deploy/local.env`):

| Path | Contents |
| --- | --- |
| `/srv/khala/app` | the code and its virtualenv (`.venv`) |
| `/srv/khala/vault.git` | the record repository (bare) |
| `/srv/khala/state` | the state database, its pre-deploy copies and backup bookkeeping |
| `/etc/khala/server.env` | `KHALA_*` and `SMTP_*` settings, see below |
| `/etc/khala/secret.key` | the key that encrypts TOTP secrets (created by `deploy.sh` on a new server) |

1. Install Python 3.12 or later, Git and OpenSSL on the server.
2. Write `/etc/khala/server.env` with at least `KHALA_ISSUER`, `KHALA_OWNERS` and the SMTP settings.
3. Put a TLS reverse proxy in front of port 8100; `deploy/Caddyfile.example` is a complete Caddy config.
4. On your own machine, copy `deploy/local.env.example` to `deploy/local.env`, fill it in, and run
   `sh deploy/deploy.sh`. On a new server it creates the `khala` user, the directories, the virtualenv, an
   empty record repository and the key. Every time, it installs the package, runs the test suite on the server,
   installs the systemd units and the pre-receive hook, restarts the service and checks the public endpoints.

Optional backups: `deploy/backup.env.example` (records to a private Git remote every 15 minutes) and
`deploy/state-backup.env.example` (a daily `age`-encrypted snapshot of the state database). Restoring is
described in [`deploy/RESTORE.md`](deploy/RESTORE.md).


## Configuration

Settings are environment variables. Names from before the rename (`MEMORY_*`) are still read, and the server
logs a warning listing them.

| Setting | Default | Meaning |
| --- | --- | --- |
| `KHALA_ISSUER` | required | Public URL of the server, for example `https://memory.example.com`. OAuth, passkeys and cookies are bound to it. |
| `KHALA_ALLOWED_HOSTS` | host of `KHALA_ISSUER` | Comma-separated `Host` values the MCP endpoint accepts. |
| `KHALA_TRUSTED_PROXIES` | `127.0.0.1,::1` | Peers whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed (your reverse proxy), as comma-separated addresses or networks; `*` for any, empty for none. The client address feeds the audit log and sign-in rate limits. |
| `KHALA_REPO` | `/var/lib/khala/vault.git` | The bare record repository (created on first start). |
| `KHALA_BRANCH` | `main` | The branch that holds the records, and the only ref a mirror may push. Notes and proposals live under `refs/khala/*`, which ordinary clones do not fetch. |
| `KHALA_DB` | `/var/lib/khala/state/khala.db` | The state database. |
| `KHALA_STATE_DIR` | directory of `KHALA_DB` | Where backup status files are read from. |
| `KHALA_OWNERS` | none | Emails that become admins on startup. An email that belongs to no account gets an admin account of its own; to make two emails one person, add one as an alias with `khala alias`. |
| `KHALA_TIMEZONE` | `UTC` | Time zone for "today" (verified and proposed dates, review due) and for times shown on the web, for example `America/Toronto`. The server's own zone is not used. |
| `KHALA_OWNER_NAME` | `Owner` | Display name for a newly created admin. |
| `KHALA_MAILER` | `smtp` | `smtp`, or `log` to print mail to the log (development only). |
| `KHALA_SECRET_KEY_FILE` | `/var/lib/khala/secret.key` | Key that encrypts TOTP secrets, created on a new instance's first start. Keep it out of backups of the database. If the database has accounts and the key is missing, the server refuses to start rather than make a new one. |
| `KHALA_SECRET_KEY` | none | The key itself, instead of the file. |
| `KHALA_GIT_PUSHER` | first admin | Account that pushes to the repository over Git (a local mirror). |
| `KHALA_PUSH_ENFORCE` | off | `1` makes the pre-receive hook refuse pushes that break scope rules; otherwise it only warns. Secrets and oversized records are always refused. |
| `KHALA_REQUIRE_STRONG_FACTOR` | off | `1` requires every account to have a passkey or an authenticator app: until it adds one, an account signed in with an email code only reaches its security settings and cannot connect agents. Tokens issued before the switch keep working. |
| `KHALA_ADOPT_EXISTING` | off | `1` lets an empty state database take over a repository that already has records, giving every scope to the first admin. |
| `KHALA_INSTANCE_NAME` | `Khala` | Name shown in page titles, sign-in emails, passkey prompts and authenticator apps. |
| `KHALA_WRITES_PER_HOUR`, `KHALA_WRITES_PER_DAY` | `60`, `300` | Writes one agent may make (records, proposals, marking outdated). |
| `KHALA_NOTES_PER_HOUR` | `120` | Inbox notes one agent may leave in an hour. |
| `KHALA_INBOX_LEASE_MINUTES` | `30` | How long an agent's claim on inbox notes lasts. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | none | Outgoing mail for sign-in codes. Without a user name the server does not sign in (a local relay). |
| `SMTP_SECURITY` | `ssl` | `ssl` (implicit TLS, port 465 by default), `starttls` (port 587) or `none` (a trusted local relay, port 25). Certificates are always verified. |

## Connecting agents

The MCP endpoint is `https://your-host/mcp`. The quickest way to connect a particular client is on the web admin
at **Agents → Connect agent**, or from the command line on the computer where the client runs:

```sh
khala connect --list
khala connect claude-code --url https://your-host
```

Both come from one catalog, [`khala/clients.yaml`](khala/clients.yaml), which knows install links, one-line
commands and config files for Claude, Claude Code, ChatGPT, Codex, Cursor, VS Code, Gemini CLI, Zed, goose,
Hermes, opencode, Cline, Kiro, Devin Desktop, LM Studio, Junie, Continue and JetBrains AI Assistant, each with
its sources. A new client is a new entry there; `tests/test_catalog.py` checks it.

How clients sign in:

- **OAuth.** Clients that publish a client ID metadata document (Claude, Claude Code, ChatGPT, Codex, VS Code,
  Zed, goose, Hermes) are recognised by it; others register themselves. Either way the person signs in and picks,
  on a consent page, which scopes the agent may reach.
- **A token.** For clients that cannot sign in, the wizard or `khala connect --token` creates one. The command
  line gets it through device authorization (you approve a short code on the web), so it never goes through the
  clipboard.
- **stdio.** `khala bridge https://your-host/mcp` connects a client that can only start local servers, with
  `KHALA_TOKEN` in its environment. `khala serve --stdio` serves one person on this computer from a local data
  directory, with no server and no sign-in.

The server's MCP instructions tell agents how to use the memory: look up the relevant scope's index at the start
of a task, read only the few records that matter, and leave a note at the end. Clients that show MCP prompts also
get these routines as commands (`recall`, `remember`, `tidy_inbox`), and clients that attach resources can read
`khala://guide`, a scope's index and single records.

## Records

A record is a Markdown file at the repository root with YAML-style frontmatter:

```markdown
---
name: data-audit
description: one line saying what this is and when it matters
metadata:
  type: project
  scope: work
  verified: 2026-10-01
  review_after: 90d
  source: claude-code
  status: active
---

The content, in Markdown.
```

`type` is one of `user`, `feedback`, `project`, `reference`, `env` or `issue`; by convention the lowercase file
name starts with it (`project_data_audit.md`). Records past `review_after` are flagged as due for review. The full
format, and the canonical form the server holds writes to, is in [docs/record-format.md](docs/record-format.md).

## Command line

`khala init` sets up an instance and `khala serve` runs it. Day-to-day administration happens on the web admin;
the other commands are the fallback on the server, run as the service user: `khala users`, `khala add`,
`khala alias`, `khala grant`, `khala revoke`, `khala disable`, `khala enable`, `khala scopes`, `khala auto-load`,
`khala agents`, `khala revoke-agent` and `khala reset-auth`. Every command reads its settings from the
environment, from `--env FILE` given first, or from `./khala.env`. Run `khala help` for the full list.

## Limits

- File names and scope ids share one namespace across the whole instance.
- The server commits under one Git identity; the real author is recorded in commit trailers.
- Admins can see all data.
- One machine: workers share a SQLite file and a Git repository on local disk. Several worker processes
  (`--workers N`) are fine, since every single-use check is one database statement and Git writes are
  compare-and-swap; a network file system is not.
- Search is a linear scan, fine for thousands of records.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). Please report vulnerabilities privately as described in
[SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
