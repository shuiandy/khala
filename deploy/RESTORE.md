# Restoring Khala from backups

A running instance is made of three things, and a restore needs all of them:

| What | Backed up to | How often |
| --- | --- | --- |
| Records (the `main` branch) and inbox notes (`refs/khala/inbox`, pushed as the branch `inbox`) | `BACKUP_REMOTE` from `/etc/khala/backup.env` | Every 15 minutes (`backup.sh`) |
| State database (accounts, grants, agents, token hashes, passkeys, note and proposal state), the TOTP encryption key, pending proposal refs | `state.tar.age` in `STATE_BACKUP_REMOTE` from `/etc/khala/state-backup.env`, encrypted with age | Daily at 03:30 (`state-backup.sh`) |
| The age private key that decrypts `state.tar.age` | Wherever you keep it offline, for example a password manager | Never changes |

With records but no state database the server refuses to start, rather than handing every scope to the first
admin. The state snapshot can be up to a day older than the records: accounts, grants and passkeys added in
that day have to be redone; records and notes follow Git.

## Steps

The paths below are the default layout (`/srv/khala`, `/etc/khala`, user `khala`); use yours if `local.env` changes
them. Write `/etc/khala/server.env` as for a new server, but do **not** deploy or start anything yet: a deploy on
a server with a database but no key stops, and a server with records but no database refuses to start.

1. Records and notes:

   ```sh
   git clone --bare "$BACKUP_REMOTE" /srv/khala/vault.git
   git --git-dir=/srv/khala/vault.git fetch origin 'refs/heads/inbox:refs/khala/inbox'
   ```

2. State snapshot. Decrypt it on a machine that holds the private key, without writing the key to disk.
   With 1Password, for example:

   ```sh
   git clone "$STATE_BACKUP_REMOTE" state && cd state && mkdir restore
   op document get "<your age key item>" | age -d -i - state.tar.age | tar -xf - -C restore/
   ```

   `restore/` then holds `memory.db`, `secret.key`, `refs.txt`, `proposals.bundle` (when proposals were
   pending) and `created`.

3. Put them back on the server: `memory.db` → the path in `KHALA_DB` (by default `/srv/khala/state/khala.db`,
   `khala:khala 600`), `secret.key` → `/etc/khala/secret.key` (`root:khala 640`). Pending proposals keep the ref
   names they had when the snapshot was taken; snapshots from before 0.3 use `refs/proposals/*`, which the server
   moves to `refs/khala/proposals/*` when it starts:

   ```sh
   git --git-dir=/srv/khala/vault.git fetch restore/proposals.bundle 'refs/*:refs/*'
   ```

4. Check against `refs.txt` that `main` and the inbox are at least as new as at snapshot time, then run
   `sh deploy/deploy.sh` from your machine, which installs and starts the server, call `/health`, sign in on the web and look at the Agents and Review pages.

Notes that reached the inbox branch after the snapshot are added back to the state database at startup.
