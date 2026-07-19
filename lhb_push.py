# -*- coding: utf-8 -*-
"""
收盘后复盘播报（完整版）：
一、市场总览：三大指数涨跌幅 + 上证指数分时图 + 上证指数日K线图 + 成交额/涨跌停家数
二、资金进攻方向：板块资金净流入前十
三、资金撤退方向：板块资金净流出前十
四、龙虎榜机构动作：机构净买入TOP10 / 机构净卖出TOP10
五、情绪指标：涨停/跌停家数 + 最高连板股票
六、明日关注：黄金/有色金属/碳酸锂/军工，按今日涨跌幅+资金流向自动打标签（🔴强趋势/🟡观察/🟢风险）
附：龙虎榜明细（净买入前十，含上榜原因）

数据来源：东方财富（通过开源库 akshare 调用，公开数据，无需注册/密钥）
生成一份图文并茂的 PDF 报告存入仓库 reports/ 目录，飞书推送精简摘要卡片 + PDF链接
"""

import json
import os
import time
import datetime
import requests
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import mplfinance as mpf

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

WATCH_KEYWORDS = ["黄金", "有色金属", "碳酸锂", "军工"]

TEST_DATE = os.environ.get("TEST_DATE", "").strip()
IS_TEST_MODE = bool(TEST_DATE)

if IS_TEST_MODE:
    TODAY = datetime.datetime.strptime(TEST_DATE, "%Y%m%d").date()
    print(f"⚠️ 测试模式：使用指定日期 {TEST_DATE} 代替今天，不受周末/重复推送限制")
else:
    TODAY = datetime.date.today()

TODAY_STR = TODAY.strftime("%Y%m%d")
TODAY_DISPLAY = TODAY.strftime("%Y年%m月%d日")

UP_COLOR = "#d9534f"    # 红涨
DOWN_COLOR = "#5cb85c"  # 绿跌


# ========== 工具函数 ==========

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


# ========== 一、市场总览 ==========

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


def fetch_index_intraday(symbol="000001"):
    """当日分时数据"""
    try:
        df = ak.index_zh_a_hist_min_em(
            symbol=symbol, period="1",
            start_date=f"{TODAY_STR} 09:00:00", end_date=f"{TODAY_STR} 15:30:00",
        )
    except Exception as ex:
        print(f"抓取指数分时数据失败：{ex}")
        return None
    if df is None or df.empty:
        print("指数分时数据为空")
        return None
    return df


def fetch_index_daily_k(symbol="000001", days=60):
    """近N个交易日的日K线数据"""
    try:
        start = TODAY - datetime.timedelta(days=int(days * 2.2) + 10)
        df = ak.index_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start.strftime("%Y%m%d"), end_date=TODAY_STR,
        )
    except Exception as ex:
        print(f"抓取指数日K线失败：{ex}")
        return None
    if df is None or df.empty:
        print("指数日K线数据为空")
        return None
    return df.tail(days)


def save_index_pct_chart(indices, filepath):
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


