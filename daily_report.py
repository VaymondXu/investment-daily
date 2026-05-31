#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
投资日报生成器
- 数据源：Polymarket Events API、yfinance、Gemini (Google Search Grounding)
- 报告生成：DeepSeek API
- 推送：飞书自定义机器人 Webhook
"""

import os
import re
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
load_dotenv()
import requests
import yfinance as yf
from datetime import datetime, timedelta, timezone
from pathlib import Path
from openai import OpenAI
from google import genai
from google.genai import types as genai_types
from zoneinfo import ZoneInfo

# ── 配置 ────────────────────────────────────────────────────────────────────

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
GEMINI_API_KEY   = os.environ["GEMINI_API_KEY"]
FEISHU_WEBHOOK   = os.environ.get("FEISHU_WEBHOOK_URL", "")
OPENAI_API_KEY   = os.environ.get("OPENAI_API_KEY", "")
REVIEW_MODEL     = os.environ.get("REVIEW_MODEL", "gpt-4o-mini")

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
# 黑名单：明确与投资无关的类别，硬过滤
BLACKLIST_TAGS = {
    "Sports", "NBA", "NFL", "MLB", "Soccer", "Golf", "Basketball", "Games",
    "Culture", "Music", "Awards", "Tweet Markets",
}
# 投资相关性筛选由 LLM 完成（filter_and_translate_polymarket），不再使用白名单

# 英文回退截断长度（LLM 翻译失败时截断 fallback 标题用）
MAX_QUESTION_LEN_EN = 24

# 分资产新闻检索资产名列表（仅 key 有效，用于 per_asset 结构定义；
# 搜索指令已内嵌在 fetch_news() 的 Gemini prompt 中，value 仅作说明）
ASSET_NEWS_QUERIES = {
    "标普500":    "What are today's key drivers and news for the S&P 500 index?",
    "纳斯达克":   "What drove Nasdaq 100 and Nasdaq Composite performance today?",
    "上证指数":   "What are today's major news and drivers for China's Shanghai Composite A-share market?",
    "恒生指数":   "What drove the Hang Seng Index performance today?",
    "黄金":       "What are today's key drivers for gold prices?",
    "原油(WTI)":  "What are today's key drivers for WTI crude oil prices?",
    "铜":         "What are today's key news and drivers for copper commodity prices?",
    "铝":         "What are today's key news and drivers for aluminum commodity prices?",
    "美债10Y":    "What drove US 10-year Treasury yield movements today?",
    "美元指数":   "What drove the DXY US dollar index today?",
    "BTC":        "What are today's major Bitcoin price drivers and crypto market news?",
}


# ── 数据采集 ─────────────────────────────────────────────────────────────────

def fetch_polymarket(top_n: int = 20) -> tuple:
    """拉取 Polymarket 热门事件候选池（按 24h 成交量排序，黑名单硬过滤体育/娱乐）"""
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
    base_params = {"active": "true", "closed": "false", "limit": 30, "ascending": "false"}

    # 双维度拉取：24h 热门 + 累计热门，合并后给 LLM 更丰富的候选池
    raw_events: list[dict] = []
    seen_event_ids: set = set()
    for order in ("volume24hr", "volume"):
        try:
            resp = requests.get(url, params={**base_params, "order": order}, timeout=15)
            resp.raise_for_status()
            for event in resp.json():
                eid = event.get("id")
                if eid and eid not in seen_event_ids:
                    seen_event_ids.add(eid)
                    raw_events.append(event)
        except Exception as exc:
            print(f"[Polymarket] {order} 请求失败: {exc}")

    if not raw_events:
        return ([], [])

    result = []
    consensus_candidates: list = []
    today_snapshot: dict[str, float] = {}

    now_utc = datetime.now(timezone.utc)

    def _is_expired(iso_str) -> bool:
        if not iso_str:
            return False  # 缺失日期不过滤，避免误伤
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt < now_utc
        except Exception:
            return False

    for event in raw_events:
        markets = event.get("markets") or []
        # 收集所有子市场的 YES 价格到 today_snapshot
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

        # 黑名单硬过滤（体育/娱乐/游戏等）
        tag_labels = {t.get("label", "") for t in (event.get("tags") or [])}
        if tag_labels & BLACKLIST_TAGS:
            continue

        if not markets:
            continue

        # 过滤已过验证时间的事件：优先取 event.endDate，回退 market.endDate
        event_end = event.get("endDate")
        if not event_end:
            market_ends = [m.get("endDate") for m in markets if m.get("endDate")]
            event_end = max(market_ends) if market_ends else None
        if _is_expired(event_end):
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

        vol_24h   = float(market.get("volume24hr") or 0)
        vol_total = sum(float(m.get("volume") or 0) for m in markets)
        item = {
            "question_en": question_en,
            "question_zh": question_en,   # 占位，由 filter_and_translate_polymarket() 填充
            "yes":         yes_pct,
            "chg_24h":     chg_24h,
            "volume_24h":  f"${vol_24h:,.0f}" if vol_24h else "N/A",
            "volume_total": f"${vol_total:,.0f}" if vol_total else "N/A",
        }
        # YES≥99% 或 ≤1% 的极端共识事件单独收集，不进入普通候选池
        if yes_price is not None and (yes_price >= 0.99 or yes_price <= 0.01):
            consensus_candidates.append(item)
        else:
            result.append(item)

    # 写入今日 snapshot，供明日回退使用
    try:
        snap_today = CACHE_DIR / f"polymarket_snapshot_{TODAY}.json"
        snap_today.write_text(json.dumps(today_snapshot, ensure_ascii=False))
    except Exception as e:
        print(f"[Polymarket] snapshot 写入失败: {e}")

    return (result, consensus_candidates)


def interpret_polymarket(items: list[dict]) -> str:
    """基于完整候选池，让 LLM 从全局视角解读 Polymarket 投注信号（2-3句话）。
    筛选步骤独立在后面进行，解读先于筛选以避免局部视角谬误。"""
    if not items:
        return ""

    # 构建候选摘要：只传核心字段，够 LLM 读懂走势即可
    candidates = []
    for it in items:
        candidates.append({
            "title":   it["question_en"],
            "yes":     it.get("yes", "--"),
            "chg_24h": it.get("chg_24h", "--"),
        })
    candidates_json = json.dumps(candidates, ensure_ascii=False)

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    prompt = f"""以下是 Polymarket 预测市场全部候选事件（含 YES 概率和 24h 变化）：

