from WindPy import w
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from local_market_db import load_price_matrix


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)
REPO_DIR = os.path.dirname(BASE_DIR)
CACHE_DIR = os.path.join(BASE_DIR, "缓存")
OUTPUT_DIR = os.path.join(BASE_DIR, "输出")
PAGES_DIR = os.path.join(REPO_DIR, "docs")
PAGES_INDEX_FILE = os.path.join(PAGES_DIR, "index.html")
os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

LOOKBACK_DAYS = 260
RECENT_REFRESH_DAYS = 10
MA_FAST = 5
MA_SLOW = 60
PRICE_FIELD = "close"
PRICE_ADJ_OPTION = "PriceAdj=F"
USE_REALTIME_SUPPLEMENT = True
OUTPUT_SIGNAL_COLUMNS = ["市场", "信号日期", "代码", "名称", "最新收盘"]

UNIVERSES = [
    {
        "name": "全部A股",
        "sector_id": "a001010100000000",
        "cache_prefix": "全部A股_最新金叉观察",
        "batch_size": 500,
        "price_option": PRICE_ADJ_OPTION,
        "adjusted": "F",
        "trading_calendar": "",
        "timezone": "Asia/Shanghai",
        "market_open": "09:30",
        "market_close": "15:00",
        "use_local_db": True,
        "require_complete_eod": True,
    },
    {
        "name": "全部港股",
        "sector_id": "a002010100000000",
        "cache_prefix": "全部港股_最新金叉观察",
        "batch_size": 300,
        "price_option": f"{PRICE_ADJ_OPTION};TradingCalendar=HKEX",
        "adjusted": "F_HKEX",
        "trading_calendar": "HKEX",
        "timezone": "Asia/Hong_Kong",
        "market_open": "09:30",
        "market_close": "16:00",
        "use_local_db": True,
        "require_complete_eod": False,
    },
    {
        "name": "标普500",
        "sector_id": "a005010800000000",
        "cache_prefix": "标普500_最新金叉观察",
        "output_sheet": "美股",
        "batch_size": 500,
        "price_option": f"{PRICE_ADJ_OPTION};TradingCalendar=NYSE",
        "adjusted": "F_NYSE",
        "trading_calendar": "NYSE",
        "timezone": "America/New_York",
        "market_open": "09:30",
        "market_close": "16:00",
        "use_local_db": True,
        "require_complete_eod": False,
    },
    {
        "name": "纳斯达克100",
        "sector_id": "1000009964000000",
        "cache_prefix": "纳斯达克100_最新金叉观察",
        "output_sheet": "美股",
        "batch_size": 500,
        "price_option": f"{PRICE_ADJ_OPTION};TradingCalendar=NYSE",
        "adjusted": "F_NYSE",
        "trading_calendar": "NYSE",
        "timezone": "America/New_York",
        "market_open": "09:30",
        "market_close": "16:00",
        "use_local_db": True,
        "require_complete_eod": False,
    },
]


def parse_hhmm(value):
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def get_market_trading_dates(universe, start_date, end_date):
    option = ""
    if universe.get("trading_calendar"):
        option = f"TradingCalendar={universe['trading_calendar']}"
    data = w.tdays(start_date, end_date, option)
    if data.ErrorCode != 0 or not data.Data or len(data.Data[0]) == 0:
        raise RuntimeError(
            f"{universe['name']} 交易日历拉取失败: ErrorCode={data.ErrorCode}"
        )
    return [
        pd.Timestamp(date).strftime("%Y-%m-%d")
        for date in data.Data[0]
    ]


def should_use_realtime(universe, latest_trading_date, now=None):
    if not USE_REALTIME_SUPPLEMENT:
        return False
    tz = ZoneInfo(universe["timezone"])
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)

    local_date = now.strftime("%Y-%m-%d")
    if local_date != latest_trading_date:
        return False

    open_hour, open_minute = parse_hhmm(universe["market_open"])
    market_open = now.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
    return now >= market_open


