# -*- coding: utf-8 -*-
"""
自动抓取「财联社电报（重要/普通）」和「百度热搜（泛指头条新闻）」
新内容推送到飞书群机器人。重要消息（财联社加红）用红色卡片 + @所有人 强提醒。

原理：
1. 通过 RSSHub 公共实例把财联社电报、百度热搜转换成 RSS
2. 用 seen.json 记录已经推送过的内容 id，避免重复推送
3. 新内容通过飞书自定义机器人 Webhook 推送出去
4. GitHub Actions 会定时（默认每 5 分钟）运行本脚本一次
"""

import json
import os
import re
import sys
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
CLS_RED_PATH = "/cls/telegraph/red"       # 财联社 - 电报（加红/重要消息）
WALLSTREETCN_PATH = "/wallstreetcn/live"  # 华尔街见闻 - 实时快讯（政策/监管类新闻常最先报）
JIN10_PATH = "/jin10/important"           # 金十数据 - 重要资讯（国际财经/汇率/大宗商品反应快）

# 每类消息单次最多推送几条，防止第一次运行时刷屏
MAX_PUSH_PER_SOURCE = 8

# 跨来源去重：如果新消息标题跟"最近一段时间内已经推送过的标题"很相似，
# 就认为是同一件事被不同来源重复报道，不再重复推送
DEDUP_SIMILARITY_THRESHOLD = 0.55   # 相似度阈值，0~1，越大越"宽松"（判定重复的门槛更高）
DEDUP_WINDOW_SECONDS = 3 * 60 * 60  # 只跟最近 3 小时内推送过的标题比较，超过这个时间不再比对

# 已推送记录文件
SEEN_FILE = "seen.json"

# 飞书 Webhook 地址，从 GitHub Secrets 里读取，不要把地址写死在代码里
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "").strip()

# 伪装成正常浏览器的请求头，避免被 RSSHub / 上游网站当成机器人拦截
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


# 战争/军事/地缘政治关键词
WAR_KEYWORDS = [
    "战争","军事","袭击","空袭","导弹","无人机","轰炸","核武","核设施",
    "军演","冲突","停火","火箭弹","美国","美军","五角大楼","伊朗","以色列",
    "俄罗斯","俄军","乌克兰","乌军","北约","胡塞","哈马斯","真主党",
    "中东局势","国防部","国防军","参谋长","革命卫队","航母","驱逐舰",
    "战机","F35","F-35","B2","B-2","爱国者","萨德","红海",
    "霍尔木兹","波斯湾","制裁","军火","武器援助","撤侨","戒严","紧急状态",
]



COMMODITY_KEYWORDS = [
    "黄金","白银","现货黄金","现货白银","COMEX黄金","COMEX白银",
    "伦敦金","伦敦银","贵金属","有色金属","铜","沪铜","伦铜",
    "铝","氧化铝","锌","铅","镍","锡","工业硅","稀土",
    "碳酸锂","锂矿","锂盐","锂电原料","铁矿石",
    "焦煤","焦炭","原油","布伦特原油","WTI原油",
    "天然气","LNG","期货","商品期货"
]


AI_KEYWORDS = [
    "人工智能","AI","大模型","算力","AIGC","机器人","人形机器人",
    "智谱","DeepSeek","OpenAI","英伟达","GPU","数据中心","CPO"
]

SEMICONDUCTOR_KEYWORDS = [
    "半导体","芯片","晶圆","光刻机","EDA","存储芯片","HBM",
    "先进封装","台积电","中芯国际","集成电路"
]

POLICY_KEYWORDS = [
    "国务院","国常会","证监会","央行","金融监管总局",
    "降准","降息","货币政策","财政政策","专项债","政治局会议"
]

def is_ai_news(title, summary=""):
    text = f"{title} {summary}".lower()
    return any(k.lower() in text for k in AI_KEYWORDS)

def is_semiconductor_news(title, summary=""):
    text = f"{title} {summary}".lower()
    return any(k.lower() in text for k in SEMICONDUCTOR_KEYWORDS)

