# -*- coding: utf-8 -*-
"""
自动抓取「财联社电报（重要）」「华尔街见闻实时快讯」「金十数据重要资讯」，
新内容按类别分组、汇总推送到飞书群机器人 V5表格版
核心改动：消息展示改为Markdown表格，原有抓取/去重/分类逻辑完全不变
情绪颜色互换：利好红色，利空绿色
"""
import json
import os
import re
import time
import requests
import feedparser
from difflib import SequenceMatcher

# ========== 配置区（一般不用改） ==========
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
# 每一项：(分类key, 展示用的分类标签, 关键词列表)
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
# 兜底分类：没命中任何关键词的消息
DEFAULT_CATEGORY = ("other", "⚪ 其他财经快讯")
# 分类展示顺序（优先级从高到低）
CATEGORY_ORDER = [k for k, _, _ in CATEGORY_DEFS] + [DEFAULT_CATEGORY[0]]
# 需要 @所有人 强提醒的分类
ALERT_CATEGORIES = {"war", "policy"}
# 每个分类对应的卡片主题色（Feishu card template 颜色）
CATEGORY_COLOR = {
    "war": "yellow",
    "policy": "red",
    "ai": "green",
    "semiconductor": "purple",
    "commodity": "blue",
    "other": "grey",
}

# ========== 工具函数（完全原版无修改） ==========
def categorize(title, summary=""):
    """返回 (分类key, 分类展示标签)，按 CATEGORY_DEFS 顺序，命中第一个就返回"""
    text = f"{title} {summary}".lower()
    for key, label, kws in CATEGORY_DEFS:
        if any(kw.lower() in text for kw in kws):
            return key, label
    return DEFAULT_CATEGORY

def sentiment_hint(title, summary=""):
    """粗略判断这条消息偏利好/利空/中性，用于在条目后面加个小标签"""
    text = f"{title} {summary}"
    positive = ["突破", "增长", "扩大", "签约", "利好", "创新高", "上调", "增持"]
    negative = ["下滑", "制裁", "下调", "减持", "亏损", "调查", "处罚"]
    p = sum(1 for x in positive if x in text)
    n = sum(1 for x in negative if x in text)
    if p > n:
        return "利好"
    if n > p:
        return "利空"
    return "中性"

def normalize_title(title):
    """把标题里的日期、时间、来源名等"干扰项"去掉，方便比较是不是同一件事"""
    t = title.strip()
    t = re.sub(r"^[\d]{1,2}[月/-][\d]{1,2}[日]?[，,：:\s]*", "", t)
    t = re.sub(r"^[\d]{1,2}[:：][\d]{1,2}[，,：:\s]*", "", t)
    t = re.sub(r"^(财联社|华尔街见闻|金十数据|消息|快讯|据悉|据报道)[，,：:\s]*", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fa5]", "", t)
    return t

def is_duplicate_across_sources(title, recent_titles):
    """判断这条标题，是不是跟"最近推送过的标题"讲的是同一件事"""
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
    """依次尝试各个 RSSHub 镜像抓取 path 对应的 RSS，
    返回 [(id, title, summary, link, published_ts), ...]，按时间从旧到新排列。
    """
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

def process_source(path, source_key, source_label, seen, collected):
    """抓取一个来源，把去重后需要推送的条目追加进 collected 列表（不在这里发送）"""
    print(f"开始抓取：{source_label}")
    try:
        entries = fetch_entries(path)
    except Exception as ex:
        print(f"抓取 {source_label} 失败：{ex}")
        return
    seen_ids = set(seen.get(source_key, []))
    recent_titles = seen.setdefault("_recent_titles", [])
    new_entries = [e for e in entries if e[0] not in seen_ids]
    first_run = len(seen_ids) == 0
    for entry_id, *_ in new_entries:
        seen_ids.add(entry_id)
    to_consider = new_entries[-MAX_PUSH_PER_SOURCE:] if new_entries else []
    if first_run:
        # 首次运行：只记录历史标题作为去重起点，不推送，避免刚上线就刷屏
        for entry_id, title, summary, link, ts in to_consider:
            recent_titles.append({
                "norm": normalize_title(title),
                "ts": time.time(),
                "source": source_key,
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
                "cat_key": cat_key,
                "cat_label": cat_label,
                "title": title,
                "link": link,
                "source": source_label,
                "sentiment": sentiment_hint(title, summary),
                "ts": ts,
            })
            recent_titles.append({
                "norm": normalize_title(title),
                "ts": time.time(),
                "source": source_key,
            })
        print(f"{source_label}：本次收集 {len(to_consider) - skipped_dup} 条待推送，因跨源重复跳过 {skipped_dup} 条")
    seen[source_key] = list(seen_ids)[-500:]
    now = time.time()
    seen["_recent_titles"] = [
        item for item in recent_titles if now - item.get("ts", 0) <= DEDUP_WINDOW_SECONDS
    ][-300:]

