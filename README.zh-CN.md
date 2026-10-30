# News Intel

[English](README.md) · **简体中文**

一个运行在树莓派上的低噪声个人新闻情报系统。它从官方和高质量信息源采集新闻，将重复报道合并为事件，判断每个事件的确认程度，按重要性打分，并通过
Telegram 推送简洁的日报、周报和月报，提供英文版和中文版。

目标是少而精：每天只读几条真正重要的新闻，每条都附有来源和"为什么重要"，而不是被信息洪流淹没。

## 功能

- **每 5 分钟采集一次**，约 25 个可配置信息源：RSS/Atom、美国联邦公报（Federal Register）API、Hacker News、BLS/BEA
  数据发布日历（ICS）、GDELT DOC API，以及通过配置驱动的 HTML 列表页（中国政府网、国家发改委、外交部发言人）。
- **去重与事件聚类**：先按 ID、规范化 URL 和内容哈希精确去重，再按标题相似度和实体重叠聚类为事件。
- **确定性的确认状态**：事件按 `未证实 → 已报道 → 已确认 → 官方` 推进，由信源等级和独立来源族决定，绝不由 LLM 决定。
- **基于规则的评分**，主题词表无需改代码即可调整（AI、半导体、政策、宏观与市场、中国、地缘政治、国际政治）。
- **两个版本**：`en_global`（英文，太平洋时间 22:00）和 `zh_cn`（原生中文，太平洋时间 07:00），各自有日报、周报（周日）和月报（每月第一个周一）。
- **可选的 LLM 增强**：支持任何兼容 OpenAI 接口的服务，并有确定性回退，LLM 不可用时简报照常发送。
- **安全的 Telegram 投递**：聊天 ID 白名单、幂等发送（同一份简报绝不重复发送）、在 Telegram 4096 字符限制内安全拆分 HTML，默认仅预览（dry-run）。
- **专为树莓派设计**：SQLite 加 systemd 用户定时器，无需其他服务。

## 工作原理

```
信息源 ──► 采集 ──► 规范化 + 去重 ──► 聚类为事件
                                          │
          Telegram ◄── 渲染 ◄── 选稿 ◄── 评分 ◄── 确认
                          ▲
                 LLM 增强（可选，带确定性回退）
```

## 环境要求