def is_policy_news(title, summary=""):
    text = f"{title} {summary}".lower()
    return any(k.lower() in text for k in POLICY_KEYWORDS)

def normalize_title(title):
    """把标题里的日期、时间、来源名等"干扰项"去掉，方便比较是不是同一件事"""
    t = title.strip()
    # 去掉开头的日期/时间，比如 "7月13日，" "14:32，"
    t = re.sub(r"^[\d]{1,2}[月/-][\d]{1,2}[日]?[，,：:\s]*", "", t)
    t = re.sub(r"^[\d]{1,2}[:：][\d]{1,2}[，,：:\s]*", "", t)
    # 去掉常见的媒体自称前缀
    t = re.sub(r"^(财联社|华尔街见闻|金十数据|消息|快讯|据悉|据报道)[，,：:\s]*", "", t)
    # 去掉标点符号，只留中英文和数字
    t = re.sub(r"[^\w\u4e00-\u9fa5]", "", t)
    return t



def is_commodity_news(title, summary=""):
    text = f"{title} {summary}".lower()
    for kw in COMMODITY_KEYWORDS:
        if kw.lower() in text:
            return True
    return False


def is_war_news(title, summary=""):
    """标题+正文联合判断战争新闻"""
    text = f"{title} {summary}".lower()
    for kw in WAR_KEYWORDS:
        if kw.lower() in text:
            return True
    return False


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
        # 一个标题的核心内容完全包含在另一个里面，直接判定重复
        short, long_ = (norm, other) if len(norm) <= len(other) else (other, norm)
        if len(short) >= 8 and short in long_:
            return True
        # 否则用整体相似度打分
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
    返回 [(id, title, link, published_ts), ...]，按时间从旧到新排列。
    任何一个镜像成功拿到内容就立即返回，不再继续尝试。
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
            entries.sort(key=lambda x: x[3])
            print(f"  ✔ 从 {base} 成功抓到 {len(entries)} 条")
            return entries
        except Exception as ex:
            last_error = str(ex)
            print(f"  镜像 {base} 抓取出错：{ex}")
            continue

    raise RuntimeError(f"所有镜像均抓取失败，最后一次错误：{last_error}")


def send_to_feishu(text, is_important=False, is_war=False, is_commodity=False, is_ai=False, is_semiconductor=False, is_policy=False):
    if not FEISHU_WEBHOOK:
        print("未配置 FEISHU_WEBHOOK，跳过推送，仅打印：")
        print(text)
        return

    if is_important:
        # 重要消息：红色卡片 + @所有人
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": ("🟢 AI算力机器人快讯" if is_ai else ("🟣 半导体芯片快讯" if is_semiconductor else ("🔵 商品期货快讯" if is_commodity else ("🟡 战争地缘政治快讯" if is_war else ("🟥 重要政策快讯" if is_policy else "🔴 财联社重要电报")))))},
                    "template": ("green" if is_ai else ("purple" if is_semiconductor else ("blue" if is_commodity else ("yellow" if is_war else ("red" if is_policy else "red"))))),
                },
                "elements": [
                    {"tag": "div", "text": {"tag": "lark_md", "content": text}},
                    {"tag": "div", "text": {"tag": "lark_md", "content": "<at id=all></at>"}},
                ],
            },
        }
    else:
        payload = {"msg_type": "text", "content": {"text": text}}

    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") not in (0, None):
            print("飞书推送返回异常：", result)
    except Exception as ex:
        print("推送失败：", ex)
    # 避免推送过快被限流
    time.sleep(0.3)


