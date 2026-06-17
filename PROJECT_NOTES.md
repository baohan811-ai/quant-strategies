# 项目备忘

## 重要提醒

- 构建全 A 股基础基本面 SQLite 数据库前，需要先提醒：这会大量调用 Wind，可能耗时较久，也可能触发 Wind 请求额度或频率限制。
- 开跑前确认 WindPy 已登录、Wind 终端可用。
- 做策略参数优化时，偏好使用枚举法/网格搜索，尽量让参数组合、评分口径和结果排序可复现、可审计。
- 枚举优化结果应保留关键中间结果和最终排名，方便之后回看为什么选择某组参数。
- 策略优化、参数选择、运行口径等项目级记录统一更新本文件，不要在 `执行中策略/` 或其子目录里新建零散 `.md` 记录文件。
- 量化开发协作中，用户是新手但希望学习专业流程；回答和改代码时应主动补充专业开发者常用的检查建议供参考，例如数据覆盖、历史成分、未来函数、停牌/退市、复权、交易执行、样本外验证、参数过拟合、日志和诊断表等，但建议应简洁，不喧宾夺主。
- 每次补数据库完成后，必须输出完整完成度表，至少包含数据内容、当前范围/截面、股票/标的数、成功批次、失败/跳过批次、非空数据量、完成度和备注；同时单列剩余问题，例如额度不足、Wind 权限/限额失败、空结果待重试、覆盖率低于预期的字段。
- 基础基本面库补数时，Wind 请求成功但整批稳定返回空值的批次，不要长期重复重试；连续空结果默认第二次标记为 `confirmed_empty`，后续直接跳过并在完成度表中单列展示。
- 基础基本面库补数时，如果显式设置了 `WIND_CELL_BUDGET`，默认允许重试此前 Wind 限额/权限类失败批次；只有在未设置新预算且未设置 `WIND_RETRY_QUOTA_FAILED=1` 时才跳过旧失败批次。可用 `WIND_RETRY_QUOTA_FAILED=0` 强制不重试。
- 行情缓存做增量更新时，不能只从“缓存最后日期 + 1”开始追加；最近若干交易日必须覆盖刷新，尤其是当天/最近交易日，避免 Wind 日线尚未最终更新时写入旧值。
- 最新信号出现异常时，优先直接从 WindPy 拉单票当前数据，与缓存逐日对比，再判断是否是策略逻辑问题。
- 跨市场行情必须显式指定对应交易日历，不能依赖 Wind 默认日历。港股使用 `TradingCalendar=HKEX`，美股/标普500 使用 `TradingCalendar=NYSE`；否则 A 股休市但港股/美股开市的日期会被漏掉，MA5/MA60 和金叉日期都会算错。
- A 股策略已迁移为优先从 `缓存/本地行情数据库.sqlite3` 读取日线行情；中证800是全部 A 股子集，不应再维护单独的 `中证800_*.pkl` 行情矩阵。
- 判断本地行情库是否“已更新”不能只看 `daily_prices` 是否存在某个最新完整交易日；必须按目标股票池检查覆盖率。中证800 只写入 800 只完整行情，也会让全库 `MAX(trade_date)` 看起来是最新，但全部 A 股实际缺 4700+ 只，金叉数量会被严重低估。
- 以后新写或修改的策略脚本，日线价格行情默认使用 SQLite 本地行情库，不再新增独立 pkl 价格缓存；除非用户明确要求临时研究脚本直接拉 Wind。
- 每次运行依赖日线行情的正式脚本前，都必须先检查 SQLite 是否已覆盖目标股票池的最新完整 EOD。检查方式必须按 `target_codes + price_fields + adjusted` 覆盖率判断，默认最低覆盖率 `95%`；不满足则先更新数据库，更新后仍不满足必须停止运行。
- SQLite `daily_prices` 只应保存完整 EOD 日线。完整日线的判断是 `open/high/low/close/volume/amt` 均非空；盘中 Wind `wsd(close)` 可能把当日最新价返回成 `close`，但其它字段为空，这种半截交易日不能进入正式回测数据。
- 当日最新信号需要盘中数据时，由策略脚本用 `wsq` 临时补到内存 DataFrame，不能写入 SQLite。正式回测和净值统计应基于完整 EOD，最新观察信号可以额外叠加当日实时行。
- 策略启动时自动更新行情库只补到“最新交易日的前一个交易日”的完整 EOD；最新交易日由 `wsq` 临时补齐。不要每次运行策略都用 `wsd` 拉当天日线，否则慢且容易写入不完整行。
- 如果发现 `daily_prices` 某天只有 `close` 有值而 `open/high/low/volume/amt` 为空，应删除该日期的不完整行情行，并清理覆盖该日期的 `fetch_batches` 状态，否则后续更新可能因为已有 `success` 被跳过。
- `load_price_matrix` 默认读取完整 EOD 行；如果传入 `end_date` 是今天但库里今天没有完整 EOD，返回结果应自然停在上一完整交易日，然后策略层再尝试 `wsq` 补今天。
- 比较策略回测效果时必须固定数据口径：完整 EOD 对完整 EOD。不要把包含盘中实时补行的“最新观察结果”和正式回测年化收益直接比较。
- MA60/MA120 “是否向上”的硬过滤对一分钱级别的数据差异非常敏感。像常熟银行这类边界样本，MA60/MA120 可能只比前一天高/低 `0.0001` 量级，缓存口径、实时快照、正式落库数据差异都可能改变是否通过硬过滤。

