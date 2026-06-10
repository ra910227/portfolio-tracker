#!/usr/bin/env python3
"""
Portfolio Weekly Report Generator
每週五自動執行：抓台股/美股收盤價 → 計算損益 → 生成 HTML 週報
"""

import json
import os
import sys
import time
import datetime
import requests
from pathlib import Path

# ── 路徑設定 ──────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
DOCS_DIR.mkdir(exist_ok=True)

# ── 常數 ──────────────────────────────────────────────────
FX_RATE = 31.5          # 固定匯率 TWD/USD
ALLIANZ_NAV = None      # 安聯淨值需手動更新（T+1 才公告）

# ── 抓台股現價（TWSE OpenAPI，不需登入）─────────────────────
def fetch_tw_prices(codes: list[str]) -> dict:
    """從台灣證交所 OpenAPI 抓取台股即時/收盤價"""
    prices = {}
    # 批次查詢，一次最多 50 檔
    batch = ",".join(codes)
    try:
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={batch}&json=1&delay=0"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers, timeout=10)
        data = r.json()
        for item in data.get("msgArray", []):
            code = item.get("c", "")
            # z = 成交價, y = 昨收
            price_str = item.get("z", item.get("y", "-"))
            if price_str and price_str != "-":
                prices[code] = float(price_str)
    except Exception as e:
        print(f"[WARN] TWSE API error: {e}")

    # 備援：用 Yahoo Finance（部分標的）
    missing = [c for c in codes if c not in prices]
    if missing:
        for code in missing:
            try:
                yf_code = f"{code}.TW"
                url2 = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_code}?interval=1d&range=1d"
                r2 = requests.get(url2, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
                result = r2.json()
                close = result["chart"]["result"][0]["meta"]["regularMarketPrice"]
                prices[code] = float(close)
                time.sleep(0.3)
            except Exception as e2:
                print(f"[WARN] Yahoo TW {code}: {e2}")
    return prices

# ── 抓美股現價（Yahoo Finance）────────────────────────────
def fetch_us_prices(codes: list[str]) -> dict:
    """從 Yahoo Finance 抓美股收盤價（USD）"""
    prices = {}
    for code in codes:
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}?interval=1d&range=1d"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            result = r.json()
            close = result["chart"]["result"][0]["meta"]["regularMarketPrice"]
            prices[code] = float(close)
            time.sleep(0.3)
        except Exception as e:
            print(f"[WARN] Yahoo US {code}: {e}")
    return prices

# ── 抓安聯淨值（投信投顧公會）────────────────────────────
def fetch_allianz_nav() -> float | None:
    """嘗試從投信投顧公會抓安聯台灣科技基金最新淨值"""
    try:
        url = "https://www.sitca.org.tw/ROC/Industry/IN2421.aspx"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        # 搜尋「安聯台灣科技」
        text = r.text
        idx = text.find("安聯台灣科技")
        if idx > 0:
            snippet = text[idx:idx+500]
            import re
            nums = re.findall(r"[\d,]+\.\d+", snippet)
            if nums:
                return float(nums[0].replace(",", ""))
    except Exception as e:
        print(f"[WARN] Allianz NAV: {e}")
    return None

# ── 計算週次 ──────────────────────────────────────────────
def get_week_label() -> str:
    today = datetime.date.today()
    # ISO week number
    week_num = today.isocalendar()[1]
    return f"W{week_num}"

def get_week_range() -> tuple[str, str]:
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    friday = monday + datetime.timedelta(days=4)
    return monday.strftime("%Y/%m/%d"), friday.strftime("%Y/%m/%d")

