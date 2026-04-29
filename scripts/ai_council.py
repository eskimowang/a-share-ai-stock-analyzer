"""让 4 家 AI 讨论分析推送频率 —— 给用户演示"AI 商量"的过程。"""
import sys, os, yaml, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.multi_brain import MultiBrain, build_brains_from_config

with open("/opt/stock-analyzer/config/config.yaml") as f:
    cfg = yaml.safe_load(f)

brains = build_brains_from_config(cfg)
mb = MultiBrain(brains)

question = """【AI 议会】讨论一个实际问题，请直接给出专业建议。

用户场景:
- 中国 A 股个人投资者
- 有 2 个功能需求:
  (A) 【交易策略】对已持仓的几只股票做多方博弈分析 + 日常操作建议（买/卖/持/补）
  (B) 【荐股发现】从大势→行业→标的筛选，按"时间(短/中/长线) × 弹性(低/中/高)"矩阵推送候选
- AI 模型调用成本: 每次全 4 家分析约 ¥0.1
- 用户每天查看时间有限，不想被过量通知轰炸

问题:
1. 【交易策略】推送频率建议? (每日几次? 具体什么时点?)
2. 【荐股发现】推送频率建议? (每周/每月? 常规更新 vs 异动触发?)
3. 两个功能如果用户同时订阅，通知频率如何平衡，不让他关掉?

请输出 (严格按格式):

### 一句话建议
（不超过 30 字）

### 交易策略频率
- 日内: （具体时点 + 原因）
- 异动触发: （什么条件触发额外分析）

### 荐股发现频率
- 常规: （周期 + 覆盖范围）
- 异动: （什么条件插播）

### 通知策略
- 每日最多 N 条
- 如何分级（紧急/重要/信息）

**直接回答，不要解释你的工作方式。**
"""

sys_role = "你是金融科技产品顾问，擅长设计投资工具的用户体验，理解成本/新鲜度/注意力经济平衡。"

print("=" * 70)
print("AI 议会开会：分析推送频率讨论")
print("=" * 70)
print()

t0 = time.time()
opinions = mb.analyze(stock_data={}, max_tokens=1000) if False else None

# 直接用 complete，不通过 analyze（不需要股票数据）
results = {}
import concurrent.futures
def _one(client):
    try:
        return client.name, client.complete(sys_role, question, max_tokens=1200)
    except Exception as e:
        return client.name, f"[失败] {type(e).__name__}: {e}"

with concurrent.futures.ThreadPoolExecutor(max_workers=len(brains)) as pool:
    futures = [pool.submit(_one, b) for b in brains]
    for f in concurrent.futures.as_completed(futures):
        name, text = f.result()
        results[name] = text

print(f"4 家并行耗时: {time.time()-t0:.1f}s\n")

for name, text in results.items():
    print(f"━━━━━━━━━━━━━━━━━━━━━ {name} ━━━━━━━━━━━━━━━━━━━━━")
    print(text)
    print()

# 综合
print("=" * 70)
print("仲裁综合（Claude）：4 家意见整合")
print("=" * 70)
joined = "\n\n".join(f"## 【{n}】\n{t}" for n, t in results.items())

# 用 Claude 做仲裁，它本地记忆里有用户画像
claude = [b for b in brains if b.name == "Claude"][0]
summary = claude.complete(
    system="你是投资决策专家，整合多家观点。",
    user=(
        f"4 家 AI 针对分析推送频率的独立建议:\n\n{joined}\n\n"
        "请整合出**最终建议**（直接可执行的参数）:\n\n"
        "### 🎯 最终推荐频率方案\n"
        "#### 交易策略\n"
        "- 主动推送: （时间点）\n"
        "- 异动插播: （触发条件 + 阈值）\n\n"
        "#### 荐股发现\n"
        "- 常规更新: （周期 + 覆盖范围）\n"
        "- 异动插播: （条件）\n\n"
        "#### 通知上限\n"
        "- 每日最多: N 条\n"
        "- 分级策略: （紧急 / 重要 / 信息）\n\n"
        "### 💡 4 家分歧点\n"
        "（谁主张更频繁、谁主张更少、核心理由）\n\n"
        "**给出最可执行、最简洁的结论。**"
    ),
    max_tokens=1500,
)
print(summary)
print(f"\n总耗时: {time.time()-t0:.1f}s")