## SQLite 基本面数据库

- 构建脚本：`执行中策略/脚本/维护工具/build_a_share_fundamental_db.py`
- 目标数据库：`执行中策略/缓存/全部A股_基础基本面.sqlite3`
- 股票池缓存：`执行中策略/缓存/全部A股_基础基本面_sector.pkl`
- 数据表：
  - `fundamentals`：按 `trade_date + wind_code` 存基础基本面数据。
  - `financial_reports`：按 `rpt_date + wind_code` 存单季度财报实际值、披露日和策略可用日。
  - `daily_valuation`：按 `trade_date + wind_code` 存日频估值和市值数据。
  - `fetch_batches`：记录分批抓取状态，用于断点续跑。
  - `stock_universe`：记录股票池代码和名称。
- 当前基础字段：
  - `pe_ttm`
  - `pb_lf`
  - `ps_ttm`
  - `dividend_yield`
  - `roe_ttm`
  - `debt_to_assets`
  - `revenue_yoy_qfa`
  - `netprofit_yoy_qfa`
  - `gross_profit_margin_qfa`
  - `net_profit_margin_qfa`
  - `qfa_report_period`
  - `qfa_announcement_date`
  - `qfa_available_date`
- `qfa_*` 财报实际字段是单季度财务分析口径，例如 `qfa_yoysales`、`qfa_yoynetprofit`、`qfa_grossprofitmargin`、`qfa_netprofitmargin`。实测发现仅传 `tradeDate` 时会返回当前可得口径，不能可靠回看历史月度截面，容易造成未来函数；入库时必须按报告期 `rptDate` 抓取，并用 `stm_issuingdate` 作为披露日。
- 财报实际值的策略可用日必须取披露日后的下一个交易日，即 `qfa_available_date`。月度截面回填时，只能使用 `qfa_available_date <= trade_date` 的最新报告期。
- 2026-05-19 已按上述 as-of 规则补充 2021Q1 至 2026Q1 单季度财报实际值：`financial_reports` 115,815 行，`fundamentals` 月度 as-of 回填 285,360 行；宁德时代样例中 2025Q1 披露日为 2025-04-15，可用日为 2025-04-16，因此 2025-04-01 截面仍使用 2024 年报，2025-05-06 截面才使用 2025Q1。
- 日频估值字段使用 `daily_valuation` 单独保存，当前只保留市值类字段：`mkt_cap_ard`（总市值）、`free_float_mkt_cap`（Wind `mkt_freeshares`，自由流通市值）。`ps_ttm` 和 `dividend_yield` 改为月频字段，和 `pe_ttm/pb_lf` 一起写入 `fundamentals`；历史已写入 `daily_valuation` 的 `ps_ttm/dividend_yield` 可以保留但后续不再日频补抓。日频字段不要按单日 `wss(tradeDate=...)` 循环拉取，必须像行情价格缓存一样按代码批次 + 年度日期区间用 `wsd(start_date, end_date)` 拉矩阵后落库；该表全量补数请求量仍较大，必须提醒后再跑。
- 待 `daily_valuation` 日频估值补完后，提醒继续补两类数据：一是盈利一致预期（FY1/FY2 预期净利润、预期营业收入、预期修正等），二是业绩预告/业绩快报及披露日；预告/快报要按披露日后下一个交易日作为策略可用日，并优先于正式财报进入回测可用信息集。

## SQLite 行情数据库