# ========== 核心改造：生成Markdown表格内容（替换原来列表逻辑） ==========
def build_card_bodies(collected):
    """
    把 collected 按分类分组，生成若干张卡片的表格内容：
    每张卡片是 (markdown正文, 是否需要@所有人, 主题色) 的元组列表。
    最多生成 MAX_CARDS_PER_RUN 张，多出的条目在最后一张卡片末尾提示条数。
    输出格式：分类标题 + Markdown表格（序号|资讯标题|来源|情绪）
    颜色调整：利好红色，利空绿色
    """
    if not collected:
        return []
    groups = {}
    for item in collected:
        groups.setdefault(item["cat_key"], []).append(item)
    # 每个分类内资讯按时间排序
    for items in groups.values():
        items.sort(key=lambda x: x["ts"])
    # 按预设分类顺序组装内容块
    blocks = []
    for key in CATEGORY_ORDER:
        items = groups.get(key)
        if items:
            label = items[0]["cat_label"]
            blocks.append((key, label, items))
    total_items = sum(len(items) for _, _, items in blocks)
    cards = []
    current_md_lines = []
    current_count = 0
    current_alert = False
    current_colors = set()

    # 清空当前卡片缓存，生成新卡片
    def flush_card():
        nonlocal current_md_lines, current_count, current_alert, current_colors
        if current_md_lines:
            full_text = "\n".join(current_md_lines).strip()
            # 卡片主色调：有预警分类用红色，单一分类用对应色，混合用蓝色
            color = "red" if current_alert else (
                sorted(current_colors)[0] if len(current_colors) == 1 else "blue"
            )
            cards.append((full_text, current_alert, color))
        # 重置缓存
        current_md_lines, current_count, current_alert, current_colors = [], 0, False, set()

    # 遍历每个分类块，生成表格
    for key, label, items in blocks:
        # 达到最大卡片数量限制，停止填充
        if len(cards) >= MAX_CARDS_PER_RUN - 1 and current_count >= MAX_ITEMS_PER_CARD:
            break
        # 当前卡片装满，先输出当前卡片再新开
        if current_count + len(items) > MAX_ITEMS_PER_CARD and current_md_lines:
            flush_card()
            if len(cards) >= MAX_CARDS_PER_RUN:
                break

        # 写入分类大标题
        current_md_lines.append(f"\n## {label}（共{len(items)}条）")
        current_md_lines.append("---")
        # 表格表头
        table_lines = [
            "| 序号 | 资讯标题 | 来源 | 情绪 |",
            "| ---- | -------- | ---- | ---- |"
        ]
        # 填充表格行，情绪颜色互换：利好红，利空绿
        for idx, it in enumerate(items, 1):
            if it["sentiment"] == "利好":
                sentiment_text = "<font color='red'>利好</font>"
            elif it["sentiment"] == "利空":
                sentiment_text = "<font color='green'>利空</font>"
            else:
                sentiment_text = "中性"
            # 标题带跳转链接
            title_link = f"[{it['title']}]({it['link']})"
            row = f"| {idx} | {title_link} | {it['source']} | {sentiment_text} |"
            table_lines.append(row)
        # 表格拼入正文
        current_md_lines.extend(table_lines)
        current_md_lines.append("\n")
        current_count += len(items)
        # 判断是否需要@所有人
        if key in ALERT_CATEGORIES:
            current_alert = True
        current_colors.add(CATEGORY_COLOR.get(key, "blue"))

    # 把最后缓存的内容生成卡片
    flush_card()

    # 统计已展示条数，补充剩余资讯提示
    shown = 0
    for text, _, _ in cards:
        # 统计表格内数据行（排除表头两行）
        table_rows = re.findall(r"^\| \d+ \|", text, re.MULTILINE)
        shown += len(table_rows)
    if shown < total_items and cards:
        text, alert, color = cards[-1]
        remaining = total_items - shown
        text += f"\n\n_另有 {remaining} 条较次要资讯，可稍后在财联社 / 华尔街见闻客户端查看_"
        cards[-1] = (text, alert, color)

    return cards[:MAX_CARDS_PER_RUN]

# ========== 发送卡片函数（增加宽屏配置，适配表格横向展示） ==========
def send_card(text, alert, color, page_info=""):
    if not FEISHU_WEBHOOK:
        print("未配置 FEISHU_WEBHOOK，跳过推送，仅打印：")
        print(text)
        return
    title = "📊 财经快讯速览" + (f" {page_info}" if page_info else "")
    elements = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": text
            }
        }
    ]
    # 战争/政策分类 @所有人
    if alert:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "<at id=all></at>"
            }
        })
    # 卡片payload，开启宽屏模式，表格展示更友好
    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True  # 关键：宽屏适配表格
            },
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": elements,
        },
    }
    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") not in (0, None):
            print("飞书推送返回异常：", result)
    except Exception as ex:
        print("推送失败：", ex)
    time.sleep(0.3)

# ========== 主入口（完全原版无修改） ==========
def main():
    seen = load_seen()
    collected = []
    # 循环抓取三个资讯源
    for path, source_key, source_label in SOURCES:
        process_source(path, source_key, source_label, seen, collected)
    # 生成表格卡片内容
    cards = build_card_bodies(collected)
    total_cards = len(cards)
    # 依次发送每张卡片
    for idx, (text, alert, color) in enumerate(cards, 1):
        page_info = f"({idx}/{total_cards})" if total_cards > 1 else ""
        send_card(text, alert, color, page_info)
    if not cards:
        print("本次没有需要推送的新消息")
    # 保存已推送记录
    save_seen(seen)

if __name__ == "__main__":
    main()