{candidates_json}

你是宏观投资分析师。请基于这份完整候选列表，用 2-3 句话写出整体解读：
- 识别多个事件的概率变化共同指向哪个风险方向（地缘缓和/升级、政策转向等）
- 若不同事件的信号相互矛盾，指出分歧所在
- 点明对大类资产（原油、黄金、美元等）的潜在影响方向

注意：
- 判断方向时必须先还原负向措辞（"结束""停火""缓和"等）的概率变化含义：概率**上升**=风险降低，概率**下降**=风险上升
- 只有当多个事件隐含方向真正相反时，才能称"矛盾"或使用"但"；同向时使用"共同指向""进一步印证"
- 输出纯文本，不加标题、不加 Markdown，直接输出 2-3 句解读"""

    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"[Polymarket解读] LLM调用失败，跳过: {exc}")
        return ""


def filter_and_translate_polymarket(items: list[dict], consensus_items=None, top_n: int = 5) -> tuple:
    """LLM 从候选事件中按双维度各选 top_n 条投资相关事件并翻译为中文，同时筛选极端概率共识信号。
    返回 (list_24h, list_total, list_consensus)：
      list_24h      — 按 24h 成交量维度选出的事件（保序）
      list_total    — 按累计投注额维度选出的事件（保序）
      list_consensus — 极端概率共识事件（最多3条，宏观相关）
    两组可有重叠（同一事件可同时入选），各自最多 top_n 条。
    """
    if consensus_items is None:
        consensus_items = []
    if not items:
        return ([], [], [])

    # 候选列表：编号 + 英文标题 + YES概率 + 24h变化 + 成交量
    candidates = []
    for i, it in enumerate(items):
        candidates.append({
            "id":           i,
            "title":        it["question_en"],
            "yes":          it.get("yes", "--"),
            "chg_24h":      it.get("chg_24h", "--"),
            "volume_24h":   it.get("volume_24h", "N/A"),
            "volume_total": it.get("volume_total", "N/A"),
        })
    candidates_json = json.dumps(candidates, ensure_ascii=False)

    # 极端概率共识候选（独立编号 c0/c1/...）
    consensus_candidates_json = ""
    if consensus_items:
        c_candidates = []
        for ci, it in enumerate(consensus_items):
            c_candidates.append({
                "c_id":         ci,
                "title":        it["question_en"],
                "yes":          it.get("yes", "--"),
                "chg_24h":      it.get("chg_24h", "--"),
                "volume_24h":   it.get("volume_24h", "N/A"),
                "volume_total": it.get("volume_total", "N/A"),
            })
        consensus_candidates_json = json.dumps(c_candidates, ensure_ascii=False)

    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

    consensus_section = ""
    if consensus_candidates_json:
        consensus_section = f"""
**第二步：共识信号筛选**

以下是 YES≥99% 或 ≤1% 的极端共识事件（市场已高度定价）：
{consensus_candidates_json}

组C — 从上述极端概率事件中，选最多 3 条与全球宏观/金融市场相关的（例：央行利率决议、重大政治稳定性判断）；
过滤掉：体育/娱乐/小国政治、与金融市场无关的社会事件。

"""

    prompt = f"""以下是 Polymarket 预测市场的普通候选事件列表（JSON 数组，含编号、英文标题、YES概率、24h变化、24h成交量、累计成交量）。

请完成以下任务：

**第一步：双维度筛选（从普通候选池）**

从候选中分别选出两组事件，每组最多 {top_n} 条：

组A — 按 volume_24h 从大到小，选最多 {top_n} 条与全球投资/宏观/金融市场相关的事件
组B — 按 volume_total 从大到小，选最多 {top_n} 条与全球投资/宏观/金融市场相关的事件

两组可以有重叠。投资相关性筛选标准（两组均适用）：
- 保留：美联储/央行政策、地缘冲突（影响油价/供应链）、大国选举（美国/欧洲主要国家）、加密货币、大宗商品价格、重大贸易政策
- 过滤掉：小国/边缘国选举（如匈牙利、秘鲁等）、纯政治人事任命、与金融市场无直接关联的事件
- 过滤掉：兑现时间过远（如2028年大选）且对近期市场走势无直接影响的事件
- 边界情况：可能间接影响市场的事件（如欧盟政策变动）可以保留
- 同一主题（如比特币价格、美国大选）同组内最多选 1 条最具代表性的
{consensus_section}
**最后一步：翻译 + 选中理由标签**

对所有选中事件（组A、组B、组C去重后），完成：
1. 将英文标题翻译并精简为中文：
   - 必须完整表达事件含义，同时尽量精简（去掉"是否会""将达到多少"等冗余措辞）
   - 必须保留核心实体：人名、组织名、数字阈值、日期、选项方向
   - 多选项事件（含"—"分隔子选项）必须保留子选项信息
2. 给出一个 4-8 字的中文标签（tag），说明该事件被选中的市场关注理由，例如：
   - "地缘风险升温"、"利率重定价"、"加密监管松绑"、"选举格局生变"、"短期高波动"、"利率共识锚定"、"政治稳定性定价"

普通候选事件：
{candidates_json}

请以 JSON 对象返回，格式：
{{
  "by_volume_24h":   [{{"id": 0, "zh": "中文标题", "tag": "地缘风险升温"}}, ...],
  "by_volume_total": [{{"id": 3, "zh": "中文标题", "tag": "利率重定价"}}, ...],
  "consensus":       [{{"c_id": 0, "zh": "中文标题", "tag": "利率共识锚定"}}, ...]
}}

