# Codex Handoff Guide

这份文档是给“以后接手这个仓库的 Codex 或开发者”看的。

如果你只是第一次打开这个仓库，先看这 4 句就够了：

1. 这是一个 `Feishu Project 状态批量预览/执行代理服务`。
2. 服务端保存 Feishu secret，调用方只拿代理地址和共享密钥。
3. 建议开启“调用方必须自己提供 `user key`”模式，并记录审计日志。
4. 真正部署前先复制 `.env.example` 为 `.env.local`。
5. 当前版本还不支持 `saved view link -> 自动拉取视图里的工作项列表`，只支持解析 view link 元信息。
6. 当前版本支持直接传原始 workflow 状态名，例如 `Finished`。
7. 状态中文别名不是全局固定值，部分工作项类型会走专用映射。
8. 如果传的是中文状态，必须命中当前工作项类型的中文映射表；否则代理会直接报错。

## 仓库目的

这个仓库把原来“每个人本地直连 Feishu Project 插件”的模式，改成：

- 一个公共代理服务统一持有 Feishu 凭据
- 其他电脑或 agent 通过 HTTP 调用代理
- 通过 `preview` 和 `execute` 两步减少误操作

## 角色区分

建议按两种角色理解这个仓库：

### 1. 部署维护者

负责：

- 配置 `.env.local`
- 保存 Feishu 插件密钥
- 启动 `server.py`
- 控制是否允许真实执行

### 2. 调用方 / 普通 Codex

负责：

- 调用 `/preview-status`
- 在确认后调用 `/execute-status`
- 或使用 `client.py`

调用方不应该直接持有：

- `PROJECT_PLUGIN_SECRET`
- `PROJECT_PLUGIN_ID`
- `PROJECT_USER_KEY`

## 目录速览

- `server.py`
  HTTP 入口，负责路由、鉴权、环境开关。
- `proxy_core.py`
  Feishu Project API 调用、匹配、workflow 路径查找、状态流转。
- `view_links.py`
  解析 `issueView`、`storyView`、`workObjectView/...` 链接。
- `client.py`
  本地 CLI，适合开发者或别的 Codex 调代理。
- `run_server.sh`
  启动脚本，会读取 `.env.local`。
- `.env.example`
  服务器环境变量模板。
- `openapi.json`
  HTTP 接口契约。

## 首次接手怎么跑起来

### 如果你是部署维护者

1. 克隆仓库：

```bash
git clone git@github.com:jiangwen92/feishu-project-status-proxy.git
cd feishu-project-status-proxy
```

2. 准备环境文件：

```bash
cp .env.example .env.local
```

3. 编辑 `.env.local`，至少填：

```bash
RELAY_SHARED_SECRET=replace-with-shared-secret
PROJECT_PLUGIN_ID=...
PROJECT_PLUGIN_SECRET=...
PROJECT_KEY=rzoecp
```

推荐共享代理配置：

```bash
ALLOW_CALLER_USER_KEY=1
REQUIRE_CALLER_USER_KEY=1
PROJECT_USER_KEY=
AUDIT_LOG_PATH=logs/audit.jsonl
```

4. 启动服务：

```bash
bash run_server.sh
```

5. 健康检查：

```bash
python3 client.py health
```

### 如果你只是调用这个服务

只需要两项：

```bash
export FEISHU_STATUS_PROXY_BASE_URL=http://<host>:8787
export FEISHU_STATUS_PROXY_SHARED_SECRET=<shared-secret>
export FEISHU_PROJECT_USER_KEY=<your-own-feishu-user-key>
```

然后：

```bash
python3 client.py preview --target 修改中 --names-file /tmp/tasks.txt
python3 client.py execute --target 修改中 --names-file /tmp/tasks.txt
```

## 建议工作流

无论人还是 Codex，建议都按这个顺序操作：

1. 先 `preview`
2. 检查 `matched / ambiguous / not_found / ready / blocked`
3. 再 `execute`
4. 如果要大批量操作，优先用工作项 ID 而不是模糊标题
5. 对共享代理，默认每个人都要传自己的 `FEISHU_PROJECT_USER_KEY`

## 已知限制

当前版本明确存在这些限制：

1. 只支持 `saved view link` 的解析，不支持直接把视图里的工作项列表拉出来
2. 如果要从 saved view 批量操作，仍需要外部先把任务名单整理出来
3. 审计日志目前是本地 JSONL 文件，不是数据库或可视化后台

### 资产子任务类型专用映射

资产子任务类型 `69ca097070c61cbef714a50f` 当前使用下面这套中文目标状态映射：

- `待办` -> `Not started`
- `进行中` -> `In Progress`
- `修改中` -> `4m5jzvqqy`
- `验收中` -> `bcoksgha8`
- `资产验收通过` -> `itl0cpgq4`
- `已完成` -> `0gmbrd0o7`

### 资产任务类型专用映射

资产任务类型 `69ca09000d0f302f2617f6fc` 当前使用下面这套中文目标状态映射：

- `待办` -> `Not started`
- `进行中` -> `In Progress`
- `修改中` -> `Finished`
- `验收中` -> `c8uwlm517`
- `已完成` -> `lad5okb29`

如果调用方传的是 `Finished`、`lad5okb29` 这类原始 workflow 值，代理也会直接按原值处理。

如果调用方传的是中文状态，例如 `已完成`、`修改中`，代理会严格按当前工作项类型的中文映射表解析；中文状态没命中时会直接报错，避免误落到错误的状态 ID。

## 对接别的 Codex 时怎么交接

最稳的交接内容是：

1. 仓库地址
2. 当前部署方式
3. 谁负责保管 `.env.local`
4. 代理地址
5. 是否允许执行
6. 已知限制

可以直接发这段：

```text
Clone the repository, read README.md and CODEX_HANDOFF.md first.
If you are the server maintainer, create .env.local from .env.example and keep all Feishu secrets on the server only.
If you are only calling the proxy, use FEISHU_STATUS_PROXY_BASE_URL and FEISHU_STATUS_PROXY_SHARED_SECRET, preview first, then execute after one explicit confirmation.
```

## 不要做的事

- 不要把 `.env.local` 提交到 Git
- 不要把 `PROJECT_PLUGIN_SECRET` 发给普通调用方
- 不要跳过 `preview` 直接 `execute`
- 不要假设当前版本已经支持 `saved view -> 自动拉任务列表`

## 后续最值得继续做的事

1. 补上 `saved view -> items` 解析器
2. 增加操作者 allowlist
3. 增加调用日志和审计日志
4. 视情况再包成单独插件或云函数部署模板
