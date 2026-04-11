#!/usr/bin/env python3
"""
投资日报生成器
- 数据源：Polymarket Events API、yfinance、Tavily
- 报告生成：DeepSeek API
- 推送：飞书自定义机器人 Webhook
"""

import os
import re
import json
from dotenv import load_dotenv
load_dotenv()
import requests
import yfinance as yf
from datetime import datetime, timedelta
from pathlib import Path
from openai import OpenAI
from tavily import TavilyClient
from zoneinfo import ZoneInfo

# ── 配置 ────────────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
TAVILY_API_KEY   = os.environ["TAVILY_API_KEY"]
FEISHU_WEBHOOK   = os.environ.get("FEISHU_WEBHOOK_URL", "")

TZ_BEIJING = ZoneInfo("Asia/Shanghai")
TODAY      = datetime.now(TZ_BEIJING).strftime("%Y-%m-%d")
YESTERDAY  = (datetime.now(TZ_BEIJING) - timedelta(days=1)).strftime("%Y-%m-%d")

CACHE_DIR  = Path(__file__).parent / ".cache"

ASSETS = [
    {"name": "标普500",    "ticker": "^GSPC"},
    {"name": "纳斯达克",   "ticker": "^IXIC"},
    {"name": "上证指数",   "ticker": "000001.SS"},
    {"name": "恒生指数",   "ticker": "^HSI"},
    {"name": "黄金",       "ticker": "GC=F"},
    {"name": "原油(WTI)",  "ticker": "CL=F"},
    {"name": "铜",         "ticker": "HG=F"},
    {"name": "铝",         "ticker": "ALI=F"},
    {"name": "美债10Y",    "ticker": "^TNX"},
    {"name": "美元指数",   "ticker": "DX-Y.NYB"},
    {"name": "BTC",        "ticker": "BTC-USD"},
]

# Polymarket 过滤规则（基于 events 端点的 tags 字段）
WHITELIST_TAGS = {
    "Politics", "Elections", "World Elections", "Global Elections", "US Election",
    "Geopolitics", "Foreign Policy", "World", "Middle East",
    "Economy", "Economic Policy", "Fed", "Fed Rates", "Macro Election 1", "Macro Election 2",
    "Finance", "Finance Updown", "Commodities", "Oil",
    "Crypto", "Bitcoin", "Crypto Prices", "Ethereum",
}
BLACKLIST_TAGS = {
    "Sports", "NBA", "NFL", "MLB", "Soccer", "Golf", "Basketball", "Games",
    "Culture", "Music", "Awards", "Tweet Markets", "Hide From New",
}

# Polymarket 中文标题最大字符数（LLM 翻译目标 ≤18，此处兜底硬上限）
MAX_QUESTION_LEN_ZH = 22
# 英文回退截断长度
MAX_QUESTION_LEN_EN = 24

# 分资产定向新闻 query（用于驱动因素归因；中性措辞，避免诱导 Tavily 强行解释）
ASSET_NEWS_QUERIES = {
    "标普500":    "S&P 500 index market news today",
    "纳斯达克":   "Nasdaq 100 tech stocks market news today",
    "上证指数":   "Shanghai Composite A-share market news today",
    "恒生指数":   "Hang Seng Hong Kong stocks market news today",
    "黄金":       "gold price market news today",
    "原油(WTI)":  "WTI crude oil market news today",
    "铜":         "copper commodity market news today",
    "铝":         "aluminum commodity market news today",
    "美债10Y":    "US Treasury 10-year yield market news today",
    "美元指数":   "DXY US dollar index market news today",
    "BTC":        "Bitcoin market news today",
}


# ── 数据采集 ─────────────────────────────────────────────────────────────────