by_volume_24h 按 volume_24h 从大到小排列，by_volume_total 按 volume_total 从大到小排列，每组不超过 {top_n} 条。
consensus 组最多 3 条，按 volume_24h 从大到小排列；若无极端共识候选则返回空数组。"""

    result_raw = None
    last_exc = None
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=1500,
            )
            result_raw = json.loads(resp.choices[0].message.content)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                print(f"[LLM筛选] 第1次失败，重试中: {exc}")
                time.sleep(5)

    def _build_list(selected_list):
        """将 LLM 返回的 [{id, zh, tag}] 转为带中文标题的 item 列表"""
        out = []
        seen_ids = set()
        for s in (selected_list or []):
            idx = s.get("id")
            zh = s.get("zh", "")
            tag = s.get("tag", "")
            if idx is None or idx < 0 or idx >= len(items) or idx in seen_ids:
                continue
            seen_ids.add(idx)
            it = dict(items[idx])  # shallow copy，避免修改原始 items
            it["question_zh"] = zh if zh else (
                it["question_en"][:MAX_QUESTION_LEN_EN] + "…"
                if len(it["question_en"]) > MAX_QUESTION_LEN_EN else it["question_en"]
            )
            it["tag"] = tag
            out.append(it)
        return out

    def _build_consensus_list(selected_list):
        """将 LLM 返回的 [{c_id, zh, tag}] 转为带中文标题的共识 item 列表"""
        out = []
        seen_ids = set()
        for s in (selected_list or []):
            c_idx = s.get("c_id")
            zh = s.get("zh", "")
            tag = s.get("tag", "")
            if c_idx is None or c_idx < 0 or c_idx >= len(consensus_items) or c_idx in seen_ids:
                continue
            seen_ids.add(c_idx)
            it = dict(consensus_items[c_idx])
            it["question_zh"] = zh if zh else (
                it["question_en"][:MAX_QUESTION_LEN_EN] + "…"
                if len(it["question_en"]) > MAX_QUESTION_LEN_EN else it["question_en"]
            )
            it["tag"] = tag
            out.append(it)
        return out

    if last_exc is not None:
        print(f"[LLM筛选] 筛选+翻译失败，回退: {last_exc}")
        fallback = []
        for it in items[:top_n]:
            it = dict(it)
            en = it["question_en"]
            it["question_zh"] = en if len(en) <= MAX_QUESTION_LEN_EN else en[:MAX_QUESTION_LEN_EN] + "…"
            it["tag"] = ""
            fallback.append(it)
        # 两组都填 fallback，避免长期关注整节消失
        return (fallback, list(fallback), [])

    list_24h      = _build_list(result_raw.get("by_volume_24h", []))
    list_total    = _build_list(result_raw.get("by_volume_total", []))
    list_consensus = _build_consensus_list(result_raw.get("consensus", []))

    if not list_24h and not list_total:
        print("[LLM筛选] 返回为空，回退前几条")
        fallback = []
        for it in items[:top_n]:
            it = dict(it)
            en = it["question_en"]
            it["question_zh"] = en if len(en) <= MAX_QUESTION_LEN_EN else en[:MAX_QUESTION_LEN_EN] + "…"
            it["tag"] = ""
            fallback.append(it)
        return (fallback, list(fallback), [])

    # list_total 为空（LLM未返回或全过滤）时，用 list_24h 兜底
    if not list_total:
        print("[LLM筛选] by_volume_total 为空，复用 24h 列表作为长期关注兜底")
        list_total = list(list_24h)

    return (list_24h, list_total, list_consensus)


def fetch_assets() -> tuple:
    """拉取大类资产行情（yfinance，并发拉取加速）。
    返回 (资产列表, 最新数据日期字符串)，日期用于判断数据是否为当日实时数据。
    """
    def _fetch_one(asset):
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
            bar_date   = hist.index[-1]
            try:
                bar_date_bj = bar_date.tz_convert(TZ_BEIJING)
            except Exception:
                bar_date_bj = bar_date
            date_str = bar_date_bj.strftime("%Y-%m-%d")
            # 期货合约换月会产生虚假大幅变动（价差而非真实涨跌）；
            # 非加密资产日内变动超过 9% 且成交量极低时标注存疑
            last_vol = hist["Volume"].iloc[-1] if "Volume" in hist.columns else 1
            is_futures = asset["ticker"].endswith("=F")
            is_crypto  = asset["name"] == "BTC"
            if is_futures and not is_crypto and abs(chg_pct) > 9 and last_vol < 500:
                chg_display = f"⚠️ {arrow} {abs(chg_pct):.2f}% (存疑)"
            else:
                chg_display = f"{arrow} {abs(chg_pct):.2f}%"
            return asset["name"], {
                "name":    asset["name"],
                "price":   f"{last_close:.2f}",
                "chg_pct": chg_display,
            }, date_str
        except Exception as exc:
            print(f"[yfinance] {asset['name']} 失败: {exc}")
            return asset["name"], {"name": asset["name"], "price": "N/A", "chg_pct": "--"}, None

    # 并发拉取，保留原始 ASSETS 顺序
    results_map = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        future_to_name = {pool.submit(_fetch_one, a): a["name"] for a in ASSETS}
        for future in as_completed(future_to_name):
            name, data, date_str = future.result()
            results_map[name] = (data, date_str)

    result = []
    latest_data_date = None
    for asset in ASSETS:
        data, date_str = results_map[asset["name"]]
        result.append(data)
        # 用第一个成功资产（ASSETS 顺序）的日期作为市场日期基准
        if latest_data_date is None and date_str:
            latest_data_date = date_str

    return result, latest_data_date


def fetch_news() -> dict:
    """
    两层新闻检索（Gemini 2.5 Flash + Google Search Grounding）：
    - macro：宏观板块宽泛查询，用于"市场舆情"板块
    - per_asset：分资产定向查询，用于"驱动因素"列归因
    当日结果缓存到 .cache/news_{TODAY}.json，重跑时直接读取。

    3 次 Gemini 调用：1 次 macro + 2 次 per_asset（股指类 / 商品债汇币类），
    在不触发免费层 5 RPM 限制的前提下提升每类资产的搜索聚焦度。
    每次调用附带该类资产的搜索引导指令，Gemini 自动触发 Google Search 并返回 JSON。
    """
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"news_{TODAY}.json"

    # 命中缓存则跳过网络请求
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            print("[Gemini] 命中当日缓存，跳过网络请求")
            return data
        except Exception:
            pass  # 缓存损坏则重新拉取

    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    search_tool = genai_types.Tool(google_search=genai_types.GoogleSearch())

    MACRO_SECTIONS = ["宏观与地缘", "中国市场", "美股", "加密货币"]
    ASSET_NAMES = list(ASSET_NEWS_QUERIES.keys())

    def _gemini_call(label, prompt):
        """单次 Gemini 调用，遇到 429/503/网络错误 自动重试（最多 3 次）。"""
        for attempt in range(3):
            try:
                resp = gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(tools=[search_tool]),
                )
                return resp.text or ""
            except Exception as exc:
                err_str = str(exc)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    m = re.search(r"retryDelay.*?(\d+)s", err_str)
                    wait = int(m.group(1)) + 3 if m else 35
                    print(f"[Gemini] {label} 限流，{wait}s 后重试 ({attempt+1}/3)")
                    time.sleep(wait)
                elif "503" in err_str or "UNAVAILABLE" in err_str:
                    wait = 20 * (attempt + 1)
                    print(f"[Gemini] {label} 服务不可用，{wait}s 后重试 ({attempt+1}/3)")
                    time.sleep(wait)
                elif "EOF" in err_str or "SSL" in err_str or "ConnectionError" in err_str:
                    wait = 5 * (attempt + 1)
                    print(f"[Gemini] {label} 网络错误，{wait}s 后重试 ({attempt+1}/3)")
                    time.sleep(wait)
                else:
                    print(f"[Gemini] {label} 失败: {exc}")
                    return ""
        print(f"[Gemini] {label} 重试耗尽，跳过")
        return ""

    def _parse_json_response(text, keys):
        """从 Gemini 回复中提取 JSON 对象，key 匹配 keys 列表。"""
        # 尝试提取 ```json ... ``` 代码块
        m = re.search(r"```json\s*([\s\S]*?)```", text)
        json_str = m.group(1).strip() if m else text.strip()
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        # JSON 解析失败：用正则按 key 逐个提取段落
        result = {}
        for i, key in enumerate(keys):
            # 查找 key 对应的文本段（到下一个 key 或文末）
            pattern = re.escape(key) + r"[:\s\-]*(.+?)(?=" + (
                "|".join(re.escape(k) for k in keys[i+1:]) if i < len(keys)-1 else r"\Z"
            ) + ")"
            match = re.search(pattern, text, re.DOTALL)
            result[key] = match.group(1).strip()[:300] if match else ""
        return result

    # ── Call 1: 宏观舆情（4 个板块合并为 1 次搜索） ──
    macro_prompt = (
        "Search for today's financial market news and provide a brief summary (2-3 sentences each) "
        "for each of the following 4 sections. Return ONLY a JSON object with these exact keys, "
        "no markdown formatting outside the JSON:\n\n"
        "{\n"
        '  "宏观与地缘": "summary of today\'s global macro economy and geopolitics news...",\n'
        '  "中国市场": "summary of China macro economy, PBOC policy, A-shares, capital flows...",\n'
        '  "美股": "summary of US stock market, S&P 500, Nasdaq drivers today...",\n'
        '  "加密货币": "summary of Bitcoin and crypto market developments today..."\n'
        "}"
    )
    print("[Gemini] 批量检索宏观舆情（4 个板块）...")
    macro_raw = _gemini_call("宏观舆情", macro_prompt)
    macro_parsed = _parse_json_response(macro_raw, MACRO_SECTIONS)
    macro = {}
    for section in MACRO_SECTIONS:
        answer = macro_parsed.get(section, "")
        if isinstance(answer, str):
            macro[section] = {"answer": answer, "snippets": []}
        else:
            macro[section] = {"answer": str(answer), "snippets": []}

    # 每次调用间隔 15s，确保不触发 5 RPM
    time.sleep(15)

    # ── Call 2: 股票指数类（4 个资产） ──
    equity_names = ["标普500", "纳斯达克", "上证指数", "恒生指数"]
    equity_prompt = (
        "Search for today's financial news about these stock market indices and provide a 2-3 sentence "
        "summary of key price drivers for each. Return ONLY a JSON object with these exact keys, "
        "no markdown formatting outside the JSON:\n\n"
        "{\n"
        '  "标普500": "S&P 500 index: key drivers and news today...",\n'
        '  "纳斯达克": "Nasdaq 100 / Nasdaq Composite index: key drivers today (focus on the index itself, avoid crypto or individual stock news)...",\n'
        '  "上证指数": "Shanghai Composite / A-share market: key drivers today (focus on PBOC policy, foreign capital flows, macro data, core indices — not individual stocks)...",\n'
        '  "恒生指数": "Hang Seng Index: key drivers today (focus on index performance and macro factors, not individual stock IPOs or listings)..."\n'
        "}"
    )
    print("[Gemini] 批量检索股指新闻（标普500、纳斯达克、上证指数、恒生指数）...")
    equity_raw = _gemini_call("股指新闻", equity_prompt)
    equity_parsed = _parse_json_response(equity_raw, equity_names)

    time.sleep(15)

    # ── Call 3: 商品/债券/汇率/加密类（7 个资产） ──
    commodity_names = ["黄金", "原油(WTI)", "铜", "铝", "美债10Y", "美元指数", "BTC"]
    commodity_prompt = (
        "Search for today's financial news about these assets and provide a 1-2 sentence summary "
        "of the key price drivers for each. Return ONLY a JSON object with these exact keys, "
        "no markdown formatting outside the JSON:\n\n"
        "{\n"
        '  "黄金": "Gold: key price drivers today...",\n'
        '  "原油(WTI)": "WTI crude oil: key price drivers today...",\n'
        '  "铜": "Copper commodity: key price drivers today...",\n'
        '  "铝": "Aluminum commodity: key price drivers today...",\n'
        '  "美债10Y": "US 10-year Treasury yield: key drivers today...",\n'
        '  "美元指数": "DXY US dollar index: key drivers today...",\n'
        '  "BTC": "Bitcoin / crypto market: key price drivers today..."\n'
        "}"
    )
    print("[Gemini] 批量检索商品/债券/汇率/加密新闻（黄金、原油、铜、铝、美债10Y、美元指数、BTC）...")
    commodity_raw = _gemini_call("商品债汇币新闻", commodity_prompt)
    commodity_parsed = _parse_json_response(commodity_raw, commodity_names)

    # 合并两组结果到 per_asset
    per_asset = {}
    for name in ASSET_NAMES:
        parsed = equity_parsed if name in equity_names else commodity_parsed
        answer = parsed.get(name, "")
        per_asset[name] = {"answer": answer if isinstance(answer, str) else str(answer), "snippets": []}

    result = {"macro": macro, "per_asset": per_asset}

    # 写入当日缓存
    try:
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"[Gemini] 缓存写入失败: {e}")

    return result


def fetch_economic_calendar() -> str:
    """通过 Gemini + Google Search Grounding 获取未来5天的高影响力经济日历。
    返回纯文本摘要，直接注入 data_block；失败时返回空字符串（优雅降级）。
    结果缓存到 .cache/econ_cal_{TODAY}.txt，重跑时复用。
    """
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"econ_cal_{TODAY}.txt"

    if cache_file.exists():
        try:
            cached = cache_file.read_text(encoding="utf-8").strip()
            if cached:
                print("[Gemini] 经济日历命中当日缓存")
                return cached
        except Exception:
            pass

    to_date = (datetime.now(TZ_BEIJING) + timedelta(days=5)).strftime("%Y-%m-%d")
    prompt = (
        f"Today is {TODAY}. Search for the major HIGH-IMPACT economic data releases and central bank events "
        f"scheduled from {TODAY} to {to_date} (next 5 days). "
        "Focus only on: US (CPI, NFP, PCE, GDP, FOMC/Fed decisions, ISM PMI, Retail Sales, PPI), "
        "China (NBS PMI, CPI, GDP, trade data, PBOC decisions), "
        "Eurozone (ECB decisions, CPI, GDP), Japan (BoJ decisions, CPI, GDP), UK (BoE decisions, CPI, GDP). "
        "List each event on one line in this format:\n"
        "YYYY-MM-DD HH:MM [COUNTRY] Event Name | Previous: X | Forecast: Y\n"
        "If no forecast is available, write Forecast: --\n"
        "Return ONLY the list, no extra text. If no high-impact events are found in this period, return: none"
    )

    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        search_tool = genai_types.Tool(google_search=genai_types.GoogleSearch())
        print(f"[Gemini] 搜索经济日历（{TODAY} ~ {to_date}）...")
        for attempt in range(3):
            try:
                resp = gemini_client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(tools=[search_tool]),
                )
                result = (resp.text or "").strip()
                break
            except Exception as exc:
                err_str = str(exc)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    m = re.search(r"retryDelay.*?(\d+)s", err_str)
                    wait = int(m.group(1)) + 3 if m else 35
                    print(f"[Gemini] 经济日历限流，{wait}s 后重试 ({attempt+1}/3)")
                    time.sleep(wait)
                else:
                    print(f"[Gemini] 经济日历失败: {exc}")
                    return ""
        else:
            return ""

        if not result or result.lower() == "none":
            result = ""

        # 合并近7天内旧缓存中仍未到期的事件（Gemini 检索不稳定时补偿）
        merged_lines = []
        seen = set()
        for line in result.splitlines():
            line = line.strip()
            if line and re.match(r"\d{4}-\d{2}-\d{2}", line):
                key = line[:10] + line[line.find("]"):line.find("|")] if "]" in line else line[:30]
                if key not in seen:
                    seen.add(key)
                    merged_lines.append(line)

        for days_back in range(1, 8):
            old_cache = CACHE_DIR / f"econ_cal_{(datetime.now(TZ_BEIJING) - timedelta(days=days_back)).strftime('%Y-%m-%d')}.txt"
            if not old_cache.exists():
                continue
            try:
                for line in old_cache.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or not re.match(r"\d{4}-\d{2}-\d{2}", line):
                        continue
                    if line[:10] <= TODAY:  # 已过期，跳过
                        continue
                    key = line[:10] + line[line.find("]"):line.find("|")] if "]" in line else line[:30]
                    if key not in seen:
                        seen.add(key)
                        merged_lines.append(line)
            except Exception:
                pass

        merged_lines.sort()  # 按日期升序
        result = "\n".join(merged_lines)

        if not result:
            print("[Gemini] 经济日历：未来5天无高影响力事件")
            return ""

        try:
            cache_file.write_text(result, encoding="utf-8")
        except Exception:
            pass

        print(f"[Gemini] 经济日历获取成功（{len(merged_lines)} 条事件）")
        return result

    except Exception as exc:
        print(f"[Gemini] 经济日历异常: {exc}")
        return ""


# ── 报告生成 ─────────────────────────────────────────────────────────────────

def build_data_block(polymarket_24h, polymarket_total, assets, news,
                     is_market_closed: bool = False,
                     market_data_date=None,
                     polymarket_interpretation: str = "",
                     polymarket_consensus=None,
                     economic_calendar=None,
                     is_morning: bool = False) -> str:
    """拼接结构化数据文本，供 LLM 参考"""
    lines = [f"报告生成日期：{TODAY}", ""]
    if is_market_closed:
        data_date_desc = market_data_date or "上一交易日"
        if is_morning:
            lines += [
                f"📋 【早盘前快报】以下大类资产价格（BTC 除外）均为 {data_date_desc} 收盘行情，今日 A 股及港股尚未开盘。",
                "请重点关注昨夜美股、欧洲盘走势及隔夜宏观事件对今日 A 股/港股开盘的潜在影响方向。",
                "注意：BTC-USD 为 7×24 小时交易资产，其价格始终为当前实时价格，不受传统市场影响。",
                "",
            ]
        else:
            lines += [
                f"⚠️ 【休市提示】当前市场处于休市状态（可能为周末、节假日或开盘前）。",
                f"以下大类资产价格（BTC 除外）均为 {data_date_desc} 收盘价，并非今日实时行情。",
                "注意：BTC-USD 为 7×24 小时交易资产，其价格始终为当前实时价格，不受传统市场休市影响。",
                "分析时请着重关注近期趋势与休市期间发酵的宏观事件，禁止使用【日内波动】【今日大涨/大跌】等词汇（BTC 除外，BTC 可描述当前实时走势）。",
                "",
            ]

    if polymarket_consensus is None:
        polymarket_consensus = []
    total_shown = len({id(m) for m in polymarket_24h + polymarket_total})
    lines.append(f"== Polymarket 热门市场（已过滤体育/娱乐，仅保留政治/地缘/宏观/金融/加密）==")
    lines.append(f"[展示条目：24h活跃 {len(polymarket_24h)} 条 + 长期关注 {len(polymarket_total)} 条 + 共识信号 {len(polymarket_consensus)} 条，共 {total_shown} 个唯一主要事件]")
    if polymarket_interpretation:
        lines.append(f"[候选池整体解读（基于全部候选事件的全局视角）] {polymarket_interpretation}")
    lines.append("[24h活跃 — 按过去24小时成交量排序]")
    for m in polymarket_24h:
        tag_str = f" 〔{m['tag']}〕" if m.get("tag") else ""
        lines.append(
            f"- {m['question_zh']}（{m['question_en']}）{tag_str} | YES: {m['yes']} | 24h变化: {m['chg_24h']} | 24h成交: {m['volume_24h']}"
        )
    lines.append("[长期关注 — 按累计投注额排序]")
    for m in polymarket_total:
        tag_str = f" 〔{m['tag']}〕" if m.get("tag") else ""
        lines.append(
            f"- {m['question_zh']}（{m['question_en']}）{tag_str} | YES: {m['yes']} | 24h变化: {m['chg_24h']} | 累计投注: {m['volume_total']}"
        )
    if polymarket_consensus:
        lines.append("[市场共识 — 极端概率定价信号（YES≥99% 或 ≤1%，市场已高度定价）]")
        for m in polymarket_consensus:
            tag_str = f" 〔{m['tag']}〕" if m.get("tag") else ""
            lines.append(
                f"- {m['question_zh']}（{m['question_en']}）{tag_str} | YES: {m['yes']} | 24h变化: {m['chg_24h']}"
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
            lines.append('  摘要: （Gemini 无返回，本板块如无其他片段支撑请写"暂无充分信息"）')
        for s in data["snippets"][:2]:
            lines.append(f"  · {s}")

    lines.append("")
    lines.append("== 资产专属新闻（驱动因素必须且只能来自对应资产的新闻，无法提取则填—）==")
    for asset_name, data in news["per_asset"].items():
        lines.append(f"[{asset_name}]")
        if data["answer"]:
            lines.append(f"  摘要: {data['answer'][:200]}")
        else:
            lines.append('  摘要: （Gemini 无返回，该资产驱动因素请填"—"）')
        for s in data["snippets"][:2]:
            lines.append(f"  · {s}")

    if economic_calendar:
        lines.append("")
        lines.append("== 经济日历（未来5日高影响力事件，来源：Gemini Search）==")
        lines.append('（以下为即将发布的重要经济数据，可作为"关注事项"依据）')
        for line in economic_calendar.strip().splitlines():
            line = line.strip()
            # 只保留以日期格式开头的行（YYYY-MM-DD），过滤 Gemini 的介绍性文字
            if line and re.match(r"\d{4}-\d{2}-\d{2}", line):
                lines.append(f"- {line}")

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
6. 大类资产"驱动因素"列提取规则：**以"资产专属新闻"的摘要（answer）字段为第一判据**——只要摘要字段有实质内容，即视为有效来源并直接提取关键词（≤20字），不需要也不应被 snippets 中无关内容影响；仅当摘要字段标注"Gemini无返回"或确实为空时，才升级为**从宏观舆情板块推断**，并加"〔宏观〕"标注；不得借用其他资产的专属新闻；实在无任何信息支撑则填"—"，不得编造；⚠️ 个股/单一公司的财报、IPO、并购、评级等微观事件不构成指数（标普500/纳斯达克/上证/恒生）的有效驱动因素，遇到此类内容应忽略并填"—"
7. Polymarket 分"24h活跃"和"长期关注"两个表格，必须分别完整列出数据块对应分组中的每一条，不得省略；若数据块含"[市场共识]"小节，则在"长期关注"表格之后输出"**市场共识**"行（一行紧凑格式，见输出格式模板）；大类资产表格同样必须完整列出所有条目；分析解读只在表格下方的文字中进行
8. 驱动因素中如存在资产涨跌方向与新闻主线冲突的情形（如金价跌但新闻谈通胀升温），填"背离：[一句解释]"，不得强行归因；[一句解释]必须尝试给出实际驱动原因（如"美元走强压制""资金获利了结"），不得仅重复矛盾现象本身（如"和谈破裂后走低但收盘上涨"只是描述矛盾，不是解释）；数据块中确实无法推断驱动时，填"背离：原因不明"
9. 跨资产层面若走势方向无一致新闻叙事（例：黄金涨 + 美债收益率同涨、美元跌 + 美股跌），"行情特征"和"市场叙事"必须如实写"信号分歧，暂无充分证据归因"或"需观察后续数据确认"，禁止在无具体新闻支撑时杜撰宏观因果故事；若有新闻支撑则正常输出跨资产联动分析
10. ⚠️ Polymarket 逻辑推演规范：分析概率变化时必须严格遵守形式逻辑，不得因否定词产生逻辑绕圈。规则：若带有"结束/停火/退出/和解/降级"等缓和字眼的事件概率**下降**，意味着冲突/危机继续风险**上升**，与避险资产（原油、黄金、美元）上涨属于同向逻辑，切勿误判为背离；反之，缓和词事件概率**上升**意味着风险下降，此时避险资产若仍在涨才是真正的背离。跨事件一致性检查：当多个事件的概率变化隐含相同方向的风险信号时（例如"和平协议"概率上升=风险降低，同时"极端油价"概率下降=风险降低），必须识别为同向信号，使用"进一步印证""共同指向"等表述，不得使用"但""然而""矛盾"等转折词；只有当一个事件暗示风险上升而另一个暗示风险下降时，才可称"矛盾信号"。判断步骤：①对每个事件推导其概率变化方向隐含的风险含义；②比较各事件风险方向；③同向用"共同指向"，真正反向才用"但"。数据块中若已提供"候选池整体解读"，应以此为参考基础加以精炼，而非忽略。
11. ⚠️ 中国市场舆情过滤规范：在提取中国市场舆情时，只保留涉及央行政策、外资流向（北向/南向）、宏观数据（PMI/CPI/贸易数据）及核心指数（沪深300/恒生）的主题；严禁引入任何单一非权重上市公司的财报或微观动态；若无宏观新闻，填"暂无核心宏观驱动"。"""

