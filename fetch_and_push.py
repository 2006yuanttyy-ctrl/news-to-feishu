# -*- coding: utf-8 -*-
"""
自动抓取「财联社电报（重要）」「华尔街见闻实时快讯」「金十数据重要资讯」，
新内容按类别分组、汇总推送到飞书群机器人。

V6 相比 V5 的变化：
1. 新增"表格卡片"输出模式 —— 用飞书 CardKit v2 的原生 table 组件，
   把所有条目渲染成一张真正的表格（类别 / 标题 / 情绪 / 来源 / 链接），
   比 V5 的分组列表更紧凑直观。
2. 表格组件需要 schema 2.0 + 较新版本飞书客户端（建议 7.20 及以上）。
   飞书表格单元格（data_type=text）不支持 markdown 语法，所以标题本身
   不能做成可点击链接 —— 改成单独一列放原始链接，飞书客户端通常会
   自动识别 URL 文本并变成可点击链接。
3. 如果你们群里客户端版本较老、表格显示不出来，把下面的
   USE_TABLE_CARD 改成 False 即可自动切换回 V5 的分组列表卡片
   （已验证在旧版客户端上也能正常显示，就是你截图里那种样式的升级版）。
"""

import json
import os
import re
import time
import requests
import feedparser
from difflib import SequenceMatcher

# ========== 配置区（一般不用改） ==========

# 是否使用"表格卡片"输出。True = 真表格（需要较新客户端）；
# False = 分组列表卡片（兼容性更好，退回 V5 的样式）
USE_TABLE_CARD = True

# 情绪列是否用飞书"选项标签"组件做成彩色单字（利=红/空=绿）。
# 这个子功能没能拿到 100% 权威的字段格式确认，如果表格里"情绪"这一列
# 显示异常（比如显示成一串奇怪的文字/报错），把这个改成 False，
# 会自动退回成纯文字"利/空/-"（没有颜色，但保证能正常显示）。
SENTIMENT_AS_COLOR_TAG = True

# 表格里"来源"列用的简称，跟抓取用的完整来源标签分开，互不影响
SOURCE_SHORT_NAME = {
    "财联社": "财联社",
    "华尔街见闻": "华尔街",
    "金十数据": "金十",
}

# 表格各列宽度：数字/窄列给固定像素，"标题"列不设宽度、自动占满剩余空间
TABLE_COL_WIDTH = {
    "cat": "90px",
    "sentiment": "50px",
    "source": "70px",
    "link": "160px",
}

# RSSHub 公共镜像列表：第一个抓不到就依次尝试下一个，提高成功率
RSSHUB_MIRRORS = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.pseudoyu.com",
    "https://rsshub.liumingye.cn",
    "https://rsshub.agrreader.com",
]

# 各来源的路径（不含域名，域名从上面镜像列表里轮流拼接）
SOURCES = [
    # (path, source_key, 来源短标签)
    ("/cls/telegraph/red", "cls_red", "财联社"),
    ("/wallstreetcn/live", "wallstreetcn", "华尔街见闻"),
    ("/jin10/important", "jin10", "金十数据"),
]

# 每类消息单次最多考虑几条新的（防止第一次运行或长时间没跑时刷屏）
MAX_PUSH_PER_SOURCE = 8

# 跨来源去重：如果新消息标题跟"最近一段时间内已经推送过的标题"很相似，
# 就认为是同一件事被不同来源重复报道，不再重复推送
DEDUP_SIMILARITY_THRESHOLD = 0.55   # 相似度阈值，0~1，越大越"宽松"
DEDUP_WINDOW_SECONDS = 3 * 60 * 60  # 只跟最近 3 小时内推送过的标题比较

# 一张卡片最多放几条，超过就拆到下一张卡片
MAX_ITEMS_PER_CARD = 20
# 一次运行最多发几张卡片；超出的条目只在卡片末尾提示条数，不再单独发卡片
MAX_CARDS_PER_RUN = 2