def get_market_data_dates(universe, trading_dates, now=None):
    latest_trading_date = trading_dates[-1]
    tz = ZoneInfo(universe["timezone"])
    now = now or datetime.now(tz)
    now = now.astimezone(tz) if now.tzinfo is not None else now.replace(tzinfo=tz)

    eod_end_date = latest_trading_date
    if now.strftime("%Y-%m-%d") == latest_trading_date:
        close_hour, close_minute = parse_hhmm(universe["market_close"])
        market_close = now.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
        if now < market_close and len(trading_dates) >= 2:
            eod_end_date = trading_dates[-2]

    realtime_date = latest_trading_date if should_use_realtime(universe, latest_trading_date, now) else None
    return eod_end_date, realtime_date


def get_field_index(fields, candidates):
    field_map = {str(field).lower(): idx for idx, field in enumerate(fields)}
    for candidate in candidates:
        idx = field_map.get(candidate.lower())
        if idx is not None:
            return idx
    raise ValueError(f"未找到字段 {candidates}，当前返回字段为: {fields}")


def get_sector_constituents(sector_id):
    sector = w.wset("sectorconstituent", f"sectorid={sector_id}")
    if sector.ErrorCode != 0 or not sector.Data:
        outmessage = ""
        if getattr(sector, "Data", None) and len(sector.Data) > 0 and len(sector.Data[0]) > 0:
            outmessage = str(sector.Data[0][0])
        raise ValueError(
            f"Wind 获取成分股失败。sector_id={sector_id}, "
            f"ErrorCode={sector.ErrorCode}, OutMessage={outmessage}"
        )

    code_idx = get_field_index(sector.Fields, ["wind_code", "sec_code", "ticker"])
    name_idx = get_field_index(sector.Fields, ["sec_name", "security_name", "name"])
    codes = sector.Data[code_idx]
    names = sector.Data[name_idx]
    return codes, names, dict(zip(codes, names))


def sanitize_price_df(df):
    if df.empty:
        return df
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df.where(df > 0, np.nan)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def get_cache_path(cache_prefix, field):
    return os.path.join(CACHE_DIR, f"{cache_prefix}_{field}_PriceAdjF.pkl")


def load_cached_df(cache_prefix, field):
    cache_path = get_cache_path(cache_prefix, field)
    if not os.path.exists(cache_path):
        return pd.DataFrame()
    return sanitize_price_df(pd.read_pickle(cache_path))


def save_cached_df(cache_prefix, field, df):
    cache_path = get_cache_path(cache_prefix, field)
    sanitize_price_df(df).to_pickle(cache_path)


def get_wsd_batch(codes, field, query_start_date, query_end_date, batch_size, price_option):
    all_df = []
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        print(f"拉取 {field}: {i}-{i + len(batch)} [{query_start_date} ~ {query_end_date}]")
        data = w.wsd(batch, field, query_start_date, query_end_date, price_option)

        if data.ErrorCode != 0 or len(data.Times) == 0:
            print(f"{field} 拉取失败: ErrorCode={data.ErrorCode}，失败批次示例: {batch[:3]}")
            continue

        if len(data.Data) == len(data.Codes):
            df = pd.DataFrame(data.Data, index=data.Codes).T
            df.index = data.Times
        elif len(data.Data) == len(data.Times):
            df = pd.DataFrame(data.Data, index=data.Times, columns=data.Codes)
        elif len(data.Times) == 1 and len(data.Data) == 1:
            df = pd.DataFrame([data.Data[0]], index=data.Times, columns=data.Codes)
        else:
            print(
                f"返回维度异常：field={field}, codes={len(data.Codes)}, "
                f"times={len(data.Times)}, data_rows={len(data.Data)}"
            )
            continue

        all_df.append(sanitize_price_df(df))

    if not all_df:
        return pd.DataFrame()

    df = pd.concat(all_df, axis=1)
    df = df.sort_index()
    df = df.loc[:, ~df.columns.duplicated()]
    return sanitize_price_df(df)


