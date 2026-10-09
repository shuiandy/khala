# Record format

A record is one Markdown file at the root of the record repository. Its name is lowercase, starts with a letter
or digit and ends in `.md`; by convention it starts with the record's type (`project_data_audit.md`). Files that
start with an uppercase letter (`INDEX.md`, `PROTOCOL.md`) and files in subdirectories are not records.

## Frontmatter

The file starts with YAML frontmatter between two `---` lines:

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

| Key | Needed | Meaning |
| --- | --- | --- |
| `name` | expected | Short kebab-case name. |
| `description` | expected | One line used in indexes to decide whether the record is relevant. |
| `metadata.type` | expected | `user`, `feedback`, `project`, `reference`, `env` or `issue`. |
| `metadata.scope` | required | The scope the record belongs to. It decides who can read and write it. |
| `metadata.status` | required | `active`, `outdated` or `proposed`. |
| `metadata.verified` | expected | `YYYY-MM-DD`, the last day the content was checked. |
| `metadata.review_after` | no | How long until it should be checked again, as `<days>d` (for example `90d`), or `never`. |
| `metadata.source` | no | The app or person that wrote it. |
| `metadata.proposed_at` | with `proposed` | `YYYY-MM-DD`, when it was proposed. The server adds it. |
| `metadata.expired_reason` | no | Why it is outdated. The server adds it when a record is marked outdated. |
| `metadata.pinned` | no | `true` means only a person edits it, by hand. The server refuses to write it. |
| `metadata.expires_at` | no | `YYYY-MM-DD` after which a short-lived rule no longer applies. |
| `metadata.sensitivity` | no | A label such as `work`. Mirror tools may limit which scopes such a record can live in. |
| `metadata.paths` | no | Local folders the record's project lives in. |

The server refuses a write without a valid `scope` or `status`. The keys marked expected are what indexes and
review dates rely on. Other keys are kept as they are.

## How it is read, and the canonical form

The server reads frontmatter as YAML, so records written by other tools (quoted strings, folded text, flow
mappings) are understood. Frontmatter that is not valid YAML is read line by line instead, so older hand-written
records keep their meaning.

Mirror tools that sync the repository over Git often read the frontmatter line by line: `name` and `description`
at the top, the rest indented two spaces under `metadata`, one plain value per line. A record must mean the same
thing to both readers, so the server holds what it writes, and what is pushed to it, to the **canonical form**:

- valid YAML;
- `metadata` as a block, each key on its own line, indented two spaces;
- values that contain ` #`, `: ` or a backslash are quoted (single quotes are simplest: the only escape is a
  doubled `'`).

Writes through the service that break these rules are refused with a hint. A push where `scope`, `status`,
`pinned` or `proposed_at` would read differently is refused; other keys that read differently only produce a
warning.

## Rules the server applies

- Records larger than 64 KB, or that look like they contain a credential, are refused.
- A record written into an auto-loaded scope by an agent, new or moved in, becomes `proposed` with
  `proposed_at`; its owner approves it.
- `pinned: true` records are never written through the service.

Mirror tools may hold further conventions of their own (for example, treating a scope named `global` as
auto-loaded). The server decides auto-loading per scope, set by its owner.