def fetch_polymarket(top_n: int = 6) -> list[dict]:
    """拉取 Polymarket 热门投资相关事件（按 24h 成交量排序，过滤体育/娱乐等）"""
    CACHE_DIR.mkdir(exist_ok=True)

    # 加载昨日 snapshot，用于 oneDayPriceChange 为 None 时回退计算 delta
    yesterday_snapshot: dict[str, float] = {}
    snap_yesterday = CACHE_DIR / f"polymarket_snapshot_{YESTERDAY}.json"
    if snap_yesterday.exists():
        try:
            yesterday_snapshot = json.loads(snap_yesterday.read_text())
        except Exception:
            pass

    url = "https://gamma-api.polymarket.com/events"
    params = {
        "active":    "true",
        "closed":    "false",
        "limit":     50,
        "order":     "volume24hr",
        "ascending": "false",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"[Polymarket] 请求失败: {e}")
        return []

    result = []
    today_snapshot: dict[str, float] = {}

    for event in events:
        markets = event.get("markets") or []
        # 收集所有子市场的 YES 价格到 today_snapshot（不限 top_n）
        for m in markets:
            mid = m.get("id")
            if not mid:
                continue
            out_prices = m.get("outcomePrices") or "[]"
            if isinstance(out_prices, str):
                try:
                    out_prices = json.loads(out_prices)
                except Exception:
                    out_prices = []
            if out_prices:
                try:
                    today_snapshot[str(mid)] = float(out_prices[0])
                except Exception:
                    pass

        if len(result) >= top_n:
            continue

        # 双层标签过滤：黑名单优先，再检查白名单
        tag_labels = {t.get("label", "") for t in (event.get("tags") or [])}
        if tag_labels & BLACKLIST_TAGS:
            continue
        if not (tag_labels & WHITELIST_TAGS):
            continue

        if not markets:
            continue

        # 多子市场取 24h 成交量最大的；单市场直接用第一个
        if len(markets) > 1:
            market = max(markets, key=lambda m: m.get("volume24hr") or 0)
            question_en = f"{event.get('title', 'N/A')} — {market.get('groupItemTitle', '')}"
        else:
            market = markets[0]
            question_en = event.get("title", "N/A")

        # YES 概率
        out_prices = market.get("outcomePrices") or "[]"
        if isinstance(out_prices, str):
            try:
                out_prices = json.loads(out_prices)
            except Exception:
                out_prices = []
        yes_price = float(out_prices[0]) if out_prices else None
        yes_pct = f"{yes_price*100:.1f}%" if yes_price is not None else "--"

        # 24h 价格变化：优先 API 原生字段，回退本地 snapshot 计算
        chg = market.get("oneDayPriceChange")
        if chg is not None:
            sign = "+" if chg >= 0 else ""
            chg_24h = f"{sign}{chg*100:.1f}pp"
        else:
            mid = str(market.get("id", ""))
            if mid and mid in yesterday_snapshot and yes_price is not None:
                delta = yes_price - yesterday_snapshot[mid]
                sign = "+" if delta >= 0 else ""
                chg_24h = f"{sign}{delta*100:.1f}pp*"  # * 表示本地计算
            else:
                chg_24h = "--"

        vol = market.get("volume24hr") or 0
        result.append({
            "question_en": question_en,   # 原始英文，供翻译和调试
            "question_zh": question_en,   # 占位，由 translate_polymarket_titles() 填充
            "yes":         yes_pct,
            "chg_24h":     chg_24h,
            "volume_24h":  f"${float(vol):,.0f}" if vol else "N/A",
        })

    # 写入今日 snapshot，供明日回退使用
    try:
        snap_today = CACHE_DIR / f"polymarket_snapshot_{TODAY}.json"
        snap_today.write_text(json.dumps(today_snapshot, ensure_ascii=False))
    except Exception as e:
        print(f"[Polymarket] snapshot 写入失败: {e}")

    return result


def translate_polymarket_titles(items: list[dict]) -> list[dict]:
    """批量将 Polymarket 英文标题翻译并精简为中文（≤18 字），更新 question_zh 字段"""
    if not items:
        return items

    titles = [it["question_en"] for it in items]
    titles_json = json.dumps(titles, ensure_ascii=False)

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    prompt = f"""以下是 Polymarket 预测市场事件标题列表（JSON 数组）。
请将每条标题翻译并精简成中文，要求：
1. 目标长度 ≤18 个中文字符（含标点）
2. 必须保留核心实体：人名、组织名、数字阈值、日期、选项方向
3. 多选项事件（含"—"分隔子选项）必须保留子选项信息
4. 无法确定语义时返回空字符串""

输入标题数组：
{titles_json}

请以 JSON 对象返回，key 为原始英文标题，value 为对应中文精简版：
{{"原始标题1": "中文版1", "原始标题2": "中文版2", ...}}"""

    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=500,
        )
        mapping: dict = json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[翻译] Polymarket 标题翻译失败，回退英文: {e}")
        mapping = {}

    for it in items:
        zh = mapping.get(it["question_en"], "")
        if zh:
            # 兜底硬上限
            it["question_zh"] = zh[:MAX_QUESTION_LEN_ZH]
        else:
            # 英文回退：软截断
            en = it["question_en"]
            it["question_zh"] = en if len(en) <= MAX_QUESTION_LEN_EN else en[:MAX_QUESTION_LEN_EN] + "…"

    return items


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
            })
        except Exception as e:
            print(f"[yfinance] {asset['name']} 失败: {e}")
            result.append({
                "name":    asset["name"],
                "price":   "N/A",
                "chg_pct": "--",
            })
    return result