- 更新脚本：`执行中策略/脚本/维护工具/update_local_market_db.py`
- 公共读取模块：`执行中策略/脚本/维护工具/local_market_db.py`
- 目标数据库：`执行中策略/缓存/本地行情数据库.sqlite3`
- 主要数据表：
  - `daily_prices`：按 `trade_date + wind_code + adjusted` 存日线行情。
  - `stock_universe`：记录股票池。
  - `universe_constituents_snapshot`：按 `snapshot_date + universe_name + wind_code` 保存股票池/指数历史成分快照。
  - `fetch_batches`：记录分批抓取状态，用于断点续跑。
  - `metadata`：记录库版本等元信息。
- `daily_prices` 的核心字段：
  - `open`
  - `high`
  - `low`
  - `close`
  - `volume`
  - `amt`
  - `turn`
- 维护原则：
  - SQLite 只保存完整 EOD。
  - 当天实时行情只在策略内存中通过 `wsq` 临时补行。
  - 自动更新时从最新完整 EOD 的下一天开始补，不做固定 15 天大窗口回刷，除非是专门做复权校验或历史修复。
  - 对于当天尚未完整落库的情况，数据库最新完整日期可能早于 Wind 最新交易日，这是正常状态。
  - “最新完整日期”必须带股票池语义：全部 A 股报告需要检查全部 A 股覆盖率，中证800策略只检查中证800覆盖率；不能把子股票池的完整 EOD 当成全市场完整 EOD。
  - 使用本地 SQLite 行情库的策略，运行前必须通过 `ensure_market_data_updated(..., target_codes=..., price_fields=...)` 按当前股票池和所需字段检查最新完整 EOD 覆盖率；低于默认 `95%` 时先调用 `update_local_market_db.py` 补库，补完仍不足则停止运行，不能静默回落到残缺行情。
  - 正式策略脚本的日线价格读取默认模式是：先 `ensure_market_data_updated()` 检查并补齐 SQLite，再用 `load_price_matrix(..., prefer_sqlite=True, fallback_pickle=False)` 读取；不要绕过 SQLite 直接用 `wsd` 或 pkl 作为主数据源。
  - 已接入上述前置检查的脚本包括：`观察_最新金叉信号_全球主要股票池.py`、`趋势回调策略/脚本/趋势回调1.py`、`趋势发现_金叉_中证800_前高回撤低效退出_执行版.py`、`趋势发现_金叉_中证800_平滑动态回撤卖出_执行版.py`、`趋势发现_金叉_中证800_分层回撤卖出_执行版.py`、`趋势发现_金叉_全部A股_基本面选池.py`。
  - `趋势发现_金叉_全部A股_基本面选池.py` 的日线价格行情已迁移到 SQLite；基本面数据仍沿用脚本内的 pkl 截面缓存。

## 股票池/指数历史成分快照

- 维护脚本：`执行中策略/脚本/维护工具/build_universe_constituent_snapshots.py`
- 目标表：`执行中策略/缓存/本地行情数据库.sqlite3` 的 `universe_constituents_snapshot`
- 表结构：
  - `snapshot_date`
  - `universe_name`
  - `sector_id`
  - `wind_code`
  - `sec_name`
  - `updated_at`
- 主键：`snapshot_date + universe_name + wind_code`
- 当前已完成：`中证800` 月频成分快照，`2018-01-31` 至 `2026-05-28`，共 `101` 个快照日期、`80,800` 行，单期均为 `800` 只，期间累计出现过 `1,377` 只不同股票。
- 抓取命令：
  - `python3 执行中策略/脚本/维护工具/build_universe_constituent_snapshots.py --universe-name 中证800 --sector-id 1000011893000000 --start-date 2018-01-01 --frequency M`
- 用途：后续中证800策略回测可按历史快照过滤当期股票池，减少用当前成分回看历史造成的幸存者偏差。当前只是完成数据入库，策略脚本尚未切换为按历史成分快照回测。

### 2026-05-28 全部A股金叉数量异常复盘