# 抓取全部失败时，报警卡片最短间隔多久才能再发一次（避免疯狂重试期间被刷屏）
FETCH_ALERT_COOLDOWN_SECONDS = 60 * 60  # 1 小时

# 已推送记录文件
SEEN_FILE = "seen.json"

# 飞书 Webhook 地址，从 GitHub Secrets 里读取
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ========== 分类关键词（按优先级从高到低，命中第一个匹配的类别就归为该类） ==========
CATEGORY_DEFS = [
    ("war", "🟡 战争地缘", [
        "战争", "军事", "袭击", "空袭", "导弹", "无人机", "轰炸", "核武", "核设施",
        "军演", "冲突", "停火", "火箭弹", "美军", "五角大楼", "伊朗", "以色列",
        "俄罗斯", "俄军", "乌克兰", "乌军", "北约", "胡塞", "哈马斯", "真主党",
        "中东局势", "国防部", "国防军", "参谋长", "革命卫队", "航母", "驱逐舰",
        "战机", "F35", "F-35", "B2", "B-2", "爱国者", "萨德", "红海",
        "霍尔木兹", "波斯湾", "军火", "武器援助", "撤侨", "戒严", "紧急状态",
    ]),
    ("policy", "🟥 政策监管", [
        "国务院", "国常会", "证监会", "央行", "金融监管总局", "制裁",
        "降准", "降息", "货币政策", "财政政策", "专项债", "政治局会议",
    ]),
    ("ai", "🟢 AI算力机器人", [
        "人工智能", "AI", "大模型", "算力", "AIGC", "机器人", "人形机器人",
        "智谱", "DeepSeek", "OpenAI", "英伟达", "GPU", "数据中心", "CPO",
    ]),
    ("semiconductor", "🟣 半导体芯片", [
        "半导体", "芯片", "晶圆", "光刻机", "EDA", "存储芯片", "HBM",
        "先进封装", "台积电", "中芯国际", "集成电路",
    ]),
    ("commodity", "🔵 商品期货贵金属", [
        "黄金", "白银", "现货黄金", "现货白银", "COMEX黄金", "COMEX白银",
        "伦敦金", "伦敦银", "贵金属", "有色金属", "铜", "沪铜", "伦铜",
        "铝", "氧化铝", "锌", "铅", "镍", "锡", "工业硅", "稀土",
        "碳酸锂", "锂矿", "锂盐", "锂电原料", "铁矿石",
        "焦煤", "焦炭", "原油", "布伦特原油", "WTI原油",
        "天然气", "LNG", "期货", "商品期货",
    ]),
]
DEFAULT_CATEGORY = ("other", "⚪ 其他财经快讯")
CATEGORY_ORDER = [k for k, _, _ in CATEGORY_DEFS] + [DEFAULT_CATEGORY[0]]
ALERT_CATEGORIES = {"war", "policy"}   # 需要 @所有人 强提醒的分类
CATEGORY_COLOR = {
    "war": "yellow", "policy": "red", "ai": "green",
    "semiconductor": "purple", "commodity": "blue", "other": "grey",
}


def categorize(title, summary=""):
    text = f"{title} {summary}".lower()
    for key, label, kws in CATEGORY_DEFS:
        if any(kw.lower() in text for kw in kws):
            return key, label
    return DEFAULT_CATEGORY


def sentiment_hint(title, summary=""):
    text = f"{title} {summary}"
    positive = ["突破", "增长", "扩大", "签约", "利好", "创新高", "上调", "增持"]
    negative = ["下滑", "制裁", "下调", "减持", "亏损", "调查", "处罚"]
    p = sum(1 for x in positive if x in text)
    n = sum(1 for x in negative if x in text)
    if p > n:
        return "利好"
    if n > p:
        return "利空"
    return "-"


def normalize_title(title):
    t = title.strip()
    t = re.sub(r"^[\d]{1,2}[月/-][\d]{1,2}[日]?[，,：:\s]*", "", t)
    t = re.sub(r"^[\d]{1,2}[:：][\d]{1,2}[，,：:\s]*", "", t)
    t = re.sub(r"^(财联社|华尔街见闻|金十数据|消息|快讯|据悉|据报道)[，,：:\s]*", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fa5]", "", t)
    return t