# ── 主計算邏輯 ────────────────────────────────────────────
def calculate_portfolio(holdings, prices_tw, prices_us, allianz_nav, fx):
    results = []
    total_cost = 0
    total_value = 0

    for h in holdings:
        code = h["code"]
        market = h["market"]
        shares = h["shares"]
        cost_twd = h["cost_twd"]

        if market == "TW":
            price = prices_tw.get(code)
            if price is None:
                price = h.get("cost_per_share", 0)
            value_twd = price * shares
            cost_per = h["cost_per_share"]

        elif market == "US":
            price_usd = prices_us.get(code)
            if price_usd is None:
                price_usd = h.get("cost_per_share", 0)
            value_twd = price_usd * shares * fx
            price = price_usd
            cost_per = h["cost_per_share"]

        elif market == "FUND":
            nav = allianz_nav or h.get("cost_per_share", 783.29)
            value_twd = nav * shares
            price = nav
            cost_per = h["cost_per_share"]
        else:
            continue

        pnl_twd = value_twd - cost_twd
        pnl_pct = (pnl_twd / cost_twd * 100) if cost_twd > 0 else 0

        total_cost += cost_twd
        total_value += value_twd

        results.append({
            **h,
            "current_price": price,
            "value_twd": value_twd,
            "pnl_twd": pnl_twd,
            "pnl_pct": pnl_pct,
        })

    return results, total_cost, total_value

# ── 計算本週新增持倉報酬 ───────────────────────────────────
def calculate_new_positions(transactions, prices_tw, prices_us, fx):
    week_label = get_week_label()
    this_week = [t for t in transactions if t["week"] == week_label]

    new_positions = {}
    for t in this_week:
        key = f"{t['code']}_{t['account']}"
        if key not in new_positions:
            new_positions[key] = {
                "code": t["code"], "name": t["name"],
                "account": t["account"], "market": t["market"],
                "total_shares": 0, "total_cost": 0,
                "action": t["action"]
            }
        new_positions[key]["total_shares"] += t["shares"]
        new_positions[key]["total_cost"] += t["amount_twd"]

    results = []
    for key, pos in new_positions.items():
        code = pos["code"]
        shares = pos["total_shares"]
        cost = pos["total_cost"]
        avg_cost = cost / shares if shares > 0 else 0

        if pos["market"] == "TW":
            cur_price = prices_tw.get(code, avg_cost)
            cur_value = cur_price * shares
        else:
            cur_price_usd = prices_us.get(code, avg_cost)
            cur_value = cur_price_usd * shares * fx
            cur_price = cur_price_usd

        pnl = cur_value - cost
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0

        results.append({
            **pos,
            "avg_cost": avg_cost,
            "current_price": cur_price,
            "current_value": cur_value,
            "pnl_twd": pnl,
            "pnl_pct": pnl_pct,
        })

    return results, sum(p["total_cost"] for p in new_positions.values())

# ── 儲存本週價格快照 ──────────────────────────────────────
def save_price_snapshot(prices_tw, prices_us, allianz_nav):
    today = datetime.date.today().isoformat()
    snapshot = {
        "date": today,
        "week": get_week_label(),
        "tw": prices_tw,
        "us": prices_us,
        "allianz_nav": allianz_nav,
        "fx_rate": FX_RATE
    }
    out = DATA_DIR / "weekly_prices.json"
    # 載入舊資料
    history = []
    if out.exists():
        with open(out) as f:
            history = json.load(f)
    # 更新或新增本週
    existing = next((i for i, s in enumerate(history) if s["week"] == snapshot["week"]), None)
    if existing is not None:
        history[existing] = snapshot
    else:
        history.append(snapshot)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"[OK] Saved price snapshot for {snapshot['week']}")

# ── 讀取歷史週報資料（for 歷史表格）────────────────────────
def load_weekly_history():
    prices_file = DATA_DIR / "weekly_prices.json"
    if not prices_file.exists():
        return []
    with open(prices_file) as f:
        return json.load(f)

# ── 格式化數字 ────────────────────────────────────────────
def fmt(n, decimals=0):
    if n is None: return "—"
    return f"{n:,.{decimals}f}"

def fmt_pct(n):
    if n is None: return "—"
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.2f}%"

def pnl_class(n):
    if n is None: return ""
    return "pos" if n >= 0 else "neg"

