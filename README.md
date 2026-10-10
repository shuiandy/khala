<p>
  <img src="khala/static/khala-icon.png" width="128" height="128" alt="Khala icon">
</p>

# Khala

[简体中文](README.zh-CN.md)

Khala is a self-hosted, shared long-term memory for the AI agents you and your team use. Claude, Codex, ChatGPT,
Cursor, local models and bots all read and write the same memory over MCP, and every agent only sees what its
person is allowed to see.

Memories are plain Markdown files in a Git repository. Git is the source of truth and the history; a small
SQLite database holds accounts, grants and agent tokens. A web admin lets people review what agents wrote,
undo it, and decide who can see what. It runs on one laptop with no server at all, or on a server for a team.

The name comes from the Khala of StarCraft's protoss, the sacred link that joins every mind.

- [What it does](#what-it-does)
- [How it compares](#how-it-compares)
- [Architecture](#architecture)
- [Three ways to run it](#three-ways-to-run-it)
- [On one computer](#on-one-computer)
- [Run it with Docker](#run-it-with-docker) and [on a server](#run-it-on-a-server)
- [Connecting agents](#connecting-agents)
- [Records](#records), [Configuration](#configuration), [Command line](#command-line)
- [Security model](#security-model) and [Limits](#limits)

## What it does

- **One memory for every agent.** Any MCP client can connect: remote clients sign in with OAuth, clients without
  OAuth get a token, and clients that only start local programs run Khala themselves over stdio.
- **Scopes and grants.** Every record belongs to a scope (`personal`, `work`, a project). Scopes are owned by a
  person and shared with others as viewer, editor or maintainer. What you cannot read does not exist for you:
  search, history and error messages never reveal it.
- **Agents with ceilings.** Each connected client is an agent of one person. Its access is the person's access
  narrowed by the agent's own ceiling, and it can be revoked or have its changes undone in one place.
- **Notes first, records after review.** Agents leave notes in an inbox at the end of a task. Any agent can later
  consolidate notes into records; the result becomes a proposal a person approves (or a scope can opt into
  automatic approval). Contradictions become conflicts for a person to settle. What waits for you comes to the
  conversation you are already in: your agent mentions it, walks you through it, and your app asks you to confirm.
  A Claude Code plugin reminds Claude to leave notes without being asked.
- **Strong sign-in without passwords.** Passkeys first, TOTP and recovery codes as backup, email codes to get
  started. OAuth consent is bound to the browser that signed in.
- **Git all the way down.** Every change is a commit with the agent and its reason in the trailers. A local
  clone of the repository works as a mirror you can read and edit with any tool; a pre-receive hook refuses
  secrets and oversized records.

## How it compares

Most agent memory projects are retrieval engines: a model extracts facts from conversations, stores them as
vectors or a graph, and finds them again by meaning. Khala makes a different bet: memory is a small set of
documents that many agents and people share, that people can read and review, and that agents find by reading an
index. Neither is better in general; they answer different questions.

| | Stores | Open source, self-hosting | How agents reach it | People and permissions | Before memory changes | Finding things |
| --- | --- | --- | --- | --- | --- | --- |
| **Khala** | Markdown in Git, SQLite for accounts | Apache-2.0, self-hosted (laptop or server) | Remote MCP with OAuth or a token, local stdio, any MCP client | Scopes shared with people as viewer, editor or maintainer; a ceiling per agent | Agents leave notes; consolidated changes are proposals a person approves; undo per commit or per agent | Keyword search and per-scope indexes; no embeddings |
| **Mem0** | Vectors (Qdrant or pgvector) and SQLite history; graph memory only on the hosted platform | Apache-2.0 library and server; hosted platform | SDKs and REST; the official MCP server is hosted-platform only (OpenMemory was retired in July 2026) | User, agent and run ids; organizations and projects on the platform | A model extracts and updates memories automatically; history per memory | Semantic, BM25 and entity matching |
| **Zep and Graphiti** | Temporal knowledge graph (Graphiti on Neo4j, FalkorDB or Neptune) | Graphiti is Apache-2.0; Zep is managed or in your cloud, its Community Edition is deprecated | SDKs, REST, MCP (Zep's remote MCP signs in with OAuth) | Zep: roles with SSO, policies for agents, read-only MCP connections. Graphiti: partitions, no users | Automatic extraction; old facts are invalidated, not deleted | Semantic, BM25 and graph traversal with rerankers |
| **Letta** | Markdown with YAML frontmatter in a Git repository per agent | Apache-2.0; Letta Cloud is the default | Letta's own apps and SDK; its hosted MCP lets other clients message an agent, not edit its memory | Roles and shared memory in Letta Cloud; a self-hosted server has one token | The agent edits its own memory, every edit a commit; optional review by another agent, not a person | Memory files in the prompt, browsed by the agent |
| **Basic Memory** | Markdown files (Obsidian-compatible) with a SQLite or Postgres index | AGPL-3.0; paid cloud | MCP over stdio or HTTP, CLI | One person locally; team roles in the paid cloud | Explicit tool calls; people edit the same files; history is up to you locally | Full-text and vector search, relations |
| **Supermemory** | Its own graph engine and document chunks | Repository MIT, but the self-hosted server binary is not open source | REST, SDKs, hosted MCP with OAuth | Spaces; organizations and roles in Enterprise | Automatic extraction; superseded memories are kept | Hybrid semantic search and relations |
| **Cognee** | Graph, vector and relational stores per dataset | Apache-2.0; hosted cloud | SDKs, REST, MCP | Dataset access lists (read, write, delete, share) for users, tenants and roles | Automatic extraction; a model promotes lessons | Graph, vector, BM25, Cypher and temporal search |
| **MCP reference memory server** | A knowledge graph in one JSONL file | MIT, described as not production-ready | MCP over stdio | None | Explicit tool calls, edited in place | Substring match |
| **ChatGPT and Claude memory** | Hosted by the vendor | No | Only that vendor's apps | Per person and per project; admins can switch it off | Automatic, or "remember this"; you can view, edit and delete | Not disclosed, or chat search |

**Where Khala is different**

- **A person decides what becomes memory.** The other systems write memory automatically or on the agent's own
  call; Letta's optional review is done by another agent and does not ask you. Memory steers every later task, so
  Khala treats what agents write as untrusted: notes wait in an inbox, consolidated changes wait outside `main`
  as proposals, and conflicts always go to a person.
- **Shared between people, open source, on your own machine, all at once.** Team roles are a paid cloud feature
  in Basic Memory and Letta; Zep and Supermemory's server are not open source; Graphiti and the MCP reference
  server have no users. Khala has scopes with roles, records you cannot read are invisible rather than forbidden,
  and each agent has its own ceiling, can be revoked, and can have everything it did undone.
- **Every MCP client, in every way it can connect.** Standard remote MCP sign-in (OAuth with client metadata
  documents, so Claude, ChatGPT, Codex and VS Code are recognised by name), tokens for clients without OAuth, a
  bridge for clients that only start local programs, and a no-server stdio mode. `khala connect` configures 18
  clients. Several of the others offer MCP only through their hosted service.
- **Data you can read and take with you.** Memory is Markdown in Git: `git clone` gives you all of it with its
  history, and every change names its agent and reason. Letta and Basic Memory also store Markdown; Khala adds
  Git history and undo as the mechanism the server itself uses.
- **Nothing to run but Khala.** One Python process, SQLite and Git. No vector database, no embedding model, no
  model calls or API keys on the server; the agents' own models do the thinking.

**Where others are stronger**

- **Finding things by meaning.** Mem0, Zep, Graphiti, Basic Memory, Supermemory and Cognee combine semantic
  search with keywords, several add graph traversal and rerankers. Khala's keyword search misses a record that
  says the same thing in other words; it relies on agents reading short indexes instead, which works for a few
  thousand curated records, not for millions of conversation fragments.
- **Capturing without asking.** They extract memories from every conversation automatically. Khala relies on
  agents leaving notes; its instructions ask them to, and the Claude Code plugin reminds Claude after a stretch of
  work, but the agent still chooses what to note.
- **More kinds of memory.** Facts with validity periods (Zep, Graphiti), ingestion of documents and other sources
  (Supermemory, Cognee, Zep), published benchmarks.
- **Finer or managed access control.** Cognee has per-dataset access lists with tenants; Zep has SSO and policies
  for agents. Khala has no SSO, and its admins can see all data.
- **Scale and hosting.** Managed services scale out; Khala runs on one machine.

Choose Khala when several people and many different agents should share a memory that people curate, on your
own hardware, in a format you can read. Choose a retrieval engine when an application should remember a lot of
conversation detail automatically and find it by meaning.

Checked on 2026-10-08 against each project's own documentation:
[Mem0](https://docs.mem0.ai/platform/platform-vs-oss) ([OpenMemory removal](https://github.com/mem0ai/mem0/pull/6530)),
[Graphiti](https://github.com/getzep/graphiti), [Zep](https://github.com/getzep/zep)
([MCP](https://help.getzep.com/v3/memory-mcp-server)),
[Letta](https://docs.letta.com/letta-code/memfs) ([memory review](https://docs.letta.com/letta-code/memory),
[hosted MCP](https://docs.letta.com/platform/hosted-mcp/index.md)),
[Basic Memory](https://github.com/basicmachines-co/basic-memory) ([Teams](https://docs.basicmemory.com/whats-new/teams)),
[Supermemory](https://supermemory.ai/docs/self-hosting/overview), [Cognee](https://docs.cognee.ai/setup-configuration/permissions),
[MCP memory server](https://github.com/modelcontextprotocol/servers/blob/main/src/memory/README.md),
[ChatGPT memory](https://help.openai.com/en/articles/8590148-memory-in-chatgpt),
[Claude memory](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context).
Corrections are welcome.

## Architecture

```
  Claude · ChatGPT · Codex · Cursor · VS Code · Gemini CLI · Zed · goose · ...
        │ remote MCP over HTTPS                 │ local MCP over stdio
        │ (OAuth 2.1, or a bearer token)        │ (no sign-in, this computer only)
        ▼                                       ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ khala serve                       khala serve --stdio        │
  │                                                              │
  │  MCP server ──► access check ──► write rules ──► store       │
  │  (tools, prompts,  role ∩ agent      no secrets,    Git,     │
  │   resources)       ceiling ∩ hard    64 KB cap,     compare  │
  │                    rules             frontmatter    and swap │
  │                                                              │
  │  OAuth server      web admin /app     inbox and proposals    │
  │  (CIMD, DCR,       (review, undo,     (notes → consolidate   │
  │   device flow)     grants, agents)     → approve)            │
  └──────────────┬──────────────────────────────┬────────────────┘
                 ▼                              ▼
     vault.git (bare Git repository)    khala.db (SQLite, WAL)
     main: the records                  accounts, scopes, grants,
     refs/khala/inbox: notes            agents, tokens, audit log
     refs/khala/proposals/*             + secret.key (encrypts TOTP)
                 │
                 ▼  git clone / pull / push (pre-receive hook)
     a local mirror you read and edit with any tool
```

**Where things live.** Records are Markdown files on the `main` branch of a bare Git repository, one file per
record, with YAML frontmatter that names its scope, type and status. Notes waiting in the inbox live on
`refs/khala/inbox` and pending proposals on `refs/khala/proposals/<id>`, outside `main`, so an ordinary clone
never fetches them and no agent can read a proposal before a person approves it. Everything that is not memory
lives in SQLite: accounts and their sign-in factors, scopes and grants, agents and their tokens, rate limits and
the audit log. The two stores are independent: the repository is the memory, the database is who may touch it.

**What happens on a call.** Each MCP call is resolved to a person and an agent (from the OAuth token, the bearer
token, or the stdio process). The access check takes the person's role on the scope, intersects it with the
agent's ceiling and the server's hard rules, and reads both from the database on every request, so a revoked
agent or a changed grant takes effect on the next call. Reads filter records the caller cannot see before
anything is returned. Writes pass the write rules (frontmatter, scope, size, credential patterns), then become a
commit made with a compare-and-swap on the branch ref, so two writers can never silently overwrite each other;
the loser gets a conflict with the current version.

**How memory grows.** An agent finishing a task calls `memory_note` with what it learned, without deciding where
it belongs. Later an agent with permission claims a batch of notes (a 30 minute lease), merges them into records
and reports each note's outcome with `memory_consolidate`. Consolidation writes proposals by default; a person
approves them on the web, and conflicts with existing records always wait for a person. A scope's owner can let
clean changes apply directly; those stay on the review page and can be undone in one click. Undo is a new commit
and only applies when nobody changed the record since, so it never destroys later work.

**How agents sign in.** The server is its own OAuth 2.1 authorization server with PKCE. Clients that publish a
client ID metadata document (Claude, ChatGPT, Codex, VS Code and others) are recognised by its URL, which the
server fetches with SSRF protection; others register dynamically. The person signs in (passkey, authenticator
app or email code) and picks on a consent page which scopes the agent may reach. Refresh tokens rotate, and
reusing an old one revokes the whole chain. Command-line tools get tokens through device authorization (approve a
short code on the web) instead of copying them.

**Code map.** `app.py` (MCP tools, prompts and resources), `access.py` (the permission rule), `rules.py` (write
rules), `store.py` (Git), `db.py` (SQLite and its migrations), `inbox.py` and `undo.py`, `oauth.py`, `cimd.py`
and `device.py` (sign-in for agents), `login.py` and `web.py` (people and the admin), `hooks.py` (the
pre-receive hook), `catalog.py` and `clients.yaml` (how each client connects), `cli.py`, `connect.py` and
`bridge.py` (the command line).

## Three ways to run it

| | On one computer, stdio | On one computer, HTTP | On a server |
| --- | --- | --- | --- |
| Start | the client starts `khala serve --stdio` | `khala serve` | systemd or Docker, behind TLS |
| Who uses it | you, on this computer | you, on this computer | everyone you invite, from anywhere |
| Sign-in | none: the process runs as you | OAuth, codes in the log | OAuth, codes by email |
| Clients | ones that start local servers | any MCP client on this computer | any MCP client, including web and mobile |
| Web admin | run `khala serve` when you want it | <http://localhost:8100/app> | `https://your-host/app` |

All three use the same data layout and the same rules, so you can start on a laptop and move to a server later.

## On one computer

Requires Python 3.12+ and Git.

```sh
pipx install khala
khala init --admin you@example.com
```

Or from a clone: `python3 -m venv .venv && .venv/bin/pip install -e .`, then the same commands from `.venv/bin`.

`khala init` creates `./khala-data` (the record repository, the state database and the key that encrypts TOTP
secrets), makes you the admin and writes the settings, with absolute paths, to `./khala.env`. Every later
command run from this directory reads it; elsewhere pass it first: `khala --env ~/khala/khala.env ...`.

**With no server (stdio).** Let the client start Khala itself. From the directory with `khala.env`:

```sh
khala connect claude-code --local
khala connect --list                # the other clients
```

This writes the client's config (or runs its own `mcp add` command) so that it starts
`/absolute/path/to/khala --env /absolute/path/to/khala.env serve --stdio`. Absolute paths matter: an app opened
from the Dock or a launcher does not get your shell's `PATH` or working directory. Nothing listens on the network
and there is nothing to sign in to; changes are attributed to an agent called "This computer (stdio)", which you
can revoke like any other. For a client the catalog does not know, `khala connect other --local` prints the
command to paste.

**With a local server (HTTP).** For clients that only speak remote MCP, or to use the web admin:

```sh
khala serve
khala connect cursor --url http://localhost:8100
```

Open <http://localhost:8100/app> and sign in as `you@example.com`; a local instance prints the sign-in code in the
server log instead of sending mail. Add a passkey on the Security page. Both modes can run at the same time on the
same data: the server is safe with several processes.

**Your data** is the `khala-data` directory. Back it up by copying it while nothing is running. To read the
records with any tool, clone them (`git clone khala-data/vault.git`); write through an agent or the web admin,
since a local repository has no pre-receive hook to check pushes. To move to a server later, copy the directory
there and change `KHALA_ISSUER` to the server's address; passkeys are bound to an address, so add them again
after the move.

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

## Connecting agents

The MCP endpoint is `https://your-host/mcp`. The quickest way to connect a particular client is on the web admin
at **Agents → Connect agent**, or from the command line on the computer where the client runs:

```sh
khala connect --list
khala connect claude-code --url https://your-host
khala connect cursor --url https://your-host --token     # a token instead of signing in
khala connect gemini-cli --local                         # this computer's own instance, over stdio
```

`khala connect` uses the quickest way this computer allows: the client's own command when it is installed, then
its config file (merged, keeping everything else and a backup beside it), then its install link. `--dry-run` only
shows what it would do. All of this comes from one catalog, [`khala/clients.yaml`](khala/clients.yaml), which
knows install links, one-line commands and config files for Claude, Claude Code, ChatGPT, Codex, Cursor, VS Code,
Gemini CLI, Zed, goose, Hermes, opencode, Cline, Kiro, Devin Desktop, LM Studio, Junie, Continue and JetBrains AI
Assistant, each with its sources and whether an official page confirms it. A new client is a new entry there;
`tests/test_catalog.py` checks it.

How clients sign in:

- **OAuth.** Clients that publish a client ID metadata document (Claude, Claude Code, ChatGPT, Codex, VS Code,
  Zed, goose, Hermes) are recognised by it; others register themselves. Either way the person signs in and picks,
  on a consent page, which scopes the agent may reach.
- **A token.** For clients that cannot sign in, the wizard or `khala connect --token` creates one. The command
  line gets it through device authorization (you approve a short code on the web), so it never goes through the
  clipboard.
- **Cloud bots.** An agent on a cloud machine has no callback your browser can reach: a loopback address there
  is your own computer here. It should use device authorization, which the server's OAuth metadata advertises
  (`device_authorization_endpoint`, polled at the token endpoint). If it only knows the authorization code flow,
  it registers `https://your-host/oauth/callback` as its redirect URI; after you sign in, that page shows the
  address to paste back to the agent. The code travels in the URL fragment, so it never reaches a server log.
- **The bridge.** `khala bridge https://your-host/mcp` lets a client that can only start local programs reach a
  remote server, with `KHALA_TOKEN` in its environment.
- **Local.** `khala serve --stdio` serves one person on this computer from a local data directory, with no
  server and no sign-in; `khala connect CLIENT --local` sets it up.

### Notes from Claude Code without asking

Agents are asked to leave notes at the end of a task, and they often forget. For Claude Code, the Khala plugin
reminds it:

```sh
claude plugin install khala --marketplace shuiandy/khala
```

After a stretch of work (each tool call counts 1, each message you send counts 3, 15 by default), when Claude
finishes a turn, the plugin asks it to look back and leave anything worth keeping with `memory_note`; Claude
decides what, if anything, that is, and saving memory restarts the count. The notes land in the inbox and go
through review like any other, so a wrong or injected note never becomes a record on its own. Set the amount
of work when installing (`--config checkpoint_every=30`, or `0` to turn the reminders off) or later in `/plugin`. The plugin reads
only the new part of the session's transcript on this computer, sends nothing anywhere itself, and needs
`python3` on the `PATH`. Its source is in [`integrations/claude-code`](integrations/claude-code).

The server's MCP instructions tell agents how to use the memory: look up the relevant scope's index at the start
of a task, read only the few records that matter, and leave a note at the end. The tools are `memory_scopes`,
`memory_index`, `memory_search`, `memory_read`, `memory_history`, `memory_note`, `memory_inbox`,
`memory_consolidate`, `memory_write`, `memory_deprecate`, `memory_review` and `memory_decide`.

### Decisions without opening the web page

When something needs your decision (a proposed change in a scope that waits for approval, a record that would load
in every session, a conflict), `memory_scopes` says so with `waiting_for_you`, and agents are told to mention it once
at a natural break. Say yes, and the agent lists the items with `memory_review`, explains them, and passes your answer
to `memory_decide`. Before anything changes, the server asks your app to show you a confirmation it wrote itself,
naming every item and its version (MCP form elicitation); the model cannot answer it. Apps that cannot show one get
a link to a page that lists exactly those decisions, applied with one click. The same link comes back when the
question is declined or dismissed, since some apps answer it without ever showing it. Unattended agents cannot decide, an agent only decides what it may read and
change, and approved changes can be undone on the Review page for 14 days.

To have fewer decisions at all, let a scope apply merges directly (on its page, or `khala consolidation SCOPE auto`):
merges land at once, conflicts still wait, and each one can be undone. Clients that show MCP prompts also get the routines
as commands (`recall`, `remember`, `tidy_inbox`), and clients that attach resources can read `khala://guide`, a
scope's index and single records.

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

## Configuration

Settings are environment variables, usually kept in the settings file `khala init` writes. Names from before the
rename (`MEMORY_*`) are still read, and the server logs a warning listing them.

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

## Command line

`khala init` sets up an instance, `khala serve` runs it and `khala connect` sets up clients. Day-to-day
administration happens on the web admin; the other commands are the fallback on the server, run as the service
user: `khala users`, `khala add`, `khala alias`, `khala grant`, `khala revoke`, `khala disable`, `khala enable`,
`khala scopes`, `khala auto-load`, `khala consolidation`, `khala agents`, `khala revoke-agent` and `khala reset-auth`. Every command
reads its settings from the environment, from `--env FILE` given first, or from `./khala.env`. Run `khala help`
for the full list.

## Security model

Memory that agents read is an input to everything they do afterwards, so Khala treats what agents write as
untrusted until a person has looked at it.

- **Least privilege per agent.** An agent never has more than its person, and usually less: its ceiling limits
  the scopes and the role it gets. Permissions are checked on every request against the database, not cached
  in tokens.
- **Invisible, not forbidden.** Records in scopes you cannot read are left out of indexes, search and history,
  and errors do not say whether they exist.
- **Injection has to pass a person.** Notes are data from other agents and the server tells agents never to
  follow instructions in them. Consolidated changes are proposals outside `main` until approved, and conflicts
  always need a person.
- **Decisions in a conversation are confirmed by the app, not the model.** `memory_decide` changes nothing until
  the person's app has shown them the server's own summary and they accepted it. That is weaker than the web page,
  since an app can be set up to answer such forms itself, so it is refused for unattended agents, limited to what
  the agent may change, recorded with the agent's name, and undoable.
- **Mistakes are cheap.** Every change is a commit naming its agent and reason. One commit, or everything an agent
  did over a period, can be undone without touching later work by others.
- **Secrets stay out.** Writes that look like credentials, and records over 64 KB, are refused by the MCP server
  and by the Git hook alike.
- **Sign-in.** Passkeys, TOTP and recovery codes, with email codes to start (an instance can require a strong
  factor). TOTP secrets are encrypted at rest with a key kept apart from the database. OAuth uses PKCE, exact
  redirect matching (any port for loopback), the issuer in every authorization response, rotating refresh
  tokens with reuse detection, and consent bound to the browser that signed in.

Please report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Limits

- File names and scope ids share one namespace across the whole instance.
- The server commits under one Git identity; the real author is recorded in commit trailers.
- Admins can see all data.
- One machine: workers share a SQLite file and a Git repository on local disk. Several worker processes
  (`--workers N`) are fine, since every single-use check is one database statement and Git writes are
  compare-and-swap; a network file system is not.
- Search is keyword matching over a linear scan: every word must appear, ranked by how often. That is fine for
  thousands of records and needs no model, but it does not find a record that says the same thing in other words.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE)
