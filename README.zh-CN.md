<p>
  <img src="khala/static/khala-icon.png" width="128" height="128" alt="Khala 图标">
</p>

# Khala

[English](README.md)

Khala 是自托管的共享长期记忆，给你和团队用的各种 AI agent 共用。Claude、Codex、ChatGPT、本地模型和各种 bot
都通过同一个远程 MCP 服务读写同一份记忆，每个 agent 只看得到它背后那个人有权看的内容。

记忆是 Git 仓库里的普通 Markdown 文件。Git 是事实源，也是全部历史；一个小 SQLite 数据库存账户、授权和 agent
令牌。网页管理端让人审核 agent 写了什么、撤销改动、决定谁能看什么。

名字取自《星际争霸》里星灵的卡拉（Khala），那条把每个人的思想连在一起的神圣链接。

## 能做什么

- **所有 agent 共用一份记忆。** 支持远程 MCP 和 OAuth 的客户端都能直接连。不支持 OAuth 的客户端在网页管理端
  领一个长期令牌。
- **分类和授权。** 每条记录属于一个分类（`personal`、`work`、某个项目）。分类有所有者，可以按查看、编辑、
  维护三种角色分享给别人。读不到的记录对你来说就不存在：搜索、历史和报错都不会透露它。
- **有上限的 agent。** 每个连上来的客户端都是某个人的 agent。它的权限是这个人的权限再收窄到 agent 自己的
  上限，可以在一个地方吊销它或撤销它做过的改动。
- **先记笔记，审核后再成记录。** agent 在任务结束时往收件箱里记笔记。之后任何 agent 都能把笔记整合成记录，
  结果变成提案等人批准（也可以给某个分类开自动通过）。互相矛盾的内容变成冲突，由人裁决。
- **不用密码的强登录。** passkey 为主，TOTP 和恢复码作后备，第一次用邮件验证码进门。OAuth 同意页绑定在
  完成登录的那个浏览器上。
- **从头到尾都是 Git。** 每次改动都是一个提交，agent 和理由写在提交尾注里。仓库的本地克隆可以当镜像用；
  pre-receive 钩子拒收凭据和超大的记录。

## 本地试用

需要 Python 3.12+ 和 Git。

```sh
pipx install khala
khala init --admin you@example.com
khala serve
```

也可以从克隆的仓库安装：`python3 -m venv .venv && .venv/bin/pip install -e .`，然后用 `.venv/bin` 里的同样命令。

`khala init` 建好 `./khala-data`（记录仓库、状态库和加密 TOTP 秘密的密钥），把你设为管理员，并把设置写进
`./khala.env`，`khala serve` 会读取它。

打开 <http://localhost:8100/app>，用 `you@example.com` 登录，验证码在服务日志里（本地实例把邮件打印到日志，
而不是发出去）。然后在 Security 页加一个 passkey，再把 agent 连到 `http://localhost:8100/mcp`。

## 用 Docker 运行

在一台装了 Docker、域名 DNS 指向它、80 和 443 端口开放的机器上：

```sh
git clone https://github.com/shuiandy/khala && cd khala
cp .env.example .env        # 填 KHALA_DOMAIN、KHALA_OWNERS 和 SMTP 设置
docker compose up -d
```

`compose.yaml` 让 Khala 跑在 Caddy 后面，由 Caddy 申请和续期 TLS 证书。服务保存的所有东西（记录仓库、状态库和加密 TOTP
秘密的密钥）都在 `khala-data` 卷里；请备份它，或者使用下面的备份脚本。打开 `https://<你的域名>/app`，用所有者邮箱登录。

## 部署到服务器

`deploy/` 里的脚本假定 Linux 主机用 systemd，目录布局如下：

| 路径 | 内容 |
| --- | --- |
| `/srv/khala/app` | 代码和它的 virtualenv（`.venv`） |
| `/srv/khala/vault.git` | 记录仓库（裸库） |
| `/srv/khala/state` | 状态库、部署前的副本和备份记账文件 |
| `/etc/khala/server.env` | `KHALA_*` 和 `SMTP_*` 设置，见下表 |
| `/etc/khala/secret.key` | 加密 TOTP 秘密的密钥（新服务器上由 `deploy.sh` 生成） |

这些名字都可以在 `deploy/local.env` 里改。

