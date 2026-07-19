# -*- coding: utf-8 -*-
"""
收盘后复盘播报（升级版）：
1. 抓取大盘概况、板块表现、资金流向、龙虎榜净买入前十
2. 生成一份图文并茂的 PDF 报告，存入仓库 reports/ 目录
3. 飞书推送一张精简摘要卡片，附带"查看完整PDF报告"的链接（指向 GitHub 上的 PDF）

数据来源：东方财富（通过开源库 akshare 调用，公开数据，无需注册/密钥）
"""

import json
import os
import time
import datetime
import requests

import matplotlib
matplotlib.use("Agg")  # 无图形界面环境下渲染图表
import matplotlib.pyplot as plt

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image, PageBreak
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

try:
    import akshare as ak
except Exception as ex:
    print(f"akshare 导入失败：{ex}")
    raise

# ========== 中文字体配置 ==========
import matplotlib.font_manager as fm

_available_fonts = {f.name for f in fm.fontManager.ttflist}
_cjk_candidates = [
    "Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans CJK TC", "Noto Sans CJK HK",
    "Noto Sans CJK KR", "WenQuanYi Zen Hei", "SimHei", "Microsoft YaHei",
]
_chosen_font = next((c for c in _cjk_candidates if c in _available_fonts), None)
if _chosen_font:
    plt.rcParams["font.sans-serif"] = [_chosen_font]
    print(f"matplotlib 中文字体使用：{_chosen_font}")
else:
    print(f"⚠️ 未找到可用中文字体，图表中文可能显示为方块。可用字体样例：{sorted(_available_fonts)[:20]}")
plt.rcParams["axes.unicode_minus"] = False

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

# ========== 配置区 ==========
STATE_FILE = "lhb_sent.json"
FEISHU_WEBHOOK_LHB = os.environ.get("FEISHU_WEBHOOK_LHB", "").strip()

GITHUB_REPO = "2006yuanttyy-ctrl/news-to-feishu"
REPORTS_DIR = "reports"
CHARTS_DIR = "_charts_tmp"

TEST_DATE = os.environ.get("TEST_DATE", "").strip()
IS_TEST_MODE = bool(TEST_DATE)

if IS_TEST_MODE:
    TODAY = datetime.datetime.strptime(TEST_DATE, "%Y%m%d").date()
    print(f"⚠️ 测试模式：使用指定日期 {TEST_DATE} 代替今天，不受周末/重复推送限制")
else:
    TODAY = datetime.date.today()

TODAY_STR = TODAY.strftime("%Y%m%d")
TODAY_DISPLAY = TODAY.strftime("%Y年%m月%d日")

UP_COLOR = "#d9534f"
DOWN_COLOR = "#5cb85c"


def load_seen_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_seen_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_weekday():
    return TODAY.weekday() < 5


def fmt_pct(x):
    try:
        return f"{float(x):+.2f}%"
    except Exception:
        return "N/A"


def fmt_amount_yi(x):
    try:
        v = float(x)
    except Exception:
        return "N/A"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿"
    return f"{v / 1e4:.1f}万"


def fetch_index_overview():
    result = {"indices": [], "total_amount": None, "zt_count": None, "dt_count": None}

    try:
        sh_df = ak.stock_zh_index_spot_em(symbol="上证系列指数")
        row = sh_df[sh_df["代码"] == "000001"]
        if not row.empty:
            r = row.iloc[0]
            result["indices"].append(("上证指数", float(r["最新价"]), float(r["涨跌幅"])))
            if "成交额" in row.columns:
                result["total_amount"] = (result["total_amount"] or 0) + float(r["成交额"])
    except Exception as ex:
        print(f"抓取上证指数失败：{ex}")

    try:
        sz_df = ak.stock_zh_index_spot_em(symbol="深证系列指数")
        for code, name in [("399001", "深证成指"), ("399006", "创业板指")]:
            row = sz_df[sz_df["代码"] == code]
            if not row.empty:
                r = row.iloc[0]
                result["indices"].append((name, float(r["最新价"]), float(r["涨跌幅"])))
                if code == "399001" and "成交额" in row.columns:
                    result["total_amount"] = (result["total_amount"] or 0) + float(r["成交额"])
    except Exception as ex:
        print(f"抓取深证/创业板指数失败：{ex}")

    try:
        zt_df = ak.stock_zt_pool_em(date=TODAY_STR)
        result["zt_count"] = len(zt_df)
    except Exception as ex:
        print(f"抓取涨停家数失败：{ex}")

    try:
        dt_df = ak.stock_zt_pool_dtgc_em(date=TODAY_STR)
        result["dt_count"] = len(dt_df)
    except Exception as ex:
        print(f"抓取跌停家数失败：{ex}")

    return result