- Python 3.11 及以上（在 Raspberry Pi OS / Debian 12 arm64 上开发，其他 Linux 应同样可用）
- 由 [@BotFather](https://t.me/BotFather) 创建的 Telegram 机器人 token 和你的聊天 ID（仅真实发送时需要）
- 可选：兼容 OpenAI 接口的 LLM 服务

## 安装

自带的 systemd 单元默认项目位于 `~/pi-news-intel`：

```bash
git clone https://github.com/miraclestar43/news-intel.git ~/pi-news-intel
cd ~/pi-news-intel
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

首次运行时会创建 SQLite 数据库 `data/news-intel.db`，`migrations/` 中的数据库迁移会自动执行。

## 配置

### 密钥

复制 `.env.example`，填写后妥善保管：

```bash
mkdir -p ~/.config/pi-news-intel
cp .env.example ~/.config/pi-news-intel/pi-news-intel.env
chmod 600 ~/.config/pi-news-intel/pi-news-intel.env
```

命令行工具会自动读取该文件。也可以自行 export 环境变量，或在 systemd 单元中用 `EnvironmentFile=` 加载。

| 变量 | 是否必需 | 用途 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 发送时必需 | @BotFather 提供的机器人 token |
| `TELEGRAM_ALLOWED_CHAT_IDS` | 发送时必需 | 逗号分隔的白名单，不会发送到白名单以外的聊天 |
| `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` | 可选 | 兼容 OpenAI 的 chat-completions 接口 |
| `LLM_PROVIDER`、`HERMES_BIN` 等 | 可选 | 另一种方式：以本地 Hermes CLI 作为 LLM |
| `SEC_USER_AGENT` | 建议 | SEC 要求带联系方式的 User-Agent |
| `GUARDIAN_API_KEY` | 可选 | 仅在启用 Guardian 信息源时需要 |

切勿提交密钥。`.env` 和 `data/` 已在 git 中忽略。

### 关注哪些内容

所有非密钥配置都是 `config/` 目录下的 YAML 文件：

| 文件 | 作用 |
|---|---|
| `sources.yaml` | 信息源、适配器、等级、轮询间隔；用 `enabled:` 开关单个信息源 |
| `topics.yaml`、`topics_zh.yaml` | 主题分组、权重和词表（英文 / 中文） |
| `editions.yaml` | 版本、标签、时区和推送时间 |
| `policy.yaml` | 评分、时效、选稿和投递策略 |
| `alert_triggers.yaml` | 突发提醒规则（默认关闭） |
| `prompts/` | 用于增强和撰写的 LLM 提示词 |

## 使用

先做只读检查和预览：

```bash
.venv/bin/pi-news doctor                  # 检查配置和环境（从不打印密钥）
.venv/bin/pi-news collect --dry-run       # 采集一次，所有数据库写入都会回滚
.venv/bin/pi-news collect --no-dry-run    # 真实采集并写入 SQLite
.venv/bin/pi-news status                  # 信息源健康状况、最近运行、队列数量
.venv/bin/pi-news digest daily --dry-run  # 生成今天的简报但不发送
.venv/bin/pi-news digest daily --dry-run --edition zh_cn
```

确认预览内容无误后，发送到 Telegram：

```bash
.venv/bin/pi-news digest daily --no-dry-run --chat-id <你的聊天ID>
```

长期定时推送请使用更严格的生产入口。它在 `--start-date` 之前不会发送，并会跳过迟到的补发：

```bash
PRODUCTION_START_DATE=2026-01-01 .venv/bin/pi-news production daily --edition zh_cn
```

`pi-news health --days 7` 可查看每天实际送达读者的内容。

## 用 systemd 定时运行

用户单元模板位于 `deploy/systemd/`：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/pi-news-collect.{service,timer} \
   deploy/systemd/pi-news-daily.{service,timer} \
   deploy/systemd/pi-news-daily-zh.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pi-news-collect.timer pi-news-daily.timer pi-news-daily-zh.timer
loginctl enable-linger "$USER"   # 未登录时也保持用户定时器运行
```

启用前请先阅读每个单元：

- `pi-news-collect` 无法访问密钥。
- 英文日报模板默认是 `--dry-run`。确认输出无误后，改为 `pi-news production daily` 并加上 `EnvironmentFile=`。
- 周报和月报单元可以使用。另外几个单元（`bot`、`held`、`cleanup`、`health`）是尚未完成功能的占位。

## 项目结构

```
src/news_intel/   采集器、处理流水线（去重、聚类、确认、评分）、任务、投递、LLM
config/           YAML 配置和 LLM 提示词
migrations/       SQLite 数据库迁移（自动执行）
deploy/systemd/   systemd 用户单元和定时器
tests/            单元测试和集成测试
docs/             需求文档、运维说明、信源策略、Git 工作流
```

## 开发

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

在短期的 `feature/*` 或 `fix/*` 分支上开发，测试通过后再合并到 `main`，详见
[docs/GIT_WORKFLOW.md](docs/GIT_WORKFLOW.md)。测试从不连接 Telegram；测试数据请使用明显的假凭据。

## 文档

- [docs/PRD.md](docs/PRD.md)：产品需求与设计
- [docs/OPERATIONS.md](docs/OPERATIONS.md)：运维说明
- [docs/SOURCE_POLICY.md](docs/SOURCE_POLICY.md)：信源选择与等级
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)：验收标准

（以上文档目前为英文。）

## 状态

这是一个个人项目，自 2026 年 8 月起在一台树莓派上每天运行。它是按一位读者的兴趣搭建的，请根据自己的需要调整 `sources.yaml`
和主题词表。部分 HTML 信息源依赖网页结构，网站改版后可能失效；`pi-news status` 会将其标记为 `degraded`，而不会导致整体失败。