def fetch_news() -> dict:
    """
    两层新闻检索：
    - macro：宏观板块宽泛查询，用于"市场舆情"板块
    - per_asset：分资产定向查询，用于"驱动因素"列归因
    当日结果缓存到 .cache/tavily_{TODAY}.json，重跑时直接读取。
    """
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"tavily_{TODAY}.json"

    # 命中缓存则跳过网络请求
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            print("[Tavily] 命中当日缓存，跳过网络请求")
            return data
        except Exception:
            pass  # 缓存损坏则重新拉取

    client = TavilyClient(api_key=TAVILY_API_KEY)

    # Layer A：宏观板块（days=1 限定当日新闻，topic="news" 过滤非新闻源）
    macro_queries = {
        "宏观与地缘": "global macro economy markets geopolitics today",
        "中国市场":   "China A-share Hang Seng stock market today",
        "美股":       "US stock market S&P500 Nasdaq today",
        "加密货币":   "Bitcoin crypto market today",
    }
    macro = {}
    for section, query in macro_queries.items():
        try:
            res = client.search(query=query, search_depth="basic",
                                max_results=4, include_answer=True,
                                days=1, topic="news")
            macro[section] = {
                "answer":   res.get("answer", ""),
                "snippets": [r.get("content", "")[:200] for r in res.get("results", [])],
            }
        except Exception as e:
            print(f"[Tavily] 宏观 {section} 失败: {e}")
            macro[section] = {"answer": "", "snippets": []}

    # Layer B：分资产定向（中性 query，无 "why moved" 诱导词）
    per_asset = {}
    for asset_name, query in ASSET_NEWS_QUERIES.items():
        try:
            res = client.search(query=query, search_depth="basic",
                                max_results=3, include_answer=True,
                                days=1, topic="news")
            per_asset[asset_name] = {
                "answer":   res.get("answer", ""),
                "snippets": [r.get("content", "")[:200] for r in res.get("results", [])],
            }
        except Exception as e:
            print(f"[Tavily] 资产 {asset_name} 失败: {e}")
            per_asset[asset_name] = {"answer": "", "snippets": []}

    result = {"macro": macro, "per_asset": per_asset}

    # 写入当日缓存
    try:
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[Tavily] 缓存写入失败: {e}")

    return result


# ── 报告生成 ─────────────────────────────────────────────────────────────────

def build_data_block(polymarket, assets, news) -> str:
    """拼接结构化数据文本，供 LLM 参考"""
    lines = [f"数据日期：{TODAY}", ""]

    lines.append("== Polymarket 热门市场（已过滤体育/娱乐，仅保留政治/地缘/宏观/金融/加密）==")
    for m in polymarket:
        lines.append(
            f"- {m['question_zh']} | YES: {m['yes']} | 24h变化: {m['chg_24h']} | 24h成交: {m['volume_24h']}"
        )

    lines.append("")
    lines.append("== 大类资产行情 ==")
    for a in assets:
        lines.append(f"- {a['name']}: {a['price']}  {a['chg_pct']}")

    lines.append("")
    lines.append("== 宏观舆情（用于市场叙事和板块分析）==")
    for section, data in news["macro"].items():
        lines.append(f"[{section}]")
        if data["answer"]:
            lines.append(f"  摘要: {data['answer'][:300]}")
        else:
            lines.append('  摘要: （Tavily 无返回，本板块如无其他片段支撑请写"暂无充分信息"）')
        for s in data["snippets"][:2]:
            lines.append(f"  · {s}")

    lines.append("")
    lines.append("== 资产专属新闻（驱动因素必须且只能来自对应资产的新闻，无法提取则填—）==")
    for asset_name, data in news["per_asset"].items():
        lines.append(f"[{asset_name}]")
        if data["answer"]:
            lines.append(f"  摘要: {data['answer'][:200]}")
        else:
            lines.append('  摘要: （Tavily 无返回，该资产驱动因素请填"—"）')
        for s in data["snippets"][:2]:
            lines.append(f"  · {s}")

    return "\n".join(lines)


