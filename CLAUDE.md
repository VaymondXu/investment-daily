# CLAUDE.md — 投资日报项目上下文

## 项目概述

每日自动生成宏观投资日报并推送飞书。单文件项目，所有逻辑在 `daily_report.py`。

**触发方式**：GitHub Actions cron（UTC 22:09 前一天 = 北京 06:09，工作日），或本地 `python3 daily_report.py`。

---

## Python 版本差异（重要）

| 环境 | Python 版本 |
|------|------------|
| 本地 macOS | **3.9** |
| GitHub Actions (ubuntu-latest) | 3.12 |

**必须兼容 Python 3.9**：禁止 `X | Y` union 语法（会报 `TypeError`）。省略类型注解，或用 `Optional[X]`（需 `from typing import Optional`）。

---

## 数据流

```
Polymarket Events API
    └─ fetch_polymarket()          # 双维度拉取（volume24hr + volume），黑名单硬过滤
                                   # 返回 (result, consensus_candidates)
    └─ filter_and_translate_polymarket()  # LLM 三组筛选 + 翻译 + AI 标签
                                   # 返回 (list_24h, list_total, list_consensus)

yfinance
    └─ fetch_assets()              # 返回 (list[dict], latest_data_date: str)
                                   # 11 个资产并发拉取（ThreadPoolExecutor），保持 ASSETS 顺序
                                   # latest_data_date 用于休市检测

Gemini 2.5 Flash + Google Search Grounding
    └─ fetch_news()                # 3 次批量调用（串行，间隔 15s）：
                                   #   Call 1: 宏观舆情（4 个板块）
                                   #   Call 2: 股指类（标普500、纳斯达克、上证、恒生）
                                   #   Call 3: 商品/债/汇/币类（黄金、原油、铜、铝、美债10Y、美元、BTC）
                                   # 结果缓存到 .cache/news_{TODAY}.json
    └─ fetch_economic_calendar()   # 拉取 TODAY ~ TODAY+5 高影响力宏观事件（中/美/欧/英/日/中国）
                                   # 返回 str（每行格式：YYYY-MM-DD HH:MM [国家] 事件 | 前值 | 预期）
                                   # 缓存到 .cache/econ_cal_{TODAY}.txt
                                   # 若当日 Gemini 返回为空，向前最多 7 天历史缓存捞取未来事件（跨日合并）

build_data_block()                 # 拼接结构化文本，注入休市提示（如适用）

generate_report()                  # DeepSeek V4 API（OpenAI SDK + base_url 覆盖），SYSTEM_PROMPT + REPORT_PROMPT
                                   # 报告生成用 deepseek-v4-pro，Polymarket 解读/筛选用 deepseek-v4-flash
                                   # ⚠️ V4 系列均为推理模型：reasoning_content 先消耗 max_tokens，剩余才写入 content
                                   # filter 调用设 max_tokens=6000，interpret 设 max_tokens=2000，为 reasoning 留足预算

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

**`fetch_polymarket()` / `filter_and_translate_polymarket()` 数据结构**：
```python
# fetch_polymarket() 返回 (result, consensus_candidates)
# result              — 普通候选（YES 2%~98%）
# consensus_candidates — 极端共识事件（YES≥99% 或 ≤1%）
# 两个列表的元素结构相同：
{
  "question_en":  "Will Trump end Iran military action?",
  "question_zh":  "占位，由 filter_and_translate 填充",
  "yes":          "67.3%",
  "chg_24h":      "+5.2pp",
  "volume_24h":   "$1,071,311",
  "volume_total": "$3,200,000",
}