def is_duplicate_across_sources(title, recent_titles):
    norm = normalize_title(title)
    if not norm:
        return False
    now = time.time()
    for item in recent_titles:
        if now - item.get("ts", 0) > DEDUP_WINDOW_SECONDS:
            continue
        other = item.get("norm", "")
        if not other:
            continue
        short, long_ = (norm, other) if len(norm) <= len(other) else (other, norm)
        if len(short) >= 8 and short in long_:
            return True
        ratio = SequenceMatcher(None, norm, other).ratio()
        if ratio >= DEDUP_SIMILARITY_THRESHOLD:
            return True
    return False


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=2)


def fetch_entries(path):
    last_error = None
    for base in RSSHUB_MIRRORS:
        url = base.rstrip("/") + path
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            print(f"  尝试镜像 {base} → HTTP {resp.status_code}，返回长度 {len(resp.text)}")
            if resp.status_code != 200 or len(resp.text) < 100:
                last_error = f"HTTP {resp.status_code}"
                continue
            feed = feedparser.parse(resp.text)
            if not feed.entries:
                last_error = "解析出 0 条 entries"
                continue

            entries = []
            for e in feed.entries:
                entry_id = e.get("id") or e.get("link") or e.get("title")
                title = e.get("title", "").strip()
                link = e.get("link", "")
                summary = (e.get("summary", "") or e.get("description", "") or "")
                summary = re.sub(r"<[^>]+>", "", summary).strip()
                t = e.get("published_parsed") or e.get("updated_parsed")
                ts = time.mktime(t) if t else time.time()
                entries.append((entry_id, title, summary, link, ts))
            entries.sort(key=lambda x: x[4])
            print(f"  ✔ 从 {base} 成功抓到 {len(entries)} 条")
            return entries
        except Exception as ex:
            last_error = str(ex)
            print(f"  镜像 {base} 抓取出错：{ex}")
            continue

    raise RuntimeError(f"所有镜像均抓取失败，最后一次错误：{last_error}")


def process_source(path, source_key, source_label, seen, collected, fetch_errors):
    """抓取一个来源，把去重后需要推送的条目追加进 collected 列表（不在这里发送）"""
    print(f"开始抓取：{source_label}")
    try:
        entries = fetch_entries(path)
    except Exception as ex:
        print(f"抓取 {source_label} 失败：{ex}")
        fetch_errors.append(f"{source_label}：{ex}")
        return

    seen_ids = set(seen.get(source_key, []))
    recent_titles = seen.setdefault("_recent_titles", [])

    new_entries = [e for e in entries if e[0] not in seen_ids]
    first_run = len(seen_ids) == 0

    for entry_id, *_ in new_entries:
        seen_ids.add(entry_id)

    to_consider = new_entries[-MAX_PUSH_PER_SOURCE:] if new_entries else []

    if first_run:
        for entry_id, title, summary, link, ts in to_consider:
            recent_titles.append({
                "norm": normalize_title(title), "ts": time.time(), "source": source_key,
            })
        print(f"{source_label} 首次运行，记录 {len(new_entries)} 条历史消息，不推送")
    else:
        skipped_dup = 0
        for entry_id, title, summary, link, ts in to_consider:
            if is_duplicate_across_sources(title, recent_titles):
                skipped_dup += 1
                print(f"  跳过（跟其他来源重复）：{title}")
                continue
            cat_key, cat_label = categorize(title, summary)
            collected.append({
                "cat_key": cat_key, "cat_label": cat_label,
                "title": title, "link": link, "source": source_label,
                "sentiment": sentiment_hint(title, summary), "ts": ts,
            })
            recent_titles.append({
                "norm": normalize_title(title), "ts": time.time(), "source": source_key,
            })
        print(f"{source_label}：本次收集 {len(to_consider) - skipped_dup} 条待推送，因跨源重复跳过 {skipped_dup} 条")

    seen[source_key] = list(seen_ids)[-500:]
    now = time.time()
    seen["_recent_titles"] = [
        item for item in recent_titles if now - item.get("ts", 0) <= DEDUP_WINDOW_SECONDS
    ][-300:]


