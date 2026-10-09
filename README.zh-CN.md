<p>
  <img src="khala/static/khala-icon.png" width="128" height="128" alt="Khala 图标">
</p>

# Khala

[English](README.md)

Khala 是自托管的共享长期记忆，给你和团队用的各种 AI agent 共用。Claude、Codex、ChatGPT、Cursor、本地模型和各种 bot
都通过 MCP 读写同一份记忆，每个 agent 只看得到它背后那个人有权看的内容。

记忆是 Git 仓库里的普通 Markdown 文件。Git 是事实源，也是全部历史；一个小 SQLite 数据库存账户、授权和 agent
令牌。网页管理端让人审核 agent 写了什么、撤销改动、决定谁能看什么。它可以在一台笔记本上完全不要服务器地跑，
也可以部署到服务器上给整个团队用。

名字取自《星际争霸》里星灵的卡拉（Khala），那条把每个人的思想连在一起的神圣链接。

- [能做什么](#能做什么)
- [和同类项目比](#和同类项目比)
- [架构](#架构)
- [三种运行方式](#三种运行方式)
- [在一台电脑上用](#在一台电脑上用)
- [用 Docker 运行](#用-docker-运行)和[部署到服务器](#部署到服务器)
- [连接 agent](#连接-agent)
- [记录](#记录)、[配置](#配置)、[命令行](#命令行)
- [安全模型](#安全模型)和[限制](#限制)

## 能做什么

- **所有 agent 共用一份记忆。** 任何 MCP 客户端都能连：远程客户端用 OAuth 登录，不支持 OAuth 的客户端用令牌，
  只能启动本地程序的客户端自己通过 stdio 把 Khala 跑起来。
- **分类和授权。** 每条记录属于一个分类（`personal`、`work`、某个项目）。分类有所有者，可以按查看、编辑、
  维护三种角色分享给别人。读不到的记录对你来说就不存在：搜索、历史和报错都不会透露它。
- **有上限的 agent。** 每个连上来的客户端都是某个人的 agent。它的权限是这个人的权限再收窄到 agent 自己的
  上限，可以在一个地方吊销它或撤销它做过的改动。
- **先记笔记，审核后再成记录。** agent 在任务结束时往收件箱里记笔记。之后任何 agent 都能把笔记整合成记录，
  结果变成提案等人批准（也可以给某个分类开自动通过）。互相矛盾的内容变成冲突，由人裁决。等你决定的事会来到你
  正在用的对话里：agent 会提一句、带你过一遍，由你的应用请你确认。Claude Code 插件会提醒 Claude 主动记笔记。
- **不用密码的强登录。** passkey 为主，TOTP 和恢复码作后备，第一次用邮件验证码进门。OAuth 同意页绑定在
  完成登录的那个浏览器上。
- **从头到尾都是 Git。** 每次改动都是一个提交，agent 和理由写在提交尾注里。仓库的本地克隆可以当镜像，
  用任何工具读写；pre-receive 钩子拒收凭据和超大的记录。

## 和同类项目比

大多数 agent 记忆项目是检索引擎：由模型从对话里抽取事实，存成向量或图，再按语义找回来。Khala 押的是另一种思路：
记忆是一小套文档，许多 agent 和人共用，人能读、能审，agent 通过读索引找到它们。两者没有绝对的好坏，回答的是不同的问题。

| | 存储 | 开源与自托管 | agent 怎么接入 | 人和权限 | 记忆改动之前 | 怎么查找 |
| --- | --- | --- | --- | --- | --- | --- |
| **Khala** | Git 里的 Markdown，账户放 SQLite | Apache-2.0，自托管（笔记本或服务器） | 远程 MCP（OAuth 或令牌）、本地 stdio，任何 MCP 客户端 | 分类按查看、编辑、维护分享给人；每个 agent 一个上限 | agent 记笔记；整合出的改动是提案，由人批准；可按提交或按 agent 撤销 | 关键词搜索和分类索引；没有向量 |
| **Mem0** | 向量（Qdrant 或 pgvector）加 SQLite 历史；图记忆只在托管平台上 | Apache-2.0 的库和服务端；另有托管平台 | SDK 和 REST；官方 MCP 服务只给托管平台（OpenMemory 已在 2026 年 7 月下线） | 按用户、agent、运行 id 区分；托管平台有组织和项目 | 模型自动抽取和更新记忆；每条记忆有历史 | 语义、BM25 和实体匹配 |
| **Zep 和 Graphiti** | 带时间的知识图谱（Graphiti 跑在 Neo4j、FalkorDB 或 Neptune 上） | Graphiti 是 Apache-2.0；Zep 是托管或部署在你的云上，社区版已弃用 | SDK、REST、MCP（Zep 的远程 MCP 用 OAuth 登录） | Zep：带 SSO 的角色、针对 agent 的策略、只读 MCP 连接。Graphiti：只有分区，没有用户 | 自动抽取；旧事实标记失效而不删除 | 语义、BM25 和图遍历，加重排 |
| **Letta** | 每个 agent 一个 Git 仓库，里面是带 YAML frontmatter 的 Markdown | Apache-2.0；默认用 Letta Cloud | Letta 自己的应用和 SDK；它的托管 MCP 让别的客户端给 agent 发消息，不能直接改记忆 | 角色和共享记忆在 Letta Cloud 上；自托管服务只有一个令牌 | agent 自己改记忆，每次改动一个提交；可选由另一个 agent 审，不是人 | 记忆文件放进提示词，由 agent 浏览 |
| **Basic Memory** | Markdown 文件（兼容 Obsidian），加 SQLite 或 Postgres 索引 | AGPL-3.0；另有付费云 | MCP（stdio 或 HTTP）、命令行 | 本地只有一个人；团队角色在付费云上 | 显式调用工具；人和 AI 改同一批文件；本地历史要自己管 | 全文和向量搜索，加关系 |
| **Supermemory** | 自研图引擎和文档分块 | 仓库是 MIT，但自托管的服务端程序不开源 | REST、SDK、带 OAuth 的托管 MCP | 空间；企业版有组织和角色 | 自动抽取；被取代的记忆保留 | 混合语义搜索和关系 |
| **Cognee** | 每个数据集一套图、向量和关系存储 | Apache-2.0；另有托管云 | SDK、REST、MCP | 数据集访问列表（读、写、删、分享），面向用户、租户和角色 | 自动抽取；由模型提升经验 | 图、向量、BM25、Cypher 和时间搜索 |
| **MCP 官方示例 memory 服务** | 一个 JSONL 文件里的知识图谱 | MIT，自称不适合生产 | 只有 MCP stdio | 没有 | 显式调用工具，原地修改 | 子串匹配 |
| **ChatGPT 和 Claude 的记忆** | 厂商托管 | 否 | 只有该厂商自己的应用 | 按人、按项目；管理员可以关掉 | 自动，或“记住这个”；可以查看、修改、删除 | 未公开，或聊天搜索 |

**Khala 的不同之处**

- **由人决定什么成为记忆。** 其他系统要么自动写入记忆，要么由 agent 自己决定写；Letta 可选的审核是另一个 agent 做的，
  不问你。记忆会左右之后的每个任务，所以 Khala 把 agent 写的东西当成不可信的：笔记在收件箱里等着，整合出的改动作为提案
  待在 `main` 之外，冲突永远交给人。
- **多人共享、开源、跑在自己机器上，三者同时满足。** Basic Memory 和 Letta 的团队角色是付费云功能；Zep 和 Supermemory 的
  服务端不开源；Graphiti 和 MCP 官方示例服务没有用户的概念。Khala 有带角色的分类，读不到的记录是看不见而不是被拒绝，
  每个 agent 有自己的上限，可以吊销，做过的事可以整体撤销。
- **任何 MCP 客户端，用它支持的任何方式接入。** 标准的远程 MCP 登录（支持客户端元数据文档的 OAuth，所以 Claude、ChatGPT、
  Codex 和 VS Code 能按名字识别），给不支持 OAuth 的客户端用的令牌，给只能启动本地程序的客户端用的桥接，以及不要服务器的
  stdio 模式。`khala connect` 能配置 18 种客户端。其他几家有的只通过自己的托管服务提供 MCP。
- **数据看得懂、带得走。** 记忆是 Git 里的 Markdown：`git clone` 就拿到全部内容和历史，每次改动都写明 agent 和理由。
  Letta 和 Basic Memory 也存 Markdown；Khala 多的是服务端本身就以 Git 历史和撤销为基础。
- **除了 Khala 什么都不用跑。** 一个 Python 进程、SQLite 和 Git。没有向量数据库、没有嵌入模型，服务端不调用模型、
  不需要 API key；思考交给 agent 自己的模型。

**别人更强的地方**

- **按语义查找。** Mem0、Zep、Graphiti、Basic Memory、Supermemory 和 Cognee 都把语义搜索和关键词结合起来，好几家还有
  图遍历和重排。Khala 的关键词搜索找不到换了说法的同一件事；它靠 agent 读简短的索引，这对几千条整理过的记录够用，
  对上百万条对话碎片不行。
- **不用开口就能记。** 它们从每段对话里自动抽取记忆。Khala 靠 agent 记笔记：instructions 会要求 agent 这么做，
  Claude Code 插件也会在一段工作之后提醒 Claude，但记什么仍由 agent 决定。
- **记忆的种类更多。** 带有效期的事实（Zep、Graphiti）、导入文档和其他数据源（Supermemory、Cognee、Zep）、公开的基准测试。
- **更细或托管的权限控制。** Cognee 有带租户的数据集访问列表；Zep 有 SSO 和针对 agent 的策略。Khala 没有 SSO，
  管理员能看到全部数据。
- **规模和托管。** 托管服务可以横向扩展；Khala 只跑在一台机器上。

如果你要让几个人和许多不同的 agent 共用一份由人整理的记忆，跑在自己的硬件上、格式自己读得懂，选 Khala。如果你要让一个应用
自动记住大量对话细节并按语义找回，选检索引擎。

以下内容于 2026-10-08 按各项目自己的文档核对：
[Mem0](https://docs.mem0.ai/platform/platform-vs-oss)（[OpenMemory 下线](https://github.com/mem0ai/mem0/pull/6530)）、
[Graphiti](https://github.com/getzep/graphiti)、[Zep](https://github.com/getzep/zep)
（[MCP](https://help.getzep.com/v3/memory-mcp-server)）、
[Letta](https://docs.letta.com/letta-code/memfs)（[记忆审核](https://docs.letta.com/letta-code/memory)、
[托管 MCP](https://docs.letta.com/platform/hosted-mcp/index.md)）、
[Basic Memory](https://github.com/basicmachines-co/basic-memory)（[Teams](https://docs.basicmemory.com/whats-new/teams)）、
[Supermemory](https://supermemory.ai/docs/self-hosting/overview)、[Cognee](https://docs.cognee.ai/setup-configuration/permissions)、
[MCP memory 服务](https://github.com/modelcontextprotocol/servers/blob/main/src/memory/README.md)、
[ChatGPT 记忆](https://help.openai.com/en/articles/8590148-memory-in-chatgpt)、
[Claude 记忆](https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context)。
有出入欢迎指正。

## 架构

```
  Claude · ChatGPT · Codex · Cursor · VS Code · Gemini CLI · Zed · goose · ...
        │ 远程 MCP，走 HTTPS                    │ 本地 MCP，走 stdio
        │ （OAuth 2.1 或 bearer 令牌）          │ （不用登录，只限本机）
        ▼                                       ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ khala serve                       khala serve --stdio        │
  │                                                              │
  │  MCP 服务 ──► 权限检查 ──► 写入规则 ──► 存储                 │
  │  (工具、prompts、 角色 ∩ agent     不收凭据、    Git，       │
  │   资源)           上限 ∩ 硬规则    64 KB 上限、  比较后交换  │
  │                                    frontmatter               │
  │                                                              │
  │  OAuth 服务        网页管理端 /app    收件箱和提案           │
  │  (CIMD、DCR、      (审核、撤销、      (笔记 → 整合           │
  │   设备码)           授权、agent)       → 批准)               │
  └──────────────┬──────────────────────────────┬────────────────┘
                 ▼                              ▼
     vault.git（Git 裸库）              khala.db（SQLite，WAL）
     main：记录                         账户、分类、授权、
     refs/khala/inbox：笔记             agent、令牌、审计日志
     refs/khala/proposals/*：提案       + secret.key（加密 TOTP）
                 │
                 ▼  git clone / pull / push（pre-receive 钩子）
     本地镜像，用任何工具读写
```

**数据放在哪。** 记录是 Git 裸库 `main` 分支上的 Markdown 文件，一条记录一个文件，YAML frontmatter 写明它的
分类、类型和状态。收件箱里等着的笔记放在 `refs/khala/inbox`，待批的提案放在 `refs/khala/proposals/<id>`，都在
`main` 之外，所以普通克隆拉不到它们，提案在人批准之前也没有 agent 读得到。记忆以外的东西都在 SQLite 里：账户和
登录因子、分类和授权、agent 和令牌、限流和审计日志。两者互相独立：仓库是记忆本身，数据库管谁能碰它。

**一次调用经过什么。** 每个 MCP 调用先解析出是哪个人、哪个 agent（来自 OAuth 令牌、bearer 令牌或 stdio 进程）。
权限检查取这个人在该分类上的角色，和 agent 的上限、服务端硬规则取交集，而且每次请求都重新查数据库，所以吊销
agent 或改授权在下一次调用就生效。读取时，调用方看不到的记录在返回任何内容之前就被过滤掉。写入要先过写入规则
（frontmatter、分类、大小、凭据特征），再以对分支引用的比较后交换（CAS）提交，两个写入方永远不会悄悄覆盖对方；
后到的一方会收到冲突和当前版本。

**记忆怎么长出来。** agent 结束任务时调用 `memory_note` 记下学到的东西，不用当场决定它该归到哪条记录。之后有权限的
agent 认领一批笔记（租约 30 分钟），把它们合进记录，并用 `memory_consolidate` 报告每条笔记的结果。整合默认写成提案，
由人在网页上批准；和现有记录冲突的内容永远等人裁决。分类所有者可以让没有冲突的改动直接生效，这些改动仍然留在审核页上，
一键就能撤销。撤销是一个新提交，而且只在这条记录之后没人改过时才执行，所以不会毁掉别人后来的工作。

**agent 怎么登录。** 服务本身就是带 PKCE 的 OAuth 2.1 授权服务器。发布了客户端元数据文档（CIMD）的客户端（Claude、
ChatGPT、Codex、VS Code 等）按文档 URL 识别，服务端在防 SSRF 的前提下抓取并校验它；其他客户端动态注册。人登录
（passkey、身份验证器或邮件验证码）后，在同意页上选这个 agent 能访问哪些分类。刷新令牌每次轮换，旧的被重复使用时
整条链一起吊销。命令行工具走设备码授权（在网页上批准一个短码）拿令牌，不用复制粘贴。

**代码地图。** `app.py`（MCP 工具、prompts 和资源）、`access.py`（权限规则）、`rules.py`（写入规则）、`store.py`（Git）、
`db.py`（SQLite 和迁移）、`inbox.py` 和 `undo.py`、`oauth.py`、`cimd.py` 和 `device.py`（agent 登录）、`login.py` 和
`web.py`（人和管理端）、`hooks.py`（pre-receive 钩子）、`catalog.py` 和 `clients.yaml`（每个客户端怎么接）、`cli.py`、
`connect.py` 和 `bridge.py`（命令行）。

## 三种运行方式

| | 单机，stdio | 单机，HTTP | 服务器 |
| --- | --- | --- | --- |
| 怎么启动 | 客户端自己启动 `khala serve --stdio` | `khala serve` | systemd 或 Docker，前面放 TLS |
| 谁用 | 你，在这台电脑上 | 你，在这台电脑上 | 你邀请的所有人，在任何地方 |
| 登录 | 不用：进程就是以你的身份运行 | OAuth，验证码在日志里 | OAuth，验证码发邮件 |
| 客户端 | 能启动本地服务的客户端 | 这台电脑上的任何 MCP 客户端 | 任何 MCP 客户端，包括网页和手机 |
| 网页管理端 | 需要时运行 `khala serve` | <http://localhost:8100/app> | `https://your-host/app` |

三种方式的数据布局和规则完全相同，可以先在笔记本上用，以后再搬到服务器。

## 在一台电脑上用

需要 Python 3.12+ 和 Git。

```sh
pipx install khala
khala init --admin you@example.com
```

也可以从克隆的仓库安装：`python3 -m venv .venv && .venv/bin/pip install -e .`，然后用 `.venv/bin` 里的同样命令。

`khala init` 建好 `./khala-data`（记录仓库、状态库和加密 TOTP 秘密的密钥），把你设为管理员，并把设置以绝对路径写进
`./khala.env`。之后在这个目录里运行的命令都会读它；在别处运行时放在最前面：`khala --env ~/khala/khala.env ...`。

**不要服务器（stdio）。** 让客户端自己启动 Khala。在 `khala.env` 所在目录里：

```sh
khala connect claude-code --local
khala connect --list                # 其他客户端
```

它会写好客户端的配置（或者运行客户端自己的 `mcp add` 命令），让客户端启动
`/绝对路径/khala --env /绝对路径/khala.env serve --stdio`。必须用绝对路径：从 Dock 或启动器打开的应用拿不到你 shell 的
`PATH` 和当前目录。不监听任何网络端口，也没有要登录的东西；改动记在一个叫 “This computer (stdio)” 的 agent 名下，
它和其他 agent 一样可以吊销。目录里没有的客户端，`khala connect other --local` 会打印要粘贴的命令。

**本地服务（HTTP）。** 给只支持远程 MCP 的客户端用，或者要用网页管理端时：

```sh
khala serve
khala connect cursor --url http://localhost:8100
```

打开 <http://localhost:8100/app>，用 `you@example.com` 登录；本地实例把验证码打印在服务日志里，不发邮件。然后在
Security 页加一个 passkey。两种方式可以同时用同一份数据：服务本来就支持多进程并发。

**你的数据**就是 `khala-data` 目录。在没有进程运行时直接复制它就是备份。想用任何工具读记录，就克隆它
（`git clone khala-data/vault.git`）；写入请通过 agent 或网页管理端，因为本地仓库没有 pre-receive 钩子检查推送。
以后要搬到服务器，把目录复制过去，把 `KHALA_ISSUER` 改成服务器地址；passkey 绑定在地址上，搬完要重新添加。

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

## 连接 agent

MCP 入口是 `https://your-host/mcp`。接入某个具体客户端最快的方法，是在网页管理端的 **Agents → Connect agent**，
或者在客户端所在的电脑上用命令行：

```sh
khala connect --list
khala connect claude-code --url https://your-host
khala connect cursor --url https://your-host --token     # 用令牌代替登录
khala connect gemini-cli --local                         # 本机自己的实例，走 stdio
```

`khala connect` 会用这台电脑上最快的方式：装了客户端程序就用它自己的命令，否则合并它的配置文件（保留原有内容，
旁边留一份备份），再不行就打开它的安装链接。`--dry-run` 只显示会做什么。这些都来自同一份目录
[`khala/clients.yaml`](khala/clients.yaml)，里面有 Claude、Claude Code、ChatGPT、Codex、Cursor、VS Code、Gemini CLI、
Zed、goose、Hermes、opencode、Cline、Kiro、Devin Desktop、LM Studio、Junie、Continue 和 JetBrains AI Assistant
的一键链接、一行命令和配置文件写法，每条都附来源，并注明有没有官方页面确认。加一个新客户端就是加一条目录，
`tests/test_catalog.py` 会检查它。

客户端怎么登录：

- **OAuth。** 发布了客户端元数据文档的客户端（Claude、Claude Code、ChatGPT、Codex、VS Code、Zed、goose、Hermes）
  按文档识别，其他客户端自己注册。两种情况都由人登录，并在同意页上选择这个 agent 能访问哪些分类。
- **令牌。** 不能登录的客户端，由向导或 `khala connect --token` 生成令牌。命令行走设备码授权（你在网页上批准一个短码），
  令牌不经过剪贴板。
- **云端 bot。** 跑在云机上的 agent 没有你的浏览器能到达的回调地址：那边的回环地址在这边就是你自己的电脑。
  它应当使用设备码授权，服务器的 OAuth 元数据里公布了入口（`device_authorization_endpoint`，在令牌端点轮询）。
  如果它只会授权码流程，就把 `https://your-host/oauth/callback` 注册为回调地址；你登录后，这个页面会显示一个地址，
  复制给 agent 即可。授权码放在 URL 片段里，不会进服务器日志。
- **桥接。** `khala bridge https://your-host/mcp` 让只能启动本地程序的客户端连上远程服务，环境变量里放 `KHALA_TOKEN`。
- **本地。** `khala serve --stdio` 在本机用本地数据目录给一个人用，不需要服务器，也不用登录；
  `khala connect 客户端 --local` 负责配好它。

### Claude Code 不用开口就记笔记

服务会要求 agent 在任务结束时记笔记，但 agent 经常忘。Claude Code 可以装 Khala 插件来提醒它：

```sh
claude plugin install khala --marketplace shuiandy/khala
```

做了一段工作之后（每次工具调用算 1，你发的每条消息算 3，默认满 15），Claude 结束一轮时，插件会让它回顾一下，
把值得保留的东西用 `memory_note` 记下来；记什么、记不记由 Claude 判断，记过之后重新计数。这些笔记进收件箱，
和其他笔记一样要经过审核，所以记错的或被注入的内容不会自己变成记录。工作量可以在安装时设置
（`--config checkpoint_every=30`，设成 `0` 就关掉提醒），以后也可以在 `/plugin` 里改。插件只读本机上这次会话对话记录里新增的部分，自己不往任何
地方发数据，需要 `PATH` 里有 `python3`。源码在 [`integrations/claude-code`](integrations/claude-code)。

服务通过 MCP instructions 告诉 agent 怎么用记忆：任务开始时查相关分类的索引，只读真正相关的几条记录，结束时记一条笔记。
工具有 `memory_scopes`、`memory_index`、`memory_search`、`memory_read`、`memory_history`、`memory_note`、
`memory_inbox`、`memory_consolidate`、`memory_write`、`memory_deprecate`、`memory_review` 和 `memory_decide`。

### 不打开网页也能做决定

有事要你决定时（等批准的分类里的提议改动、会加载进每个会话的记录、冲突），`memory_scopes` 会用 `waiting_for_you`
标出来，服务端说明让 agent 在合适的空当提一次。你同意后，agent 用 `memory_review` 列出这些事、讲清楚，再把你的
回答交给 `memory_decide`。真正改动之前，服务器会请你的应用弹出一个由服务器自己写的确认框，列出每一项和它的版本
（MCP 表单 elicitation），模型没法替你回答。弹不出确认框的应用会改为给你一个链接，页面上正好列着这一批决定，点一下就全部生效。无人值守的 agent 不能做
决定，agent 只能决定它能读、能改的内容，批准的改动 14 天内可以在审核页撤销。

想让要决定的事本身变少，可以让分类直接应用整合（在分类页设置，或 `khala consolidation 分类 auto`）：整合立即
生效，冲突仍然等人，每一条都能撤销。支持 MCP prompts 的客户端还能把这些流程
当成命令用（`recall`、`remember`、`tidy_inbox`），支持资源的客户端可以读 `khala://guide`、某个分类的索引和单条记录。

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

## 配置

设置都是环境变量，通常放在 `khala init` 写的设置文件里。改名前的旧名字（`MEMORY_*`）仍然认，服务启动时会在日志里列出
用到的旧名字。

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

## 命令行

`khala init` 建实例，`khala serve` 运行它，`khala connect` 配客户端。日常管理都在网页管理端；其余命令是服务器上的兜底
工具，以服务用户运行：`khala users`、`khala add`、`khala alias`、`khala grant`、`khala revoke`、`khala disable`、
`khala enable`、`khala scopes`、`khala auto-load`、`khala consolidation`、`khala agents`、`khala revoke-agent` 和 `khala reset-auth`。
所有命令都从环境变量、放在最前面的 `--env FILE` 或 `./khala.env` 读取设置。完整列表见 `khala help`。

## 安全模型

agent 读到的记忆会影响它之后做的每件事，所以 Khala 把 agent 写的东西当成不可信的输入，直到有人看过。

- **每个 agent 最小权限。** agent 的权限永远不超过它背后的人，通常更少：上限限定了它能碰的分类和角色。权限每次请求都
  查数据库，不缓存在令牌里。
- **看不见，而不是被禁止。** 你读不到的分类里的记录，不会出现在索引、搜索和历史里，报错也不会透露它是否存在。
- **注入必须过人这一关。** 笔记是别的 agent 写的数据，服务明确告诉 agent 不要执行其中的指令。整合出的改动在批准前是
  `main` 之外的提案，冲突永远要人裁决。
- **对话里的决定由应用确认，不由模型确认。** `memory_decide` 在你的应用把服务器写的摘要给你看过、你接受之前，
  什么都不改。这比网页弱一些，因为应用可以被设置成自己回答这类表单，所以它不对无人值守的 agent 开放，只能动
  agent 有权修改的内容，会记下是哪个 agent，而且可以撤销。
- **犯错代价低。** 每次改动都是一个写明 agent 和理由的提交。单个提交，或某个 agent 一段时间内的全部改动，都能撤销，
  而且不碰别人后来的工作。
- **凭据进不来。** 像凭据的写入和超过 64 KB 的记录，MCP 服务和 Git 钩子都会拒收。
- **登录。** passkey、TOTP 和恢复码，邮件验证码用于第一次进门（实例可以要求必须有强因子）。TOTP 秘密用一把和数据库
  分开存放的密钥加密。OAuth 用 PKCE、精确匹配回调地址（回环地址不限端口）、每个授权响应都带 issuer、刷新令牌轮换并
  检测重用，同意页绑定在完成登录的浏览器上。

漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

## 限制

- 文件名和分类 id 在整个实例里共用一个命名空间。
- 服务端用同一个 Git 身份提交，真正的作者记在提交尾注里。
- 管理员能看到全部数据。
- 只能单机部署：各 worker 共用本地磁盘上的同一个 SQLite 文件和 Git 仓库。多个 worker 进程（`--workers N`）没问题，
  因为每个一次性检查都是一条数据库语句，Git 写入是比较后交换（CAS）；但不能放在网络文件系统上。
- 搜索是线性扫描的关键词匹配：每个词都要出现，按出现次数排序。几千条记录的规模没问题，也不需要任何模型，
  但换了说法的同一件事它找不到。

## 参与

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[Apache License 2.0](LICENSE)
