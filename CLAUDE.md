# CLAUDE.md — 投资日报项目上下文

## 项目概述

每日自动生成宏观投资日报并推送飞书。单文件项目，所有逻辑在 `daily_report.py`。

**触发方式**：GitHub Actions cron（UTC 08:09 = 北京 16:09），或本地 `python3 daily_report.py`。

---

## Python 版本差异（重要）

| 环境 | Python 版本 |
|------|------------|
| 本地 macOS | **3.9** |
| GitHub Actions (ubuntu-latest) | 3.12 |

**必须兼容 Python 3.9**，以下写法会在本地报 `TypeError`：

```python
# ❌ 3.10+ 才支持
def foo() -> str | None: ...
x: str | None = None
def bar() -> tuple[list[dict], str | None]: ...

# ✅ 3.9 兼容写法
def foo(): ...          # 直接省略类型注解
x = None
def bar() -> tuple: ...
```

不要用 `X | Y` union 语法。如果必须加类型注解，用 `Optional[X]`（需 `from typing import Optional`）。

---

## 数据流

```
Polymarket Events API
    └─ fetch_polymarket()          # 双维度拉取（volume24hr + volume），黑名单硬过滤
    └─ filter_and_translate_polymarket()  # LLM 筛选 top 6 + 翻译中文标题

yfinance
    └─ fetch_assets()              # 返回 (list[dict], latest_data_date: str)
                                   # 11 个资产并发拉取（ThreadPoolExecutor），保持 ASSETS 顺序
                                   # latest_data_date 用于休市检测

Gemini 2.5 Flash + Google Search Grounding
    └─ fetch_news()                # 3 次批量调用（串行，间隔 15s）：
                                   #   Call 1: 宏观舆情（4 个板块）
                                   #   Call 2: 股指类（标普500、纳斯达克、上证、恒生）
                                   #   Call 3: 商品/债/汇/币类（黄金、原油、铜、铝、美债10Y、美元、BTC）
                                   # 每次调用 Gemini 自动触发 Google Search 并合成摘要
                                   # 结果缓存到 .cache/news_{TODAY}.json

build_data_block()                 # 拼接结构化文本，注入休市提示（如适用）

generate_report()                  # DeepSeek API，SYSTEM_PROMPT + REPORT_PROMPT

format_for_feishu()                # 飞书格式转换（表格 → bullet list）

send_to_feishu()                   # Webhook 推送
```

---

## 关键数据结构

**`fetch_assets()` 返回值**：`(assets, latest_data_date)`
```python
assets = [{"name": "标普500", "price": "6816.89", "chg_pct": "▼ 0.11%"}, ...]
latest_data_date = "2026-04-10"  # yfinance 最新 bar 的北京日期，用于休市检测
```

**`fetch_news()` 返回值**：
```python
{
  "macro": {
    "宏观与地缘": {"answer": "...", "snippets": ["...", "..."]},
    "中国市场":   {"answer": "...", "snippets": [...]},
    "美股":       {...},
    "加密货币":   {...},
  },
  "per_asset": {
    "标普500": {"answer": "...", "snippets": [...]},
    # ... 11 个资产
  }
}
```

**`fetch_polymarket()` 返回值**：
```python
[{
  "question_en": "Will Trump end Iran military action?",
  "question_zh": "占位，由 filter_and_translate 填充",
  "yes": "67.3%",
  "chg_24h": "+5.2pp",
  "volume_24h": "$1,071,311",
  "volume_total": "$3,200,000",
}, ...]
```

---

## 休市检测逻辑

**不用 `weekday()`**，而是直接比对 yfinance 数据日期与今天：

```python
assets, latest_data_date = fetch_assets()
is_market_closed = (latest_data_date != TODAY) if latest_data_date else False
```

覆盖场景：周末、节假日、周一早盘前（美股未开盘）。`is_market_closed=True` 时：
- `build_data_block()` 在数据块顶部插入休市提示（包含具体数据日期）
- 休市提示中明确标注 **BTC 为 7×24 实时价格**，不受传统市场休市影响，避免 LLM 将 BTC 数据误当作历史收盘价处理
- `generate_report()` 切换 `report_type` 为 "宏观复盘 (Market Review)"，禁止 LLM 使用"日内波动"等词汇（BTC 除外）

---

## Prompt 工程规范

`SYSTEM_PROMPT` 中的关键约束（改动时必须保持）：

- **规则10（Polymarket 逻辑）**：缓和词（结束/停火/退出）概率**下降** = 冲突风险**上升** = 与避险资产上涨同向，不是背离。反向同理。
- **规则11（中国市场过滤）**：只保留央行政策/外资流向/宏观数据/核心指数；禁止个股微观动态。
- **证据优先原则**：所有分析必须来自数据块，不能编造宏观因果故事。
- **关注事项约束**：可基于数据块中的新闻和行情推导关注点；禁止凭背景知识补充定期数据发布（如 CPI/非农/PMI），除非数据块中有明确提及。
- **驱动因素来源（规则6）**：以 `per_asset` 对应资产的**摘要（answer）字段**为第一判据，有内容即直接提取，不受 snippets 中无关内容干扰；摘要为空时才从宏观舆情兜底并标注 `〔宏观〕`；不得跨资产借用专属新闻；个股 IPO/财报/并购等微观事件不构成指数驱动因素，遇到填"—"。