def get_wsq_last_batch(codes, trade_date, batch_size=1000):
    all_rows = []
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        print(f"拉取 rt_last: {i}-{i + len(batch)} [{trade_date}]")
        data = w.wsq(batch, "rt_last")
        if data.ErrorCode != 0 or len(data.Codes) == 0 or len(data.Data) == 0:
            print(f"rt_last 拉取失败: ErrorCode={data.ErrorCode}，失败批次示例: {batch[:3]}")
            continue
        batch_df = pd.DataFrame([data.Data[0]], index=[pd.Timestamp(trade_date)], columns=data.Codes)
        all_rows.append(sanitize_price_df(batch_df))

    if not all_rows:
        return pd.DataFrame()

    df = pd.concat(all_rows, axis=1)
    df = df.loc[:, ~df.columns.duplicated()]
    return sanitize_price_df(df)


def get_close_df_with_cache(universe, codes, start_date, end_date, realtime_date):
    if universe.get("use_local_db"):
        close_df = load_price_matrix(
            universe["cache_prefix"],
            PRICE_FIELD,
            codes=codes,
            start_date=start_date,
            end_date=end_date,
            prefer_sqlite=True,
            fallback_pickle=False,
            require_complete_eod=universe.get("require_complete_eod", True),
            adjusted=universe.get("adjusted", "F"),
        )
        close_df = sanitize_price_df(close_df)
        close_df = close_df.loc[:, [code for code in codes if code in close_df.columns]]
        if not close_df.empty and close_df.notna().sum().sum() > 0:
            print(
                f"{universe['name']} {PRICE_FIELD} 使用本地 SQLite 行情库 "
                f"adjusted={universe.get('adjusted', 'F')}"
            )
            if realtime_date is not None:
                rt_df = get_wsq_last_batch(codes, realtime_date)
                if not rt_df.empty:
                    close_df = pd.concat([close_df, rt_df], axis=0)
                    close_df = sanitize_price_df(close_df)
                    close_df = close_df[~close_df.index.duplicated(keep="last")]
                    close_df = close_df.loc[:, [code for code in codes if code in close_df.columns]]
            return close_df
        print(f"{universe['name']} 本地 SQLite 行情为空，回退 Wind/pkl 缓存")

    cache_prefix = universe["cache_prefix"]
    batch_size = universe["batch_size"]
    price_option = universe["price_option"]
    cached_df = load_cached_df(cache_prefix, PRICE_FIELD)

    if cached_df.empty:
        print(f"{universe['name']} {PRICE_FIELD} 未命中缓存，开始拉取")
        close_df = get_wsd_batch(codes, PRICE_FIELD, start_date, end_date, batch_size, price_option)
    else:
        cached_df = cached_df.loc[:, [code for code in cached_df.columns if code in codes]]
        cached_codes = set(cached_df.columns)
        missing_codes = [code for code in codes if code not in cached_codes]

        close_df = cached_df
        latest_cached_date = cached_df.index.max().date()
        refresh_start = latest_cached_date - timedelta(days=RECENT_REFRESH_DAYS - 1)
        update_start = max(refresh_start, pd.Timestamp(start_date).date())
        if update_start <= pd.Timestamp(end_date).date():
            print(f"{universe['name']} {PRICE_FIELD} 命中缓存，增量更新: {update_start} ~ {end_date}")
            inc_df = get_wsd_batch(
                list(cached_df.columns),
                PRICE_FIELD,
                update_start.strftime("%Y-%m-%d"),
                end_date,
                batch_size,
                price_option,
            )
            if not inc_df.empty:
                close_df = close_df.loc[close_df.index < pd.Timestamp(update_start)]
                close_df = pd.concat([close_df, inc_df], axis=0)

        if missing_codes:
            print(f"{universe['name']} 新增股票 {len(missing_codes)} 只，补拉观察窗口")
            missing_df = get_wsd_batch(
                missing_codes,
                PRICE_FIELD,
                start_date,
                end_date,
                batch_size,
                price_option,
            )
            if not missing_df.empty:
                close_df = pd.concat([close_df, missing_df], axis=1)

    if close_df.empty:
        raise ValueError(f"{universe['name']} close 数据为空，无法计算金叉。")

    close_df = sanitize_price_df(close_df)
    close_df = close_df[~close_df.index.duplicated(keep="last")]
    close_df = close_df.loc[:, [code for code in codes if code in close_df.columns]]

    if realtime_date is not None:
        rt_df = get_wsq_last_batch(codes, realtime_date)
        if not rt_df.empty:
            close_df = pd.concat([close_df, rt_df], axis=0)
            close_df = sanitize_price_df(close_df)
            close_df = close_df[~close_df.index.duplicated(keep="last")]
            close_df = close_df.loc[:, [code for code in codes if code in close_df.columns]]

    save_cached_df(cache_prefix, PRICE_FIELD, close_df)
    return close_df