SYSTEM_PROMPT = """你是一位专业的宏观投资分析师，擅长跨资产分析和市场叙事提炼。
你的任务是基于提供的实时数据，生成一份简洁、有见地的中文投资日报。

证据优先原则：本报告所有分析必须基于"数据块"中的量化数据和新闻片段。无法从数据块直接推导的判断一律标注为"观察中"或"数据不足"，禁止调用未在数据块中出现的背景知识做归因或编造宏观因果故事。

格式要求（严格遵守）：
1. 第一行必须是一句话市场叙事总结（不超过60字，点明当前市场在交易什么核心主题；若数据块中信息不足以判断核心主题，写"今日信号分歧，暂无明确主线"）
2. 按指定结构输出，使用 Markdown
3. AI研判部分必须包含具体的潜在风险点（至少2条）
4. 语言简练，避免废话，每个分析要有观点而非仅描述数据
5. AI研判部分必须包含"交易线索"小节（至少2条），每条给出：涉及的资产或板块 + 方向倾向（看多/看空/套利/对冲）+ 触发条件或前提假设；线索要具体可执行，不得出现"关注宏观走势"这类空话
6. 大类资产"驱动因素"列必须来自数据块中"资产专属新闻"区段的对应条目，无法提取明确关键词时填"—"，不得编造，不得借用其他资产的新闻
7. 所有数据表格（Polymarket、大类资产）必须完整列出数据块中提供的每一条，不得自行挑选、省略或合并；分析解读只在表格下方的文字中进行
8. 驱动因素中如存在资产涨跌方向与新闻主线冲突的情形（如金价跌但新闻谈通胀升温），填"背离：[一句解释]"，不得强行归因
9. 跨资产层面若走势方向无一致新闻叙事（例：黄金涨 + 美债收益率同涨、美元跌 + 美股跌），"行情特征"和"市场叙事"必须如实写"信号分歧，暂无充分证据归因"或"需观察后续数据确认"，禁止在无具体新闻支撑时杜撰宏观因果故事；若有新闻支撑则正常输出跨资产联动分析"""