REPORT_PROMPT = """请基于以下数据生成【{report_type}】：

{data_block}

---

输出格式：

> 🔍 **{today} 市场叙事**：[一句话，说明当前市场核心交易主题/叙事，不超过60字]

---

## 一、Polymarket 热门押注

### 24h 活跃

| 事件 | YES概率 | 24h变化 | 24h成交量 |
|------|---------|---------|-----------|
（**必须完整列出数据块"[24h活跃]"中的所有条目，一条不少，顺序保持原样**；事件列格式：`中文标题（英文原名）〔tag标签〕`，完整保留；24h变化按数据原样填写，无数据填"--"）

### 长期关注

| 事件 | YES概率 | 24h变化 | 累计投注 |
|------|---------|---------|----------|
（**必须完整列出数据块"[长期关注]"中的所有条目，一条不少，顺序保持原样**；事件列格式同上）

（**仅当数据块含"[市场共识]"小节时**，在此处输出以下行，否则整行省略：）
**市场共识**：（逐条格式：`中文标题 YES {{概率}} {{24h变化}} 〔tag〕`，各条用"·"连接为一行；只用中文标题，不含英文）

**解读**：[2-3句。数据块中若有"候选池整体解读"，以其为基础精炼输出，不必另起炉灶；**重定价优先**——绝对概率高但24h变化幅度也大的事件，比低概率小变化事件更值得深入分析；优先分析24h变化>5pp的事件，深挖其背后叙事；多个事件同向时用"共同指向"，真正反向才用"但/矛盾"]

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

**关注事项**：[今明两天需要关注的关键事件或风险点；可基于数据块中的新闻和行情推导；禁止凭背景知识补充"惯例上本周将公布XX数据"等定期数据发布（如CPI/非农/PMI），除非数据块中有明确提及；无关注点则填"—"]

---

## 五、经济日历

（**仅当数据块含"经济日历"区块时**输出本节，否则整节省略）
（逐条列出，每条格式：`日期时间 [国家] 事件中文名（前值: X，预期: Y），[结合当前市场环境推导该数据对哪些资产/板块的潜在影响，1句]`；数据块中的英文事件名必须翻译为中文，例如 "CPI"→"消费者物价指数"、"NFP"→"非农就业"、"FOMC"→"美联储议息决议"）
"""