def _group_and_chunk(collected):
    """按分类优先级分组，再按 MAX_ITEMS_PER_CARD / MAX_CARDS_PER_RUN 切成若干批次。
    返回 (chunks, remaining_count)，chunks 是 [[item,...], ...]，每个子列表对应一张卡片。
    """
    groups = {}
    for item in collected:
        groups.setdefault(item["cat_key"], []).append(item)
    for items in groups.values():
        items.sort(key=lambda x: x["ts"])

    ordered_items = []
    for key in CATEGORY_ORDER:
        ordered_items.extend(groups.get(key, []))

    total = len(ordered_items)
    max_total = MAX_ITEMS_PER_CARD * MAX_CARDS_PER_RUN
    shown_items = ordered_items[:max_total]
    remaining = total - len(shown_items)

    chunks = [
        shown_items[i:i + MAX_ITEMS_PER_CARD]
        for i in range(0, len(shown_items), MAX_ITEMS_PER_CARD)
    ]
    return chunks, remaining


def build_table_cards(collected):
    """生成表格样式的卡片（CardKit v2 table 组件）。返回 payload["card"] 的列表。"""
    chunks, remaining = _group_and_chunk(collected)
    cards = []
    total_cards = len(chunks)

    for idx, items in enumerate(chunks, 1):
        has_alert = any(it["cat_key"] in ALERT_CATEGORIES for it in items)
        color = "red" if has_alert else "blue"
        page_info = f"({idx}/{total_cards})" if total_cards > 1 else ""

        rows = []
        for it in items:
            row = {
                "cat": it["cat_label"],
                "title": it["title"],
                "source": SOURCE_SHORT_NAME.get(it["source"], it["source"]),
                "link": it["link"] or "-",
            }
            if SENTIMENT_AS_COLOR_TAG:
                if it["sentiment"] == "利好":
                    row["sentiment"] = [{"id": "up", "text": "利", "color": "red"}]
                elif it["sentiment"] == "利空":
                    row["sentiment"] = [{"id": "down", "text": "空", "color": "green"}]
                else:
                    row["sentiment"] = []
            else:
                row["sentiment"] = {"利好": "利", "利空": "空"}.get(it["sentiment"], "-")
            rows.append(row)

        sentiment_column = {
            "name": "sentiment", "display_name": "情绪",
            "width": TABLE_COL_WIDTH["sentiment"],
        }
        if SENTIMENT_AS_COLOR_TAG:
            sentiment_column["data_type"] = "options"
            sentiment_column["options"] = [
                {"id": "up", "text": "利", "color": "red"},
                {"id": "down", "text": "空", "color": "green"},
            ]
        else:
            sentiment_column["data_type"] = "text"

        elements = [{
            "tag": "table",
            "columns": [
                {"name": "cat", "display_name": "类别", "data_type": "text", "width": TABLE_COL_WIDTH["cat"]},
                {"name": "title", "display_name": "标题", "data_type": "text"},  # 不设宽度，自动占满剩余空间
                sentiment_column,
                {"name": "source", "display_name": "来源", "data_type": "text", "width": TABLE_COL_WIDTH["source"]},
                {"name": "link", "display_name": "链接", "data_type": "text", "width": TABLE_COL_WIDTH["link"]},
            ],
            "rows": rows,
            "header_style": {
                "bold": True,
                "text_align": "center",
                "text_size": "normal",
                "background_style": "grey",
                "lines": 1,
            },
        }]

        if idx == total_cards and remaining > 0:
            elements.append({
                "tag": "markdown",
                "content": f"_另有 {remaining} 条较次要资讯，可稍后在财联社 / 华尔街见闻客户端查看_",
            })
        if has_alert:
            elements.append({"tag": "markdown", "content": "<at id=all></at>"})

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 财经快讯速览 {page_info}".strip()},
                "template": color,
            },
            "body": {"elements": elements},
        }
        cards.append(card)

    return cards


