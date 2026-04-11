#!/usr/bin/env python3
"""
投资日报生成器
- 数据源：Polymarket API、yfinance、Tavily
- 报告生成：DeepSeek API
- 推送：飞书自定义机器人 Webhook
"""

import os
import json
import requests
import yfinance as yf
from datetime import datetime, date
from openai import OpenAI
from tavily import TavilyClient
from zoneinfo import ZoneInfo

# ── 配置 ────────────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
TAVILY_API_KEY   = os.environ["TAVILY_API_KEY"]
FEISHU_WEBHOOK   = os.environ["FEISHU_WEBHOOK_URL"]

TZ_BEIJING = ZoneInfo("Asia/Shanghai")
TODAY      = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")

ASSETS = [
    {"name": "标普500",   "ticker": "^GSPC"},
    {"name": "纳斯达克",  "ticker": "^IXIC"},
    {"name": "黄金",      "ticker": "GC=F"},
    {"name": "原油(WTI)", "ticker": "CL=F"},
    {"name": "铜",        "ticker": "HG=F"},
    {"name": "铝",        "ticker": "ALI=F"},
    {"name": "美债10Y",   "ticker": "^TNX"},
    {"name": "美元指数",  "ticker": "DX-Y.NYB"},
    {"name": "BTC",       "ticker": "BTC-USD"},
]


# ── 数据采集 ─────────────────────────────────────────────────────────────────