def generate_report(data_block: str, is_market_closed: bool = False, is_morning: bool = False) -> str:
    """调用 DeepSeek API 生成报告（失败自动重试一次）"""
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
    )
    if is_morning and is_market_closed:
        report_type = "早盘前参考 (Pre-Market Brief)"
    elif is_market_closed:
        report_type = "宏观复盘 (Market Review)"
    else:
        report_type = "投资日报 (Daily Update)"
    prompt = REPORT_PROMPT.format(data_block=data_block, today=TODAY, report_type=report_type)
    last_exc = None
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model="deepseek-v4-pro",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": prompt},
                ],
                temperature=0.5,
                max_tokens=3500,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                print(f"[DeepSeek] 第1次失败，重试中: {exc}")
    print(f"[DeepSeek] 报告生成失败: {last_exc}")
    return f"[报告生成失败: {last_exc}]\n\n原始数据块如下，请手动分析：\n\n{data_block}"


# ── 报告审核 ─────────────────────────────────────────────────────────────────

REVIEW_PROMPT = """你是投资报告质量审核员。请对照原始数据块，审查以下投资日报的质量。

审核清单：
1. Polymarket逻辑一致性：
   - 每个事件的概率变化方向是否被正确解读（注意含"结束/停火/缓和"等词的事件：概率上升=风险降低，下降=风险上升）
   - 多个事件的风险信号方向是否被正确识别（同向信号不得使用"但/矛盾/背离"，反向才可以）
2. 数据准确性：表格中的数字与数据块是否一致
3. 驱动因素归因：是否使用了正确资产的专属新闻，有无跨资产借用或凭空编造
4. 格式完整性：数据块中的所有 Polymarket 事件和资产是否均已列出，有无遗漏
5. 逻辑自洽：市场叙事、行情特征、AI研判之间是否存在明显矛盾

== 原始数据块 ==
{data_block}

== 待审查报告 ==
{report}

请以JSON格式返回审核结果（不要加代码块标记，直接返回JSON）：
{{"pass": true或false, "issues": [{{"severity": "high或medium或low", "location": "报告中的位置", "description": "问题描述", "suggestion": "修改建议"}}], "summary": "一句话总结"}}
无问题时返回：{{"pass": true, "issues": [], "summary": "审核通过"}}"""


