# -*- coding: utf-8 -*-
"""
盘中财经快讯 → 飞书（V6）

相对 V5：GitHub Actions 调度；交易日整点+15:30；周日 19:00 热点周报；
其它时间仅战争/重要政策且热点才推；状态文件固定；Webhook 名与股票项目对齐。
不读持仓、不 @ 持仓。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import feedparser
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

TZ = ZoneInfo("Asia/Shanghai")
HERE = Path(__file__).resolve().parent
# 与 news-to-feishu 仓库现有 workflow 对齐：提交的是 seen.json
STATE_FILE = HERE / "seen.json"

RSSHUB_MIRRORS = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://rsshub.pseudoyu.com",
    "https://rsshub.liumingye.cn",
    "https://rsshub.agrreader.com",
]

SOURCES = [
    ("/cls/telegraph/red", "cls_red", "财联社"),
    ("/wallstreetcn/live", "wallstreetcn", "华尔街见闻"),
    ("/jin10/important", "jin10", "金十数据"),
]

MAX_PUSH_PER_SOURCE = 8
DEDUP_SIMILARITY_THRESHOLD = 0.55
DEDUP_WINDOW_SECONDS = 3 * 60 * 60
MAX_ITEMS_PER_CARD = 20
MAX_CARDS_PER_RUN = 2
SLOT_GRACE_MIN = 12
SUNDAY_DIGEST_GRACE_MIN = 20

# 上交所 2026 年已公布休市（含公告中的周末休市日）；再叠加六日判定
_HOLIDAY_RANGES_2026 = [
    (date(2026, 1, 1), date(2026, 1, 4)),
    (date(2026, 2, 14), date(2026, 2, 23)),
    (date(2026, 2, 28), date(2026, 2, 28)),
    (date(2026, 4, 4), date(2026, 4, 6)),
    (date(2026, 5, 1), date(2026, 5, 5)),
    (date(2026, 5, 9), date(2026, 5, 9)),
    (date(2026, 6, 19), date(2026, 6, 21)),
    (date(2026, 9, 20), date(2026, 9, 20)),
    (date(2026, 9, 25), date(2026, 9, 27)),
    (date(2026, 10, 1), date(2026, 10, 7)),
    (date(2026, 10, 10), date(2026, 10, 10)),
]

HOURLY_SLOTS = [(9, 0), (10, 0), (11, 0), (12, 0), (13, 0), (14, 0), (15, 0)]
CLOSE_SLOT = (15, 30)
WEEKEND_DIGEST = (19, 0)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

CATEGORY_DEFS = [
    ("war", "🟡 战争地缘", [
        "战争", "军事", "袭击", "空袭", "导弹", "无人机", "轰炸", "核武", "核设施",
        "军演", "冲突", "停火", "火箭弹", "美军", "五角大楼", "伊朗", "以色列",
        "俄罗斯", "俄军", "乌克兰", "乌军", "北约", "胡塞", "哈马斯", "真主党",
        "中东局势", "国防部", "国防军", "参谋长", "革命卫队", "航母", "驱逐舰",
        "战机", "F35", "F-35", "B2", "B-2", "爱国者", "萨德", "红海",
        "霍尔木兹", "波斯湾", "军火", "武器援助", "撤侨", "戒严", "紧急状态",
        "开战",
    ]),
    ("policy", "🟥 政策监管", [
        "国务院", "国常会", "证监会", "央行", "金融监管总局", "制裁",
        "降准", "降息", "货币政策", "财政政策", "专项债", "政治局会议",
        "中央政治局",
    ]),
    ("ai", "🟢 AI算力机器人", [
        "人工智能", "大模型", "算力", "AIGC", "人形机器人",
        "智谱", "DeepSeek", "OpenAI", "英伟达", "GPU", "数据中心", "CPO",
    ]),
    ("semiconductor", "🟣 半导体芯片", [
        "半导体", "芯片", "晶圆", "光刻机", "EDA", "存储芯片", "HBM",
        "先进封装", "台积电", "中芯国际", "集成电路",
    ]),
    ("commodity", "🔵 商品期货贵金属", [
        "黄金", "白银", "现货黄金", "现货白银", "COMEX黄金", "COMEX白银",
        "伦敦金", "伦敦银", "贵金属", "有色金属", "沪铜", "伦铜",
        "氧化铝", "工业硅", "稀土", "碳酸锂", "锂矿", "锂盐",
        "铁矿石", "焦煤", "焦炭", "布伦特原油", "WTI原油", "LNG",
    ]),
]
DEFAULT_CATEGORY = ("other", "⚪ 其他财经快讯")
CATEGORY_ORDER = [k for k, _, _ in CATEGORY_DEFS] + [DEFAULT_CATEGORY[0]]
ALERT_CATEGORIES = {"war", "policy"}
CATEGORY_COLOR = {
    "war": "yellow",
    "policy": "red",
    "ai": "green",
    "semiconductor": "purple",
    "commodity": "blue",
    "other": "grey",
}

HOT_TITLE_KWS = ["重磅", "突发", "紧急", "重大", "开战", "停火"]
IMPORTANT_POLICY_KWS = [
    "国务院", "国常会", "证监会", "央行", "金融监管总局", "制裁",
    "降准", "降息", "政治局",
]
HOT_SOURCES = {"cls_red", "jin10"}

POSITIVE = ["突破", "增长", "扩大", "签约", "利好", "创新高", "上调", "增持"]
NEGATIVE = ["下滑", "制裁", "下调", "减持", "亏损", "调查", "处罚"]


def now_bj() -> datetime:
    return datetime.now(TZ)


def webhook_url() -> str:
    return (
        os.environ.get("FEISHU_WEBHOOK_URL")
        or os.environ.get("FEISHU_WEBHOOK")
        or ""
    ).strip()


def in_holiday_2026(d: date) -> bool:
    for a, b in _HOLIDAY_RANGES_2026:
        if a <= d <= b:
            return True
    return False


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return not in_holiday_2026(d)


def categorize(title: str, summary: str = "") -> tuple[str, str]:
    text = f"{title} {summary}".lower()
    for key, label, kws in CATEGORY_DEFS:
        if any(kw.lower() in text for kw in kws):
            return key, label
    return DEFAULT_CATEGORY


def sentiment_hint(title: str, summary: str = "") -> str:
    text = f"{title} {summary}"
    p = sum(1 for x in POSITIVE if x in text)
    n = sum(1 for x in NEGATIVE if x in text)
    if p > n:
        return "利好"
    if n > p:
        return "利空"
    return "中性"


def is_important_policy(title: str, summary: str = "") -> bool:
    text = f"{title} {summary}"
    return any(kw in text for kw in IMPORTANT_POLICY_KWS)


def is_hot(item: dict) -> bool:
    if item.get("source_key") in HOT_SOURCES:
        return True
    text = f"{item.get('title', '')} {item.get('summary', '')}"
    if any(kw in text for kw in HOT_TITLE_KWS):
        return True
    if item.get("cat_key") == "war":
        return True
    if item.get("cat_key") == "policy" and is_important_policy(
        item.get("title", ""), item.get("summary", "")
    ):
        return True
    return False


def is_breaking(item: dict) -> bool:
    if not is_hot(item):
        return False
    if item.get("cat_key") == "war":
        return True
    if item.get("cat_key") == "policy" and is_important_policy(
        item.get("title", ""), item.get("summary", "")
    ):
        return True
    return False


def normalize_title(title: str) -> str:
    t = title.strip()
    t = re.sub(r"^[\d]{1,2}[月/-][\d]{1,2}[日]?[，,：:\s]*", "", t)
    t = re.sub(r"^[\d]{1,2}[:：][\d]{1,2}[，,：:\s]*", "", t)
    t = re.sub(r"^(财联社|华尔街见闻|金十数据|消息|快讯|据悉|据报道)[，,：:\s]*", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fa5]", "", t)
    return t


def is_duplicate_across_sources(title: str, recent_titles: list) -> bool:
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
        if SequenceMatcher(None, norm, other).ratio() >= DEDUP_SIMILARITY_THRESHOLD:
            return True
    return False


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    raw = STATE_FILE.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    text = json.dumps(state, ensure_ascii=False, indent=2)
    STATE_FILE.write_text(text + "\n", encoding="utf-8")


def fetch_entries(path: str) -> list:
    last_error = None
    for base in RSSHUB_MIRRORS:
        url = base.rstrip("/") + path
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            print(f"  尝试镜像 {base} → HTTP {resp.status_code}，长度 {len(resp.text)}")
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
                title = (e.get("title") or "").strip()
                link = e.get("link") or ""
                summary = (e.get("summary") or e.get("description") or "")
                summary = re.sub(r"<[^>]+>", "", summary).strip()
                t = e.get("published_parsed") or e.get("updated_parsed")
                ts = time.mktime(t) if t else time.time()
                entries.append((entry_id, title, summary, link, ts))
            entries.sort(key=lambda x: x[4])
            print(f"  从 {base} 抓到 {len(entries)} 条")
            return entries
        except Exception as ex:
            last_error = str(ex)
            print(f"  镜像 {base} 出错：{ex}")
    raise RuntimeError(f"所有镜像失败：{last_error}")


def item_from_entry(source_key: str, source_label: str, entry: tuple) -> dict:
    entry_id, title, summary, link, ts = entry
    cat_key, cat_label = categorize(title, summary)
    return {
        "id": entry_id,
        "cat_key": cat_key,
        "cat_label": cat_label,
        "title": title,
        "summary": summary,
        "link": link,
        "source": source_label,
        "source_key": source_key,
        "sentiment": sentiment_hint(title, summary),
        "ts": ts,
    }


def collect_new(state: dict, *, seed_only: bool) -> list[dict]:
    collected: list[dict] = []
    for path, source_key, source_label in SOURCES:
        print(f"开始抓取：{source_label}")
        try:
            entries = fetch_entries(path)
        except Exception as ex:
            print(f"抓取 {source_label} 失败：{ex}")
            continue

        seen_ids = set(state.get(source_key, []))
        recent_titles = state.setdefault("_recent_titles", [])
        new_entries = [e for e in entries if e[0] not in seen_ids]
        for entry_id, *_ in new_entries:
            seen_ids.add(entry_id)
        to_consider = new_entries[-MAX_PUSH_PER_SOURCE:] if new_entries else []

        if seed_only:
            for entry_id, title, summary, link, ts in to_consider:
                recent_titles.append({
                    "norm": normalize_title(title),
                    "ts": time.time(),
                    "source": source_key,
                })
            print(f"{source_label} 首次/种子：记录 {len(new_entries)} 条，不入待发")
        else:
            skipped = 0
            for entry in to_consider:
                entry_id, title, summary, link, ts = entry
                if is_duplicate_across_sources(title, recent_titles):
                    skipped += 1
                    print(f"  跳过重复：{title}")
                    continue
                collected.append(item_from_entry(source_key, source_label, entry))
                recent_titles.append({
                    "norm": normalize_title(title),
                    "ts": time.time(),
                    "source": source_key,
                })
            print(f"{source_label}：待处理 {len(to_consider) - skipped}，重复 {skipped}")

        state[source_key] = list(seen_ids)[-500:]
        now = time.time()
        state["_recent_titles"] = [
            x for x in recent_titles if now - x.get("ts", 0) <= DEDUP_WINDOW_SECONDS
        ][-300:]
    return collected


def build_card_bodies(collected: list[dict]) -> list[tuple[str, bool, str]]:
    if not collected:
        return []
    groups: dict[str, list] = {}
    for item in collected:
        groups.setdefault(item["cat_key"], []).append(item)
    for items in groups.values():
        items.sort(key=lambda x: x["ts"])

    blocks = []
    for key in CATEGORY_ORDER:
        items = groups.get(key)
        if items:
            blocks.append((key, items[0]["cat_label"], items))

    total_items = sum(len(items) for _, _, items in blocks)
    cards: list[tuple[str, bool, str]] = []
    current_lines: list[str] = []
    current_count = 0
    current_alert = False
    current_colors: set[str] = set()

    def flush_card() -> None:
        nonlocal current_lines, current_count, current_alert, current_colors
        if current_lines:
            color = "red" if current_alert else (
                sorted(current_colors)[0] if len(current_colors) == 1 else "blue"
            )
            cards.append(("\n".join(current_lines).strip(), current_alert, color))
        current_lines, current_count, current_alert, current_colors = [], 0, False, set()

    for key, label, items in blocks:
        if len(cards) >= MAX_CARDS_PER_RUN - 1 and current_count >= MAX_ITEMS_PER_CARD:
            break
        if current_count + len(items) > MAX_ITEMS_PER_CARD and current_lines:
            flush_card()
            if len(cards) >= MAX_CARDS_PER_RUN:
                break
        current_lines.append(f"**{label}**（{len(items)}条）")
        for i, it in enumerate(items, 1):
            tag = f" · {it['sentiment']}" if it["sentiment"] != "中性" else ""
            current_lines.append(
                f"{i}. [{it['title']}]({it['link']}){tag}　*{it['source']}*"
            )
        current_lines.append("")
        current_count += len(items)
        if key in ALERT_CATEGORIES:
            current_alert = True
        current_colors.add(CATEGORY_COLOR.get(key, "blue"))

    flush_card()
    shown = sum(
        len([ln for ln in c[0].split("\n") if re.match(r"^\d+\.", ln)]) for c in cards
    )
    if shown < total_items and cards:
        text, alert, color = cards[-1]
        remaining = total_items - shown
        text += f"\n\n_另有 {remaining} 条较次要资讯，可稍后在财联社 / 华尔街见闻查看_"
        cards[-1] = (text, alert, color)
    return cards[:MAX_CARDS_PER_RUN]


def send_card(text: str, alert: bool, color: str, title: str) -> None:
    hook = webhook_url()
    if not hook:
        print("未配置 FEISHU_WEBHOOK_URL，跳过推送，仅打印：")
        print(text)
        return
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": text}}]
    if alert:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "<at id=all></at>"}})
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
            "elements": elements,
        },
    }
    try:
        resp = requests.post(hook, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") not in (0, None):
            print("飞书推送返回异常：", result)
        else:
            print("飞书推送成功")
    except Exception as ex:
        print("推送失败：", ex)
    time.sleep(0.3)


def minutes_since(dt: datetime, hh: int, mm: int) -> float:
    slot = dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (dt - slot).total_seconds() / 60.0


def detect_mode(now: datetime, slots_done: list[str], force_test: bool) -> tuple[str, str]:
    """返回 (mode, slot_id)。mode: test / trading / weekend / breaking / quiet"""
    if force_test:
        return "test", f"test-{now.strftime('%Y-%m-%dT%H%M')}"

    d = now.date()
    trading = is_trading_day(d)

    if trading:
        for hh, mm in HOURLY_SLOTS + [CLOSE_SLOT]:
            elapsed = minutes_since(now, hh, mm)
            if 0 <= elapsed <= SLOT_GRACE_MIN:
                slot_id = f"{d.isoformat()}T{hh:02d}:{mm:02d}"
                if slot_id in slots_done:
                    return "breaking", slot_id
                return "trading", slot_id

    if (not trading) and now.weekday() == 6:
        elapsed = minutes_since(now, WEEKEND_DIGEST[0], WEEKEND_DIGEST[1])
        if 0 <= elapsed <= SUNDAY_DIGEST_GRACE_MIN:
            slot_id = f"{d.isoformat()}T19:00"
            if slot_id in slots_done:
                return "breaking", slot_id
            return "weekend", slot_id

    return "breaking", ""


def weekend_window_start(now: datetime) -> float:
    d = now.date()
    # 本周日 19:00 回顾：从本周六 00:00 起
    saturday = d - timedelta(days=(d.weekday() - 5) % 7)
    if d.weekday() == 6:
        saturday = d - timedelta(days=1)
    start = datetime.combine(saturday, dtime(0, 0), TZ)
    return start.timestamp()


def merge_pending(state: dict, new_items: list[dict]) -> list[dict]:
    pending = state.get("_pending") or []
    by_id = {it.get("id"): it for it in pending if it.get("id")}
    for it in new_items:
        by_id[it["id"]] = it
    return list(by_id.values())


def push_items(items: list[dict], title: str) -> None:
    cards = build_card_bodies(items)
    total = len(cards)
    for idx, (text, alert, color) in enumerate(cards, 1):
        page = f" ({idx}/{total})" if total > 1 else ""
        send_card(text, alert, color, title + page)
    if not cards:
        print("没有可推送条目")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="盘中财经快讯 V6")
    p.add_argument("--test", action="store_true", help="忽略时段，强制推一张样卡")
    p.add_argument("--dry-run", action="store_true", help="抓取分类但不写状态、不推飞书")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    now = now_bj()
    print(f"北京时间 {now.strftime('%Y-%m-%d %H:%M:%S')} 交易日={is_trading_day(now.date())}")

    state = load_state()
    has_history = any(state.get(k) for k, _, _ in SOURCES)
    slots_done = list(state.get("_slots_done") or [])
    mode, slot_id = detect_mode(now, slots_done, args.test)
    print(f"模式={mode} 槽={slot_id or '-'}")

    seed_only = (not has_history) and (not args.test)
    new_items = collect_new(state, seed_only=seed_only)

    if args.dry_run:
        print(f"dry-run：新条目 {len(new_items)}，不写盘不推送")
        for it in new_items:
            print(f"  [{it['cat_label']}] {it['title']} hot={is_hot(it)} brk={is_breaking(it)}")
        return 0

    if seed_only:
        state["_pending"] = []
        save_state(state)
        print("首次运行只记历史，不推送")
        return 0

    pending = merge_pending(state, new_items)

    if mode == "test":
        batch = pending[-MAX_ITEMS_PER_CARD:] if pending else new_items
        push_items(batch, "📊 财经快讯试推")
        state["_pending"] = []
        if slot_id:
            slots_done.append(slot_id)
        state["_slots_done"] = slots_done[-40:]
        save_state(state)
        return 0

    if mode == "trading":
        push_items(pending, "📊 财经快讯速览")
        state["_pending"] = []
        slots_done.append(slot_id)
        state["_slots_done"] = slots_done[-40:]
        save_state(state)
        return 0

    if mode == "weekend":
        start_ts = weekend_window_start(now)
        batch = [it for it in pending if is_hot(it) and it.get("ts", 0) >= start_ts]
        batch_ids = {it.get("id") for it in batch}
        leftover = [it for it in pending if it.get("id") not in batch_ids]
        push_items(batch, "📊 周末热点速览")
        state["_pending"] = leftover
        slots_done.append(slot_id)
        state["_slots_done"] = slots_done[-40:]
        save_state(state)
        return 0

    # breaking / quiet：仅战争/重要政策热点立刻推，其余进待发
    breaking = [it for it in pending if is_breaking(it)]
    rest = [it for it in pending if not is_breaking(it)]
    if breaking:
        push_items(breaking, "🚨 热点快讯")
        state["_pending"] = rest
    else:
        state["_pending"] = pending
        print("非推送窗口，无战争/重要政策热点，已写入待发")
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
