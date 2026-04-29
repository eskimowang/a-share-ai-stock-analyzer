# A-Share AI Stock Analyzer

一个面向 A 股研究交流的 AI 股票分析系统。它把多源行情、Tushare/AKShare/Baostock 数据、研报蒸馏、14 类市场操作手法、用户持仓/互动跟踪、AI PK 模拟盘和复盘反馈机制放在同一套看板里。

> 仅供研究交流和系统工程讨论，不构成投资建议。真实交易请自行承担风险。

## 核心能力

- 多源数据：Tushare、AKShare、Baostock，可按可用性降级。
- AI 分析：DeepSeek、Gemini、本地 CLI Agent 等可插拔。
- 14 类操作手法：诱多出货、假突破、拉升派发、洗盘、吸筹、融资爆仓等规则化复盘。
- 研报加工：支持 Tushare 研报缓存、蒸馏、观点抽取、作者/团队命中率反测。
- 互动股票跟踪：记录用户和系统聊过的股票，持续跟踪风险和机会。
- AI PK 模拟盘：多个 AI 账户用 100 万虚拟资金按 A 股规则进行模拟交易，并有指数基金基准和裁判审计。
- 决策闭环：分析、决策、行动、复盘、反馈记忆。

## 快速开始

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

## 配置

把 `config/config.example.yaml` 复制为 `config/config.yaml`，填入自己的数据源和 AI key。不要提交真实的 `config/config.yaml`。

## 开源前已移除的内容

- 真实 API key 和 token
- 真实持仓、交易、聊天、研报缓存数据库
- 日志、私钥、推送密钥
- 服务器部署路径和临时补丁脚本

## 项目结构

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
scripts/              辅助脚本
docs/                 架构和开源说明
```

## 交流方向

欢迎围绕这些问题交流：

- A 股数据源融合和质量控制
- 研报蒸馏与作者/团队反测
- AI 交易模拟的公平性和真实摩擦建模
- 14 类市场操作手法的规则化、回测和可解释性
- 用户持仓/互动股票的长期跟踪机制

## License

许可证尚未选择。公开发布前建议选择 MIT、Apache-2.0 或 GPL-3.0 之一。