def fetch_sector_performance(top_n=10):
    try:
        df = ak.stock_board_industry_name_em()
    except Exception as ex:
        print(f"抓取板块行情失败：{ex}")
        return None

    if df is None or df.empty or "涨跌幅" not in df.columns:
        print(f"板块数据为空或列名不符，实际列名：{list(df.columns) if df is not None else None}")
        return None

    name_col = "板块名称" if "板块名称" in df.columns else df.columns[1]
    df_sorted = df.sort_values(by="涨跌幅", ascending=False)
    gainers = df_sorted.head(top_n)[[name_col, "涨跌幅"]].values.tolist()
    losers = df_sorted.tail(top_n)[[name_col, "涨跌幅"]].values.tolist()[::-1]
    return {"gainers": gainers, "losers": losers}


def fetch_fund_flow(top_n=10):
    try:
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
    except Exception as ex:
        print(f"抓取资金流向失败：{ex}")
        return None

    if df is None or df.empty:
        print("资金流向数据为空")
        return None

    net_col = None
    for cand in df.columns:
        if "主力净流入" in cand and "净额" in cand:
            net_col = cand
            break
    if net_col is None:
        print(f"资金流向列名不符，实际列名：{list(df.columns)}")
        return None

    name_col = "名称" if "名称" in df.columns else None
    code_col = "代码" if "代码" in df.columns else None
    if name_col is None:
        print("资金流向数据缺少'名称'列")
        return None

    df_sorted = df.sort_values(by=net_col, ascending=False).head(top_n)
    rows = []
    for _, r in df_sorted.iterrows():
        rows.append((
            r.get(name_col, ""),
            r.get(code_col, "") if code_col else "",
            float(r.get(net_col, 0) or 0),
        ))
    return rows


def fetch_lhb_top(top_n=10):
    try:
        df = ak.stock_lhb_detail_em(start_date=TODAY_STR, end_date=TODAY_STR)
    except Exception as ex:
        print(f"抓取龙虎榜数据失败：{ex}")
        return None

    if df is None or df.empty:
        print("今天龙虎榜数据暂时还没有")
        return []

    net_buy_col = "龙虎榜净买额" if "龙虎榜净买额" in df.columns else None
    if net_buy_col is None:
        print(f"龙虎榜数据列名不符，实际列名：{list(df.columns)}")
        return []

    df_sorted = df.sort_values(by=net_buy_col, ascending=False).head(top_n)
    rows = []
    for _, r in df_sorted.iterrows():
        rows.append({
            "name": r.get("名称", ""),
            "code": r.get("代码", ""),
            "pct": r.get("涨跌幅", None),
            "net_buy": float(r.get(net_buy_col, 0) or 0),
            "reason": r.get("上榜原因", ""),
        })
    return rows


def save_bar_chart_h(pairs, title, filepath, unit=""):
    if not pairs:
        return False
    labels = [p[0] for p in pairs][::-1]
    values = [float(p[1]) for p in pairs][::-1]
    colors_ = [UP_COLOR if v >= 0 else DOWN_COLOR for v in values]

    fig, ax = plt.subplots(figsize=(7, max(2.5, 0.4 * len(labels))))
    bars = ax.barh(labels, values, color=colors_)
    ax.set_title(title, fontsize=13)
    ax.axvline(0, color="#888", linewidth=0.8)
    for bar, v in zip(bars, values):
        ax.text(
            v, bar.get_y() + bar.get_height() / 2,
            f" {v:+.2f}{unit}", va="center",
            ha="left" if v >= 0 else "right", fontsize=9,
        )
    fig.tight_layout()
    fig.savefig(filepath, dpi=150)
    plt.close(fig)
    return True


def save_index_chart(indices, filepath):
    if not indices:
        return False
    labels = [i[0] for i in indices]
    pcts = [i[2] for i in indices]
    colors_ = [UP_COLOR if v >= 0 else DOWN_COLOR for v in pcts]

    fig, ax = plt.subplots(figsize=(6, 3.2))
    bars = ax.bar(labels, pcts, color=colors_, width=0.5)
    ax.axhline(0, color="#888", linewidth=0.8)
    ax.set_title("三大指数涨跌幅", fontsize=13)
    for bar, v in zip(bars, pcts):
        ax.text(bar.get_x() + bar.get_width() / 2, v, f"{v:+.2f}%",
                 ha="center", va="bottom" if v >= 0 else "top", fontsize=10)
    fig.tight_layout()
    fig.savefig(filepath, dpi=150)
    plt.close(fig)
    return True


