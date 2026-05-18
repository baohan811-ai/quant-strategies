from WindPy import w
import os
import subprocess
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from local_market_db import load_price_matrix, load_stock_industry_map


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
AUTO_PUBLISH_PAGES = os.environ.get("AUTO_PUBLISH_PAGES", "1").lower() not in {"0", "false", "no"}
HEAT_LOOKBACK_DAYS = 30
OUTPUT_SIGNAL_COLUMNS = ["市场", "信号日期", "代码", "名称", "万得一级行业", "最新收盘"]
HTML_HIDDEN_COLUMNS = {"市场", "信号日期"}
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
            latest_local_date = close_df.dropna(how="all").index.max()
            if pd.notna(latest_local_date) and latest_local_date < pd.Timestamp(end_date):
                missing_start = latest_local_date + pd.Timedelta(days=1)
                print(
                    f"{universe['name']} 本地 SQLite 行情截止 {latest_local_date.date()}，"
                    f"从 Wind 补拉 {missing_start.date()} ~ {end_date}"
                )
                missing_df = get_wsd_batch(
                    codes,
                    PRICE_FIELD,
                    missing_start.strftime("%Y-%m-%d"),
                    end_date,
                    universe["batch_size"],
                    universe["price_option"],
                )
                if not missing_df.empty:
                    close_df = close_df.loc[close_df.index < missing_start]
                    close_df = pd.concat([close_df, missing_df], axis=0)
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


def build_latest_golden_cross_df(universe_name, close_df, code_to_name, code_to_industry=None):
    code_to_industry = code_to_industry or {}
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
        "万得一级行业": [code_to_industry.get(code, "") for code in signal_codes],
        "价格口径": PRICE_ADJ_OPTION,
        "最新收盘": [close_df.at[latest_date, code] for code in signal_codes],
        f"MA{MA_FAST}": [ma_fast.at[latest_date, code] for code in signal_codes],
        f"MA{MA_SLOW}": [ma_slow.at[latest_date, code] for code in signal_codes],
        "当前差值": [spread.at[latest_date, code] for code in signal_codes],
        "上一交易日": prev_date,
        "上一日差值": [spread.at[prev_date, code] if pd.notna(prev_date) else np.nan for code in signal_codes],
    })
    if not df.empty:
        df["_行业排序"] = df["万得一级行业"].replace("", "未分类")
        df = (
            df.sort_values(
                by=["_行业排序", "当前差值"],
                ascending=[True, False],
                na_position="last",
            )
            .drop(columns="_行业排序")
        )
    return df, latest_date


def calculate_golden_cross_counts(close_df):
    ma_fast = close_df.rolling(MA_FAST).mean()
    ma_slow = close_df.rolling(MA_SLOW).mean()
    signal = np.sign(ma_fast - ma_slow)
    cross = signal.diff() == 2
    return cross.sum(axis=1).astype(int)