def build_list_cards(collected):
    """兼容旧版客户端的分组列表卡片（JSON 1.0 风格，即 V5 的样式）。返回 payload["card"] 的列表。"""
    chunks, remaining = _group_and_chunk(collected)
    cards = []
    total_cards = len(chunks)

    for idx, items in enumerate(chunks, 1):
        has_alert = any(it["cat_key"] in ALERT_CATEGORIES for it in items)
        color = "red" if has_alert else "blue"
        page_info = f"({idx}/{total_cards})" if total_cards > 1 else ""

        by_cat = {}
        for it in items:
            by_cat.setdefault(it["cat_key"], []).append(it)

        lines = []
        for key in CATEGORY_ORDER:
            group = by_cat.get(key)
            if not group:
                continue
            lines.append(f"**{group[0]['cat_label']}**（{len(group)}条）")
            for i, it in enumerate(group, 1):
                tag = f" · {it['sentiment']}" if it["sentiment"] != "-" else ""
                lines.append(f"{i}. [{it['title']}]({it['link']}){tag}　*{it['source']}*")
            lines.append("")

        if idx == total_cards and remaining > 0:
            lines.append(f"_另有 {remaining} 条较次要资讯，可稍后在财联社 / 华尔街见闻客户端查看_")

        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines).strip()}}]
        if has_alert:
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "<at id=all></at>"}})

        card = {
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 财经快讯速览 {page_info}".strip()},
                "template": color,
            },
            "elements": elements,
        }
        cards.append(card)

    return cards


def send_card(card):
    if not FEISHU_WEBHOOK:
        print("未配置 FEISHU_WEBHOOK，跳过推送，仅打印：")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    payload = {"msg_type": "interactive", "card": card}
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") not in (0, None):
            print("飞书推送返回异常：", result)
    except Exception as ex:
        print("推送失败：", ex)
    time.sleep(0.3)


def send_fetch_failure_alert(fetch_errors, seen):
    """三个来源全部抓取失败时，推一张提示卡片到飞书，让人知道流水线卡住了
    （而不是安安静静地什么都不发，让人误以为是"真的没有新消息"）。
    带 1 小时冷却，避免抓取持续失败期间反复报警刷屏。
    """
    last_alert_ts = seen.get("_last_fetch_alert_ts", 0)
    if time.time() - last_alert_ts < FETCH_ALERT_COOLDOWN_SECONDS:
        print("抓取全部失败，但报警冷却中，跳过本次报警")
        return

    detail = "\n".join(f"- {e}" for e in fetch_errors)
    card = {
        "header": {
            "title": {"tag": "plain_text", "content": "⚠️ 快讯抓取全部失败"},
            "template": "orange",
        },
        "elements": [{
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    "本次运行三个来源全部抓取失败，可能是 RSSHub 公共镜像限流/宕机，"
                    "或网络问题。这段时间群里不会有新快讯推送，请留意排查。\n\n" + detail
                ),
            },
        }],
    }
    send_card(card)
    seen["_last_fetch_alert_ts"] = time.time()


def main():
    seen = load_seen()
    collected = []
    fetch_errors = []

    for path, source_key, source_label in SOURCES:
        process_source(path, source_key, source_label, seen, collected, fetch_errors)

    if len(fetch_errors) == len(SOURCES):
        # 三个来源这次全都没抓到，值得报警；跟"抓到了但没有新内容"是两码事
        send_fetch_failure_alert(fetch_errors, seen)
    elif not collected:
        print("本次没有需要推送的新消息")
    else:
        builder = build_table_cards if USE_TABLE_CARD else build_list_cards
        cards = builder(collected)
        for card in cards:
            send_card(card)

    save_seen(seen)


if __name__ == "__main__":
    main()
