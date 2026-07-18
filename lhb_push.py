# -*- coding: utf-8 -*-
"""
收盘后复盘播报：大盘概况 + 龙虎榜净买入前十，推送到独立的飞书机器人。

数据来源：东方财富（通过开源库 akshare 调用，公开数据，无需注册/密钥）
运行节奏：交易日 17:00-19:30 期间，每隔一段时间检查一次；
          如果当天数据还没发布，就先不推，下次再看；
          一旦当天已经推送过，就不会重复推送。
"""

import json
import os
import time
import datetime
import requests

try:
    import akshare as ak
except Exception as ex:
    print(f"akshare 导入失败：{ex}")
    raise

STATE_FILE = "lhb_sent.json"  # 记录"今天有没有推送过"，避免重复推送
FEISHU_WEBHOOK_LHB = os.environ.get("FEISHU_WEBHOOK_LHB", "").strip()

TODAY = datetime.date.today()
TODAY_STR = TODAY.strftime("%Y%m%d")          # 例如 20260713，给 akshare 用
TODAY_DISPLAY = TODAY.strftime("%Y年%m月%d日")  # 给消息展示用


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_weekday():
    # 0=周一 ... 6=周日；周末直接跳过（不处理法定节假日，节假日当天数据本来也抓不到，等同于自动跳过）
    return TODAY.weekday() < 5


def fmt_pct(x):
    try:
        return f"{float(x):+.2f}%"
    except Exception:
        return "N/A"


def fmt_amount_yi(x):
    """把"元"换算成"亿元"，方便阅读"""
    try:
        return f"{float(x) / 1e8:.1f}亿"
    except Exception:
        return "N/A"


def get_index_overview():
    """大盘概况：上证指数、深证成指、创业板指 的收盘点位和涨跌幅，以及两市成交额"""
    lines = []
    total_amount = 0.0
    got_amount = False

    try:
        sh_df = ak.stock_zh_index_spot_em(symbol="上证系列指数")
        row = sh_df[sh_df["代码"] == "000001"]
        if not row.empty:
            r = row.iloc[0]
            lines.append(f"上证指数 {float(r['最新价']):.2f}（{fmt_pct(r['涨跌幅'])}）")
            if "成交额" in row.columns:
                total_amount += float(r["成交额"])
                got_amount = True
    except Exception as ex:
        print(f"抓取上证指数失败：{ex}")

    try:
        sz_df = ak.stock_zh_index_spot_em(symbol="深证系列指数")
        for code, name in [("399001", "深证成指"), ("399006", "创业板指")]:
            row = sz_df[sz_df["代码"] == code]
            if not row.empty:
                r = row.iloc[0]
                lines.append(f"{name} {float(r['最新价']):.2f}（{fmt_pct(r['涨跌幅'])}）")
                if code == "399001" and "成交额" in row.columns:
                    total_amount += float(r["成交额"])
                    got_amount = True
    except Exception as ex:
        print(f"抓取深证/创业板指数失败：{ex}")

    if got_amount:
        lines.append(f"两市成交额：约 {fmt_amount_yi(total_amount)}元")

    try:
        zt_df = ak.stock_zt_pool_em(date=TODAY_STR)
        lines.append(f"涨停：{len(zt_df)}家")
    except Exception as ex:
        print(f"抓取涨停家数失败：{ex}")

    try:
        dt_df = ak.stock_zt_pool_dtgc_em(date=TODAY_STR)
        lines.append(f"跌停：{len(dt_df)}家")
    except Exception as ex:
        print(f"抓取跌停家数失败：{ex}")

    return lines


def get_lhb_top10():
    """龙虎榜净买入前十"""
    try:
        df = ak.stock_lhb_detail_em(start_date=TODAY_STR, end_date=TODAY_STR)
    except Exception as ex:
        print(f"抓取龙虎榜数据失败：{ex}")
        return None  # None 表示"抓取失败"，跟"抓到但是空"要区分开

    if df is None or df.empty:
        print("今天龙虎榜数据暂时还没有（可能还没发布，或者今天没有龙虎榜）")
        return []

    net_buy_col = "龙虎榜净买额" if "龙虎榜净买额" in df.columns else None
    if net_buy_col is None:
        print(f"龙虎榜数据列名跟预期不一致，实际列名：{list(df.columns)}")
        return []

    df_sorted = df.sort_values(by=net_buy_col, ascending=False).head(10)

    lines = []
    for i, (_, r) in enumerate(df_sorted.iterrows(), 1):
        name = r.get("名称", "")
        code = r.get("代码", "")
        pct = fmt_pct(r.get("涨跌幅", ""))
        net_buy = fmt_amount_yi(r.get(net_buy_col, 0)).replace("亿", "万") \
            if abs(float(r.get(net_buy_col, 0) or 0)) < 1e8 else fmt_amount_yi(r.get(net_buy_col, 0))
        reason = r.get("上榜原因", "")
        lines.append(f"{i}. {name}（{code}）净买入 {net_buy}元，{pct}，上榜原因：{reason}")
    return lines


def send_to_feishu(text):
    if not FEISHU_WEBHOOK_LHB:
        print("未配置 FEISHU_WEBHOOK_LHB，跳过推送，仅打印：")
        print(text)
        return False

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 {TODAY_DISPLAY} 收盘复盘"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            ],
        },
    }
    try:
        resp = requests.post(FEISHU_WEBHOOK_LHB, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") not in (0, None):
            print("飞书推送返回异常：", result)
            return False
        return True
    except Exception as ex:
        print("推送失败：", ex)
        return False


def main():
    if not is_weekday():
        print("今天是周末，跳过")
        return

    state = load_state()
    if state.get("last_sent_date") == TODAY_STR:
        print("今天已经推送过复盘，跳过")
        return

    print("开始抓取大盘概况...")
    overview_lines = get_index_overview()

    print("开始抓取龙虎榜...")
    lhb_lines = get_lhb_top10()

    if lhb_lines is None:
        print("龙虎榜抓取失败（网络或接口问题），本次不推送，等下次自动重试")
        return

    if not lhb_lines:
        print("龙虎榜数据暂未发布，本次不推送，等下次自动重试")
        return

    text_parts = []
    if overview_lines:
        text_parts.append("**大盘概况**\n" + "\n".join(overview_lines))
    text_parts.append("**🔥 龙虎榜净买入前十**\n" + "\n".join(lhb_lines))
    full_text = "\n\n".join(text_parts)

    ok = send_to_feishu(full_text)
    if ok:
        state["last_sent_date"] = TODAY_STR
        save_state(state)
        print("推送成功，已记录今天已推送")
    else:
        print("推送失败，不记录，等下次自动重试")


if __name__ == "__main__":
    main()
