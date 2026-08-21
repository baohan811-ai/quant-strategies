#!/usr/bin/env python3
"""纳指场内基金监控：盘中同类异常跌幅 + 日频折溢价列表。

intraday 模式使用 Wind WSQ 实时行情，只在跟踪相同指数的产品之间比较；
premium 模式使用 Wind WSD 日频折溢价，输出最新值和近60个交易日均值。
脚本只监控和提醒，不自动下单。
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from urllib import request

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPT_ROOT = SCRIPT_DIR.parent
OUTPUT_DIR = SCRIPT_ROOT.parent / "输出" / "纳指ETF监控"

# 目前在沪深交易所挂牌、跟踪纳斯达克100指数的 ETF/LOF。
PRODUCTS = [
    {"code": "513100.SH", "name": "纳指ETF国泰", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "513300.SH", "name": "纳斯达克ETF华夏", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "513390.SH", "name": "纳指100ETF博时", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "513110.SH", "name": "纳指ETF华泰柏瑞", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "513870.SH", "name": "纳指ETF富国", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "159941.SZ", "name": "纳指ETF广发", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "159632.SZ", "name": "纳斯达克ETF华安", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "159659.SZ", "name": "纳斯达克100ETF招商", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "159660.SZ", "name": "纳指ETF汇添富", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "159501.SZ", "name": "纳指ETF嘉实", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "159513.SZ", "name": "纳斯达克100ETF大成", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "159696.SZ", "name": "纳指ETF易方达", "type": "ETF", "index": "纳斯达克100", "peer_group": "NDX"},
    {"code": "161130.SZ", "name": "纳斯达克100LOF", "type": "LOF", "index": "纳斯达克100", "peer_group": "NDX"},
]
PRODUCT_BY_CODE = {item["code"]: item for item in PRODUCTS}


def parse_args():
    parser = argparse.ArgumentParser(description="监控纳指场内基金盘中异常跌幅或查询折溢价")
    parser.add_argument(
        "--mode", choices=["intraday", "premium", "replay", "backtest"], default="intraday",
        help="intraday=盘中监控；premium=折溢价；replay=回放；backtest=阈值收益检验",
    )
    parser.add_argument("--interval", type=float, default=5.0, help="盘中轮询间隔秒数，默认5秒")
    parser.add_argument(
        "--relative-drop", type=float, default=2.0,
        help="相对同类中位数落后多少个百分点时提醒，默认2.0",
    )
    parser.add_argument(
        "--window-minutes", type=float, default=3.0,
        help="识别突然下跌的滚动窗口分钟数，默认3分钟",
    )
    parser.add_argument("--cooldown-minutes", type=float, default=15.0, help="同一产品重复提醒冷却时间")
    parser.add_argument(
        "--min-amount-mn", type=float, default=1.0,
        help="盘中最低累计成交额，百万元，默认1；过滤无成交或极低流动性报价",
    )
    parser.add_argument("--once", action="store_true", help="盘中模式只检查一次后退出")
    parser.add_argument("--ignore-market-hours", action="store_true", help="忽略A股交易时段限制，仅用于诊断")
    parser.add_argument("--date", default=date.today().isoformat(), help="折溢价查询截止日 YYYY-MM-DD")
    parser.add_argument("--premium-days", type=int, default=60, help="折溢价均值交易日数，默认60")
    parser.add_argument("--replay-days", type=int, default=5, help="历史回放交易日数，默认5")
    parser.add_argument("--replay-limit", type=int, default=10, help="历史回放最多显示最近多少条提示")
    parser.add_argument("--backtest-days", type=int, default=120, help="回测交易日数，默认120")
    parser.add_argument("--thresholds", default="1.5,2.0", help="回测阈值列表，默认1.5,2.0")
    parser.add_argument("--horizons", default="5,15,30,60", help="回测持有分钟列表")
    parser.add_argument("--daily-horizons", default="1,3,5,10", help="回测持有交易日列表")
    parser.add_argument("--cost-bps", type=float, default=10.0, help="估算单次往返成本，基点，默认10")
    parser.add_argument(
        "--premium-lag-days", type=int, default=2,
        help="回测折溢价特征滞后交易日数，默认2，避免使用当时尚未披露的净值",
    )
    parser.add_argument(
        "--notify", choices=["macos", "none"], default="macos",
        help="盘中信号提醒方式，默认 macOS 通知中心",
    )
    parser.add_argument(
        "--webhook-url", default=os.environ.get("NASDAQ_ETF_WEBHOOK_URL", ""),
        help="可选：接收 JSON 的 HTTPS webhook；也可设置 NASDAQ_ETF_WEBHOOK_URL",
    )
    parser.add_argument("--no-save", action="store_true", help="不保存盘中信号或折溢价列表")
    return parser.parse_args()


def clean_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def ensure_wind_ok(data, label):
    if data.ErrorCode != 0:
        raise RuntimeError(f"Wind {label} failed: {data.ErrorCode} {getattr(data, 'Data', None)}")


def wind_start():
    try:
        from WindPy import w
    except ImportError as exc:
        raise RuntimeError("未找到 WindPy，请使用已安装 Wind Python API 的解释器运行") from exc
    result = w.start()
    if getattr(result, "ErrorCode", 0) != 0:
        raise RuntimeError(f"Wind 启动失败: {result.ErrorCode}")
    return w


def send_macos_notification(title, message):
    if sys.platform != "darwin":
        print("提示：当前不是 macOS，已跳过通知中心提醒。", file=sys.stderr)
        return
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe_message}" with title "{safe_title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=True)


def send_webhook(url, title, message, records):
    payload = json.dumps(
        {"title": title, "message": message, "records": records}, ensure_ascii=False,
    ).encode("utf-8")
    req = request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=15) as response:
        if response.status >= 300:
            raise RuntimeError(f"Webhook 返回 HTTP {response.status}")


def fetch_realtime_snapshot(w):
    codes = [item["code"] for item in PRODUCTS]
    fields = ["rt_last", "rt_pre_close", "rt_amt"]
    data = w.wsq(",".join(codes), ",".join(fields))
    ensure_wind_ok(data, "wsq realtime")
    rows = []
    for index, code in enumerate(data.Codes):
        product = PRODUCT_BY_CODE.get(code)
        if product is None:
            continue
        values = {field: clean_number(data.Data[pos][index]) for pos, field in enumerate(fields)}
        last = values["rt_last"]
        pre_close = values["rt_pre_close"]
        # 不直接使用 RT_PCT_CHG：Wind WSQ 返回的是小数比例（0.0126=1.26%），
        # 为避免不同终端字段缩放口径差异，统一由实时价和昨收自行计算百分数。
        pct_chg = None
        if last is not None and pre_close not in (None, 0):
            pct_chg = (last / pre_close - 1) * 100
        rows.append(
            {
                **product, "last": last, "pre_close": pre_close, "pct_chg": pct_chg,
                "amount_mn": (values["rt_amt"] or 0) / 1_000_000,
            }
        )
    snapshot = pd.DataFrame(rows)
    if snapshot.empty:
        raise RuntimeError("Wind 未返回可用实时行情")
    return snapshot


def calculate_intraday_signals(current, past, relative_drop, min_amount_mn, window_minutes):
    current = current.copy()
    signals = []
    for _, group in current.groupby("peer_group"):
        valid = group[group["pct_chg"].notna() & group["last"].notna()].copy()
        if len(valid) < 3:
            continue
        peer_day_median = valid["pct_chg"].median()
        valid["day_relative_ppt"] = valid["pct_chg"] - peer_day_median
        if past is not None:
            old = past.set_index("code")["last"]
            valid["past_last"] = valid["code"].map(old)
            valid["window_return_pct"] = (valid["last"] / valid["past_last"] - 1) * 100
            peer_window_median = valid["window_return_pct"].median(skipna=True)
            valid["window_relative_ppt"] = valid["window_return_pct"] - peer_window_median
        else:
            valid["window_return_pct"] = pd.NA
            valid["window_relative_ppt"] = pd.NA

        for _, row in valid.iterrows():
            if row["amount_mn"] < min_amount_mn:
                continue
            day_hit = row["day_relative_ppt"] <= -relative_drop
            window_relative = clean_number(row["window_relative_ppt"])
            # 同时覆盖自身突然下跌，以及同类快速上涨但该产品明显没跟上的补涨机会。
            window_hit = window_relative is not None and window_relative <= -relative_drop
            if not (day_hit or window_hit):
                continue
            trigger = "较昨收相对跌幅" if day_hit else f"{window_minutes:g}分钟相对落后"
            signals.append(
                {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "signal": trigger, "code": row["code"], "name": row["name"],
                    "last": row["last"], "pct_chg": row["pct_chg"],
                    "peer_day_median": peer_day_median,
                    "day_relative_ppt": row["day_relative_ppt"],
                    "window_return_pct": clean_number(row["window_return_pct"]),
                    "window_relative_ppt": window_relative, "amount_mn": row["amount_mn"],
                }
            )
    return pd.DataFrame(signals)


def intraday_message(row, window_minutes):
    message = (
        f"{row['name']}({row['code']}) 现价 {row['last']:.3f}，"
        f"较昨收 {row['pct_chg']:+.2f}%，同类较昨收中位数 {row['peer_day_median']:+.2f}%，"
        f"相对落后 {abs(row['day_relative_ppt']):.2f} 个百分点"
    )
    window_relative = clean_number(row.get("window_relative_ppt"))
    if window_relative is not None:
        message += f"；近{window_minutes:g}分钟相对落后 {abs(window_relative):.2f} 个百分点"
    return message


def append_intraday_alert(row):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"盘中异常信号_{date.today().isoformat()}.csv"
    pd.DataFrame([row]).to_csv(
        path, mode="a", header=not path.exists(), index=False,
        encoding="utf-8-sig", float_format="%.4f",
    )


def in_a_share_session(now):
    if now.weekday() >= 5:
        return False
    current = now.time()
    return clock_time(9, 30) <= current <= clock_time(11, 30) or clock_time(13, 0) <= current <= clock_time(15, 0)


def seconds_until_next_session(now):
    current = now.time()
    if now.weekday() >= 5 or current > clock_time(15, 0):
        return None
    if current < clock_time(9, 30):
        target = datetime.combine(now.date(), clock_time(9, 30))
        return max(1.0, (target - now).total_seconds())
    if clock_time(11, 30) < current < clock_time(13, 0):
        target = datetime.combine(now.date(), clock_time(13, 0))
        return max(1.0, (target - now).total_seconds())
    return 0.0


def is_wind_trade_day(w, query_date):
    data = w.tdays(query_date.isoformat(), query_date.isoformat(), "TradingCalendar=SSE")
    ensure_wind_ok(data, f"tdays {query_date}")
    return bool(getattr(data, "Times", None))


def run_intraday(w, args):
    if args.interval < 1:
        raise ValueError("--interval 不能小于1秒")
    if args.relative_drop <= 0 or args.window_minutes <= 0:
        raise ValueError("--relative-drop 和 --window-minutes 必须大于0")
    if not args.ignore_market_hours and not is_wind_trade_day(w, date.today()):
        print(f"{date.today().isoformat()} 不是上交所交易日，监控退出。")
        return
    print(
        f"开始盘中监控：每 {args.interval:g} 秒检查；相对同类落后 "
        f"{args.relative_drop:g} 个百分点提醒；滚动窗口 {args.window_minutes:g} 分钟。"
    )
    history = deque()
    last_alert_at = {}
    active_codes = set()
    window_seconds = args.window_minutes * 60
    cooldown_seconds = args.cooldown_minutes * 60
    while True:
        now = datetime.now()
        if not args.ignore_market_hours and not in_a_share_session(now):
            if args.once:
                print("当前不在A股交易时段，未检查；诊断可加 --ignore-market-hours。")
                return
            wait_seconds = seconds_until_next_session(now)
            if wait_seconds is None:
                print("当日交易时段已结束，监控退出。")
                return
            time.sleep(min(wait_seconds, 60))
            continue

        snapshot = fetch_realtime_snapshot(w)
        timestamp = time.monotonic()
        history.append((timestamp, snapshot))
        while history and timestamp - history[0][0] > window_seconds * 2:
            history.popleft()
        eligible = [item for item in history if timestamp - item[0] >= window_seconds]
        past = eligible[-1][1] if eligible else None
        signals = calculate_intraday_signals(
            snapshot, past, args.relative_drop, args.min_amount_mn, args.window_minutes,
        )
        print(
            f"[{datetime.now():%H:%M:%S}] 已检查 {snapshot['last'].notna().sum()} 只产品，异常 {len(signals)} 条",
            flush=True,
        )
        current_signal_codes = set(signals["code"]) if not signals.empty else set()
        active_codes.intersection_update(current_signal_codes)
        for _, signal in signals.iterrows():
            code = signal["code"]
            if code in active_codes:
                continue
            previous_alert = last_alert_at.get(code, 0)
            if timestamp - previous_alert < cooldown_seconds:
                active_codes.add(code)
                continue
            record = signal.to_dict()
            message = intraday_message(record, args.window_minutes)
            print(f"交易机会提醒：{message}", flush=True)
            if not args.no_save:
                append_intraday_alert(record)
            if args.notify == "macos":
                send_macos_notification("纳指ETF盘中异常", message)
            if args.webhook_url:
                send_webhook(args.webhook_url, "纳指ETF盘中异常", message, [record])
            last_alert_at[code] = timestamp
            active_codes.add(code)
        if args.once:
            return
        time.sleep(args.interval)


def recent_trade_dates(w, end_date, count):
    if count < 1:
        raise ValueError("--replay-days 不能小于1")
    start_date = end_date - timedelta(days=max(20, count * 3))
    data = w.tdays(start_date.isoformat(), end_date.isoformat(), "TradingCalendar=SSE")
    ensure_wind_ok(data, "tdays replay")
    dates = [pd.Timestamp(item).date() for item in data.Times]
    # 当天尚未收盘时只回放此前完整交易日。
    if dates and dates[-1] == date.today() and datetime.now().time() < clock_time(15, 5):
        dates = dates[:-1]
    return dates[-count:]


def fetch_replay_minutes(w, trade_dates):
    if not trade_dates:
        raise RuntimeError("没有可回放的完整交易日")
    first_date, last_date = trade_dates[0], trade_dates[-1]
    minute_frames = []
    daily_close = {}

    for product in PRODUCTS:
        code = product["code"]
        daily = w.wsd(
            code, "close", (first_date - timedelta(days=15)).isoformat(),
            last_date.isoformat(), "PriceAdj=F",
        )
        ensure_wind_ok(daily, f"wsd replay close {code}")
        close_series = pd.Series(
            [clean_number(value) for value in daily.Data[0]],
            index=pd.to_datetime(daily.Times), dtype="float64",
        ).dropna().sort_index()
        daily_close[code] = close_series

        minute = w.wsi(
            code, "close",
            f"{first_date.isoformat()} 09:30:00",
            f"{last_date.isoformat()} 15:00:00",
            "BarSize=1;showblank=0",
        )
        ensure_wind_ok(minute, f"wsi replay {code}")
        if not minute.Times:
            continue
        frame = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(minute.Times),
                "code": code,
                "last": [clean_number(value) for value in minute.Data[0]],
            }
        ).dropna(subset=["last"])
        frame = frame[frame["timestamp"].dt.date.isin(trade_dates)]
        minute_frames.append(frame)

    if not minute_frames:
        raise RuntimeError("Wind 未返回历史分钟行情")
    minutes = pd.concat(minute_frames, ignore_index=True)
    return minutes, daily_close


def replay_snapshot_at(timestamp, prices, daily_close):
    rows = []
    trade_date = timestamp.date()
    for code, last in prices.items():
        product = PRODUCT_BY_CODE[code]
        close_series = daily_close[code]
        previous = close_series[close_series.index.date < trade_date]
        if previous.empty:
            continue
        pre_close = previous.iloc[-1]
        rows.append(
            {
                **product, "last": last, "pre_close": pre_close,
                "pct_chg": (last / pre_close - 1) * 100, "amount_mn": float("inf"),
            }
        )
    return pd.DataFrame(rows)


def run_replay(w, args):
    end_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    trade_dates = recent_trade_dates(w, end_date, args.replay_days)
    minutes, daily_close = fetch_replay_minutes(w, trade_dates)
    pivot = minutes.pivot_table(index="timestamp", columns="code", values="last", aggfunc="last").sort_index()
    alerts = []
    last_alert_at = {}
    active_codes = set()
    window_delta = pd.Timedelta(minutes=args.window_minutes)
    cooldown_delta = pd.Timedelta(minutes=args.cooldown_minutes)

    for trade_date in trade_dates:
        active_codes.clear()
        day_prices = pivot[pivot.index.date == trade_date].ffill()
        for timestamp, prices in day_prices.iterrows():
            prices = prices.dropna()
            if len(prices) < 3:
                continue
            current = replay_snapshot_at(timestamp, prices, daily_close)
            past_timestamp = timestamp - window_delta
            past = None
            eligible = day_prices.loc[:past_timestamp]
            if not eligible.empty:
                past_prices = eligible.iloc[-1].dropna()
                past = replay_snapshot_at(eligible.index[-1], past_prices, daily_close)
            signals = calculate_intraday_signals(
                current, past, args.relative_drop, 0, args.window_minutes,
            )
            current_signal_codes = set(signals["code"]) if not signals.empty else set()
            active_codes.intersection_update(current_signal_codes)
            for _, signal in signals.iterrows():
                code = signal["code"]
                if code in active_codes:
                    continue
                previous = last_alert_at.get(code)
                if previous is not None and timestamp - previous < cooldown_delta:
                    active_codes.add(code)
                    continue
                record = signal.to_dict()
                record["time"] = timestamp.strftime("%Y-%m-%d %H:%M:%S")
                alerts.append(record)
                last_alert_at[code] = timestamp
                active_codes.add(code)

    if not alerts:
        print(
            f"回放 {trade_dates[0]} 至 {trade_dates[-1]}：未出现相对同类落后 "
            f"{args.relative_drop:g} 个百分点的提示。"
        )
        return pd.DataFrame()
    table = pd.DataFrame(alerts).sort_values("time")
    shown = table.tail(args.replay_limit).copy()
    print(
        f"回放 {trade_dates[0]} 至 {trade_dates[-1]}：共 {len(table)} 条提示，"
        f"以下为最近 {len(shown)} 条："
    )
    for _, row in shown.iterrows():
        print(f"[{row['time']}] {row['signal']}：{intraday_message(row.to_dict(), args.window_minutes)}")
    return table


def parse_number_list(value, cast=float):
    numbers = [cast(item.strip()) for item in value.split(",") if item.strip()]
    if not numbers or any(number <= 0 for number in numbers):
        raise ValueError(f"无效的正数列表: {value}")
    return numbers


def collect_replay_events(pivot, daily_close, trade_dates, threshold, window_minutes, cooldown_minutes):
    events = []
    last_alert_at = {}
    active_codes = set()
    window_delta = pd.Timedelta(minutes=window_minutes)
    cooldown_delta = pd.Timedelta(minutes=cooldown_minutes)

    for trade_date in trade_dates:
        active_codes.clear()
        raw_day = pivot[pivot.index.date == trade_date]
        day_prices = raw_day.ffill()
        for timestamp, prices in day_prices.iterrows():
            prices = prices.dropna()
            if len(prices) < 3:
                continue
            current = replay_snapshot_at(timestamp, prices, daily_close)
            eligible = day_prices.loc[: timestamp - window_delta]
            past = None
            if not eligible.empty:
                past_prices = eligible.iloc[-1].dropna()
                past = replay_snapshot_at(eligible.index[-1], past_prices, daily_close)
            signals = calculate_intraday_signals(current, past, threshold, 0, window_minutes)
            # 目标产品在触发分钟必须有实际成交，排除前值填充造成的陈旧价格信号。
            fresh_codes = set(raw_day.loc[timestamp].dropna().index)
            if not signals.empty:
                signals = signals[signals["code"].isin(fresh_codes)]
            current_codes = set(signals["code"]) if not signals.empty else set()
            active_codes.intersection_update(current_codes)
            for _, signal in signals.iterrows():
                code = signal["code"]
                if code in active_codes:
                    continue
                previous = last_alert_at.get(code)
                if previous is not None and timestamp - previous < cooldown_delta:
                    active_codes.add(code)
                    continue
                record = signal.to_dict()
                record["timestamp"] = timestamp
                events.append(record)
                last_alert_at[code] = timestamp
                active_codes.add(code)
    return pd.DataFrame(events)


def first_actual_price(raw_day, code, start_position, max_wait_bars=2):
    if code not in raw_day.columns:
        return None, None
    end_position = min(len(raw_day), start_position + max_wait_bars + 1)
    candidates = raw_day.iloc[start_position:end_position][code].dropna()
    if candidates.empty:
        return None, None
    return candidates.index[0], float(candidates.iloc[0])


def fetch_backtest_premium_history(w, trade_dates):
    start_date = trade_dates[0] - timedelta(days=180)
    histories = {}
    for product in PRODUCTS:
        data = w.wsd(
            product["code"], "discount_ratio", start_date.isoformat(),
            trade_dates[-1].isoformat(), "",
        )
        ensure_wind_ok(data, f"wsd backtest premium {product['code']}")
        series = pd.Series(
            [clean_number(value) for value in data.Data[0]],
            index=pd.to_datetime(data.Times), dtype="float64",
        ).dropna().sort_index()
        histories[product["code"]] = series
    return histories


def value_on_date(series, query_date):
    values = series[series.index.date == query_date]
    return None if values.empty else float(values.iloc[-1])


def attach_premium_features(events, premium_histories, trade_dates, lag_days):
    if events.empty:
        return events
    if lag_days < 1:
        raise ValueError("--premium-lag-days 至少为1，不能使用信号日收盘后数据")
    trade_date_position = {item: pos for pos, item in enumerate(trade_dates)}
    rows = []
    for _, event in events.iterrows():
        row = event.to_dict()
        signal_date = pd.Timestamp(row["timestamp"]).date()
        position = trade_date_position.get(signal_date)
        if position is None or position < lag_days:
            continue
        reference_date = trade_dates[position - lag_days]
        code = row["code"]
        own_series = premium_histories[code]
        own_premium = value_on_date(own_series, reference_date)
        own_history = own_series[own_series.index.date <= reference_date].tail(60)
        peer_premiums = [
            value_on_date(series, reference_date)
            for peer_code, series in premium_histories.items()
            if peer_code != code
        ]
        peer_premiums = [value for value in peer_premiums if value is not None]
        row["premium_reference_date"] = reference_date.isoformat()
        row["lagged_premium_pct"] = own_premium
        row["own_60d_premium_mean_pct"] = None if own_history.empty else float(own_history.mean())
        row["peer_premium_median_pct"] = None if not peer_premiums else float(pd.Series(peer_premiums).median())
        row["premium_not_high_vs_own"] = (
            own_premium is not None
            and row["own_60d_premium_mean_pct"] is not None
            and own_premium <= row["own_60d_premium_mean_pct"]
        )
        row["premium_not_high_vs_peer"] = (
            own_premium is not None
            and row["peer_premium_median_pct"] is not None
            and own_premium <= row["peer_premium_median_pct"]
        )
        rows.append(row)
    return pd.DataFrame(rows)


def daily_close_on(daily_close, code, query_date):
    return value_on_date(daily_close[code], query_date)


def evaluate_events(events, pivot, horizons, daily_horizons, cost_bps, daily_close, trade_dates):
    rows = []
    if events.empty:
        return pd.DataFrame()
    for _, event in events.iterrows():
        timestamp = pd.Timestamp(event["timestamp"])
        code = event["code"]
        raw_day = pivot[pivot.index.date == timestamp.date()]
        filled_day = raw_day.ffill()
        positions = raw_day.index.get_indexer([timestamp])
        if positions[0] < 0:
            continue
        signal_position = int(positions[0])
        entry_time, entry_price = first_actual_price(raw_day, code, signal_position + 1)
        if entry_time is None:
            continue
        entry_position = int(raw_day.index.get_indexer([entry_time])[0])
        peer_codes = [item for item in PRODUCT_BY_CODE if item != code]

        base = {
            "signal_time": timestamp,
            "entry_time": entry_time,
            "code": code,
            "name": event["name"],
            "signal": event["signal"],
            "threshold": event.get("threshold"),
            "lagged_premium_pct": event.get("lagged_premium_pct"),
            "own_60d_premium_mean_pct": event.get("own_60d_premium_mean_pct"),
            "peer_premium_median_pct": event.get("peer_premium_median_pct"),
            "premium_not_high_vs_own": bool(event.get("premium_not_high_vs_own", False)),
            "premium_not_high_vs_peer": bool(event.get("premium_not_high_vs_peer", False)),
        }
        for horizon in [*horizons, "close"]:
            if horizon == "close":
                target_position = len(raw_day) - 1
                label = "收盘"
            else:
                target_position = entry_position + int(horizon)
                label = f"{int(horizon)}分钟"
                if target_position >= len(raw_day):
                    continue
            exit_time, exit_price = first_actual_price(raw_day, code, target_position)
            if exit_time is None:
                continue
            exit_position = int(raw_day.index.get_indexer([exit_time])[0])
            entry_peers = filled_day.iloc[entry_position].reindex(peer_codes)
            exit_peers = filled_day.iloc[exit_position].reindex(peer_codes)
            peer_returns = (exit_peers / entry_peers - 1) * 100
            peer_returns = peer_returns.replace([float("inf"), float("-inf")], pd.NA).dropna()
            if len(peer_returns) < 6:
                continue
            target_return = (exit_price / entry_price - 1) * 100
            peer_return = float(peer_returns.median())
            alpha = target_return - peer_return
            rows.append(
                {
                    **base,
                    "horizon": label,
                    "exit_time": exit_time,
                    "target_return_pct": target_return,
                    "peer_return_pct": peer_return,
                    "alpha_pct": alpha,
                    "net_alpha_pct": alpha - cost_bps / 100,
                }
            )
        signal_date_position = trade_dates.index(timestamp.date())
        entry_peers = filled_day.iloc[entry_position].reindex(peer_codes)
        for horizon_days in daily_horizons:
            exit_date_position = signal_date_position + int(horizon_days)
            if exit_date_position >= len(trade_dates):
                continue
            exit_date = trade_dates[exit_date_position]
            exit_price = daily_close_on(daily_close, code, exit_date)
            if exit_price is None:
                continue
            peer_exit_values = {
                peer_code: daily_close_on(daily_close, peer_code, exit_date)
                for peer_code in peer_codes
            }
            exit_peers = pd.Series(peer_exit_values, dtype="float64")
            peer_returns = (exit_peers / entry_peers - 1) * 100
            peer_returns = peer_returns.replace([float("inf"), float("-inf")], pd.NA).dropna()
            if len(peer_returns) < 6:
                continue
            target_return = (exit_price / entry_price - 1) * 100
            peer_return = float(peer_returns.median())
            alpha = target_return - peer_return
            rows.append(
                {
                    **base,
                    "horizon": f"{int(horizon_days)}日",
                    "exit_time": pd.Timestamp(exit_date),
                    "target_return_pct": target_return,
                    "peer_return_pct": peer_return,
                    "alpha_pct": alpha,
                    "net_alpha_pct": alpha - cost_bps / 100,
                }
            )
    return pd.DataFrame(rows)


def summarize_backtest(evaluations):
    if evaluations.empty:
        return pd.DataFrame()
    rows = []
    filters = [
        ("不筛选", lambda frame: pd.Series(True, index=frame.index)),
        ("不高于同类", lambda frame: frame["premium_not_high_vs_peer"]),
        ("不高于自身60日均值", lambda frame: frame["premium_not_high_vs_own"]),
        (
            "同时不高于同类和自身均值",
            lambda frame: frame["premium_not_high_vs_peer"] & frame["premium_not_high_vs_own"],
        ),
    ]
    for filter_name, mask_func in filters:
        filtered = evaluations[mask_func(evaluations)].copy()
        for (threshold, horizon), group in filtered.groupby(["threshold", "horizon"], sort=False):
            alpha = group["alpha_pct"]
            net_alpha = group["net_alpha_pct"]
            rows.append(
                {
                    "溢价过滤": filter_name,
                    "阈值(百分点)": threshold,
                    "持有期": horizon,
                    "样本数": len(group),
                    "平均超额收益(%)": alpha.mean(),
                    "中位超额收益(%)": alpha.median(),
                    "超额胜率(%)": (alpha > 0).mean() * 100,
                    "扣成本平均超额(%)": net_alpha.mean(),
                    "扣成本中位超额(%)": net_alpha.median(),
                    "平均绝对收益(%)": group["target_return_pct"].mean(),
                }
            )
    horizon_order = {
        "5分钟": 0, "15分钟": 1, "30分钟": 2, "60分钟": 3,
        "120分钟": 4, "收盘": 5, "1日": 6, "3日": 7, "5日": 8, "10日": 9,
        "20日": 10, "40日": 11, "60日": 12,
    }
    filter_order = {name: pos for pos, (name, _) in enumerate(filters)}
    summary = pd.DataFrame(rows)
    summary["_order"] = summary["持有期"].map(horizon_order).fillna(99)
    summary["_filter_order"] = summary["溢价过滤"].map(filter_order)
    return summary.sort_values(
        ["阈值(百分点)", "_filter_order", "_order"]
    ).drop(columns=["_order", "_filter_order"])


def run_backtest(w, args):
    thresholds = parse_number_list(args.thresholds, float)
    horizons = parse_number_list(args.horizons, int)
    daily_horizons = parse_number_list(args.daily_horizons, int)
    end_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    trade_dates = recent_trade_dates(w, end_date, args.backtest_days)
    minutes, daily_close = fetch_replay_minutes(w, trade_dates)
    premium_histories = fetch_backtest_premium_history(w, trade_dates)
    pivot = minutes.pivot_table(index="timestamp", columns="code", values="last", aggfunc="last").sort_index()
    all_events = []
    all_evaluations = []
    for threshold in thresholds:
        events = collect_replay_events(
            pivot, daily_close, trade_dates, threshold, args.window_minutes, args.cooldown_minutes,
        )
        if not events.empty:
            events["threshold"] = threshold
            events = attach_premium_features(
                events, premium_histories, trade_dates, args.premium_lag_days,
            )
            evaluated = evaluate_events(
                events, pivot, horizons, daily_horizons, args.cost_bps, daily_close, trade_dates,
            )
            all_events.append(events)
            all_evaluations.append(evaluated)
        peer_ok = int(events["premium_not_high_vs_peer"].sum()) if not events.empty else 0
        own_ok = int(events["premium_not_high_vs_own"].sum()) if not events.empty else 0
        both_ok = int(
            (events["premium_not_high_vs_peer"] & events["premium_not_high_vs_own"]).sum()
        ) if not events.empty else 0
        print(
            f"阈值 {threshold:g}：{len(events)} 个独立信号；"
            f"不高于同类 {peer_ok}；不高于自身60日均值 {own_ok}；两项同时满足 {both_ok}"
        )
    events = pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame()
    evaluations = pd.concat(all_evaluations, ignore_index=True) if all_evaluations else pd.DataFrame()
    summary = summarize_backtest(evaluations)
    print(f"回测区间：{trade_dates[0]} 至 {trade_dates[-1]}，共 {len(trade_dates)} 个交易日")
    if summary.empty:
        print("没有足够的可成交样本计算收益。")
    else:
        print("\n事件研究结果（下一分钟成交；扣成本按单次往返估算）：")
        print(summary.to_string(index=False, float_format=lambda value: f"{value:.3f}"))
    return events, evaluations, summary


def fetch_premium_table(w, target_date, trading_days):
    if trading_days < 2:
        raise ValueError("--premium-days 不能小于2")
    start_date = target_date - timedelta(days=max(150, int(trading_days * 2.5)))
    rows = []
    for product in PRODUCTS:
        data = w.wsd(
            product["code"], "discount_ratio,amt",
            start_date.isoformat(), target_date.isoformat(), "",
        )
        ensure_wind_ok(data, f"wsd premium {product['code']}")
        frame = pd.DataFrame(
            {
                "trade_date": pd.to_datetime(data.Times),
                "premium_pct": [clean_number(value) for value in data.Data[0]],
                "amount": [clean_number(value) for value in data.Data[1]],
            }
        )
        frame = frame[frame["premium_pct"].notna() & frame["amount"].fillna(0).gt(0)].tail(trading_days)
        if frame.empty:
            continue
        latest = frame.iloc[-1]
        rows.append(
            {
                "数据日期": latest["trade_date"].date().isoformat(), "代码": product["code"],
                "名称": product["name"], "类型": product["type"], "跟踪指数": product["index"],
                "最新折溢价率(%)": latest["premium_pct"],
                f"近{trading_days}日平均折溢价率(%)": frame["premium_pct"].mean(),
                "有效交易日数": len(frame), "最新成交额(百万元)": latest["amount"] / 1_000_000,
            }
        )
    table = pd.DataFrame(rows)
    if table.empty:
        raise RuntimeError("Wind 未返回可用折溢价数据")
    return table.sort_values(
        ["跟踪指数", "最新折溢价率(%)", "最新成交额(百万元)"], ascending=[True, True, False]
    )


def save_premium_table(table, target_date, trading_days):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / f"纳指ETF_LOF折溢价_{target_date}.csv"
    html_path = OUTPUT_DIR / f"纳指ETF_LOF折溢价_{target_date}.html"
    table.to_csv(csv_path, index=False, encoding="utf-8-sig", float_format="%.4f")
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>纳指ETF/LOF折溢价 {target_date}</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:32px;color:#172033}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d7dde5;padding:7px}}
th{{background:#f2f5f8}}td:nth-last-child(-n+4){{text-align:right}}</style></head><body>
<h1>纳指ETF/LOF折溢价</h1><p>最新折溢价率与近{trading_days}个有效交易日均值；正数为溢价，负数为折价。</p>
{table.to_html(index=False, float_format=lambda value: f'{value:.4f}')}</body></html>"""
    html_path.write_text(html, encoding="utf-8")
    return csv_path, html_path


def run_premium(w, args):
    target_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    table = fetch_premium_table(w, target_date, args.premium_days)
    print(table.to_string(index=False, float_format=lambda value: f"{value:.2f}"))
    if not args.no_save:
        csv_path, html_path = save_premium_table(table, target_date, args.premium_days)
        print(f"\n已保存：{csv_path}")
        print(f"已保存：{html_path}")
    return table


def main():
    args = parse_args()
    w = wind_start()
    try:
        if args.mode == "intraday":
            run_intraday(w, args)
        elif args.mode == "premium":
            run_premium(w, args)
        elif args.mode == "replay":
            run_replay(w, args)
        else:
            run_backtest(w, args)
    finally:
        w.close()


if __name__ == "__main__":
    main()