def review_report(report: str, data_block: str) -> tuple:
    """用 OpenAI 模型对生成报告做质量审核，返回 (report, review_result)。
    OPENAI_API_KEY 未设置时跳过，不阻断主流程。"""
    if not OPENAI_API_KEY:
        print("[Review] OPENAI_API_KEY 未设置，跳过审核")
        return report, None

    from openai import OpenAI as _OpenAI
    client = _OpenAI(api_key=OPENAI_API_KEY)

    try:
        resp = client.chat.completions.create(
            model=REVIEW_MODEL,
            messages=[{"role": "user", "content": REVIEW_PROMPT.format(
                data_block=data_block, report=report
            )}],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=1000,
        )
        review = json.loads(resp.choices[0].message.content)
    except Exception as exc:
        print(f"[Review] 审核调用失败，跳过: {exc}")
        return report, None

    issues = review.get("issues", [])
    if review.get("pass"):
        print(f"[Review] 审核通过：{review.get('summary', '')}")
    else:
        high = [i for i in issues if i.get("severity") == "high"]
        print(f"[Review] 发现 {len(issues)} 个问题（{len(high)} 个高严重度）：{review.get('summary', '')}")
        for iss in issues:
            print(f"  [{iss.get('severity','?')}] {iss.get('location','')}: {iss.get('description','')}")

    return report, review