---

## Gemini 检索规范

Gemini + Google Search Grounding 与 Tavily 的关键区别：过滤意图通过 **prompt 自然语言指令**传达，而不是靠 query 关键词规避。Gemini 作为 LLM 能理解"不要 XX 类新闻"的指令，即使 Google Search 返回了无关结果，Gemini 也会在合成阶段过滤。

**每类资产的过滤指令已内嵌在 `fetch_news()` 的 prompt 中**：

- **纳斯达克**：`focus on the index itself, avoid crypto or individual stock news`
- **上证指数**：`focus on PBOC policy, foreign capital flows, macro data, core indices — not individual stocks`
- **恒生指数**：`focus on index performance and macro factors, not individual stock IPOs or listings`

改动 prompt 时需保留这些过滤指令，否则 Gemini 可能将加密货币、个股微观事件混入指数驱动因素。

**`ASSET_NEWS_QUERIES` 的 value 已不再使用**（原为 Tavily query 字符串）。该常量仅靠 key 定义资产名称顺序，搜索指令已改为直接写在 Gemini prompt 里。

---

## 飞书渲染规范

飞书 interactive card 不支持 GFM 表格，有专门的转换层：
- `format_for_feishu(md)` 将 Polymarket 表格和大类资产表格转换为 emoji 装饰的 bullet list
- 本地保存的 `.md` 文件**不经过**此函数，保留原始表格格式
- 成交量在飞书推送时自动缩写（`$1,071,311` → `$107万`）

---

## 缓存机制

`.cache/` 目录（不入 git）：

| 文件 | 用途 |
|------|------|
| `news_{TODAY}.json` | 当日新闻缓存，重跑时直接读取（跳过 Gemini 调用） |
| `polymarket_snapshot_{TODAY}.json` | 当日 YES 价格快照，供次日计算 24h 变化回退 |
| `polymarket_snapshot_{YESTERDAY}.json` | 昨日快照，`oneDayPriceChange` 为 None 时用来自算 delta |

---

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | 是 | 用于 LLM 筛选 + 报告生成 |
| `GEMINI_API_KEY` | 是 | 新闻检索（Gemini 2.5 Flash + Google Search Grounding） |
| `FEISHU_WEBHOOK_URL` | 否 | 不填则跳过推送，仅生成本地 `.md` 文件 |

本地通过 `.env` 文件加载（`python-dotenv`，已在 `.gitignore`）。

---

## 常量位置速查

| 常量 | 位置 | 说明 |
|------|------|------|
| `ASSETS` | 文件顶部 | 11 个资产及 yfinance ticker |
| `BLACKLIST_TAGS` | 文件顶部 | Polymarket 硬过滤标签（体育/娱乐） |
| `ASSET_NEWS_QUERIES` | 文件顶部 | 资产名称列表（key 有效，value 已废弃；搜索指令内嵌在 Gemini prompt） |
| `SYSTEM_PROMPT` | 报告生成区 | LLM 分析规范，含11条规则 |
| `REPORT_PROMPT` | 报告生成区 | 输出格式模板，含 `{data_block}` `{today}` `{report_type}` 占位符 |

---

## DeepSeek API 调用方式

通过 OpenAI SDK + base_url 覆盖，不是官方 DeepSeek SDK：

```python
from openai import OpenAI
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
client.chat.completions.create(model="deepseek-chat", ...)
```

---

## 已知陷阱

1. **`str | None` 语法**：本地 Python 3.9 不支持，会报 `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'`。省略类型注解或用 `Optional`。
2. **yfinance 周末数据**：周末返回周五收盘价，涨跌幅为周五数据（非 0），不能靠涨跌幅是否为 0 来判断休市，要比对日期。
3. **Polymarket `oneDayPriceChange` 可能为 None**：API 不稳定返回此字段，有本地 snapshot 回退逻辑，改动时不要删。
4. **飞书表格**：飞书消息不支持 Markdown 表格，只有 `format_for_feishu()` 之后的 bullet list 格式能正常渲染。直接把原始 markdown 推飞书会显示乱码。
5. **字符串内引号**：Python 字符串内如果有 ASCII 双引号 `"` 要改为中文引号 `""`（`\u201c\u201d`）或用 `【】` 代替，否则会提前截断字符串报 `SyntaxError`。
6. **BTC 是 7×24 交易**：`is_market_closed` 基于标普500 判定，但 BTC-USD 全天候交易。休市模式下 `build_data_block()` 已单独标注 BTC 价格为实时，改动休市逻辑时注意不要丢失此区分。
7. **`MAX_QUESTION_LEN_ZH` 已移除**：曾定义但从未使用，v4 已删除。中文标题长度由 LLM prompt 中"尽量精简"约束，无硬截断。
