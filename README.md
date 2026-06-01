# 投资日报自动推送

每日自动生成宏观投资日报，推送至飞书。

## 功能

- **Polymarket 热门押注**：三层呈现——「24h活跃」（当日交易最活跃）+ 「长期关注」（累计投注最高）+ 「市场共识」（极端概率信号，YES≥99% 或 ≤1%）；每条事件附 AI 选中理由标签（如"地缘风险升温"）；本地 `.md` 保留英文原名供回溯
- **大类资产行情**：11 项资产（美股/中国股市/商品/债券/加密），含 AI 推断的涨跌驱动因素
- **市场舆情**：Gemini 2.5 Flash + Google Search 实时新闻，按板块分类
- **AI 综合研判**：核心观点、潜在风险、可执行交易线索
- **飞书优化渲染**：推送前自动将 Markdown 表格转换为 emoji 装饰的列表（飞书不支持 GFM 表格），本地 `.md` 文件仍保留原始表格格式

## 数据源

| 数据 | 来源 |
|------|------|
| Polymarket 押注 | [Polymarket Events API](https://gamma-api.polymarket.com/events)（免费，无需 key） |
| 大类资产行情 | yfinance |
| 新闻舆情 | Gemini 2.5 Flash + Google Search Grounding |
| 报告生成 | DeepSeek V4 Pro（报告）+ DeepSeek V4 Flash（筛选/解读） |

## 资产覆盖

标普500 · 纳斯达克 · 上证指数 · 恒生指数 · 黄金 · 原油(WTI) · 铜 · 铝 · 美债10Y · 美元指数 · BTC

## 触发方式

- **定时**：每日北京时间 06:09 自动运行（GitHub Actions cron，A 股开盘前约 20 分钟，工作日）
- **手动**：仓库 Actions 页面 → `Run workflow`

## 配置

在 GitHub 仓库 `Settings → Secrets and variables → Actions` 添加：

| Secret | 说明 |
|--------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `GEMINI_API_KEY` | Google AI Studio API Key（[免费申请](https://aistudio.google.com/apikey)） |
| `FEISHU_WEBHOOK_URL` | 飞书自定义机器人 Webhook URL |

### 飞书机器人配置

1. 飞书新建群聊（只加自己）
2. 群设置 → 群机器人 → 添加自定义机器人
3. 复制 Webhook URL 填入上方 Secret

## 本地运行

```bash
pip install -r requirements.txt
```

在项目根目录创建 `.env` 文件（已加入 `.gitignore`，不会提交）：

```
DEEPSEEK_API_KEY=your_key
GEMINI_API_KEY=your_key
FEISHU_WEBHOOK_URL=your_webhook_url   # 不填则跳过推送，仅生成本地文件
```

然后直接运行：

```bash
python3 daily_report.py
```

运行完成后在当前目录生成 `report_YYYY-MM-DD.md`。

---

## 更新记录

### v10（2026-05-31）
- **推送时间前移**：16:09 → 06:09（北京），A 股开盘（09:30）前约 20 分钟推送，提供开盘前策略参考
- **早盘前模式**：北京 06-10 点运行时自动切换，数据块提示从「⚠️ 休市」变为「📋 早盘前快报」，重点引导分析昨夜美股/欧盘对今日开盘的影响，报告类型改为「早盘前参考 (Pre-Market Brief)」
- **Polymarket 筛选稳定性修复**：`deepseek-v4-flash/pro` 均为推理模型，思维链消耗 `max_tokens` 导致 `content` 为空；筛选/解读改用 `deepseek-chat`（V3 非推理），恢复 `response_format=json_object`；LLM 失败 fallback 从 `(fallback, [], [])` 改为 `(fallback, list(fallback), [])`，长期关注不再整节消失
- **铝期货换月检测**：`ALI=F` 流动性极低，换月时出现虚假大幅跳空；新增检测逻辑（`>9%` 且成交量 `<500`），自动标注 `⚠️ (存疑)` 避免误导
- **经济日历中文化**：REPORT_PROMPT 明确指令将英文事件名翻译为中文，附常见示例

### v9（2026-04-30）
- **DeepSeek V4 迁移**：`deepseek-chat` → DeepSeek V4 双模型策略；报告生成切换至 `deepseek-v4-pro`（更强推理与世界知识），Polymarket 解读/筛选保留 `deepseek-v4-flash`（轻量快速）
- **必要性**：`deepseek-chat` 将于 2026-07-24 下线，提前迁移至官方新模型 ID

### v8（2026-04-18）
- **经济日历**：新增「五、经济日历」独立报告栏目，拉取未来 5 日高影响力宏观事件（中/美/欧/英/日/中国），每条附 LLM 推导的潜在市场影响（如"若数据超预期可能影响全球利率预期"）
- **数据源**：Gemini 2.5 Flash + Google Search Grounding，与新闻检索共用同一 API key，无额外成本
- **跨日缓存合并**：当日 Gemini 返回为空时，自动向前最多 7 天的历史缓存中捞取未来日期事件，防止已发现事件"消失"
- **关注事项还原**：`REPORT_PROMPT` 中关注事项恢复为纯新闻/Polymarket 推导，不再依赖经济日历注入；经济日历作为独立栏目单独呈现

### v7（2026-04-15）
- **市场共识栏**：YES≥99%/≤1% 的极端概率事件不再丢弃，LLM 从中筛选最多 3 条宏观相关信号，以「市场共识」小节独立呈现，避免稀释主列表同时保留关键定价信息（如"美联储4月不降息 99%"）
- **AI 选中理由标签**：每条 Polymarket 事件附 4-8 字标签说明关注理由（如"地缘风险升温""利率重定价"），与翻译在同一次 LLM 调用中生成，无额外 API 成本
- **双语标题**：本地 `.md` 文件事件列保留 `中文标题（英文原名）` 格式，飞书推送时自动剥除英文括注，手机端只展示简洁中文+标签
- **解读重定价优先**：REPORT_PROMPT 新增"绝对概率高但变化幅度也大的事件优先展开分析"的权重指引

### v6（2026-04-15）
- **信息检索层重构**：Tavily → Gemini 2.5 Flash + Google Search Grounding，免费且 Google 索引覆盖更全
- **3 次批量调用**：宏观舆情（1 次）+ 股指类（1 次）+ 商品/债/汇/币类（1 次），替代原来 15 次并发调用，在免费层 5 RPM 限制内稳定运行
- **过滤机制升级**：从 Tavily 关键词规避（避免抓到个股/IPO/加密噪音）改为 Gemini prompt 自然语言指令，更可靠
- **环境变量**：`TAVILY_API_KEY` → `GEMINI_API_KEY`

### v5（2026-04-13）
- **定时调整**：cron 改为 UTC 01:23（北京 09:23），缓解 GitHub Actions 00:xx 时段队列拥堵导致的延迟到账问题
- **恒生指数 query 优化**：去掉 `Hong Kong stocks` / `listing` 语义，改为 `Hang Seng Index HSI performance market movement today`，避免 Tavily 抓回个股 IPO 新闻
- **驱动因素规则补充**：明确个股 IPO/财报/并购等微观事件不构成指数驱动因素，LLM 遇到此类内容应填"—"
- **关注事项约束**：禁止 LLM 凭背景知识补充"惯例上本周会有XX数据"，只允许引用数据块中明确提及的事件

### v4（2026-04-12）
- **并发加速**：Tavily 15 个请求（4 宏观 + 11 分资产）从串行改为并发，yfinance 11 个资产也改为并发拉取，新闻检索耗时从约 40s 降至 5s 以内
- **驱动因素质量提升**：Tavily per_asset 摘要有内容时直接提取，不再被无关 snippets 干扰；摘要为空时才从宏观舆情兜底，并标注 `〔宏观〕`
- **BTC 24/7 正确处理**：休市模式下数据块中明确标注 BTC 为实时价格，避免 LLM 将 BTC 当作历史收盘价处理
- **稳健性**：LLM 筛选和报告生成均加入失败自动重试（最多 2 次）；报告生成失败时返回原始数据块而非崩溃
- **max_tokens 扩容**：报告生成从 2500 → 3500，避免长报告被截断

### v3（2026-04-12）
- **Polymarket 智能筛选**：废弃白名单标签过滤，改为 LLM 智能筛选——从候选池中挑选最具投资价值的 6 条事件，同一主题自动去重，过滤小国选举/远期噪音
- **双维度候选池**：同时按 24h 成交量和累计成交量拉取事件，合并去重，避免遗漏长期高关注度事件
- **过期事件过滤**：基于 Polymarket endDate 字段过滤已过验证时间的事件
- **结果已定过滤**：YES 概率 >=99% 或 <=1% 的事件自动排除
- **飞书移动端优化**：去掉列表蓝色圆点、精简表头、缩写成交量（$1.1M→$110万），适配手机窄屏
- **定时优化**：cron 改为 UTC 00:37（北京 08:37），避开 GitHub Actions 整点拥堵

### v2（2026-04-12）
- **飞书渲染优化**：新增 `format_for_feishu()` 转换层，推送前将 Polymarket 和大类资产两张表转为 emoji 装饰的 bullet 列表（飞书 interactive card 不支持 GFM 表格语法），同时为四个章节标题添加 emoji 前缀
- **本地开发体验**：集成 `python-dotenv`，支持从 `.env` 文件自动加载环境变量，本地运行无需手动 `export`

### v1（2026-04-11）
- Polymarket 切换 `/events` 端点，白名单+黑名单双层过滤，新增 24h 价格变化列
- 大类资产新增上证指数、恒生指数（共 11 项）
- 大类资产表新增 AI 推断的「驱动因素」列
- AI 研判新增「交易线索」小节（资产/板块 + 方向倾向 + 触发条件）