def build_heat_rows(universe_name, close_df, stock_count):
    cross_counts = calculate_golden_cross_counts(close_df)
    recent_counts = cross_counts.tail(HEAT_LOOKBACK_DAYS)
    return [
        {
            "市场": universe_name,
            "日期": date,
            "当日金叉数量": int(count),
            "股票数量": stock_count,
            "当日金叉覆盖": count / stock_count if stock_count else np.nan,
        }
        for date, count in recent_counts.items()
    ]


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

    display_columns = [column for column in df.columns if column not in HTML_HIDDEN_COLUMNS]
    headers = "".join(f"<th>{escape(str(column))}</th>" for column in display_columns)
    rows = []
    for _, row in df.iterrows():
        cells = "".join(
            f"<td>{format_html_value(row[column], column)}</td>"
            for column in display_columns
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


def industry_distribution_to_html(df):
    if df.empty or "万得一级行业" not in df.columns:
        return ""

    industries = df["万得一级行业"].replace("", "未分类").fillna("未分类")
    counts = industries.value_counts().sort_values(ascending=False)
    total = int(counts.sum())
    if total == 0:
        return ""

    max_count = int(counts.max())
    bars = []
    for industry, count in counts.items():
        ratio = count / total
        width = max(4, count / max_count * 100)
        bars.append(f"""
          <div class="industry-bar-row">
            <span class="industry-name">{escape(str(industry))}</span>
            <div class="industry-bar-track">
              <div class="industry-bar-fill" style="width:{width:.1f}%"></div>
            </div>
            <span class="industry-count">{int(count)}</span>
            <span class="industry-ratio">{ratio:.1%}</span>
          </div>
        """)

    return f"""
    <div class="industry-chart industry-bars">
      <div class="industry-chart-head">
        <strong>行业分布</strong>
        <span>{total} 个信号，按个股数量降序</span>
      </div>
      <div class="industry-bar-list">
        {''.join(bars)}
      </div>
    </div>
    """


def market_heat_to_html(heat_df):
    if heat_df.empty:
        return ""

    market_sections = []
    for market, market_df in heat_df.groupby("市场", sort=False):
        market_df = market_df.sort_values("日期")
        max_count = max(int(market_df["当日金叉数量"].max()), 1)
        total_count = int(market_df["当日金叉数量"].sum())
        avg_count = market_df["当日金叉数量"].mean()
        start_date = format_html_value(market_df["日期"].iloc[0])
        end_date = format_html_value(market_df["日期"].iloc[-1])
        total_days = len(market_df)
        label_interval = 5
        chart_width = 720
        chart_height = 210
        margin_left = 38
        margin_right = 12
        margin_top = 12
        margin_bottom = 34
        plot_width = chart_width - margin_left - margin_right
        plot_height = chart_height - margin_top - margin_bottom
        step = plot_width / max(total_days, 1)
        bar_width = max(4, step * 0.62)

        grid_lines = []
        for tick in np.linspace(0, max_count, 4):
            tick_value = int(round(tick))
            y = margin_top + plot_height - (tick_value / max_count * plot_height)
            grid_lines.append(f"""
              <line class="heat-grid-line" x1="{margin_left}" y1="{y:.1f}" x2="{chart_width - margin_right}" y2="{y:.1f}" />
              <text class="heat-axis-label" x="{margin_left - 8}" y="{y + 3:.1f}" text-anchor="end">{tick_value}</text>
            """)

        bars = []
        date_labels = []
        for day_idx, row in enumerate(market_df.itertuples(index=False)):
            date = getattr(row, "日期")
            count = int(getattr(row, "当日金叉数量"))
            height = count / max_count * plot_height if count > 0 else 0
            x = margin_left + day_idx * step + (step - bar_width) / 2
            y = margin_top + plot_height - height
            show_label = day_idx == 0 or day_idx == total_days - 1 or day_idx % label_interval == 0
            bars.append(f"""
              <rect class="heat-bar" x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{height:.1f}">
                <title>{escape(format_html_value(date))}: {count} 个金叉</title>
              </rect>
            """)
            if show_label:
                label = pd.Timestamp(date).strftime("%m-%d")
                label_x = x + bar_width / 2
                label_y = margin_top + plot_height + 18
                date_labels.append(f"""
                  <text class="heat-date-label" x="{label_x:.1f}" y="{label_y:.1f}" text-anchor="middle">{escape(label)}</text>
                """)

        svg = f"""
          <svg class="heat-svg" viewBox="0 0 {chart_width} {chart_height}" role="img" aria-label="{escape(str(market))}过去{HEAT_LOOKBACK_DAYS}日每日金叉数量">
            {''.join(grid_lines)}
            <line class="heat-axis-line" x1="{margin_left}" y1="{margin_top + plot_height}" x2="{chart_width - margin_right}" y2="{margin_top + plot_height}" />
            {''.join(bars)}
            {''.join(date_labels)}
          </svg>
        """
        market_sections.append(f"""
          <div class="heat-market-card">
            <div class="heat-market-head">
              <strong>{escape(str(market))}</strong>
              <span>{escape(start_date)} 至 {escape(end_date)} · 合计 {total_count} · 日均 {avg_count:.1f}</span>
            </div>
            <div class="heat-svg-wrap">
              {svg}
            </div>
          </div>
        """)

    return f"""
    <section class="section heat-section">
      <div class="section-title">
        <h2>过去{HEAT_LOOKBACK_DAYS}日各市场每日金叉数量统计</h2>
        <span>横轴为日期，柱高为当日触发金叉的个股数量</span>
      </div>
      <div class="heat-chart">
        {''.join(market_sections)}
      </div>
    </section>
    """


def write_visual_report(
    output_html,
    report_date,
    start_date,
    summary_df,
    sheet_frames,
    heat_df,
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
        signal_dates = []
        if "信号日期" in df.columns and not df.empty:
            signal_dates = [
                format_html_value(value)
                for value in pd.Series(df["信号日期"]).dropna().drop_duplicates()
            ]
        date_label = "、".join(signal_dates) if signal_dates else "暂无"
        grouped_sections.append(f"""
        <section class="section">
          <div class="section-title">
            <h2>{escape(str(sheet_name))}</h2>
            <span>信号日期：{escape(date_label)} · {section_count} 个信号</span>
          </div>
          {industry_distribution_to_html(df)}
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
	    .heat-chart {{
	      display: grid;
	      grid-template-columns: repeat(2, minmax(0, 1fr));
	      gap: 16px;
	      padding: 18px;
	    }}
	    .heat-market-card {{
	      border: 1px solid #edf0f5;
	      border-radius: 8px;
	      padding: 14px;
	      background: #fbfcfe;
	    }}
	    .heat-market-head {{
	      display: flex;
	      align-items: baseline;
	      justify-content: space-between;
	      gap: 12px;
	      margin-bottom: 12px;
	    }}
	    .heat-market-head strong {{
	      font-size: 14px;
	    }}
	    .heat-market-head span {{
	      color: var(--muted);
	      font-size: 12px;
	      overflow: hidden;
	      text-overflow: ellipsis;
	      white-space: nowrap;
	    }}
	    .heat-svg-wrap {{
	      width: 100%;
	    }}
	    .heat-svg {{
	      display: block;
	      width: 100%;
	      height: auto;
	      overflow: visible;
	    }}
	    .heat-grid-line {{
	      stroke: #e8edf4;
	      stroke-width: 1;
	    }}
	    .heat-axis-line {{
	      stroke: #cbd5e1;
	      stroke-width: 1;
	    }}
	    .heat-bar {{
	      fill: #1f7a8c;
	      rx: 3;
	      ry: 3;
	    }}
	    .heat-axis-label {{
	      fill: #8a94a6;
	      font-size: 11px;
	      font-variant-numeric: tabular-nums;
	    }}
	    .heat-date-label {{
	      fill: #667085;
	      font-size: 11px;
	    }}
	    .industry-chart {{
	      padding: 18px;
	      border-bottom: 1px solid var(--line);
	      background: #ffffff;
	    }}
	    .industry-chart-head {{
	      display: flex;
	      align-items: baseline;
	      justify-content: space-between;
	      gap: 12px;
	      margin-bottom: 12px;
	    }}
	    .industry-chart-head strong {{
	      font-size: 14px;
	    }}
	    .industry-chart-head span {{
	      color: var(--muted);
	      font-size: 12px;
	    }}
	    .industry-bar-list {{
	      display: grid;
	      gap: 9px;
	    }}
	    .industry-bar-row {{
	      display: grid;
	      grid-template-columns: minmax(92px, 150px) minmax(140px, 1fr) 36px 54px;
	      gap: 10px;
	      align-items: center;
	      min-height: 26px;
	      color: #344054;
	      font-size: 13px;
	    }}
	    .industry-name {{
	      overflow: hidden;
	      text-overflow: ellipsis;
	      white-space: nowrap;
	    }}
	    .industry-bar-track {{
	      height: 10px;
	      background: #eef2f6;
	      border-radius: 999px;
	      overflow: hidden;
	    }}
	    .industry-bar-fill {{
	      height: 100%;
	      background: #1f7a8c;
	      border-radius: 999px;
	    }}
	    .industry-count {{
	      color: var(--text);
	      font-variant-numeric: tabular-nums;
	      text-align: right;
	    }}
	    .industry-ratio {{
	      color: var(--muted);
	      font-variant-numeric: tabular-nums;
	      text-align: right;
	    }}
	    .table-wrap {{
	      overflow-x: auto;
	    }}
	    table {{
	      width: 100%;
	      border-collapse: collapse;
	      font-size: 13px;
	      min-width: 640px;
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
	      .heat-chart {{ grid-template-columns: 1fr; }}
	      h1 {{ font-size: 24px; }}
	    }}
	    @media (max-width: 560px) {{
	      .metrics {{ grid-template-columns: 1fr; }}
	      .heat-chart {{ padding: 14px; }}
	      .heat-market-head {{ align-items: start; flex-direction: column; }}
	      .industry-chart-head {{ align-items: start; flex-direction: column; }}
	      .industry-bar-row {{ grid-template-columns: minmax(72px, 104px) minmax(96px, 1fr) 30px 48px; gap: 8px; }}
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

	    {market_heat_to_html(heat_df)}

	    {''.join(grouped_sections)}
  </main>
</body>
</html>
"""
    with open(output_html, "w", encoding="utf-8") as file:
        file.write(html)


def publish_pages_index(report_date):
    if not AUTO_PUBLISH_PAGES:
        print("已跳过 GitHub Pages 自动推送：AUTO_PUBLISH_PAGES=0")
        return
    if not os.path.exists(os.path.join(REPO_DIR, ".git")):
        print(f"已跳过 GitHub Pages 自动推送：未找到 Git 仓库 {REPO_DIR}")
        return

    pages_path = os.path.relpath(PAGES_INDEX_FILE, REPO_DIR)
    status = subprocess.run(
        ["git", "-C", REPO_DIR, "status", "--porcelain", "--", pages_path],
        check=True,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        print("GitHub Pages 首页无变化，跳过提交推送。")
        return

    subprocess.run(["git", "-C", REPO_DIR, "add", pages_path], check=True)
    commit = subprocess.run(
        [
            "git",
            "-C",
            REPO_DIR,
            "commit",
            "-m",
            f"Update golden cross report page {report_date}",
        ],
        text=True,
        capture_output=True,
    )
    if commit.returncode != 0:
        print("GitHub Pages 自动提交失败：")
        print((commit.stderr or commit.stdout).strip())
        return

    branch = subprocess.check_output(
        ["git", "-C", REPO_DIR, "branch", "--show-current"],
        text=True,
    ).strip()
    subprocess.run(["git", "-C", REPO_DIR, "push", "origin", branch], check=True)
    print(f"GitHub Pages 已提交并推送：{pages_path} -> origin/{branch}")


def main():
    w.start()
    try:
        end_dt = datetime.today()
        end_date = end_dt.strftime("%Y-%m-%d")
        start_date = (end_dt.date() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        output_file = os.path.join(OUTPUT_DIR, f"全球主要股票池_最新金叉信号_{end_date}.xlsx")
        output_html = os.path.join(OUTPUT_DIR, f"全球主要股票池_最新金叉信号_{end_date}.html")

        summary_rows = []
        heat_rows = []
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
            code_to_industry = load_stock_industry_map(codes)
            print(f"{universe['name']} 股票数量：{len(codes)}")
            if universe["name"] == "全部A股":
                print(f"{universe['name']} 本地万得一级行业覆盖：{sum(bool(code_to_industry.get(code)) for code in codes)}")
            close_df = get_close_df_with_cache(
                universe,
                codes,
                market_start_date,
                eod_end_date,
                realtime_date,
            )
            signal_df, latest_date = build_latest_golden_cross_df(
                universe["name"],
                close_df,
                code_to_name,
                code_to_industry,
            )
            heat_rows.extend(build_heat_rows(universe["name"], close_df, len(codes)))
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
        heat_df = pd.DataFrame(heat_rows)

        with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
            summary_df.to_excel(writer, sheet_name="摘要", index=False)
            heat_df.to_excel(writer, sheet_name="近30日热度", index=False)
            for sheet_name, df in sheet_frames.items():
                df.to_excel(writer, sheet_name=sheet_name[:31], index=False)

        write_visual_report(
            output_html,
            end_date,
            start_date,
            summary_df,
            sheet_frames,
            heat_df,
        )
        write_visual_report(
            PAGES_INDEX_FILE,
            end_date,
            start_date,
            summary_df,
            sheet_frames,
            heat_df,
        )

        print("\n【摘要】")
        print(summary_df)
        print("输出完成：", output_file)
        print("可视化输出完成：", output_html)
        print("GitHub Pages 首页已更新：", PAGES_INDEX_FILE)
        publish_pages_index(end_date)
    finally:
        w.close()


if __name__ == "__main__":
    main()