- 现象：`观察_最新金叉信号_全球主要股票池.py` 生成的全部 A 股最新金叉数量显著偏低，`2026-05-28` 输出中全部 A 股只有 `9` 个金叉，且近 30 日热度里 `2026-05-20` 之后多日只有个位数。
- 直接原因：`缓存/本地行情数据库.sqlite3` 的 `daily_prices` 表在 `2026-05-20` 至 `2026-05-27` 期间每天只有约 `800` 只有完整行情，覆盖的是中证800，而不是全部 A 股。全部 A 股股票池约 `5524` 只，最近几天缺失 4700+ 只股票的行情。
- 深层原因：行情库是多股票池共用表，主键只有 `trade_date + wind_code + adjusted`，没有把“该交易日对某个 universe 是否完整”作为元数据记录。`get_latest_price_date()` 只判断某天是否存在完整 EOD 行，并不判断目标股票池覆盖率；中证800策略更新后，数据库层面看起来已经有 `2026-05-27` 完整 EOD，但对全部 A 股来说是不完整的。
- 另一个疏漏：观察脚本读取本地 SQLite 后，只检查了矩阵非空和最新日期，没有检查最近交易日每行的有效股票数是否接近股票池总数，因此残缺矩阵被直接用于 MA5/MA60 和金叉统计。
- 修复动作：已用 `update_local_market_db.py --prices-from-wind --universe-name 全部A股 --sector-id a001010100000000 --start-date 2026-05-20 --end-date 2026-05-27 --price-fields open high low close volume amt` 补齐缺失行情。补齐后这些日期的覆盖恢复到 `5519~5524` 只，`2026-05-28` 全部 A 股金叉数重跑恢复为 `49`。
- 防复发：`观察_最新金叉信号_全球主要股票池.py` 已增加最近数据覆盖率检查。最近 `RECENT_REFRESH_DAYS` 内任一交易日有效收盘数低于股票池数量的 `95%` 时，会从 Wind 按全股票池补拉；若补拉失败且该市场要求完整 EOD，则停止生成报告，避免静默输出失真的金叉数量。
- 2026-06-11 跟进：覆盖率检查已下沉到 `local_market_db.ensure_market_data_updated()`，支持 `target_codes`、`price_fields`、`adjusted` 和 `min_coverage_ratio`。使用本地 SQLite 行情库的主要策略脚本已改为运行前按当前股票池覆盖率检查并自动补库，不再只依赖 `get_latest_price_date()`。

## 中证800金叉策略低效持仓卖出参数

- 记录日期：2026-05-19。
- 策略文件：`执行中策略/脚本/趋势发现_金叉_中证800_前高回撤低效退出_执行版.py`。
- 背景：部分个股买入后长时间波动很小，占用仓位和资金效率，因此增加低效持仓退出规则。
- 旧规则：
  - `LOW_EFFICIENCY_MIN_HOLDING_DAYS = 100`
  - `LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD = 0.03`
  - `LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD = 0.00`
  - 判断逻辑是持有超过 100 个交易日后，`历史最高浮盈 <= 3%` 或 `当前浮盈 <= 0%` 任一满足即触发。
- 旧规则问题：`or` 条件过宽，只要当前不赚钱就可能触发，即使该持仓历史上曾经有过较明显浮盈，也会被归入低效持仓。
- 优化后规则：
  - `LOW_EFFICIENCY_MIN_HOLDING_DAYS = 60`
  - `LOW_EFFICIENCY_MAX_PROFIT_THRESHOLD = 0.05`
  - `LOW_EFFICIENCY_CURRENT_PROFIT_THRESHOLD = 0.00`
  - 判断逻辑改为持有超过 60 个交易日，且 `历史最高浮盈 <= 5%`，且 `当前浮盈 <= 0%`。
- 优化含义：更聚焦于“从买入后一直没有明显表现”的低效率持仓，避免把曾经有效上涨、后来回撤的票误归类。
- 参数评估范围：
  - 等待交易日：`60 / 80 / 100 / 120 / 150`
  - 历史最高浮盈阈值：`1% / 2% / 3% / 4% / 5% / 6%`
  - 当前浮盈阈值：固定 `0%`
  - 基准组：不启用低效持仓卖出
- 回测对比：
  - 不启用低效卖出：年化收益 `17.58%`，夏普 `1.19`，最大回撤 `-11.63%`，平仓 `454` 笔。
  - 60 日且历史最高浮盈 <= 5%、当前浮盈 <= 0%：年化收益 `18.73%`，夏普 `1.25`，最大回撤 `-11.40%`，平仓 `468` 笔，其中低效卖出 `8` 笔。
  - 相对基准：年化收益提升约 `+1.15%`，夏普提升约 `+0.06`，最大回撤改善约 `+0.23%`。
- 当前处理：优化结论已固化进执行版策略参数和低效持仓判断逻辑。参数优化过程属于研究记录，不作为每日策略运行输出内容，因此执行版脚本不打印参数评估，也不向 Excel 写入参数评估 sheet。
- 归档位置：参数优化脚本已移至 `执行中策略/脚本/归档_参数优化/`，日常策略运行不依赖这些脚本。

## 后续可做

- 让全 A 股基本面选池策略优先从 SQLite 读取基础基本面数据，缺失时再走 Wind 拉取。
- 将已有 `.pkl` 基本面缓存逐步迁移或同步到 SQLite，减少重复抓取。