def build_pdf(pdf_path, overview, sector, fund_flow, lhb, chart_paths):
    styles = getSampleStyleSheet()
    for name in ["Normal", "Heading1", "Heading2", "Title"]:
        styles[name].fontName = "STSong-Light"

    title_style = ParagraphStyle("cn_title", parent=styles["Title"], fontName="STSong-Light", fontSize=20)
    h2_style = ParagraphStyle("cn_h2", parent=styles["Heading2"], fontName="STSong-Light", fontSize=14,
                                spaceBefore=14, spaceAfter=8, textColor=colors.HexColor("#1a3d7c"))
    body_style = ParagraphStyle("cn_body", parent=styles["Normal"], fontName="STSong-Light", fontSize=10, leading=15)

    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                             topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                             leftMargin=1.8 * cm, rightMargin=1.8 * cm)
    story = []

    story.append(Paragraph(f"{TODAY_DISPLAY} 收盘复盘报告", title_style))
    story.append(Spacer(1, 0.5 * cm))

    story.append(Paragraph("一、大盘概况", h2_style))
    if overview["indices"]:
        idx_text = "　".join(
            f"{n} {p:.2f}（{fmt_pct(c)}）" for n, p, c in overview["indices"]
        )
        story.append(Paragraph(idx_text, body_style))
    extra_bits = []
    if overview.get("total_amount"):
        extra_bits.append(f"两市成交额：约 {fmt_amount_yi(overview['total_amount'])}元")
    if overview.get("zt_count") is not None:
        extra_bits.append(f"涨停：{overview['zt_count']}家")
    if overview.get("dt_count") is not None:
        extra_bits.append(f"跌停：{overview['dt_count']}家")
    if extra_bits:
        story.append(Paragraph("　｜　".join(extra_bits), body_style))
    if chart_paths.get("index"):
        story.append(Spacer(1, 0.3 * cm))
        story.append(Image(chart_paths["index"], width=14 * cm, height=7 * cm))

    story.append(Paragraph("二、板块表现", h2_style))
    if sector:
        if chart_paths.get("sector_gainers"):
            story.append(Paragraph("涨幅前十板块", body_style))
            story.append(Image(chart_paths["sector_gainers"], width=15 * cm,
                                 height=0.6 * cm * len(sector["gainers"]) + 2 * cm))
        if chart_paths.get("sector_losers"):
            story.append(Paragraph("跌幅前十板块", body_style))
            story.append(Image(chart_paths["sector_losers"], width=15 * cm,
                                 height=0.6 * cm * len(sector["losers"]) + 2 * cm))
    else:
        story.append(Paragraph("（本节数据暂缺）", body_style))

    story.append(PageBreak())

    story.append(Paragraph("三、资金流向（主力净流入前十）", h2_style))
    if fund_flow:
        if chart_paths.get("fund_flow"):
            story.append(Image(chart_paths["fund_flow"], width=15 * cm,
                                 height=0.6 * cm * len(fund_flow) + 2 * cm))
    else:
        story.append(Paragraph("（本节数据暂缺）", body_style))

    story.append(Spacer(1, 0.5 * cm))

    story.append(Paragraph("四、龙虎榜净买入前十", h2_style))
    if lhb:
        table_data = [["排名", "名称", "代码", "涨跌幅", "净买入", "上榜原因"]]
        for i, item in enumerate(lhb, 1):
            table_data.append([
                str(i), item["name"], item["code"], fmt_pct(item["pct"]),
                fmt_amount_yi(item["net_buy"]),
                Paragraph(str(item["reason"])[:40], body_style),
            ])
        t = Table(table_data, colWidths=[1.2 * cm, 2.5 * cm, 1.8 * cm, 1.8 * cm, 2 * cm, 6.5 * cm])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3d7c")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
        ]))
        story.append(t)
        if chart_paths.get("lhb"):
            story.append(Spacer(1, 0.4 * cm))
            story.append(Image(chart_paths["lhb"], width=15 * cm, height=0.6 * cm * len(lhb) + 2 * cm))
    else:
        story.append(Paragraph("（今日无龙虎榜数据）", body_style))

    story.append(Spacer(1, 1 * cm))
    story.append(Paragraph(
        "数据来源：东方财富（经 akshare 抓取），仅供参考，不构成投资建议。",
        ParagraphStyle("footer", parent=body_style, fontSize=8, textColor=colors.grey),
    ))

    doc.build(story)