1. 在服务器上装好 Python 3.12 以上、Git 和 OpenSSL。
2. 写 `/etc/khala/server.env`，至少有 `KHALA_ISSUER`、`KHALA_OWNERS` 和 SMTP 设置。
3. 在 8100 端口前面放一个带 TLS 的反向代理；`deploy/Caddyfile.example` 是完整的 Caddy 配置。
4. 在自己电脑上把 `deploy/local.env.example` 复制成 `deploy/local.env` 填好，跑 `sh deploy/deploy.sh`。
   新服务器上，它会建好 `khala` 用户、目录、virtualenv、空的记录仓库和密钥；每次部署都会装包、在服务器上跑测试、
   装 systemd 单元和 pre-receive 钩子、重启服务并检查公网入口。

可选备份：`deploy/backup.env.example`（每 15 分钟把记录推到一个私有 Git 远端）和
`deploy/state-backup.env.example`（每天一份 `age` 加密的状态库快照）。恢复步骤见
[`deploy/RESTORE.md`](deploy/RESTORE.md)。


## 配置

设置都是环境变量。改名前的旧名字（`MEMORY_*`）仍然认，服务启动时会在日志里列出用到的旧名字。

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| `KHALA_ISSUER` | 必填 | 服务的公网地址，例如 `https://memory.example.com`。OAuth、passkey 和 cookie 都绑定在它上面。 |
| `KHALA_ALLOWED_HOSTS` | `KHALA_ISSUER` 的主机名 | MCP 入口接受的 `Host`，逗号分隔。 |
| `KHALA_TRUSTED_PROXIES` | `127.0.0.1,::1` | 信任哪些对端发来的 `X-Forwarded-For` 和 `X-Forwarded-Proto`（也就是你的反向代理），逗号分隔的地址或网段；`*` 表示全部，留空表示都不信。客户端地址用于审计日志和登录限流。 |
| `KHALA_REPO` | `/var/lib/khala/vault.git` | 记录裸库（第一次启动时自动建）。 |
| `KHALA_BRANCH` | `main` | 存放记录的分支，也是镜像唯一能推送的引用。笔记和提案放在 `refs/khala/*` 下，普通克隆不会拉取。 |
| `KHALA_DB` | `/var/lib/khala/state/khala.db` | 状态库。 |
| `KHALA_STATE_DIR` | `KHALA_DB` 所在目录 | 读取备份状态文件的位置。 |
| `KHALA_OWNERS` | 无 | 启动时设为管理员的邮箱。不属于任何账户的邮箱会单独建一个管理员账户；要让两个邮箱算同一个人，用 `khala alias` 把其中一个加成别名。 |
| `KHALA_TIMEZONE` | `UTC` | “今天”（verified、proposed_at 日期和到期复核）和网页上显示时间所用的时区，例如 `America/Toronto`。不看服务器自身的时区。 |
| `KHALA_OWNER_NAME` | `Owner` | 新建管理员的显示名。 |
| `KHALA_MAILER` | `smtp` | `smtp`；或 `log`，把邮件打印到日志（只用于开发）。 |
| `KHALA_SECRET_KEY_FILE` | `/var/lib/khala/secret.key` | 加密 TOTP 秘密的密钥，新实例第一次启动时生成。别和数据库备份放在一起。数据库里已有账户而密钥找不到时，服务拒绝启动，不会另生成一把。 |
| `KHALA_SECRET_KEY` | 无 | 直接给密钥本身，代替文件。 |
| `KHALA_GIT_PUSHER` | 第一个管理员 | 通过 Git 推送到仓库的账户（本地镜像）。 |
| `KHALA_PUSH_ENFORCE` | 关 | `1` 时 pre-receive 钩子拒收违反分类规则的推送，否则只告警。凭据和超大记录一律拒收。 |
| `KHALA_REQUIRE_STRONG_FACTOR` | 关 | `1` 时要求每个账户都有 passkey 或身份验证器：添加之前，用邮件码登录的账户只能进安全设置页，也不能连接 agent。打开之前签发的令牌继续有效。 |
| `KHALA_ADOPT_EXISTING` | 关 | `1` 时允许空状态库接管已有记录的仓库，把所有分类交给第一个管理员。 |
| `KHALA_INSTANCE_NAME` | `Khala` | 页面标题、登录邮件、passkey 提示和身份验证器里显示的名字。 |
| `KHALA_WRITES_PER_HOUR`、`KHALA_WRITES_PER_DAY` | `60`、`300` | 每个 agent 的写入上限（记录、提案、标记过期）。 |
| `KHALA_NOTES_PER_HOUR` | `120` | 每个 agent 每小时能记的笔记数。 |
| `KHALA_INBOX_LEASE_MINUTES` | `30` | agent 认领收件箱笔记的时长。 |
| `SMTP_HOST`、`SMTP_PORT`、`SMTP_USERNAME`、`SMTP_PASSWORD`、`SMTP_FROM` | 无 | 发送登录验证码的邮件设置。没有用户名时不登录 SMTP（本地中继）。 |
| `SMTP_SECURITY` | `ssl` | `ssl`（隐式 TLS，默认端口 465）、`starttls`（端口 587）或 `none`（可信的本地中继，端口 25）。始终校验证书。 |