def build_latest_golden_cross_df(universe_name, close_df, code_to_name):
    ma_fast = close_df.rolling(MA_FAST).mean()
    ma_slow = close_df.rolling(MA_SLOW).mean()
    spread = ma_fast - ma_slow
    signal = np.sign(spread)
    cross = signal.diff() == 2

    latest_date = close_df.index[-1]
    prev_date = close_df.index[-2] if len(close_df.index) >= 2 else pd.NaT
    signal_series = cross.loc[latest_date].fillna(False)
    signal_codes = signal_series[signal_series].index.tolist()

    df = pd.DataFrame({
        "市场": universe_name,
        "信号日期": latest_date,
        "代码": signal_codes,
        "名称": [code_to_name.get(code, code) for code in signal_codes],
        "价格口径": PRICE_ADJ_OPTION,
        "最新收盘": [close_df.at[latest_date, code] for code in signal_codes],
        f"MA{MA_FAST}": [ma_fast.at[latest_date, code] for code in signal_codes],
        f"MA{MA_SLOW}": [ma_slow.at[latest_date, code] for code in signal_codes],
        "当前差值": [spread.at[latest_date, code] for code in signal_codes],
        "上一交易日": prev_date,
        "上一日差值": [spread.at[prev_date, code] if pd.notna(prev_date) else np.nan for code in signal_codes],
    })
    if not df.empty:
        df = df.sort_values(by="当前差值", ascending=False, na_position="last")
    return df, latest_date


def format_html_value(value, column_name=None):
    if pd.isna(value):
        return ""
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float):
        if column_name and ("差值" in column_name):
            return f"{value:.4f}"
        return f"{value:.2f}"
    if isinstance(value, (np.integer, int)):
        return f"{int(value)}"
    return escape(str(value))


def dataframe_to_html_table(df, empty_text="暂无信号"):
    if df.empty:
        return f'<div class="empty-state">{escape(empty_text)}</div>'

    headers = "".join(f"<th>{escape(str(column))}</th>" for column in df.columns)
    rows = []
    for _, row in df.iterrows():
        cells = "".join(
            f"<td>{format_html_value(row[column], column)}</td>"
            for column in df.columns
        )
        rows.append(f"<tr>{cells}</tr>")
    return f"""
    <div class="table-wrap">
      <table>
        <thead><tr>{headers}</tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    """