def fetch_polymarket(top_n: int = 6) -> list[dict]:
    """拉取 Polymarket 热门市场（按成交量排序）"""
    url = "https://gamma-api.polymarket.com/markets"
    params = {
        "active": "true",
        "closed": "false",
        "limit": 50,
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        print(f"[Polymarket] 请求失败: {e}")
        return []

    result = []
    for m in markets[:top_n]:
        # 取第一个 outcome 的概率作为 YES 概率
        outcomes     = json.loads(m.get("outcomes", "[]"))
        out_prices   = json.loads(m.get("outcomePrices", "[]"))
        yes_pct = "--"
        no_pct  = "--"
        if len(out_prices) >= 2:
            try:
                yes_pct = f"{float(out_prices[0])*100:.1f}%"
                no_pct  = f"{float(out_prices[1])*100:.1f}%"
            except Exception:
                pass
        vol = m.get("volume24hr") or m.get("volume") or 0
        result.append({
            "question": m.get("question", "N/A"),
            "yes": yes_pct,
            "no":  no_pct,
            "volume_24h": f"${float(vol):,.0f}" if vol else "N/A",
        })
    return result


def fetch_assets() -> list[dict]:
    """拉取大类资产行情（yfinance）"""
    result = []
    for asset in ASSETS:
        try:
            ticker = yf.Ticker(asset["ticker"])
            hist   = ticker.history(period="2d")
            if len(hist) < 2:
                hist = ticker.history(period="5d")
            if len(hist) < 2:
                raise ValueError("数据不足")
            prev_close = hist["Close"].iloc[-2]
            last_close = hist["Close"].iloc[-1]
            chg_pct    = (last_close - prev_close) / prev_close * 100
            arrow      = "▲" if chg_pct >= 0 else "▼"
            result.append({
                "name":    asset["name"],
                "price":   f"{last_close:.2f}",
                "chg_pct": f"{arrow} {abs(chg_pct):.2f}%",
                "raw_chg": chg_pct,
            })
        except Exception as e:
            print(f"[yfinance] {asset['name']} 失败: {e}")
            result.append({
                "name":    asset["name"],
                "price":   "N/A",
                "chg_pct": "--",
                "raw_chg": 0,
            })
    return result


def fetch_news() -> dict:
    """用 Tavily 搜索各板块新闻"""
    client  = TavilyClient(api_key=TAVILY_API_KEY)
    queries = {
        "宏观与地缘": "global macro economy markets geopolitics today",
        "股票市场":   "US stock market S&P500 Nasdaq today",
        "大宗商品":   "commodities oil gold copper aluminum today",
        "加密货币":   "Bitcoin crypto market today",
        "Polymarket": "Polymarket prediction market trending today",
    }
    news = {}
    for topic, query in queries.items():
        try:
            res = client.search(
                query=query,
                search_depth="basic",
                max_results=4,
                include_answer=True,
            )
            snippets = [r.get("content", "")[:200] for r in res.get("results", [])]
            news[topic] = {
                "answer":   res.get("answer", ""),
                "snippets": snippets,
            }
        except Exception as e:
            print(f"[Tavily] {topic} 失败: {e}")
            news[topic] = {"answer": "", "snippets": []}
    return news


# ── 报告生成 ─────────────────────────────────────────────────────────────────

def build_data_block(polymarket, assets, news) -> str:
    """拼接结构化数据文本，供 LLM 参考"""
    lines = [f"数据日期：{TODAY}", ""]

    # Polymarket
    lines.append("== Polymarket 热门市场 ==")
    for m in polymarket:
        lines.append(
            f"- {m['question']} | YES: {m['yes']} | NO: {m['no']} | 24h成交: {m['volume_24h']}"
        )

    lines.append("")
    lines.append("== 大类资产行情 ==")
    for a in assets:
        lines.append(f"- {a['name']}: {a['price']}  {a['chg_pct']}")

    lines.append("")
    lines.append("== 新闻舆情 ==")
    for topic, data in news.items():
        lines.append(f"[{topic}]")
        if data["answer"]:
            lines.append(f"  摘要: {data['answer'][:300]}")
        for s in data["snippets"][:2]:
            lines.append(f"  · {s}")

    return "\n".join(lines)


SYSTEM_PROMPT = """你是一位专业的宏观投资分析师，擅长跨资产分析和市场叙事提炼。
你的任务是基于提供的实时数据，生成一份简洁、有见地的中文投资日报。

格式要求（严格遵守）：
1. 第一行必须是一句话市场叙事总结（不超过60字，点明当前市场在交易什么核心主题）
2. 按指定结构输出，使用 Markdown
3. AI研判部分必须包含具体的潜在风险点（至少2条）
4. 语言简练，避免废话，每个分析要有观点而非仅描述数据"""

REPORT_PROMPT = """请基于以下数据生成今日投资日报：

{data_block}

---

输出格式：

> 🔍 **{today} 市场叙事**：[一句话，说明当前市场核心交易主题/叙事]

---

## 一、Polymarket 热门押注

| 事件 | YES | NO | 24h成交量 |
|------|-----|----|-----------|
（填入数据）

**解读**：[2-3句，说明市场对哪些事件的概率判断值得关注]

---

## 二、大类资产行情

| 资产 | 最新价 | 涨跌幅 |
|------|--------|--------|
（填入数据）

**行情特征**：[跨资产联动分析，2-3句]

---

## 三、市场舆情

（按宏观、股市、大宗、加密分板块简述，每板块1-2句）

---

## 四、AI 综合研判

**核心观点**：[2-3句核心判断]

**潜在风险**：
- 风险1：[具体描述]
- 风险2：[具体描述]
- 风险3（可选）：[具体描述]

**关注事项**：[今明两天需要关注的关键数据或事件]
"""


def generate_report(data_block: str) -> str:
    """调用 DeepSeek API 生成报告"""
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
    )
    prompt = REPORT_PROMPT.format(data_block=data_block, today=TODAY)
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        temperature=0.7,
        max_tokens=2000,
    )
    return resp.choices[0].message.content.strip()


# ── 飞书推送 ─────────────────────────────────────────────────────────────────

def send_to_feishu(report_md: str) -> None:
    """通过飞书自定义机器人 Webhook 推送 Markdown 卡片"""
    # 飞书 interactive card 支持完整 Markdown
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag":     "plain_text",
                    "content": f"📊 投资日报 {TODAY}",
                },
                "template": "blue",
            },
            "elements": [
                {
                    "tag":     "markdown",
                    "content": report_md,
                }
            ],
        },
    }
    resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if result.get("code", 0) != 0:
        raise RuntimeError(f"飞书推送失败: {result}")
    print(f"[飞书] 推送成功")


# ── 主流程 ───────────────────────────────────────────────────────────────────

def main():
    print(f"[{TODAY}] 开始生成投资日报...")

    print("  1/4 拉取 Polymarket 数据...")
    polymarket = fetch_polymarket()

    print("  2/4 拉取大类资产行情...")
    assets = fetch_assets()

    print("  3/4 搜索新闻舆情...")
    news = fetch_news()

    print("  4/4 生成报告并推送...")
    data_block = build_data_block(polymarket, assets, news)
    report     = generate_report(data_block)

    # 控制台输出（便于调试）
    print("\n" + "="*60)
    print(report)
    print("="*60 + "\n")

    send_to_feishu(report)
    print("完成。")


if __name__ == "__main__":
    main()
