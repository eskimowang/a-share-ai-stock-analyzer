# A-Share AI Stock Analyzer

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-research%20prototype-009688.svg)](https://fastapi.tiangolo.com/)

一个面向 A 股研究交流的 AI 股票分析系统。它把多源行情、Tushare / AKShare / Baostock 数据、研报蒸馏、14 类市场操作手法、用户持仓与互动股票跟踪、AI PK 模拟盘和复盘反馈机制放在同一套看板里。

> 仅供研究交流、系统工程和策略复盘讨论，不构成投资建议。真实交易请自行承担风险。

## Why This Project

很多股票分析工具只做“单次问答”或“单股报告”。这个项目更关注闭环：

```text
数据获取 -> 多维分析 -> 决策建议 -> 模拟行动 -> 复盘评分 -> 记忆更新
```

系统目标不是让 AI 一次性给出神奇答案，而是把数据、规则、研报、市场行为、AI 分歧和历史反馈组织成可持续改进的研究流程。

## Core Features

- 多源数据融合：Tushare、AKShare、Baostock，可按可用性降级。
- AI 分析协同：DeepSeek、Gemini、本地 CLI Agent 等可插拔。
- 14 类市场操作手法：诱多出货、假突破、拉升派发、洗盘、吸筹、融资爆仓等规则化复盘。
- 研报加工：支持 Tushare 研报缓存、蒸馏、观点抽取、作者/团队命中率反测。
- 互动股票跟踪：记录用户和系统聊过的股票，持续跟踪风险和机会。
- AI PK 模拟盘：多个 AI 账户用 100 万虚拟资金按 A 股规则模拟交易，并加入指数基金基准和裁判审计。
- 决策反馈闭环：分析、决策、行动、复盘、记忆不断回流。
- 看板化交互：聊天、AI PK、14 手法复盘、交流过的股票、持仓变动、架构说明等页面。

## Screens And Pages

本项目包含以下主要页面：

- `/chat`：与系统交流、触发分析和工具调用。
- `/ai-pk`：AI 模拟账户 PK、仓位、成交、收益率和裁判记录。
- `/playbook`：14 类操作手法复盘。
- `/interacted-stocks`：用户和系统交流过的股票跟踪。
- `/holding-changes`：持仓变动录入。
- `/analysis-architecture`：分析体系架构说明。

## Quick Start

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
python -m app.db_init
STOCK_ENABLE_DOCS=1 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

访问：

- `http://127.0.0.1:8000/chat`
- `http://127.0.0.1:8000/ai-pk`
- `http://127.0.0.1:8000/playbook`
- `http://127.0.0.1:8000/analysis-architecture`

## Configuration

复制示例配置：

```bash
cp config/config.example.yaml config/config.yaml
```

然后填入自己的数据源和 AI key。不要提交真实的 `config/config.yaml`、`.env`、数据库、研报缓存或持仓数据。

## What Is Not Included

公开仓库不包含：

- 真实 API key 和 token
- 真实持仓、交易、聊天、研报缓存数据库
- 日志、私钥、推送密钥
- 服务器部署路径和临时补丁脚本
- 付费研报原文或任何不可公开的数据

## Project Structure

```text
app/
  ai/                 AI 客户端与多脑协同
  api/                FastAPI 路由
  data_sources/       Tushare / AKShare / Baostock 数据源
  scheduler_jobs/     分组后的定时任务
  services/           核心业务服务
  templates/          看板页面
config/
  config.example.yaml 示例配置
docs/
  ARCHITECTURE.md     架构说明
  ROADMAP.md          后续路线
scripts/              辅助脚本
```

## Good Discussion Topics

欢迎围绕这些问题交流：

- A 股数据源融合和质量控制
- 研报蒸馏、作者/团队反测和观点结构化
- AI 模拟交易的公平性、交易摩擦和真实约束建模
- 14 类市场操作手法的规则化、回测和可解释性
- 用户持仓与互动股票的长期跟踪机制
- 多 AI 分歧、反共识机制和裁判规则设计

## Roadmap

见 [docs/ROADMAP.md](docs/ROADMAP.md)。

## Contributing

欢迎提 issue 和 PR。请不要在 issue、PR 或截图中包含真实账号、token、持仓明细、交易记录或付费研报原文。

## License

MIT License. See [LICENSE](LICENSE).
