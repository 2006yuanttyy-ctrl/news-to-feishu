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
import sys
import time
import requests
import feedparser

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

# 每类消息单次最多推送几条，防止第一次运行时刷屏
MAX_PUSH_PER_SOURCE = 8

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
                t = e.get("published_parsed") or e.get("updated_parsed")
                ts = time.mktime(t) if t else time.time()
                entries.append((entry_id, title, link, ts))
            entries.sort(key=lambda x: x[3])
            print(f"  ✔ 从 {base} 成功抓到 {len(entries)} 条")
            return entries
        except Exception as ex:
            last_error = str(ex)
            print(f"  镜像 {base} 抓取出错：{ex}")
            continue

    raise RuntimeError(f"所有镜像均抓取失败，最后一次错误：{last_error}")


def send_to_feishu(text, is_important=False):
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
                    "title": {"tag": "plain_text", "content": "🔴 财联社重要电报"},
                    "template": "red",
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
    new_entries = [e for e in entries if e[0] not in seen_ids]

    # 首次运行（seen_ids 为空）时不刷屏，只记录不推送
    first_run = len(seen_ids) == 0

    to_push = new_entries[-MAX_PUSH_PER_SOURCE:] if new_entries else []

    for entry_id, title, link, ts in new_entries:
        seen_ids.add(entry_id)

    if not first_run:
        for entry_id, title, link, ts in to_push:
            text = f"{label}\n{title}\n{link}" if link else f"{label}\n{title}"
            send_to_feishu(text, is_important=is_important)
    else:
        print(f"{label} 首次运行，记录 {len(new_entries)} 条历史消息，不推送")

    # 只保留最近 500 条，防止文件无限增大
    seen[source_key] = list(seen_ids)[-500:]


def main():
    seen = load_seen()

    process_source(CLS_RED_PATH, seen, "cls_red", "🔴 财联社重要电报", is_important=True)

    save_seen(seen)


if __name__ == "__main__":
    main()