def save_intraday_chart(df, filepath, title="上证指数 当日分时走势"):
    if df is None or df.empty:
        return False
    time_col = "时间" if "时间" in df.columns else df.columns[0]
    price_col = None
    for cand in ["收盘", "最新价", "close"]:
        if cand in df.columns:
            price_col = cand
            break
    if price_col is None:
        print(f"分时数据列名不符，实际列名：{list(df.columns)}")
        return False

    y = df[price_col].astype(float).tolist()
    if not y:
        return False
    base = y[0]
    line_color = UP_COLOR if y[-1] >= base else DOWN_COLOR
    x = list(range(len(y)))

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(x, y, color=line_color, linewidth=1.3)
    ax.fill_between(x, y, base, color=line_color, alpha=0.08)
    ax.axhline(base, color="#888", linewidth=0.8, linestyle="--")
    ax.set_title(title, fontsize=13)

    step = max(1, len(df) // 6)
    tick_idx = x[::step]
    tick_labels = []
    for i in tick_idx:
        raw = str(df[time_col].iloc[i])
        tick_labels.append(raw[-8:-3] if len(raw) >= 5 else raw)
    ax.set_xticks(tick_idx)
    ax.set_xticklabels(tick_labels)

    fig.tight_layout()
    fig.savefig(filepath, dpi=150)
    plt.close(fig)
    return True


def save_kline_chart(df, filepath, title="上证指数 近60日K线"):
    if df is None or df.empty:
        return False
    rename_map = {"开盘": "Open", "收盘": "Close", "最高": "High", "最低": "Low", "成交量": "Volume"}
    missing = [k for k in rename_map if k not in df.columns]
    if missing:
        print(f"日K线数据缺少列：{missing}，实际列名：{list(df.columns)}")
        return False

    df2 = df.copy().rename(columns=rename_map)
    date_col = "日期" if "日期" in df2.columns else df2.columns[0]
    df2.index = pd.to_datetime(df2[date_col])
    df2 = df2[["Open", "High", "Low", "Close", "Volume"]].astype(float)

    try:
        mc = mpf.make_marketcolors(up=UP_COLOR, down=DOWN_COLOR, edge="inherit",
                                    wick="inherit", volume="inherit")
        style = mpf.make_mpf_style(marketcolors=mc, rc={"font.sans-serif": plt.rcParams["font.sans-serif"]})
        fig, _ = mpf.plot(df2, type="candle", style=style, volume=True,
                            returnfig=True, figsize=(10, 6), title=title)
        fig.savefig(filepath, dpi=150)
        plt.close(fig)
        return True
    except Exception as ex:
        print(f"绘制K线图失败：{ex}")
        return False


# ========== 二/三、资金进攻/撤退方向（板块资金流） ==========

def fetch_sector_fund_flow(top_n=10):
    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
    except Exception as ex:
        print(f"抓取板块资金流失败：{ex}")
        return None

    if df is None or df.empty:
        print("板块资金流数据为空")
        return None

    net_col = None
    for cand in df.columns:
        if "净额" in cand and ("主力" in cand or "今日" in cand):
            net_col = cand
            break
    if net_col is None:
        for cand in df.columns:
            if "净额" in cand:
                net_col = cand
                break
    if net_col is None:
        print(f"板块资金流列名不符，实际列名：{list(df.columns)}")
        return None

    name_col = "名称" if "名称" in df.columns else df.columns[1]
    df_sorted = df.sort_values(by=net_col, ascending=False)
    inflow = df_sorted.head(top_n)[[name_col, net_col]].values.tolist()
    outflow = df_sorted.tail(top_n)[[name_col, net_col]].values.tolist()[::-1]
    return {"inflow": inflow, "outflow": outflow}


# ========== 四、龙虎榜机构动作 ==========

def fetch_lhb_institution(top_n=10):
    try:
        df = ak.stock_lhb_jgmmtj_em(start_date=TODAY_STR, end_date=TODAY_STR)
    except Exception as ex:
        print(f"抓取机构龙虎榜失败：{ex}")
        return None

    if df is None or df.empty:
        print("今天机构龙虎榜数据暂时还没有")
        return []

    net_col = None
    for cand in ["机构净买额", "净买额"]:
        if cand in df.columns:
            net_col = cand
            break
    if net_col is None:
        print(f"机构龙虎榜列名不符，实际列名：{list(df.columns)}")
        return []

    name_col = "名称" if "名称" in df.columns else None
    code_col = "代码" if "代码" in df.columns else None
    if name_col is None:
        print("机构龙虎榜数据缺少'名称'列")
        return []

    df_sorted = df.sort_values(by=net_col, ascending=False)

    def to_rows(d):
        rows = []
        for _, r in d.iterrows():
            rows.append((
                r.get(name_col, ""),
                r.get(code_col, "") if code_col else "",
                float(r.get(net_col, 0) or 0),
            ))
        return rows

    buy_top = to_rows(df_sorted.head(top_n))
    sell_top = to_rows(df_sorted.tail(top_n).sort_values(by=net_col))
    return {"buy_top": buy_top, "sell_top": sell_top}


# ========== 附：龙虎榜明细（原有功能保留） ==========

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


# ========== 五、情绪指标（连板） ==========

def fetch_zt_streak():
    try:
        df = ak.stock_zt_pool_em(date=TODAY_STR)
    except Exception as ex:
        print(f"抓取连板信息失败：{ex}")
        return None

    if df is None or df.empty:
        return None

    streak_col = None
    for cand in ["连板数", "连板天数"]:
        if cand in df.columns:
            streak_col = cand
            break
    if streak_col is None:
        print(f"涨停池数据里没找到连板列，实际列名：{list(df.columns)}")
        return None

    try:
        max_streak = int(df[streak_col].max())
    except Exception as ex:
        print(f"连板列取最大值失败：{ex}")
        return None

    top_stocks_df = df[df[streak_col] == max_streak]
    name_col = "名称" if "名称" in df.columns else None
    code_col = "代码" if "代码" in df.columns else None
    stocks = []
    for _, r in top_stocks_df.iterrows():
        stocks.append((r.get(name_col, "") if name_col else "", r.get(code_col, "") if code_col else ""))

    return {"max_streak": max_streak, "stocks": stocks}


# ========== 六、明日关注板块 ==========

def fetch_watch_sectors():
    try:
        industry_df = ak.stock_board_industry_name_em()
    except Exception as ex:
        print(f"抓取行业板块列表失败：{ex}")
        industry_df = None

    try:
        concept_df = ak.stock_board_concept_name_em()
    except Exception as ex:
        print(f"抓取概念板块列表失败：{ex}")
        concept_df = None

    try:
        concept_flow_df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="概念资金流")
    except Exception as ex:
        print(f"抓取概念板块资金流失败：{ex}")
        concept_flow_df = None

    concept_net_col = None
    if concept_flow_df is not None and not concept_flow_df.empty:
        for cand in concept_flow_df.columns:
            if "净额" in cand:
                concept_net_col = cand
                break

    results = []
    for kw in WATCH_KEYWORDS:
        matched_name, pct, net_amount = None, None, None

        if industry_df is not None and not industry_df.empty:
            name_col = "板块名称" if "板块名称" in industry_df.columns else industry_df.columns[1]
            hit = industry_df[industry_df[name_col].astype(str).str.contains(kw, na=False)]
            if not hit.empty:
                matched_name = hit.iloc[0][name_col]
                pct = hit.iloc[0].get("涨跌幅")

        if matched_name is None and concept_df is not None and not concept_df.empty:
            name_col2 = "板块名称" if "板块名称" in concept_df.columns else concept_df.columns[1]
            hit = concept_df[concept_df[name_col2].astype(str).str.contains(kw, na=False)]
            if not hit.empty:
                matched_name = hit.iloc[0][name_col2]
                pct = hit.iloc[0].get("涨跌幅")

        if matched_name is not None and concept_flow_df is not None and concept_net_col:
            name_col3 = "名称" if "名称" in concept_flow_df.columns else concept_flow_df.columns[1]
            hit2 = concept_flow_df[concept_flow_df[name_col3] == matched_name]
            if not hit2.empty:
                try:
                    net_amount = float(hit2.iloc[0][concept_net_col])
                except Exception:
                    net_amount = None

        tag = "🟡"
        if pct is not None:
            try:
                pctf = float(pct)
                if pctf > 1.5 and (net_amount is None or net_amount > 0):
                    tag = "🔴"
                elif pctf < -1.5 or (net_amount is not None and net_amount < -1e8):
                    tag = "🟢"
            except Exception:
                pass

        results.append({
            "keyword": kw, "matched": matched_name, "pct": pct,
            "net_amount": net_amount, "tag": tag,
        })

    return results