REPORT_PROMPT = """请基于以下数据生成今日投资日报：

{data_block}

---

输出格式：

> 🔍 **{today} 市场叙事**：[一句话，说明当前市场核心交易主题/叙事，不超过60字]

---

## 一、Polymarket 热门押注

| 事件 | YES概率 | 24h变化 | 24h成交量 |
|------|---------|---------|-----------|
（**必须列出数据块中所有 Polymarket 条目，一条不少，顺序保持原样**；24h变化按数据原样填写，无数据填"--"）

**解读**：[2-3句。若存在24h变化>5pp的事件，优先分析其背后叙事；若变化均不显著，则分析当前概率水位反映的中长期押注方向]

---

## 二、大类资产行情

| 资产 | 最新价 | 涨跌幅 | 驱动因素 |
|------|--------|--------|----------|
（**必须列出所有资产条目**；驱动因素只能来自数据块"资产专属新闻"中该资产对应的片段，需含具体关键词，无支撑填"—"，涨跌与新闻主线冲突填"背离：[解释]"，每条≤20字）

**行情特征**：[跨资产联动分析，2-3句。若走势一致且新闻支持，说明核心联动逻辑；若跨资产方向分歧且数据块中无统一叙事，写"今日信号分歧：XX与YY背离，当前新闻不足以判断因果，建议观察ZZ数据"，禁止强行编剧本]

---

## 三、市场舆情

（按宏观地缘、中国市场、美股、大宗商品、加密分板块简述，每板块1-2句）

---

## 四、AI 综合研判

**核心观点**：[2-3句核心判断]

**潜在风险**：
- 风险1：[具体描述]
- 风险2：[具体描述]
- 风险3（可选）：[具体描述]

**交易线索**：
- 线索1：[资产/板块 + 方向倾向 + 触发条件，例如"若美债10Y突破4.5%，黄金短期承压，可关注回调做多机会"]
- 线索2：[资产/板块 + 方向倾向 + 触发条件]
- 线索3（可选）：[资产/板块 + 方向倾向 + 触发条件]

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
        temperature=0.5,
        max_tokens=2500,
    )
    return resp.choices[0].message.content.strip()


# ── 飞书推送 ─────────────────────────────────────────────────────────────────

_HEADING_EMOJI = {
    "一、Polymarket": "🎯",
    "二、大类资产":   "📊",
    "三、市场舆情":   "📰",
    "四、AI 综合":    "🧠",
}

_POLY_HEADER  = "**📋 事件 · YES概率 · 24h变化 · 24h成交量**"
_ASSET_HEADER = "**📋 资产 · 最新价 · 涨跌幅 · 驱动因素**"


def format_for_feishu(md: str) -> str:
    """将 Markdown 报告转换为飞书友好格式。

    飞书 interactive card 的 markdown 元素不支持 GFM 表格，
    此函数把表格块替换为 emoji 装饰的 bullet 列表，其余内容原样透传。
    本地保存的 .md 文件不经过此函数，仍保留原始表格格式。
    """

    def _chg_icon(chg: str) -> str:
        s = chg.strip()
        if s.startswith("+"):
            return "📈"
        if s.startswith("-") and not s.startswith("--"):
            return "📉"
        return "➖"

    def _fmt_polymarket(cells: list[str]) -> str:
        event, yes, chg, vol = cells[0], cells[1], cells[2], cells[3]
        return f"- 🎯 **{event}**\n  YES `{yes}` · {_chg_icon(chg)} `{chg}` · 💰 `{vol}`"

    def _fmt_asset(cells: list[str]) -> str:
        name, price, chg, driver = cells[0], cells[1], cells[2], cells[3]
        if "▲" in chg:
            direction = "📈"
            chg_clean = "+" + chg.replace("▲", "").strip()
        elif "▼" in chg:
            direction = "📉"
            chg_clean = "-" + chg.replace("▼", "").strip()
        else:
            direction = "➖"
            chg_clean = chg.strip()
        return f"- {direction} **{name}** `{price}` · {chg_clean} · {driver}"

    lines = md.split("\n")
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # 章节主标题加 emoji 前缀
        if line.startswith("## "):
            for key, emoji in _HEADING_EMOJI.items():
                if key in line:
                    line = "## " + emoji + " " + line[3:]
                    break
            out.append(line)
            i += 1
            continue

        # 识别表格块：当前行以 | 开头，下一行是 |---| 分隔行
        if (line.strip().startswith("|")
                and i + 1 < len(lines)
                and re.match(r"^\s*\|[\s\-|:]+\|", lines[i + 1])):
            header_cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if header_cells and "事件" in header_cells[0]:
                table_type = "polymarket"
            elif header_cells and "资产" in header_cells[0]:
                table_type = "asset"
            else:
                table_type = None

            i += 2  # 跳过 GFM header 行和 |---| 分隔行

            if table_type == "polymarket":
                out.append(_POLY_HEADER)
                out.append("")
            elif table_type == "asset":
                out.append(_ASSET_HEADER)
                out.append("")

            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if table_type == "polymarket" and len(cells) >= 4:
                    out.append(_fmt_polymarket(cells))
                elif table_type == "asset" and len(cells) >= 4:
                    out.append(_fmt_asset(cells))
                else:
                    out.append(lines[i])  # 未知表格原样保留
                i += 1
            continue

        out.append(line)
        i += 1

    return "\n".join(out)


def send_to_feishu(report_md: str) -> None:
    """通过飞书自定义机器人 Webhook 推送 Markdown 卡片"""
    feishu_md = format_for_feishu(report_md)
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
                    "content": feishu_md,
                }
            ],
        },
    }
    resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if result.get("code", 0) != 0:
        raise RuntimeError(f"飞书推送失败: {result}")
    print("[飞书] 推送成功")


# ── 主流程 ───────────────────────────────────────────────────────────────────

def main():
    local_mode = not os.environ.get("FEISHU_WEBHOOK_URL")
    print(f"[{TODAY}] 开始生成投资日报...{'（本地模式，跳过飞书推送）' if local_mode else ''}")
    CACHE_DIR.mkdir(exist_ok=True)

    print("  1/5 拉取 Polymarket 数据...")
    polymarket = fetch_polymarket()

    print("  2/5 翻译 Polymarket 标题为中文...")
    polymarket = translate_polymarket_titles(polymarket)

    print("  3/5 拉取大类资产行情...")
    assets = fetch_assets()

    print("  4/5 搜索新闻舆情（宏观 + 分资产定向）...")
    news = fetch_news()

    print("  5/5 生成报告...")
    data_block = build_data_block(polymarket, assets, news)
    report     = generate_report(data_block)

    output_path = f"report_{TODAY}.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"报告已保存至 {output_path}")

    print("\n" + "="*60)
    print(report)
    print("="*60 + "\n")

    if local_mode:
        print("本地模式：跳过飞书推送。")
    else:
        send_to_feishu(report)
        print("完成。")


if __name__ == "__main__":
    main()