# ── 生成週報 HTML ─────────────────────────────────────────
def generate_html(results, total_cost, total_value, cash_total,
                  new_positions, week_buy_total, transactions,
                  allianz_nav, prices_tw, prices_us):

    week = get_week_label()
    w_start, w_end = get_week_range()
    today = datetime.date.today().strftime("%Y/%m/%d")
    total_assets = total_value + cash_total
    pnl_total = total_value - total_cost
    pnl_pct = pnl_total / total_cost * 100 if total_cost > 0 else 0
    equity_ratio = total_value / total_assets * 100 if total_assets > 0 else 0

    # 現金
    with open(DATA_DIR / "holdings.json") as f:
        hdata = json.load(f)
    baseline_add_budget = hdata["add_budget_total"]
    add_budget_used = hdata.get("add_budget_used", 0) + week_buy_total
    add_budget_remain = baseline_add_budget - add_budget_used

    # 本週交易表格
    tx_this_week = [t for t in transactions if t["week"] == week]
    tx_rows = ""
    for t in tx_this_week:
        action_badge = '<span class="badge badge-buy">買進</span>' if t["action"] == "buy" else '<span class="badge badge-sell">賣出</span>'
        tx_rows += f"""
      <tr class="new-buy">
        <td>{t['date'][5:]}</td>
        <td>{t['name']} {t['code']}</td>
        <td>{action_badge}</td>
        <td>{t['account']}帳</td>
        <td>{fmt(t['shares'])}</td>
        <td>{fmt(t['cost_per_share'], 2)}</td>
        <td>{fmt(t['amount_twd'])}</td>
        <td class="dim">{t.get('note','')}</td>
      </tr>"""

    # 本週新增持倉報酬表格
    new_pos_rows = ""
    for p in new_positions:
        pct_cls = pnl_class(p["pnl_pct"])
        new_pos_rows += f"""
      <tr>
        <td>{p['name']} {p['code']}</td>
        <td>{p['account']}帳</td>
        <td>{fmt(p['total_shares'])}</td>
        <td>{fmt(p['avg_cost'], 2)}</td>
        <td>{fmt(p['total_cost'])}</td>
        <td>{fmt(p['current_price'], 2)}</td>
        <td>{fmt(p['current_value'])}</td>
        <td class="{pct_cls}">{fmt(p['pnl_twd'])}</td>
        <td class="{pct_cls}">{fmt_pct(p['pnl_pct'])}</td>
      </tr>"""

    # 完整持倉表格
    holding_rows = ""
    for h in results:
        if h["shares"] == 0:
            continue
        mkt = "🇹🇼" if h["market"] == "TW" else ("📈" if h["market"] == "FUND" else "🌏")
        price_disp = fmt(h["current_price"], 2) if h["market"] != "US" else f"{fmt(h['current_price'], 2)} USD"
        pct_cls = pnl_class(h["pnl_pct"])
        holding_rows += f"""
      <tr>
        <td>{mkt} {h['name']}</td>
        <td class="dim">{h['code']}</td>
        <td>{h['account']}帳</td>
        <td>{fmt(h['shares'])}</td>
        <td>{fmt(h['cost_per_share'], 2)}</td>
        <td>{price_disp}</td>
        <td>{fmt(h['cost_twd'])}</td>
        <td>{fmt(h['value_twd'])}</td>
        <td class="{pct_cls}">{fmt(h['pnl_twd'])}</td>
        <td class="{pct_cls}">{fmt_pct(h['pnl_pct'])}</td>
      </tr>"""

    # 週報歷史
    history = load_weekly_history()
    history_rows = f"""
      <tr>
        <td><span class="badge badge-none">W23</span></td>
        <td>06/02–06/06</td>
        <td class="dim">基準週，無交易</td>
        <td class="dim">—</td>
        <td>5,992,384</td>
        <td>3,639,439</td>
        <td>19,012,075</td>
        <td>65.9%</td>
        <td class="dim">—</td>
        <td class="pos">+5,096,064（+68.6%）</td>
      </tr>"""

    for snap in history:
        if snap["week"] == "W23":
            continue
        wk = snap["week"]
        dt = snap.get("date", "")
        history_rows += f"""
      <tr>
        <td><span class="badge badge-tw">{wk}</span></td>
        <td>{dt}</td>
        <td class="dim">—</td>
        <td class="dim">—</td>
        <td class="pend">—</td>
        <td class="pend">—</td>
        <td class="pend">—</td>
        <td class="pend">—</td>
        <td class="pend">—</td>
        <td class="pend">—</td>
      </tr>"""

    # 市場報酬率卡片
    tw_idx = prices_tw.get("TAIEX", None)
    ts0050 = prices_tw.get("0050", None)
    ts2330 = prices_tw.get("2330", None)
    ts00981A = prices_tw.get("00981A", None)
    ts00990A = prices_tw.get("00990A", None)
    tsQQQ = prices_us.get("QQQ", None)

    def perf_card(label, prev, curr, prev_label="W23末"):
        if curr is None:
            chg_html = '<div class="chg pend">待更新</div>'
            curr_html = '<div class="curr pend">—</div>'
        else:
            pct = (curr - prev) / prev * 100 if prev else 0
            sign = "+" if pct >= 0 else ""
            cls = "pos" if pct >= 0 else "neg"
            chg_html = f'<div class="chg {cls}">{sign}{pct:.2f}%</div>'
            curr_html = f'<div class="curr">{fmt(curr, 2)}</div>'
        return f"""
    <div class="perf-card">
      <div class="lbl">{label}</div>
      <div class="prev">{prev_label}：{fmt(prev, 2)}</div>
      {curr_html}
      {chg_html}
    </div>"""

    perf_cards = (
        perf_card("台股加權指數", 45683, tw_idx) +
        perf_card("元大台灣50 0050", 94.90, ts0050) +
        perf_card("台積電 2330", 2365, ts2330) +
        perf_card("00981A 觸發觀察", 29.89, ts00981A) +
        perf_card("00990A 主動元大AI", 20.25, ts00990A) +
        perf_card(f"安聯科技基金", 783.29, allianz_nav, "上次淨值 6/5") +
        perf_card("QQQ（USD）", 715.40, tsQQQ)
    )

    allianz_note = f"最新淨值：{fmt(allianz_nav, 2)}" if allianz_nav else "淨值待公告（T+1更新）"

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>投資週報 {week}｜{w_start}–{w_end}</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@300;400;500;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #f4f6fb; color: #1a2035; font-family: 'Noto Sans TC', sans-serif; font-size: 13px; line-height: 1.6; padding-bottom: 60px; }}
.header {{ background: linear-gradient(135deg, #1a2a6c, #2a4a9c, #1a3a7c); padding: 28px 28px 22px; color: #fff; }}
.header-top {{ display: flex; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; gap: 8px; }}
.header h1 {{ font-size: 20px; font-weight: 700; letter-spacing: 0.04em; }}
.week-badge {{ background: rgba(255,255,255,0.2); border: 1px solid rgba(255,255,255,0.35); border-radius: 6px; padding: 4px 12px; font-size: 12px; font-family: 'DM Mono', monospace; font-weight: 600; }}
.header .date {{ font-size: 11px; color: rgba(255,255,255,0.7); margin-top: 4px; font-family: 'DM Mono', monospace; }}
.summary-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-top: 18px; }}
.sum-card {{ background: rgba(255,255,255,0.13); border: 1px solid rgba(255,255,255,0.2); border-radius: 10px; padding: 12px 14px; }}
.sum-card .label {{ font-size: 9px; color: rgba(255,255,255,0.65); letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 4px; }}
.sum-card .value {{ font-family: 'DM Mono', monospace; font-size: 14px; font-weight: 600; color: #fff; }}
.sum-card .sub {{ font-size: 9px; color: rgba(255,255,255,0.55); margin-top: 2px; font-family: 'DM Mono', monospace; }}
.sum-card.pos .value {{ color: #7fffcf; }}
.sum-card.gold .value {{ color: #ffe57a; }}
.sum-card.warn .value {{ color: #fbbf24; }}
.main {{ padding: 20px 28px; }}
.sec-title {{ font-size: 10px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; color: #6b7a99; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }}
.sec-title::after {{ content: ''; flex: 1; height: 1px; background: #dde2ef; }}
.tbl-wrap {{ overflow-x: auto; border-radius: 10px; border: 1px solid #dde2ef; background: #fff; margin-bottom: 20px; }}
table {{ width: 100%; border-collapse: collapse; min-width: 560px; }}
thead tr {{ background: #f0f3fa; }}
thead th {{ padding: 9px 12px; text-align: right; font-size: 10px; font-weight: 700; color: #3a4a6a; letter-spacing: 0.07em; text-transform: uppercase; white-space: nowrap; border-bottom: 1px solid #dde2ef; }}
thead th:first-child, thead th:nth-child(2), thead th:nth-child(3) {{ text-align: left; }}
tbody tr {{ border-bottom: 1px solid #edf0f7; }}
tbody tr:last-child {{ border-bottom: none; }}
tbody tr:hover {{ background: #f7f9ff; }}
tbody td {{ padding: 9px 12px; text-align: right; font-family: 'DM Mono', monospace; font-size: 12px; color: #1a2035; white-space: nowrap; }}
tbody td:first-child, tbody td:nth-child(2), tbody td:nth-child(3) {{ text-align: left; font-family: 'Noto Sans TC', sans-serif; }}
tfoot tr {{ background: #f0f3fa; }}
tfoot td {{ padding: 10px 12px; text-align: right; font-family: 'DM Mono', monospace; font-size: 12px; font-weight: 700; color: #1a2035; border-top: 2px solid #dde2ef; white-space: nowrap; }}
tfoot td:first-child, tfoot td:nth-child(2) {{ text-align: left; font-family: 'Noto Sans TC', sans-serif; }}
.pos {{ color: #059669 !important; font-weight: 600; }}
.neg {{ color: #dc2626 !important; font-weight: 600; }}
.dim {{ color: #9ca3af !important; }}
.pend {{ color: #d97706 !important; font-style: italic; }}
.badge {{ display: inline-block; font-size: 9px; padding: 1px 7px; border-radius: 3px; font-weight: 700; font-family: 'DM Mono', monospace; }}
.badge-buy {{ background: #dcfce7; color: #15803d; }}
.badge-sell {{ background: #fee2e2; color: #b91c1c; }}
.badge-none {{ background: #f3f4f6; color: #6b7a99; }}
.badge-tw {{ background: #dbeafe; color: #1d4ed8; }}
.new-buy {{ background: #f0fdf4 !important; }}
.new-buy td:first-child {{ border-left: 3px solid #22c55e; }}
.perf-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 20px; }}
.perf-card {{ background: #fff; border: 1px solid #dde2ef; border-radius: 10px; padding: 12px 14px; }}
.perf-card .lbl {{ font-size: 9px; color: #6b7a99; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 5px; }}
.perf-card .prev {{ font-family: 'DM Mono', monospace; font-size: 10px; color: #9ca3af; margin-bottom: 2px; }}
.perf-card .curr {{ font-family: 'DM Mono', monospace; font-size: 15px; font-weight: 700; color: #1a2035; }}
.perf-card .chg {{ font-family: 'DM Mono', monospace; font-size: 12px; font-weight: 700; margin-top: 3px; }}
.chg.pos {{ color: #059669; }}
.chg.neg {{ color: #dc2626; }}
.chg.pend {{ color: #d97706; font-style: italic; }}
.footnote {{ font-size: 10px; color: #9ca3af; margin-top: 20px; padding: 0 2px; line-height: 1.8; }}
.auto-badge {{ background: #dcfce7; color: #15803d; border: 1px solid #86efac; border-radius: 4px; padding: 2px 8px; font-size: 9px; font-family: 'DM Mono', monospace; font-weight: 700; }}
</style>
</head>
<body>
<div class="header">
  <div class="header-top">
    <div>
      <h1>📋 投資週報 <span class="auto-badge">AUTO</span></h1>
      <div class="date">週期：{w_start} – {w_end}（{week}）｜自動更新：{today}</div>
    </div>
    <div class="week-badge">{week}</div>
  </div>
  <div class="summary-grid">
    <div class="sum-card"><div class="label">期末總資產</div><div class="value">{fmt(total_assets)}</div><div class="sub">TWD</div></div>
    <div class="sum-card gold"><div class="label">可用現金</div><div class="value">{fmt(cash_total)}</div><div class="sub">三帳戶合計</div></div>
    <div class="sum-card pos"><div class="label">未實現損益</div><div class="value">{fmt_pct(pnl_pct) if pnl_total >= 0 else fmt_pct(pnl_pct)}</div><div class="sub">{fmt(pnl_total)} TWD</div></div>
    <div class="sum-card"><div class="label">本週買進</div><div class="value">{fmt(week_buy_total)}</div><div class="sub">TWD</div></div>
    <div class="sum-card"><div class="label">持股比</div><div class="value">{equity_ratio:.1f}%</div><div class="sub">目標 85%</div></div>
    <div class="sum-card warn"><div class="label">加碼預算剩餘</div><div class="value">{fmt(add_budget_remain)}</div><div class="sub">TWD</div></div>
  </div>
</div>

<div class="main">

<div class="sec-title">📈 本週各標的報酬率</div>
<div class="perf-grid">{perf_cards}</div>
<p style="font-size:10px;color:#9ca3af;margin-bottom:20px;">
  ※ 安聯台灣科技基金：{allianz_note}｜報酬率 = (本週收盤 − W23末收盤) ÷ W23末收盤
</p>

<div class="sec-title">🔄 當週交易明細</div>
<div class="tbl-wrap"><table>
  <thead><tr>
    <th>日期</th><th>標的</th><th>類別</th><th>帳戶</th>
    <th>股數</th><th>成本均價</th><th>應收付(TWD)</th><th>備註</th>
  </tr></thead>
  <tbody>{tx_rows if tx_rows else '<tr><td colspan="8" style="text-align:center;color:#9ca3af;padding:20px;">本週無交易</td></tr>'}</tbody>
  <tfoot><tr><td colspan="4">本週合計</td><td>—</td><td>—</td><td>{fmt(week_buy_total)}</td><td></td></tr></tfoot>
</table></div>

<div class="sec-title">📊 本週新增持倉報酬追蹤</div>
<div class="tbl-wrap"><table>
  <thead><tr>
    <th>標的</th><th>帳戶</th><th>買進股數</th><th>合併均價</th>
    <th>買入成本</th><th>現價</th><th>現值</th><th>損益</th><th>報酬率</th>
  </tr></thead>
  <tbody>{new_pos_rows if new_pos_rows else '<tr><td colspan="9" style="text-align:center;color:#9ca3af;padding:20px;">本週無新增持倉</td></tr>'}</tbody>
  <tfoot><tr><td colspan="4">合計</td><td>{fmt(week_buy_total)}</td><td>—</td><td>—</td><td>—</td><td>—</td></tr></tfoot>
</table></div>

<div class="sec-title">📋 完整持倉快照</div>
<div class="tbl-wrap"><table>
  <thead><tr>
    <th>標的</th><th>代碼</th><th>帳戶</th><th>股數</th>
    <th>成本均價</th><th>現價</th><th>成本(TWD)</th><th>市值(TWD)</th>
    <th>損益(TWD)</th><th>報酬率</th>
  </tr></thead>
  <tbody>{holding_rows}</tbody>
  <tfoot><tr>
    <td colspan="3">合計</td><td>—</td><td>—</td><td>—</td>
    <td>{fmt(total_cost)}</td><td>{fmt(total_value)}</td>
    <td class="{pnl_class(pnl_total)}">{fmt(pnl_total)}</td>
    <td class="{pnl_class(pnl_pct)}">{fmt_pct(pnl_pct)}</td>
  </tr></tfoot>
</table></div>

<div class="sec-title">📈 週報歷史（下半年累計）</div>
<div class="tbl-wrap"><table>
  <thead><tr>
    <th>週次</th><th>週期</th><th>當週買進</th><th>買進總額</th>
    <th>期末現金</th><th>加碼預算剩餘</th><th>期末總資產</th>
    <th>持股比</th><th>本週新增持倉報酬</th><th>整體未實現損益</th>
  </tr></thead>
  <tbody>{history_rows}</tbody>
</table></div>

<div class="footnote">
  ※ 現價來源：台股 → 台灣證交所 OpenAPI｜美股 → Yahoo Finance｜安聯淨值 → 投信投顧公會（T+1）<br>
  ※ 海外股票匯率固定 31.5 TWD/USD｜GLD 歸類現金<br>
  ※ 本報告由 GitHub Actions 每週五 16:30 自動生成｜有交易時請在週五前更新 transactions.json<br>
  ※ ★ 子帳戶 981r-1581999（V帳內）
</div>
</div>
</body>
</html>"""

    return html


# ── 主程式 ────────────────────────────────────────────────
def main():
    print(f"[START] Portfolio report generator — {datetime.date.today()}")

    # 讀取持倉資料
    with open(DATA_DIR / "holdings.json", encoding="utf-8") as f:
        hdata = json.load(f)

    holdings = hdata["holdings"]
    cash_items = hdata["cash"]
    cash_total = sum(c["amount_twd"] for c in cash_items)

    with open(DATA_DIR / "transactions.json", encoding="utf-8") as f:
        tdata = json.load(f)
    transactions = tdata["transactions"]

    # 取得需要查詢的代碼清單
    tw_codes = list({h["code"] for h in holdings if h["market"] == "TW"})
    us_codes = list({h["code"] for h in holdings if h["market"] == "US"})

    # 把 TWSE 代碼格式化（加 .TW 部分用備援，主查詢用原始代碼）
    tw_query = [f"{c}.TW@TWSE" if not c.startswith("009") else f"{c}.TWO@TPEX" for c in tw_codes]

    print(f"[INFO] Fetching TW prices for: {tw_codes}")
    prices_tw = fetch_tw_prices(tw_codes)
    print(f"[INFO] Got {len(prices_tw)} TW prices: {prices_tw}")

    print(f"[INFO] Fetching US prices for: {us_codes}")
    prices_us = fetch_us_prices(us_codes)
    print(f"[INFO] Got {len(prices_us)} US prices")

    print("[INFO] Fetching Allianz NAV...")
    allianz_nav = fetch_allianz_nav()
    print(f"[INFO] Allianz NAV: {allianz_nav}")

    # 儲存價格快照
    save_price_snapshot(prices_tw, prices_us, allianz_nav)

    # 計算持倉損益
    results, total_cost, total_value = calculate_portfolio(
        holdings, prices_tw, prices_us, allianz_nav, FX_RATE
    )

    # 計算本週新增持倉
    new_positions, week_buy_total = calculate_new_positions(
        transactions, prices_tw, prices_us, FX_RATE
    )

    print(f"[INFO] Total cost: {total_cost:,.0f} | Total value: {total_value:,.0f}")
    print(f"[INFO] Cash: {cash_total:,.0f} | Week buy: {week_buy_total:,.0f}")

    # 生成 HTML
    html = generate_html(
        results, total_cost, total_value, cash_total,
        new_positions, week_buy_total, transactions,
        allianz_nav, prices_tw, prices_us
    )

    # 寫出週報
    week = get_week_label()
    out_path = DOCS_DIR / "index.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[OK] Generated: {out_path}")

    # 也存一份帶週次的版本
    archive_path = DOCS_DIR / f"report_{week}.html"
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[OK] Archived: {archive_path}")

    print("[DONE] Report generation complete.")


if __name__ == "__main__":
    main()