# ── 飞书推送 ─────────────────────────────────────────────────────────────────

_HEADING_EMOJI = {
    "一、Polymarket": "🎯",
    "二、大类资产":   "📊",
    "三、市场舆情":   "📰",
    "四、AI 综合":    "🧠",
}

_POLY_HEADER  = "**📋 事件 · YES · 24h变化 · 成交量**"
_ASSET_HEADER = "**📋 资产 · 最新价 · 涨跌幅 · 驱动因素**"


def format_for_feishu(md: str) -> str:
    """将 Markdown 报告转换为飞书友好格式。

    飞书 interactive card 的 markdown 元素不支持 GFM 表格，
    此函数把表格块替换为 emoji 装饰的 bullet 列表，其余内容原样透传。
    本地保存的 .md 文件不经过此函数，仍保留原始表格格式。
    """

    def _abbr_volume(vol: str) -> str:
        """$1,071,311 → $107万, $438,463 → $44万, $22,530 → $2.3万"""
        raw = vol.replace("$", "").replace(",", "").strip()
        try:
            v = float(raw)
        except Exception:
            return vol
        if v >= 1_000_000:
            return f"${v/10000:.0f}万"
        elif v >= 10_000:
            return f"${v/10000:.1f}万".replace(".0万", "万")
        else:
            return f"${v:,.0f}"

    def _chg_icon(chg: str) -> str:
        s = chg.strip()
        if s.startswith("+"):
            return "📈"
        if s.startswith("-") and not s.startswith("--"):
            return "📉"
        return "➖"

    def _fmt_polymarket(cells: list[str]) -> str:
        event, yes, chg, vol = cells[0], cells[1], cells[2], cells[3]
        # 飞书端去除英文原名括注（保留中文标题和〔tag〕标签）
        event = re.sub(r'（[^）]*）', '', event).strip()
        vol_short = _abbr_volume(vol)
        return f"🎯 {event}\nYES `{yes}` · {_chg_icon(chg)} `{chg}` · 💰{vol_short}"

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
        return f"{direction} **{name}** `{price}` · {chg_clean} · {driver}"

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
    # 早盘前模式：北京时间 06:00-10:00 运行时启用（此时 A 股/港股尚未开盘）
    beijing_hour = datetime.now(TZ_BEIJING).hour
    is_morning = (6 <= beijing_hour < 10)

    print(f"[{TODAY}] 开始生成投资日报...{'（本地模式，跳过飞书推送）' if local_mode else ''}")
    CACHE_DIR.mkdir(exist_ok=True)

    print("  1/7 拉取 Polymarket 候选事件...")
    polymarket_candidates, consensus_candidates = fetch_polymarket()

    print(f"  2/7 LLM 解读候选池（全局视角，共 {len(polymarket_candidates)} 条 + 极端共识 {len(consensus_candidates)} 条）...")
    polymarket_interpretation = interpret_polymarket(polymarket_candidates + consensus_candidates)

    print("  3/7 LLM 筛选投资相关事件 + 翻译（双维度：24h成交 / 累计投注各 top 5 + 共识信号）...")
    polymarket_24h, polymarket_total, polymarket_consensus = filter_and_translate_polymarket(
        polymarket_candidates, consensus_items=consensus_candidates
    )

    print("  4/7 拉取大类资产行情...")
    assets, latest_data_date = fetch_assets()

    # 若市场最新数据日期不是今天，说明处于休市或开盘前状态
    is_market_closed = (latest_data_date != TODAY) if latest_data_date else False
    if is_market_closed:
        if is_morning:
            print(f"  📋 早盘前模式：价格数据截至 {latest_data_date}，A 股尚未开盘")
        else:
            print(f"  ⚠️  市场休市：最新价格数据来自 {latest_data_date}，非今日实时行情，切换为复盘模式")
    if is_morning and is_market_closed:
        mode_tag = f"早盘前参考模式（数据截至 {latest_data_date}）"
    elif is_market_closed:
        mode_tag = f"复盘模式（数据截至 {latest_data_date}）"
    else:
        mode_tag = "实时日报模式"
    print(f"  运行模式：{mode_tag}")

    print("  5/7 搜索新闻舆情（宏观 + 分资产定向）...")
    news = fetch_news()

    print("  6/7 拉取经济日历（未来5日高影响力事件）...")
    economic_calendar = fetch_economic_calendar()

    print(f"  7/7 生成报告...（Polymarket：24h活跃 {len(polymarket_24h)} 条，长期关注 {len(polymarket_total)} 条，共识 {len(polymarket_consensus)} 条）")
    data_block = build_data_block(polymarket_24h, polymarket_total, assets, news,
                                  is_market_closed=is_market_closed,
                                  market_data_date=latest_data_date,
                                  polymarket_interpretation=polymarket_interpretation,
                                  polymarket_consensus=polymarket_consensus,
                                  economic_calendar=economic_calendar,
                                  is_morning=is_morning)
    report     = generate_report(data_block, is_market_closed=is_market_closed, is_morning=is_morning)

    output_path = f"report_{TODAY}.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"报告已保存至 {output_path}")

    print("\n" + "="*60)
    print(report)
    print("="*60 + "\n")

    print("  [QA] 审核报告质量...")
    report, review_result = review_report(report, data_block)
    if review_result and not review_result.get("pass") and review_result.get("issues"):
        # 将审核结果追加到本地 .md 文件末尾（HTML 注释，不影响渲染）
        issues_summary = "; ".join(
            f"[{i.get('severity','?')}] {i.get('description','')}"
            for i in review_result["issues"]
        )
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"\n<!-- QA Review: {len(review_result['issues'])} issue(s) — {issues_summary} -->")

    if local_mode:
        print("本地模式：跳过飞书推送。")
    else:
        send_to_feishu(report)
        print("完成。")


if __name__ == "__main__":
    main()