def process_source(path, seen, source_key, label, is_important=False):
    if not path:
        return
    print(f"开始抓取：{label}")
    try:
        entries = fetch_entries(path)
    except Exception as ex:
        print(f"抓取 {label} 失败：{ex}")
        return

    seen_ids = set(seen.get(source_key, []))
    recent_titles = seen.setdefault("_recent_titles", [])

    new_entries = [e for e in entries if e[0] not in seen_ids]

    # 首次运行（seen_ids 为空）时不刷屏，只记录不推送，但要把标题存进"最近标题池"，
    # 这样下一次真正开始推送时，不会因为看不到这些历史标题而误判重复
    first_run = len(seen_ids) == 0

    to_consider = new_entries[-MAX_PUSH_PER_SOURCE:] if new_entries else []

    pushed_count = 0
    skipped_dup_count = 0

    for entry_id, title, summary, link, ts in new_entries:
        seen_ids.add(entry_id)

    if not first_run:
        for entry_id, title, summary, link, ts in to_consider:
            if is_duplicate_across_sources(title, recent_titles):
                skipped_dup_count += 1
                print(f"  跳过（跟其他来源重复）：{title}")
                continue
            preview = summary[:120] if summary else ""
            text = f"{label}\n\n{title}\n\n{preview}\n\n{link}" if link else f"{label}\n\n{title}\n\n{preview}"
            war_flag = is_war_news(title, summary)
            commodity_flag = is_commodity_news(title, summary)
            ai_flag = is_ai_news(title, summary)
            semiconductor_flag = is_semiconductor_news(title, summary)
            policy_flag = is_policy_news(title, summary)
            send_to_feishu(
                text,
                is_important=is_important,
                is_war=war_flag,
                is_commodity=commodity_flag,
                is_ai=ai_flag,
                is_semiconductor=semiconductor_flag,
                is_policy=policy_flag
            )
            pushed_count += 1
            recent_titles.append({
                "norm": normalize_title(title),
                "ts": time.time(),
                "source": source_key,
            })
        print(f"{label}：本次推送 {pushed_count} 条，因跨源重复跳过 {skipped_dup_count} 条")
    else:
        # 首次运行：把已有标题也记进"最近标题池"，作为去重的起点
        for entry_id, title, summary, link, ts in new_entries[-MAX_PUSH_PER_SOURCE:]:
            recent_titles.append({
                "norm": normalize_title(title),
                "ts": time.time(),
                "source": source_key,
            })
        print(f"{label} 首次运行，记录 {len(new_entries)} 条历史消息，不推送")

    # 只保留最近 500 条 id，防止文件无限增大
    seen[source_key] = list(seen_ids)[-500:]

    # 清理"最近标题池"：只留 DEDUP_WINDOW_SECONDS 时间窗口内的，避免文件越滚越大
    now = time.time()
    seen["_recent_titles"] = [
        item for item in recent_titles if now - item.get("ts", 0) <= DEDUP_WINDOW_SECONDS
    ][-300:]


def main():
    seen = load_seen()

    process_source(CLS_RED_PATH, seen, "cls_red", "🔴 财联社重要电报", is_important=True)
    process_source(WALLSTREETCN_PATH, seen, "wallstreetcn", "🟠 华尔街见闻快讯", is_important=True)
    process_source(JIN10_PATH, seen, "jin10", "🌍 金十数据重要资讯", is_important=True)

    save_seen(seen)


if __name__ == "__main__":
    main()


# ================= V4 板块映射增强 =================

SECTOR_KEYWORDS = {
    "机器人": ["机器人","人形机器人","工业机器人"],
    "AI算力": ["AI","人工智能","算力","GPU","数据中心","大模型"],
    "半导体": ["半导体","芯片","晶圆","光刻机","HBM"],
    "军工": ["军工","导弹","战机","航母","国防"],
    "创新药": ["创新药","CXO","ADC","减肥药","PD-1"],
    "新能源": ["碳酸锂","锂矿","储能","光伏","风电"],
    "黄金有色": ["黄金","白银","铜","铝","镍","稀土"],
}

def detect_sectors(title, summary=""):
    text = f"{title} {summary}".lower()
    sectors = []
    for sector, kws in SECTOR_KEYWORDS.items():
        if any(k.lower() in text for k in kws):
            sectors.append(sector)
    return sectors

def sentiment_hint(title, summary=""):
    text = f"{title} {summary}"
    positive = ["突破","增长","扩大","签约","利好","创新高","上调","增持"]
    negative = ["下滑","制裁","下调","减持","亏损","调查","处罚"]
    p = sum(1 for x in positive if x in text)
    n = sum(1 for x in negative if x in text)
    if p > n:
        return "利好"
    if n > p:
        return "利空"
    return "中性"
