# 投资日报自动推送

每日自动生成宏观投资日报，推送至飞书。

## 功能

- **Polymarket 热门押注**：过滤体育/娱乐，仅保留政治、地缘、宏观、金融、加密五大类；展示 YES 概率及 24h 变化
- **大类资产行情**：11 项资产（美股/中国股市/商品/债券/加密），含 AI 推断的涨跌驱动因素
- **市场舆情**：Tavily 实时新闻，按板块分类
- **AI 综合研判**：核心观点、潜在风险、可执行交易线索
- **飞书优化渲染**：推送前自动将 Markdown 表格转换为 emoji 装饰的列表（飞书不支持 GFM 表格），本地 `.md` 文件仍保留原始表格格式

## 数据源

| 数据 | 来源 |
|------|------|
| Polymarket 押注 | [Polymarket Events API](https://gamma-api.polymarket.com/events)（免费，无需 key） |
| 大类资产行情 | yfinance |
| 新闻舆情 | Tavily Search API |
| 报告生成 | DeepSeek API |

## 资产覆盖

标普500 · 纳斯达克 · 上证指数 · 恒生指数 · 黄金 · 原油(WTI) · 铜 · 铝 · 美债10Y · 美元指数 · BTC

## 触发方式

- **定时**：每日北京时间 09:00 自动运行（GitHub Actions cron）
- **手动**：仓库 Actions 页面 → `Run workflow`

## 配置

在 GitHub 仓库 `Settings → Secrets and variables → Actions` 添加：

| Secret | 说明 |
|--------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `TAVILY_API_KEY` | Tavily API Key |
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
TAVILY_API_KEY=your_key
FEISHU_WEBHOOK_URL=your_webhook_url   # 不填则跳过推送，仅生成本地文件
```

然后直接运行：

```bash
python3 daily_report.py
```

运行完成后在当前目录生成 `report_YYYY-MM-DD.md`。

---

## 更新记录

### v2（2026-04-12）
- **飞书渲染优化**：新增 `format_for_feishu()` 转换层，推送前将 Polymarket 和大类资产两张表转为 emoji 装饰的 bullet 列表（飞书 interactive card 不支持 GFM 表格语法），同时为四个章节标题添加 emoji 前缀（🎯 📊 📰 🧠）
- **本地开发体验**：集成 `python-dotenv`，支持从 `.env` 文件自动加载环境变量，本地运行无需手动 `export`

### v1（2026-04-11）
- Polymarket 切换 `/events` 端点，白名单+黑名单双层过滤，新增 24h 价格变化列
- 大类资产新增上证指数、恒生指数（共 11 项）
- 大类资产表新增 AI 推断的「驱动因素」列
- AI 研判新增「交易线索」小节（资产/板块 + 方向倾向 + 触发条件）