# filter_and_translate_polymarket() 返回 (list_24h, list_total, list_consensus)
# 在上述结构基础上增加：
{
  "question_zh": "特朗普宣布结束对伊朗军事行动 — 4月15日",  # LLM 中文翻译
  "tag":         "地缘风险升温",                             # LLM 4-8字选中理由标签
}
```

---

## 休市检测逻辑

**不用 `weekday()`**，而是直接比对 yfinance 数据日期与今天：

```python
assets, latest_data_date = fetch_assets()
is_market_closed = (latest_data_date != TODAY) if latest_data_date else False
```

覆盖场景：周末、节假日、开盘前（早上运行时 A 股/港股尚未开盘，最新数据为前一交易日）。`is_market_closed=True` 时：
- `build_data_block()` 在数据块顶部插入提示，内容因 `is_morning` 而异：
  - `is_morning=True`（北京 06-10 点）：「📋 早盘前快报」，引导 LLM 关注昨夜美股/欧盘对今日开盘的影响
  - `is_morning=False`（其他时段）：「⚠️ 休市提示」，禁止使用"日内波动"等词汇
- 两种模式均明确标注 **BTC 为 7×24 实时价格**，不受传统市场影响
- `generate_report()` 的 `report_type`：
  - 早盘前：`"早盘前参考 (Pre-Market Brief)"`
  - 休市复盘：`"宏观复盘 (Market Review)"`
  - 正常盘中：`"投资日报 (Daily Update)"`

---

## Prompt 工程规范

`SYSTEM_PROMPT` 中的关键约束（改动时必须保持）：

- **规则7（Polymarket 完整性）**：24h活跃和长期关注两表必须完整列出所有事件；若数据块含 `[市场共识]` 小节，还需输出 `**市场共识**` 单行。
- **规则10（Polymarket 逻辑）**：缓和词（结束/停火/退出）概率**下降** = 冲突风险**上升** = 与避险资产上涨同向，不是背离。反向同理。
- **规则11（中国市场过滤）**：只保留央行政策/外资流向/宏观数据/核心指数；禁止个股微观动态。
- **证据优先原则**：所有分析必须来自数据块，不能编造宏观因果故事。
- **关注事项约束**：可基于数据块中的新闻和行情推导关注点；禁止凭背景知识补充定期数据发布（如 CPI/非农/PMI），除非数据块中有明确提及。
- **驱动因素来源（规则6）**：以 `per_asset` 对应资产的**摘要（answer）字段**为第一判据，有内容即直接提取；摘要为空时才从宏观舆情兜底并标注 `〔宏观〕`；不得跨资产借用专属新闻；个股 IPO/财报/并购等微观事件不构成指数驱动因素，遇到填"—"。
- **重定价优先原则（REPORT_PROMPT）**：解读段中，绝对概率高但 24h 变化幅度也大的事件，比低概率小变化事件更值得深入分析。

---

## Gemini 检索规范

过滤意图通过 **prompt 自然语言指令**传达，Gemini 在合成阶段过滤，无需靠 query 关键词规避。

**每类资产的过滤指令已内嵌在 `fetch_news()` 的 prompt 中**（改动时必须保留）：

- **纳斯达克**：`focus on the index itself, avoid crypto or individual stock news`
- **上证指数**：`focus on PBOC policy, foreign capital flows, macro data, core indices — not individual stocks`
- **恒生指数**：`focus on index performance and macro factors, not individual stock IPOs or listings`

---

## 飞书渲染规范

飞书 interactive card 不支持 GFM 表格，有专门的转换层：
- `format_for_feishu(md)` 将 Polymarket 表格和大类资产表格转换为 emoji 装饰的 bullet list
- 本地保存的 `.md` 文件**不经过**此函数，保留原始表格格式（含英文原名和 tag）
- 成交量在飞书推送时自动缩写（`$1,071,311` → `$107万`）
- `_fmt_polymarket()` 用正则剥除英文括注 `（...）`，飞书端只展示中文标题 + `〔tag〕`
- `**市场共识**：...` 行不是表格，直接透传，飞书可正常渲染

---

## 缓存机制

`.cache/` 目录（不入 git）：

| 文件 | 用途 |
|------|------|
| `news_{TODAY}.json` | 当日新闻缓存，重跑时直接读取（跳过 Gemini 调用） |
| `econ_cal_{TODAY}.txt` | 当日经济日历缓存（每行一条事件）；跨日合并时也会读取历史文件中的未来事件 |
| `polymarket_snapshot_{TODAY}.json` | 当日 YES 价格快照，供次日计算 24h 变化回退 |
| `polymarket_snapshot_{YESTERDAY}.json` | 昨日快照，`oneDayPriceChange` 为 None 时用来自算 delta |

---

## 环境变量

| 变量 | 必填 | 说明 |
|------|------|------|
| `DEEPSEEK_API_KEY` | 是 | 用于 LLM 筛选/解读（deepseek-v4-flash）+ 报告生成（deepseek-v4-pro） |
| `GEMINI_API_KEY` | 是 | 新闻检索（Gemini 2.5 Flash + Google Search Grounding） |
| `FEISHU_WEBHOOK_URL` | 否 | 不填则跳过推送，仅生成本地 `.md` 文件 |

本地通过 `.env` 文件加载（`python-dotenv`，已在 `.gitignore`）。

---

## 常量位置速查

| 常量 | 位置 | 说明 |
|------|------|------|
| `ASSETS` | 文件顶部 | 11 个资产及 yfinance ticker |
| `BLACKLIST_TAGS` | 文件顶部 | Polymarket 硬过滤标签（体育/娱乐） |
| `ASSET_NEWS_QUERIES` | 文件顶部 | key 定义资产名称顺序；value（原 Tavily query）已废弃，搜索指令内嵌在 Gemini prompt |
| `SYSTEM_PROMPT` | 报告生成区 | LLM 分析规范，含11条规则 |
| `REPORT_PROMPT` | 报告生成区 | 输出格式模板，含 `{data_block}` `{today}` `{report_type}` 占位符 |

---

## 已知陷阱

1. **`str | None` 语法**：本地 Python 3.9 不支持，省略类型注解或用 `Optional`。
2. **yfinance 周末数据**：周末返回周五收盘价，涨跌幅非 0，不能靠涨跌幅判断休市，要比对日期。
3. **Polymarket `oneDayPriceChange` 可能为 None**：API 不稳定返回此字段，有本地 snapshot 回退逻辑，改动时不要删。
4. **飞书表格**：飞书消息不支持 Markdown 表格，只有 `format_for_feishu()` 之后的 bullet list 格式能正常渲染。
5. **BTC 是 7×24 交易**：`is_market_closed` 基于标普500 判定，但 BTC-USD 全天候交易。休市模式下 `build_data_block()` 已单独标注 BTC 价格为实时，改动休市逻辑时不要丢失此区分。
6. **`fetch_polymarket()` 和 `filter_and_translate_polymarket()` 返回元组**（v7 起）：前者返回 `(result, consensus_candidates)`，后者返回 `(list_24h, list_total, list_consensus)`。调用处须正确解包，否则 `len()` 会返回元组长度（2或3）而非事件数量。
7. **`tag` 字段可能为空字符串**：`build_data_block()` 已用 `m.get("tag")` 判空后才追加 `〔tag〕`，不会出现 `〔〕` 空标签。
8. **经济日历跨日缓存**：`fetch_economic_calendar()` 写入当日缓存前先做跨日合并（向前 7 天历史文件），捞取其中仍为未来日期的事件行。合并逻辑依赖行首 `YYYY-MM-DD` 格式做去重和过滤，Gemini prompt 若返回非标准格式行会被静默丢弃。
9. **DeepSeek V4 系列（flash/pro）是推理模型**：`reasoning_content` 先消耗 `max_tokens`，当 prompt 较长且 `max_tokens` 不足时 `content` 为空（`finish_reason: length`）。解法是给 V4 调用留足预算：filter 用 `max_tokens=6000`，interpret 用 `max_tokens=2000`，不得缩减，否则 `json.loads("")` 必然报错。
10. **ALI=F（铝期货）流动性极低**：日均成交量仅数百手，期货换月时会出现虚假大幅变动（>10%），与真实行情无关。代码已加检测：`abs(chg_pct) > 9` 且 `Volume < 500` 时标注 `⚠️ (存疑)`。yfinance 内无更优铝期货替代 ticker，如需可靠数据需接入其他数据源（如 LME、Wind）。
11. **早盘前模式**（北京 06-10 点）：此时 A 股/港股尚未开盘，`is_market_closed` 必然为 True，但与周末/节假日语义不同。`is_morning` 标志用于区分二者，改动 `build_data_block` 或 `generate_report` 时须同时处理两种路径。