def write_visual_report(
    output_html,
    report_date,
    start_date,
    summary_df,
    sheet_frames,
):
    os.makedirs(os.path.dirname(output_html), exist_ok=True)
    summary = summary_df.copy()
    summary["金叉占比"] = np.where(
        summary["股票数量"] > 0,
        summary["最新金叉数量"] / summary["股票数量"],
        np.nan,
    )
    max_signals = max(int(summary["最新金叉数量"].max()), 1) if not summary.empty else 1

    summary_cards = []
    for row in summary.itertuples(index=False):
        count = int(getattr(row, "最新金叉数量"))
        total = int(getattr(row, "股票数量"))
        ratio = getattr(row, "金叉占比")
        width = min(100, max(2, count / max_signals * 100))
        summary_cards.append(f"""
        <section class="metric-card">
          <div class="metric-head">
            <span class="market-name">{escape(str(getattr(row, "市场")))}</span>
            <span class="date-chip">{format_html_value(getattr(row, "最新行情日期"))}</span>
          </div>
          <div class="metric-main">{count}</div>
          <div class="metric-sub">金叉 / {total} 只，覆盖 {ratio:.2%}</div>
          <div class="bar-track"><div class="bar-fill" style="width:{width:.1f}%"></div></div>
        </section>
        """)

    grouped_sections = []
    for sheet_name, df in sheet_frames.items():
        section_count = len(df)
        grouped_sections.append(f"""
        <section class="section">
          <div class="section-title">
            <h2>{escape(str(sheet_name))}</h2>
            <span>{section_count} 个信号</span>
          </div>
          {dataframe_to_html_table(df)}
        </section>
        """)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>全球主要股票池最新金叉信号 {escape(report_date)}</title>
  <style>
    :root {{
      --bg: #f6f8fb;
      --panel: #ffffff;
      --text: #172033;
      --muted: #667085;
      --line: #d9e0ea;
      --accent: #1f7a8c;
      --accent-2: #b45309;
      --soft: #e8f3f5;
      --shadow: 0 10px 28px rgba(20, 34, 60, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif;
      line-height: 1.5;
    }}
    .page {{
      width: min(1320px, calc(100vw - 48px));
      margin: 0 auto;
      padding: 32px 0 48px;
    }}
    .topbar {{
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 20px;
      padding-bottom: 22px;
      border-bottom: 1px solid var(--line);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 30px;
      font-weight: 760;
      letter-spacing: 0;
    }}
    .subtitle, .generated {{
      color: var(--muted);
      font-size: 14px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin: 24px 0;
    }}
    .metric-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 16px;
    }}
    .metric-head {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      min-height: 28px;
    }}
    .market-name {{ font-weight: 700; }}
    .date-chip {{
      white-space: nowrap;
      color: var(--accent);
      background: var(--soft);
      border: 1px solid #c9e4ea;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
    }}
    .metric-main {{
      font-size: 38px;
      font-weight: 780;
      margin-top: 12px;
    }}
    .metric-sub {{
      color: var(--muted);
      font-size: 13px;
      margin: 2px 0 12px;
    }}
    .bar-track {{
      height: 8px;
      background: #edf1f6;
      border-radius: 999px;
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      background: linear-gradient(90deg, var(--accent), var(--accent-2));
      border-radius: 999px;
    }}
    .section {{
      margin-top: 22px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .section-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }}
    h2 {{
      margin: 0;
      font-size: 18px;
      font-weight: 720;
      letter-spacing: 0;
    }}
    .section-title span {{
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }}
    .table-wrap {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      min-width: 920px;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid #edf0f5;
      text-align: left;
      white-space: nowrap;
    }}
    th {{
      position: sticky;
      top: 0;
      background: #f4f7fb;
      color: #344054;
      font-weight: 680;
      z-index: 1;
    }}
    tbody tr:hover {{
      background: #f8fbfd;
    }}
    .empty-state {{
      color: var(--muted);
      padding: 24px 18px;
      font-size: 14px;
    }}
    @media (max-width: 980px) {{
      .page {{ width: min(100vw - 28px, 1320px); padding-top: 22px; }}
      .topbar {{ align-items: start; flex-direction: column; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      h1 {{ font-size: 24px; }}
    }}
    @media (max-width: 560px) {{
      .metrics {{ grid-template-columns: 1fr; }}
      .metric-main {{ font-size: 32px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <header class="topbar">
      <div>
        <h1>全球主要股票池最新金叉信号</h1>
        <div class="subtitle">观察窗口：{escape(start_date)} 至 {escape(report_date)}，MA{MA_FAST} 上穿 MA{MA_SLOW}</div>
      </div>
      <div class="generated">生成时间：{escape(generated_at)}</div>
    </header>

    <section class="metrics">
      {''.join(summary_cards)}
    </section>

    {''.join(grouped_sections)}
  </main>
</body>
</html>
"""
    with open(output_html, "w", encoding="utf-8") as file:
        file.write(html)


def main():
    w.start()
    try:
        end_dt = datetime.today()
        end_date = end_dt.strftime("%Y-%m-%d")
        start_date = (end_dt.date() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        output_file = os.path.join(OUTPUT_DIR, f"全球主要股票池_最新金叉信号_{end_date}.xlsx")
        output_html = os.path.join(OUTPUT_DIR, f"全球主要股票池_最新金叉信号_{end_date}.html")

        summary_rows = []
        sheet_frames = {}

        print(f"观察窗口: {start_date} ~ {end_date}")
        for universe in UNIVERSES:
            print(f"\n===== {universe['name']} =====")
            market_trading_dates = get_market_trading_dates(universe, start_date, end_date)
            market_start_date = market_trading_dates[0]
            latest_trading_date = market_trading_dates[-1]
            eod_end_date, realtime_date = get_market_data_dates(universe, market_trading_dates)
            print(
                f"{universe['name']} 交易日历: "
                f"{market_start_date} ~ {latest_trading_date} "
                f"({universe.get('trading_calendar') or 'Wind默认'})"
            )
            print(
                f"{universe['name']} EOD读取截止: {eod_end_date}"
                + (f"，实时补行日期: {realtime_date}" if realtime_date else "，不补实时行")
            )
            codes, names, code_to_name = get_sector_constituents(universe["sector_id"])
            print(f"{universe['name']} 股票数量：{len(codes)}")
            close_df = get_close_df_with_cache(
                universe,
                codes,
                market_start_date,
                eod_end_date,
                realtime_date,
            )
            signal_df, latest_date = build_latest_golden_cross_df(universe["name"], close_df, code_to_name)
            output_sheet = universe.get("output_sheet", universe["name"])
            signal_output_df = signal_df.reindex(columns=OUTPUT_SIGNAL_COLUMNS)
            if output_sheet in sheet_frames:
                sheet_frames[output_sheet] = pd.concat(
                    [sheet_frames[output_sheet], signal_output_df],
                    ignore_index=True,
                )
            else:
                sheet_frames[output_sheet] = signal_output_df
            summary_rows.append({
                "市场": universe["name"],
                "股票数量": len(codes),
                "最新行情日期": latest_date,
                "最新金叉数量": len(signal_df),
            })
            print(f"{universe['name']} 最新行情日期: {latest_date.date()}，最新金叉数量: {len(signal_df)}")

        summary_df = pd.DataFrame(summary_rows)

        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            summary_df.to_excel(writer, sheet_name="摘要", index=False)
            for sheet_name, df in sheet_frames.items():
                df.to_excel(writer, sheet_name=sheet_name[:31], index=False)

        write_visual_report(
            output_html,
            end_date,
            start_date,
            summary_df,
            sheet_frames,
        )
        write_visual_report(
            PAGES_INDEX_FILE,
            end_date,
            start_date,
            summary_df,
            sheet_frames,
        )

        print("\n【摘要】")
        print(summary_df)
        print("输出完成：", output_file)
        print("可视化输出完成：", output_html)
        print("GitHub Pages 首页已更新：", PAGES_INDEX_FILE)
    finally:
        w.close()


if __name__ == "__main__":
    main()
