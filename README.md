# X 博主推文监控 → 飞书

![License](https://img.shields.io/badge/license-MIT-green) ![Python](https://img.shields.io/badge/python-3.12-blue) ![Deploy](https://img.shields.io/badge/deploy-Docker-2496ED) ![Cost](https://img.shields.io/badge/成本-~$2%2F月-brightgreen)

监控一批 X（Twitter）博主的**原创**推文，自动**翻译成中文 + AI 分析**后推送到飞书群；每天早上生成一份 AI 提炼的「**昨日信号**」情报早报。低成本、可自托管、一条命令部署。

> 实测运行成本约 **$2/月**（TwitterAPI + DeepSeek；飞书机器人、DNS、证书均免费）。

---

## 效果预览

**实时推文推送**——英文推文只显中文译文，长文附 AI 分析：

![推文推送样例](docs/sample-tweet.svg)

**每日「昨日信号」早报**——顶部先列昨日被提及的股票，再给结论、重点与趋势：

![早报卡片样例](docs/sample-report.svg)

<sub>以上为示例图，内容为演示用虚构数据，排版与真实推送一致。</sub>

---

## 它解决什么问题

关注几十个高质量博主，但：信息量太大看不过来、英文推文读起来慢、真正重要的消息淹没在闲聊和转推里、隔天就忘了昨天讨论过什么。

这个项目做三件事：
1. **只留原创**——转推/回复/引用在 API 侧就被过滤掉（顺便省钱）
2. **翻译 + 分析**——英文自动转中文，长文附一段 AI 要点
3. **每日聚合**——把昨天几十条推文压缩成一张卡片：提及股票、一句话主线、三件最重要的事、主题热度与跨天趋势

---

## 架构

```
TwitterAPI.io Filter Rule (webhook)
        │  HTTPS + 长随机 secret
        ▼
FastAPI 接收端 ──► SQLite（tweet_id 去重 + delivery 幂等）
        │
        ├─► 实时推送流：翻译 + AI分析（DeepSeek）→ 飞书（可按博主路由到不同群）
        │
        └─► 每日北京 09:00 早报：
              全量去噪/分类/聚合（deepseek-v4-flash，便宜）
            → Python 算硬指标（热度/趋势/连续/异常/提及股票）
            → 写作（deepseek-v4-pro）→ 飞书交互卡片
```

**设计要点**：贵的模型只看筛选后的重点，便宜的模型处理全量，确定性数字一律由 Python 算（不让模型编数字）。

---

## 功能

**推文监控与推送**
- Filter Rule webhook 近实时接收，只推**纯原创**（服务端 + 本地双重过滤）
- 英文推文自动翻成中文（只显译文，保留原文段落排版）；含中文的直接推送
- 正文 >300 非空白字符的推文附一段**中文 AI 分析**（只依据推文内容，不编价格、不给买卖建议）
- 去裸链、换算北京时间、可自定义消息模板
- **按博主路由**：指定博主的推文可发到专属飞书群

**每日情报早报「昨日信号」**
- 每天北京 09:00 生成，推送飞书交互卡片
- **📈 提及股票**：用正则从原文确定性提取 `$MU` 这类 cashtag（不依赖模型、不会臆造），按提及频次排序，模型只负责补公司名
- 今日一句 TL;DR、三件最重要的事（附原推链接）、主题热度（升温/降温）
- **连续信号**：跨天趋势记忆，识别「某主题已连续 N 天出现」
- **异常信号**：某博主发推量突增、某主题被多人同时提及
- 你可能错过了、已折叠噪音统计

**可靠性与运维**
- tweet_id 唯一索引去重 + delivery 幂等，重启不重复推送
- 推送失败递增退避重试（1/5/15/30/60 分钟），最终进 dead-letter 并告警
- 每日心跳体检、成本报告（每 7 天）、余额监控、handle 改名检测——异常推送到独立**告警群**
- 按实测 token 用量核算成本（含 TwitterAPI 检查成本）

---

## 快速开始

### 1. 前置准备

| 需要 | 说明 |
|---|---|
| 一台服务器 | 装好 Docker + Compose V2，1C1G 足够 |
| 一个域名 | 托管在 Cloudflare（用于自动签发 HTTPS 证书） |
| TwitterAPI.io API Key | 推文数据源，按量计费 |
| 飞书群机器人 Webhook | 群设置 → 添加机器人 → 自定义机器人（建议开签名） |
| DeepSeek API Key | 翻译 / 分析 / 早报，全部功能只需这一个 LLM key |
| Cloudflare API Token | `Zone:DNS:Edit` 权限，Caddy 用它做 DNS-01 签证书 |

### 2. 一键部署

```bash
git clone <repo-url> x-monitor && cd x-monitor
bash deploy/install.sh
```

脚本会：检查 Docker 环境 → 生成 `.env` 和 `rules.yml` → **自动生成 `WEBHOOK_SECRET`** → 逐项提示填写必填配置 → 构建并启动容器 → 健康检查 → 打印后续步骤。

其它用法：

```bash
bash deploy/install.sh --yes       # 非交互：只校验 .env，不提问（CI / 重装）
bash deploy/install.sh --rebuild   # 改完代码后重建并重启
```

### 3. 收尾（3 步外部配置）

1. **Cloudflare** 加一条 A 记录，把你的子域名指向服务器 IP —— 必须**灰云 / DNS only**（橙云代理会挡掉非标端口）
2. **TwitterAPI.io 控制台 → Webhook Configuration**，填入脚本打印出的回调地址（账户级全局配置，只需填一次）
3. 编辑 `config/rules.yml` 填入要监控的博主，然后同步规则：

```bash
docker compose --env-file .env -f deploy/cloudflare/docker-compose.yml exec -T app python -m scripts.manage_rules sync
```

### 4. 验收

```bash
DC="docker compose --env-file .env -f deploy/cloudflare/docker-compose.yml"
$DC exec -T app python -m scripts.send_test              # 飞书应收到测试消息
$DC exec -T app python -m scripts.manage_rules list      # 查看远端规则是否已激活
$DC exec -T app python -m scripts.run_report 2026-01-01  # 手动生成一份早报
curl https://your-domain.com:8443/healthz                # {"status":"ok"}
```

> ⚠️ TwitterAPI.io 的规则创建后**默认未激活**，`sync` 会用 `is_effect=1` 自动激活。

<details>
<summary>手动部署（不用脚本）</summary>

```bash
cp .env.example .env                              # 填入各项密钥
cp config/rules.example.yml config/rules.yml      # 填入博主 handle
openssl rand -hex 24                              # 生成 WEBHOOK_SECRET 填进 .env
docker compose --env-file .env -f deploy/cloudflare/docker-compose.yml up -d --build
```

Caddy 通过 Cloudflare DNS-01 自动签发/续期证书，监听独立端口（默认 8443，可用 `WEBHOOK_PORT` 改），**不占用 80/443**——适合服务器上已有其它服务的情况。
</details>

---

## 配置说明（`.env`）

| 变量 | 说明 |
|---|---|
| `WEBHOOK_SECRET` | webhook 路径里的长随机 secret（部署脚本自动生成）|
| `DOMAIN` / `WEBHOOK_PORT` | webhook 域名与端口（默认 8443）|
| `CF_API_TOKEN` / `ACME_EMAIL` | Cloudflare DNS-01 自动签证书 |
| `TWITTERAPI_KEY` | TwitterAPI.io key |
| `RULE_INTERVAL_SECONDS` | 全局默认检查间隔（也可在 rules.yml 按规则覆盖）|
| `FEISHU_WEBHOOK_URL` / `FEISHU_SECRET` | 主群机器人（推文）|
| `FEISHU_ALERT_WEBHOOK_URL` / `FEISHU_ALERT_SECRET` | 告警群（心跳/成本/失败）|
| `FEISHU_ROUTES` | 按博主路由 JSON：`[{"handles":["x"],"url":"...","secret":"..."}]` |
| `PUSH_ALLOW_REPLY_QUOTE` | 放宽这些博主的本地过滤（允许引用+回复），逗号分隔 |
| `DEEPSEEK_API_KEY` | **唯一必填的 LLM key**，翻译/分析/早报都用它 |
| `TRANSLATE_PROVIDER` / `TRANSLATE_MODEL` | 翻译（默认 `deepseek` / `deepseek-v4-flash`）|
| `ANALYSIS_PROVIDER` / `ANALYSIS_MODEL` | 逐条 AI 分析（默认 `deepseek` / `deepseek-v4-pro`）|
| `REPORT_PROVIDER` / `REPORT_MODEL` | 早报写作（默认 `deepseek` / `deepseek-v4-pro`）|
| `AI_SUMMARY_MIN_CHARS` | 触发 AI 分析的正文字符阈值（默认 300，调大更省）|
| `REPORT_HOUR_BEIJING` | 早报生成时间（北京时区整点，默认 9）|
| `OPENAI_API_KEY` / `OPENAI_MODEL` | 可选：把任一 `*_PROVIDER` 改成 `openai` 才需要 |

规则配置见 [config/rules.example.yml](config/rules.example.yml)：每规则可设 `interval_seconds` 和 `exclude`（`retweet`/`reply`/`quote` 子集）。

---

## 成本模型（重要）

TwitterAPI.io **按量计费，且规则「检查」本身也扣费**（实测约 14 credits/次，即使返回 0 条推文）——所以：

- **检查越频繁越贵**。间隔调长省钱、调短费钱，不是「反正按推文算，越快越好」。
- 服务端 `-is:retweet -is:reply -is:quote` 前置过滤能减少返回推文数 → 省钱。
- 规则数 × 检查频率 = 检查成本，合并规则 / 拉长间隔都能降。

实测一份典型账单（18 位博主、4 条规则、10 分钟间隔、每天约 55 条推文）：

| 项目 | 7 天 | 占比 |
|---|---|---|
| TwitterAPI 规则检查 | $0.24 | 59% |
| TwitterAPI 返回推文 | $0.06 | 15% |
| DeepSeek（翻译+分析+早报） | $0.10 | 26% |
| **合计** | **$0.40** | 折合月度 **~$1.7** |

大头是**轮询检查**而不是 AI。想再降先拉长 `RULE_INTERVAL_SECONDS`，而不是换更便宜的模型。

`app/cost.py` 按实测 token 用量 + 检查次数核算，每 7 天推一份成本报告到告警群。模型单价可用 `.env` 的 `LLM_PRICES` 按真实账单校准。

---

## 定时任务（内嵌 APScheduler，调度器时区为 UTC）

| 任务 | 频率 | 说明 |
|---|---|---|
| 推送 worker | 每 15 秒 | 消费推送队列、退避重试 |
| 昨日信号早报 | 每天北京 09:00 | 生成并推送早报卡片（`REPORT_HOUR_BEIJING` 可改）|
| 成本报告 | 每月 1/8/15/22/29 号 | 推送告警群 |
| 心跳体检 | 每天 | 规则/key/余额/webhook/推送状态，异常告警 |
| 去重账本裁剪 | 每天 04:50 UTC | tweets 表保留最近 `PUSHED_RETENTION` 条 |
| handle 改名检测 | 每月 1 号 | user_id 变化告警 |
| 补漏 | 默认关闭 | `BACKFILL_TIMES_PER_DAY=0`；webhook 已足够可靠 |

> ⚠️ 必须**单进程**（`uvicorn --workers 1`）。内嵌调度器多进程会重复执行定时任务。

---

## 常用命令

```bash
DC="docker compose --env-file .env -f deploy/cloudflare/docker-compose.yml"
$DC exec -T app python -m scripts.manage_rules build        # 预览规则文本/字符数（不调 API）
$DC exec -T app python -m scripts.manage_rules sync         # 同步 + 激活规则（含跨规则重复 handle 检测）
$DC exec -T app python -m scripts.manage_rules list         # 列出远端规则
$DC exec -T app python -m scripts.run_report [YYYY-MM-DD]   # 手动生成早报
$DC exec -T app python -m scripts.enrich_backfill [N]       # 回填历史推文的中文/AI分析
$DC exec -T app python -m scripts.send_test                 # 飞书测试消息
$DC logs -f app                                             # 实时日志
```

## 项目结构

```
app/
  main.py          FastAPI：webhook 接收端（快速返回 2xx，推送异步）
  config.py        .env 配置
  db.py            SQLite：去重、推送队列、趋势记忆、用量记录
  webhook.py       解析 TwitterAPI.io 负载（兼容字段别名）
  feishu.py        飞书发送（文本 + 交互卡片，含 HMAC 签名）
  notifier.py      推送分发 + 消息格式化（翻译/AI分析/按博主路由）
  llm.py           LLM 客户端（每类任务可独立选 provider）
  textutils.py     中英文判定、去裸链、北京时间换算
  report.py        「昨日信号」早报管线（含提及股票提取）
  cost.py          成本核算 + 周期成本报告
  twitterapi.py    TwitterAPI.io 客户端（规则管理、余额、补漏）
  push_worker.py   后台推送 worker（退避重试）
  tasks.py         心跳 / 补漏 / 成本 / handle 检测
  scheduler.py     APScheduler 定时任务编排
config/rules.example.yml   规则配置模板（复制为 rules.yml）
deploy/install.sh          一键部署脚本
deploy/cloudflare/         Docker Compose + Caddy(Cloudflare DNS-01)  ← 推荐部署方式
scripts/                   init_db / send_test / manage_rules / run_report / enrich_backfill
schema.sql                 SQLite 表结构
```

## 数据表

`tweets` · `push_jobs` · `dead_letters` · `webhook_deliveries` · `author_state`(last_tweet_id) · `rules` · `handles` · `daily_topics`(趋势记忆) · `daily_reports`(早报存档) · `llm_usage`(token 用量) · `cost_snapshots` · `ops_events`

---

## 安全与隐私

- **密钥全部走 `.env`**，已在 `.gitignore` 与 `.dockerignore` 中排除，不会进版本库或镜像。
- `config/rules.yml`（真实博主名单）同样已忽略，仓库内只保留 `.example` 模板。
- webhook 路径带长随机 secret 做鉴权；若 TwitterAPI.io 支持自定义请求头，可再配 `WEBHOOK_HEADER_NAME/VALUE` 做二次校验。
- 若曾误提交过密钥，**轮换密钥**比重写历史更重要（TwitterAPI / DeepSeek / 飞书机器人均可在各自控制台重置）。

## 常见问题

<details>
<summary>webhook 收不到推文？</summary>

按顺序排查：① Cloudflare 是否**灰云**（橙云会挡非标端口）；② TwitterAPI.io 控制台的 Webhook Configuration 是否填了正确地址；③ `manage_rules list` 看规则 `is_effect` 是否为 1；④ `curl https://域名:端口/healthz` 是否通；⑤ 换过服务器 IP 后是否同步改了 DNS A 记录。
</details>

<details>
<summary>早报是空的 / 没生成？</summary>

早报只统计**昨天**（北京时间）的**纯原创**推文。刚部署当天没有历史数据是正常的。「连续信号」需要累积几天数据才会出现。可用 `scripts.run_report YYYY-MM-DD` 指定日期手动生成。
</details>

<details>
<summary>想换成 OpenAI / 其它模型？</summary>

`llm.py` 支持每类任务独立选 provider：把 `TRANSLATE_PROVIDER` / `ANALYSIS_PROVIDER` / `REPORT_PROVIDER` 改成 `openai` 并填 `OPENAI_API_KEY` 即可。任何兼容 OpenAI `/chat/completions` 协议的服务，改 `OPENAI_BASE_URL` 也能接。
</details>

<details>
<summary>TwitterAPI.io 的字段/接口和项目假设不一致？</summary>

webhook 字段在 [app/webhook.py](app/webhook.py) 的 `_norm_tweet` 里补别名，endpoint 路径在 [app/twitterapi.py](app/twitterapi.py) 顶部常量集中修改。
</details>

## 说明与限制

- **存储用 SQLite**，单机自托管足够；量大可换 PostgreSQL（改 `db.py`）。
- 推送渠道目前以飞书为一等公民，`APPRISE_URLS` 预留了其它渠道扩展位。
- 本项目只做信息聚合与摘要。**AI 生成内容可能有误，不构成任何投资建议**，提示词已明确禁止模型给出买卖建议或编造数字，但请自行核实原推。

## 贡献

欢迎 issue 和 PR。改动前建议先跑一遍冒烟测试：

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

## License

[MIT](LICENSE)