# ========== 通用横向柱状图 ==========

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


# ========== PDF 组装 ==========

def build_pdf(pdf_path, data, chart_paths):
    overview = data["overview"]
    sector_flow = data["sector_flow"]
    lhb_inst = data["lhb_inst"]
    lhb_detail = data["lhb_detail"]
    zt_streak = data["zt_streak"]
    watch_sectors = data["watch_sectors"]

    styles = getSampleStyleSheet()
    for name in ["Normal", "Heading1", "Heading2", "Title"]:
        styles[name].fontName = "STSong-Light"

    title_style = ParagraphStyle("cn_title", parent=styles["Title"], fontName="STSong-Light", fontSize=20)
    h2_style = ParagraphStyle("cn_h2", parent=styles["Heading2"], fontName="STSong-Light", fontSize=14,
                                spaceBefore=14, spaceAfter=8, textColor=colors.HexColor("#1a3d7c"))
    body_style = ParagraphStyle("cn_body", parent=styles["Normal"], fontName="STSong-Light", fontSize=10, leading=15)
    small_style = ParagraphStyle("cn_small", parent=styles["Normal"], fontName="STSong-Light", fontSize=9, leading=13)

    doc = SimpleDocTemplate(pdf_path, pagesize=A4,
                             topMargin=1.5 * cm, bottomMargin=1.5 * cm,
                             leftMargin=1.8 * cm, rightMargin=1.8 * cm)
    story = []

    story.append(Paragraph(f"{TODAY_DISPLAY} 收盘复盘报告", title_style))
    story.append(Spacer(1, 0.5 * cm))

    # ---- 一、市场总览 ----
    story.append(Paragraph("一、市场总览", h2_style))
    if overview["indices"]:
        idx_text = "　".join(f"{n} {p:.2f}（{fmt_pct(c)}）" for n, p, c in overview["indices"])
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
    if chart_paths.get("index_pct"):
        story.append(Spacer(1, 0.3 * cm))
        story.append(Image(chart_paths["index_pct"], width=14 * cm, height=7 * cm))
    if chart_paths.get("intraday"):
        story.append(Spacer(1, 0.3 * cm))
        story.append(Image(chart_paths["intraday"], width=16 * cm, height=6.4 * cm))
    if chart_paths.get("kline"):
        story.append(Spacer(1, 0.3 * cm))
        story.append(Image(chart_paths["kline"], width=16 * cm, height=9.6 * cm))

    story.append(PageBreak())

    # ---- 二/三、资金进攻/撤退方向 ----
    story.append(Paragraph("二、资金进攻方向（板块净流入前十）", h2_style))
    if sector_flow and chart_paths.get("sector_inflow"):
        story.append(Image(chart_paths["sector_inflow"], width=15 * cm,
                             height=0.6 * cm * len(sector_flow["inflow"]) + 2 * cm))
    else:
        story.append(Paragraph("（本节数据暂缺）", body_style))

    story.append(Paragraph("三、资金撤退方向（板块净流出前十）", h2_style))
    if sector_flow and chart_paths.get("sector_outflow"):
        story.append(Image(chart_paths["sector_outflow"], width=15 * cm,
                             height=0.6 * cm * len(sector_flow["outflow"]) + 2 * cm))
    else:
        story.append(Paragraph("（本节数据暂缺）", body_style))

    story.append(PageBreak())

    # ---- 四、龙虎榜机构动作 ----
    story.append(Paragraph("四、龙虎榜机构动作", h2_style))
    if lhb_inst:
        if chart_paths.get("lhb_inst_buy"):
            story.append(Paragraph("机构净买入 TOP10", body_style))
            story.append(Image(chart_paths["lhb_inst_buy"], width=15 * cm,
                                 height=0.6 * cm * len(lhb_inst["buy_top"]) + 2 * cm))
        if chart_paths.get("lhb_inst_sell"):
            story.append(Paragraph("机构净卖出 TOP10", body_style))
            story.append(Image(chart_paths["lhb_inst_sell"], width=15 * cm,
                                 height=0.6 * cm * len(lhb_inst["sell_top"]) + 2 * cm))
    else:
        story.append(Paragraph("（今日无机构龙虎榜数据）", body_style))

    story.append(PageBreak())

    # ---- 五、情绪指标 ----
    story.append(Paragraph("五、情绪指标", h2_style))
    mood_bits = []
    if overview.get("zt_count") is not None:
        mood_bits.append(f"涨停家数：{overview['zt_count']}家")
    if overview.get("dt_count") is not None:
        mood_bits.append(f"跌停家数：{overview['dt_count']}家")
    if mood_bits:
        story.append(Paragraph("　｜　".join(mood_bits), body_style))
    if zt_streak and zt_streak.get("stocks"):
        names = "、".join(f"{n}（{c}）" for n, c in zt_streak["stocks"])
        story.append(Paragraph(f"最高连板：{zt_streak['max_streak']}板 —— {names}", body_style))
    else:
        story.append(Paragraph("（连板数据暂缺）", body_style))

    story.append(Spacer(1, 0.5 * cm))

    # ---- 六、明日关注 ----
    story.append(Paragraph("六、明日关注板块", h2_style))
    if watch_sectors:
        table_data = [["板块", "匹配到的实际板块", "今日涨跌幅", "资金净流入", "标签"]]
        for w in watch_sectors:
            table_data.append([
                w["keyword"],
                w["matched"] or "未找到匹配板块",
                fmt_pct(w["pct"]) if w["pct"] is not None else "N/A",
                fmt_amount_yi(w["net_amount"]) if w["net_amount"] is not None else "N/A",
                w["tag"],
            ])
        t = Table(table_data, colWidths=[2.2 * cm, 4.5 * cm, 2.5 * cm, 2.8 * cm, 1.5 * cm])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3d7c")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (2, 1), (-1, -1), "CENTER"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.3 * cm))
        story.append(Paragraph(
            "标签说明：🔴强趋势＝今日上涨且资金净流入；🟢风险＝今日下跌或资金明显净流出；🟡观察＝其余情况。"
            "本标签仅基于当天涨跌幅与资金流向的客观数据规则自动生成，不构成投资建议，不代表基本面/消息面判断。",
            small_style,
        ))
    else:
        story.append(Paragraph("（本节数据暂缺）", body_style))

    story.append(PageBreak())

    # ---- 附：龙虎榜明细 ----
    story.append(Paragraph("附：龙虎榜明细（净买入前十）", h2_style))
    if lhb_detail:
        table_data = [["排名", "名称", "代码", "涨跌幅", "净买入", "上榜原因"]]
        for i, item in enumerate(lhb_detail, 1):
            table_data.append([
                str(i), item["name"], item["code"], fmt_pct(item["pct"]),
                fmt_amount_yi(item["net_buy"]),
                Paragraph(str(item["reason"])[:40], small_style),
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
    else:
        story.append(Paragraph("（今日无龙虎榜数据）", body_style))

    story.append(Spacer(1, 1 * cm))
    story.append(Paragraph(
        "数据来源：东方财富（经 akshare 抓取），仅供参考，不构成投资建议。",
        ParagraphStyle("footer", parent=body_style, fontSize=8, textColor=colors.grey),
    ))

    doc.build(story)


# ========== 飞书推送 ==========

def send_summary_card(overview, lhb_detail, zt_streak, watch_sectors, pdf_url):
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

    if zt_streak and zt_streak.get("stocks"):
        names = "、".join(f"{n}" for n, c in zt_streak["stocks"][:3])
        lines.append(f"最高连板 {zt_streak['max_streak']}板：{names}")

    if lhb_detail:
        lines.append("")
        lines.append("**龙虎榜净买入前三：**")
        for item in lhb_detail[:3]:
            lines.append(f"· {item['name']}（{item['code']}）净买入 {fmt_amount_yi(item['net_buy'])}元")

    if watch_sectors:
        lines.append("")
        lines.append("**明日关注：**")
        for w in watch_sectors:
            lines.append(f"{w['tag']} {w['keyword']}（{fmt_pct(w['pct']) if w['pct'] is not None else 'N/A'}）")

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

    print("===== 开始抓取各模块数据 =====")

    print("[1] 大盘概况...")
    overview = fetch_index_overview()

    print("[1] 上证指数分时数据...")
    intraday_df = fetch_index_intraday(symbol="000001")

    print("[1] 上证指数日K线数据...")
    kline_df = fetch_index_daily_k(symbol="000001", days=60)

    print("[2/3] 板块资金流向...")
    sector_flow = fetch_sector_fund_flow()

    print("[4] 机构龙虎榜...")
    lhb_inst = fetch_lhb_institution()

    print("[附] 龙虎榜明细...")
    lhb_detail = fetch_lhb_top()

    print("[5] 连板情绪指标...")
    zt_streak = fetch_zt_streak()

    print("[6] 明日关注板块...")
    watch_sectors = fetch_watch_sectors()

    if lhb_detail is None:
        print("龙虎榜明细抓取失败（网络或接口问题），本次不推送，等下次自动重试")
        return
    if not lhb_detail:
        print("今天龙虎榜数据暂未发布，本次不推送，等下次自动重试")
        return

    print("===== 开始生成图表 =====")
    chart_paths = {}
    if save_index_pct_chart(overview["indices"], f"{CHARTS_DIR}/index_pct.png"):
        chart_paths["index_pct"] = f"{CHARTS_DIR}/index_pct.png"
    if save_intraday_chart(intraday_df, f"{CHARTS_DIR}/intraday.png"):
        chart_paths["intraday"] = f"{CHARTS_DIR}/intraday.png"
    if save_kline_chart(kline_df, f"{CHARTS_DIR}/kline.png"):
        chart_paths["kline"] = f"{CHARTS_DIR}/kline.png"

    if sector_flow:
        if save_bar_chart_h(sector_flow["inflow"], "板块资金净流入前十", f"{CHARTS_DIR}/sector_inflow.png"):
            chart_paths["sector_inflow"] = f"{CHARTS_DIR}/sector_inflow.png"
        if save_bar_chart_h(sector_flow["outflow"], "板块资金净流出前十", f"{CHARTS_DIR}/sector_outflow.png"):
            chart_paths["sector_outflow"] = f"{CHARTS_DIR}/sector_outflow.png"

    if lhb_inst:
        if save_bar_chart_h(lhb_inst["buy_top"], "机构净买入TOP10（元）", f"{CHARTS_DIR}/lhb_inst_buy.png"):
            chart_paths["lhb_inst_buy"] = f"{CHARTS_DIR}/lhb_inst_buy.png"
        if save_bar_chart_h(lhb_inst["sell_top"], "机构净卖出TOP10（元）", f"{CHARTS_DIR}/lhb_inst_sell.png"):
            chart_paths["lhb_inst_sell"] = f"{CHARTS_DIR}/lhb_inst_sell.png"

    print("===== 生成 PDF =====")
    pdf_filename = f"fupan_{TODAY_STR}.pdf"
    pdf_path = f"{REPORTS_DIR}/{pdf_filename}"

    data = {
        "overview": overview,
        "sector_flow": sector_flow,
        "lhb_inst": lhb_inst,
        "lhb_detail": lhb_detail,
        "zt_streak": zt_streak,
        "watch_sectors": watch_sectors,
    }
    build_pdf(pdf_path, data, chart_paths)
    print(f"PDF 已生成：{pdf_path}")

    pdf_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{REPORTS_DIR}/{pdf_filename}"
    print(f"PDF 访问链接：{pdf_url}")

    ok = send_summary_card(overview, lhb_detail, zt_streak, watch_sectors, pdf_url)
    if ok:
        if not IS_TEST_MODE:
            state["last_sent_date"] = TODAY_STR
            save_seen_state(state)
        print("推送成功")
    else:
        print("推送失败，不记录，等下次自动重试")


if __name__ == "__main__":
    main()