def send_summary_card(overview, lhb, pdf_url):
    if not FEISHU_WEBHOOK_LHB:
        print("未配置 FEISHU_WEBHOOK_LHB，跳过推送")
        return False

    lines = []
    if overview["indices"]:
        lines.append("　".join(f"{n} {fmt_pct(c)}" for n, p, c in overview["indices"]))
    bits = []
    if overview.get("total_amount"):
        bits.append(f"成交额 {fmt_amount_yi(overview['total_amount'])}元")
    if overview.get("zt_count") is not None:
        bits.append(f"涨停{overview['zt_count']}家")
    if overview.get("dt_count") is not None:
        bits.append(f"跌停{overview['dt_count']}家")
    if bits:
        lines.append("　".join(bits))

    if lhb:
        lines.append("")
        lines.append("**龙虎榜净买入前三：**")
        for item in lhb[:3]:
            lines.append(f"· {item['name']}（{item['code']}）净买入 {fmt_amount_yi(item['net_buy'])}元")

    content_text = "\n".join(lines) if lines else "今日数据获取不完整，详见PDF报告"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📊 {TODAY_DISPLAY} 收盘复盘"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content_text}},
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [{
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "📄 查看完整PDF报告"},
                        "type": "primary",
                        "url": pdf_url,
                    }],
                },
            ],
        },
    }

    try:
        resp = requests.post(FEISHU_WEBHOOK_LHB, json=card, timeout=10)
        result = resp.json()
        if result.get("code") not in (0, None):
            print("飞书推送返回异常：", result)
            return False
        return True
    except Exception as ex:
        print("推送失败：", ex)
        return False


def main():
    if not IS_TEST_MODE and not is_weekday():
        print("今天是周末，跳过")
        return

    state = load_seen_state()
    if not IS_TEST_MODE and state.get("last_sent_date") == TODAY_STR:
        print("今天已经推送过复盘，跳过")
        return

    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(CHARTS_DIR, exist_ok=True)

    print("开始抓取大盘概况...")
    overview = fetch_index_overview()

    print("开始抓取板块表现...")
    sector = fetch_sector_performance()

    print("开始抓取资金流向...")
    fund_flow = fetch_fund_flow()

    print("开始抓取龙虎榜...")
    lhb = fetch_lhb_top()

    if lhb is None:
        print("龙虎榜抓取失败（网络或接口问题），本次不推送，等下次自动重试")
        return
    if not lhb:
        print("今天龙虎榜数据暂未发布，本次不推送，等下次自动重试")
        return

    print("生成图表...")
    chart_paths = {}
    if save_index_chart(overview["indices"], f"{CHARTS_DIR}/index.png"):
        chart_paths["index"] = f"{CHARTS_DIR}/index.png"
    if sector:
        if save_bar_chart_h(sector["gainers"], "涨幅前十板块", f"{CHARTS_DIR}/sector_gainers.png", "%"):
            chart_paths["sector_gainers"] = f"{CHARTS_DIR}/sector_gainers.png"
        if save_bar_chart_h(sector["losers"], "跌幅前十板块", f"{CHARTS_DIR}/sector_losers.png", "%"):
            chart_paths["sector_losers"] = f"{CHARTS_DIR}/sector_losers.png"
    if fund_flow:
        pairs = [(f"{n}({c})", v / 1e8) for n, c, v in fund_flow]
        if save_bar_chart_h(pairs, "主力净流入前十（亿元）", f"{CHARTS_DIR}/fund_flow.png", "亿"):
            chart_paths["fund_flow"] = f"{CHARTS_DIR}/fund_flow.png"
    if lhb:
        pairs = [(f"{i['name']}({i['code']})", i["net_buy"] / 1e8) for i in lhb]
        if save_bar_chart_h(pairs, "龙虎榜净买入前十（亿元）", f"{CHARTS_DIR}/lhb.png", "亿"):
            chart_paths["lhb"] = f"{CHARTS_DIR}/lhb.png"

    pdf_filename = f"fupan_{TODAY_STR}.pdf"
    pdf_path = f"{REPORTS_DIR}/{pdf_filename}"
    print(f"生成 PDF：{pdf_path}")
    build_pdf(pdf_path, overview, sector, fund_flow, lhb, chart_paths)

    pdf_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{REPORTS_DIR}/{pdf_filename}"
    print(f"PDF 访问链接：{pdf_url}")

    ok = send_summary_card(overview, lhb, pdf_url)
    if ok:
        if not IS_TEST_MODE:
            state["last_sent_date"] = TODAY_STR
            save_seen_state(state)
        print("推送成功")
    else:
        print("推送失败，不记录，等下次自动重试")


if __name__ == "__main__":
    main()