## 连接 agent

MCP 入口是 `https://your-host/mcp`。接入某个具体客户端最快的方法，是在网页管理端的 **Agents → Connect agent**，
或者在客户端所在的电脑上用命令行：

```sh
khala connect --list
khala connect claude-code --url https://your-host
```

两者都来自同一份目录 [`khala/clients.yaml`](khala/clients.yaml)，里面有 Claude、Claude Code、ChatGPT、Codex、Cursor、
VS Code、Gemini CLI、Zed、goose、Hermes、opencode、Cline、Kiro、Devin Desktop、LM Studio、Junie、Continue 和
JetBrains AI Assistant 的一键链接、一行命令和配置文件写法，每条都附来源。加一个新客户端就是加一条目录，
`tests/test_catalog.py` 会检查它。

客户端怎么登录：

- **OAuth。** 发布了客户端元数据文档的客户端（Claude、Claude Code、ChatGPT、Codex、VS Code、Zed、goose、Hermes）
  按文档识别，其他客户端自己注册。两种情况都由人登录，并在同意页上选择这个 agent 能访问哪些分类。
- **令牌。** 不能登录的客户端，由向导或 `khala connect --token` 生成令牌。命令行走设备码授权（你在网页上批准一个短码），
  令牌不经过剪贴板。
- **stdio。** `khala bridge https://your-host/mcp` 让只能启动本地服务的客户端连上远程服务，环境变量里放 `KHALA_TOKEN`。
  `khala serve --stdio` 在本机用本地数据目录给一个人用，不需要服务器，也不用登录。

服务通过 MCP instructions 告诉 agent 怎么用记忆：任务开始时查相关分类的索引，只读真正相关的几条记录，结束时记一条笔记。
支持 MCP prompts 的客户端还能把这些流程当成命令用（`recall`、`remember`、`tidy_inbox`），支持资源的客户端可以读
`khala://guide`、某个分类的索引和单条记录。

## 记录

一条记录是仓库根目录下的 Markdown 文件，开头是 YAML 风格的 frontmatter：

```markdown
---
name: data-audit
description: 一行话说明这是什么、什么时候有用
metadata:
  type: project
  scope: work
  verified: 2026-10-01
  review_after: 90d
  source: claude-code
  status: active
---

正文，用 Markdown 写。
```

`type` 取 `user`、`feedback`、`project`、`reference`、`env` 或 `issue`；按惯例小写文件名以它开头
（`project_data_audit.md`）。超过 `review_after` 的记录会标成待复核。完整格式，以及服务对写入要求的规范写法，
见 [docs/record-format.md](docs/record-format.md)（英文）。

## 命令行

`khala init` 建实例，`khala serve` 运行它。日常管理都在网页管理端；其余命令是服务器上的兜底工具，以服务用户运行：
`khala users`、`khala add`、`khala alias`、`khala grant`、`khala revoke`、`khala disable`、`khala enable`、
`khala scopes`、`khala auto-load`、`khala agents`、`khala revoke-agent` 和 `khala reset-auth`。所有命令都从环境变量、
放在最前面的 `--env FILE` 或 `./khala.env` 读取设置。完整列表见 `khala help`。

## 限制

- 文件名和分类 id 在整个实例里共用一个命名空间。
- 服务端用同一个 Git 身份提交，真正的作者记在提交尾注里。
- 管理员能看到全部数据。
- 只能单机部署：各 worker 共用本地磁盘上的同一个 SQLite 文件和 Git 仓库。多个 worker 进程（`--workers N`）没问题，
  因为每个一次性检查都是一条数据库语句，Git 写入是比较后交换（CAS）；但不能放在网络文件系统上。
- 搜索是线性扫描，几千条记录的规模没问题。

## 参与和安全

见 [CONTRIBUTING.md](CONTRIBUTING.md)。漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

[Apache License 2.0](LICENSE)
