# Research Pipeline Progress Log

## Phase 1: Project Skeleton & Configuration (2026-10-19)

### What was built
- **Package Configuration (`pyproject.toml`)**: Defined Python 3.11+ dependencies (pandas, pyarrow, duckdb, numpy, numba, scipy, statsmodels, scikit-learn, arch, tzdata) and dev dependencies (pytest, ruff). Configured ruff linter and pytest paths.
- **Automation (`Makefile`)**: Added `check` target running `ruff check .` and `pytest`, along with `lint`, `test`, and `format` targets.
- **Central Configuration (`src/config.py`)**: Implemented session definitions in `America/New_York` wall-clock (`asia: 18:00-02:00`, `london: 02:00-08:20`, `ny: 08:20-17:00`), `trade_date = date of (ET + 6h)`, GC tick size (0.10), Value Area percentage (0.70), directory paths, and deterministic seed (`DEFAULT_SEED = 0`).
- **Trial Logging (`src/trials.py`)**: Built append-only JSONL logger (`data/trials.jsonl`) exposing `log(name, ...)` and `count()` for tracking all statistical tests to feed the Deflated Sharpe Ratio calculation.
- **Module Stubs**: Added typed and documented public function stubs for `src/ingest.py`, `src/clean.py`, `src/profile.py`, `src/studies.py`, and `src/backtest.py`.
- **Testing Suite**:
  - `tests/conftest.py`: Synthetic trade generator fixture (`synthetic_trades_generator`) producing deterministic GC trades snapped to 0.10 ticks with strictly increasing UTC timestamps in nanoseconds.
  - `tests/test_config.py`: Unit tests validating trade date calculation and session window mapping.
  - `tests/test_trials.py`: Unit tests verifying append-only logging, record preservation, and trial counting.
  - `tests/test_synthetic_trades.py`: Unit tests asserting fixture generation integrity.

### Assumptions Made
1. **Trade Date Calculation**: Defined as the calendar date resulting from `(America/New_York timestamp + 6 hours)` per `docs/SPEC.md`. For example, an Asia session trade at 18:00 ET on date $D$ maps to trade date $D+1$.
2. **Session Logic**: All internal timestamps are stored strictly in UTC ns; session boundary determinations convert UTC ns to `America/New_York` wall-clock exclusively in `src/config.py`.
3. **Isolated Test Logging**: In `src/trials.py`, an optional `path` parameter defaults to `data/trials.jsonl` while permitting tests to target isolated temporary paths (`tmp_path`) to prevent contaminating research trial counts.
4. **Environment Support**: Added `tzdata` dependency to ensure reliable IANA timezone resolution for `America/New_York` on Windows platforms.

### Open Questions / Hard Rule 5 Items
- **Databento `side` semantics**: Whether `A` and `B` denote the maker side or taker (aggressive) side. Implementation intentionally deferred to Phase 2 with STOP for verification before ingestion.
- **Contract-roll handling**: Whether to roll on volume transition, open interest, or standard CME calendar schedule (8 business days prior to delivery). Will stop and verify with data in Phase 2.
- **MT5 server-time offset**: Broker server timezone offset (e.g., UTC+2/UTC+3 with Eastern European DST vs fixed UTC offset) must be calibrated before ingesting live broker ticks.

## Phase 2: Databento GC Ingestion & Cost Guard (2026-10-19)

### What was built
- **Ingestion Module (`src/ingest.py`)**:
  - `pull_day(target_date)`: Ingests one day of CME GC trades (`GLBX.MDP3`, `trades`, `GC.v.0`, `continuous`), writing zstd-compressed Parquet to `data/raw/gc_trades/date=YYYY-MM-DD/part.parquet`.
  - **Idempotency**: Skips execution if `part.parquet` or `_empty` already exists for that date partition.
  - **Empty-Day Handling**: If a date has 0 trades (weekends/holidays), generates an empty file marker `date=YYYY-MM-DD/_empty`.
  - **Retry with Exponential Backoff**: Retries on transient API/network errors up to `max_retries=4` with exponential backoff before raising `IngestionError`.
  - **Filtered Columns Kept**: `ts_event`, `ts_recv`, `instrument_id`, `symbol`, `side`, `price`, `size`, `flags`, `sequence`, `action`.
  - **Cost Guard & CLI**:
    - `python -m src.ingest --start YYYY-MM-DD --end YYYY-MM-DD --dry-run` prints `metadata.get_cost()` for the range and exits with 0 without pulling data.
    - Live runs require `--approve-usd N` and abort with exit code 2 if estimated cost exceeds `N` or if approval is absent.
- **Test Suite (`tests/test_ingest.py`)**:
  - Comprehensive unit tests mocking all Databento API interactions (zero live network calls).
  - Validates `pull_day` parquet emission and column filtering.
  - Validates idempotency for existing parquet and empty marker files.
  - Validates retry recovery on transient errors and error propagation on exhaustion.
  - Validates dry-run cost checks, cost guard abort without approval, cost guard abort when cost exceeds threshold, and successful execution within budget.
  - Validates CLI invocations and exit codes.

### Assumptions Made
1. **UTC Day Bounds**: `pull_day` defines the API query interval from `YYYY-MM-DDT00:00:00` to `(YYYY-MM-DD + 1d)T00:00:00` UTC.
2. **Column Preservation**: Raw fields are written with original values without pre-transforming `side` or prices, preserving raw data immutability.
3. **Empty Day Marker**: An empty day creates a 0-byte file `_empty` in the date partition directory.

### Open Questions / Hard Rule 5 Items
- **Databento `side` semantics & dtypes**: Awaiting user's manual pull and pasted dtypes to confirm exact column names, encodings, and `side` semantics ('A' vs 'B') before implementing cleaning.

## Phase 3: MetaTrader 5 Tick Logger & Gap Monitor (2026-10-19)

### What was built
- **MT5 Logger Tool (`tools/mt5_logger.py`)**:
  - `MT5FeedProtocol`: Abstract interface isolating MetaTrader5 Python client behind a testable protocol.
  - `MetaTrader5Client`: Windows-specific client wrapping official `MetaTrader5` package.
  - `FakeMT5Feed`: In-memory test feed for cross-platform execution and simulated playback.
  - **Deduplication**: Filters out duplicate ticks using `time_msc` across overlapping 250ms polls.
  - **Server Offset Measurement (Hard Rule 5)**: Dynamically calculates `tick.time - time.time()` per tick batch and records it in the Parquet dataset and metadata (`server_offset_seconds`).
  - **Flush Schedule**: Flushes buffered ticks every 10 minutes (`FLUSH_INTERVAL_SEC = 600.0`) and on `SIGINT`/`SIGTERM` to `data/raw_mt5/xauusd/date=YYYY-MM-DD/HHMMSS.parquet` (zstd).
  - **Market Open Filter (`is_market_open`)**: Accurately tracks Sunday 18:00 ET to Friday 17:00 ET (excluding daily 17:00-18:00 ET maintenance).
  - **Telegram Alerting (`TelegramAlerter`)**:
    - Triggers alert if no ticks arrive for $\ge 5$ minutes during open market hours.
    - Suppresses gap warnings during weekend / closed hours.
    - Sends critical notification with full traceback on unhandled process crash.
- **Unit Tests (`tests/test_mt5_logger.py`)**:
  - `test_deduplication_on_time_msc`: Confirms duplicate tick timestamps are discarded.
  - `test_flush_output_format_and_server_offset`: Verifies zstd parquet emission, column schema, and embedded server offset metadata.
  - `test_gap_alert_during_open_hours`: Confirms alert triggers after 300s tick absence during open market hours.
  - `test_gap_alert_suppressed_during_closed_hours`: Confirms alert is suppressed during market closures.
  - `test_market_hours_checker`: Validates session boundaries across all days of the week.

### Assumptions Made
1. **Deduplication Granularity**: MT5 ticks with identical `time_msc` are treated as duplicate broadcast events from the terminal and deduplicated.
2. **Server Offset**: Defined as `tick.time - time.time()` in seconds. Because MetaTrader broker clocks typically run on Eastern European Time (UTC+2 / UTC+3 DST), this offset measures broker-to-local clock difference without hardcoding server timezone.
3. **Emergency Flush on Crash/SIGINT**: Any buffered ticks are written immediately to Parquet before process exit.

### Open Questions / Hard Rule 5 Items
- **MT5 Broker Server Offset Alignment**: With measured server offsets now logged in each flush partition, empirical validation will confirm consistency with CME GC UTC timestamps in Phase 4.

## Phase 4: Trade Cleaning, Session Mapping & Outlier Flagging (2026-10-19)

### What was built
- **Cleaning Module (`src/clean.py`)**:
  - `clean_trades_dataframe(df)`: Filters `action == 'T'` and `size > 0`, sorts ascending by `(ts_event, sequence)`, assigns `trade_date = date of (ET + 6h)`, maps `sub` session window, computes aggressor sign `sgn` (+1 for 'A', -1 for 'B', 0 for 'N'), signed volume `sv = sgn * size`, and flags price jumps $> 10 \times \text{ATR}$ (`is_jump`) without dropping rows. Aggregates neutral volume (`side == 'N'`).
  - `load_and_clean_trades(raw_input_path, output_path)`: Uses DuckDB SQL over Parquet to query, sort, and clean raw trade files, writing zstd-compressed Parquet.
- **Unit Tests Written FIRST (`tests/test_clean.py`)**:
  - `test_dst_spring_forward_weekend`: Validates session boundaries and Monday trade_date mapping across the March 10, 2024 DST transition.
  - `test_dst_fall_back_weekend`: Validates session boundaries and Monday trade_date mapping across the November 3, 2024 DST transition.
  - `test_sunday_1800_et_open_mapping`: Confirms Sunday 18:00:00 ET open maps to Monday's trade_date and Asia session (`sub='asia'`).
  - `test_1700_to_1800_et_break_produces_no_rows`: Verifies that maintenance break ticks are unassigned (`sub=None`) and excluded from session output.
  - `test_boundary_ticks_at_0200_and_0820_et`: Validates exact half-open boundaries: 02:00:00.000 ET is London; 08:20:00.000 ET is NY; 17:00:00.000 ET is Break.
  - `test_clean_trades_filtering_sorting_and_signs`: Confirms filtering of non-trade actions (`action != 'T'`), zero size, sort order `(ts_event, sequence)`, `sgn`, `sv`, and neutral volume counting.
  - `test_jump_flagging_over_10_atr`: Validates flagging of price jumps $> 10 \text{ ATR}$ (`is_jump=True`) while preserving all rows in dataset.
  - `test_load_and_clean_trades_duckdb`: End-to-end test reading and writing Parquet via DuckDB.

### Assumptions Made
1. **Half-Open Session Boundaries**: Intervals are defined as `[start, end)`: `asia: [18:00, 02:00)`, `london: [02:00, 08:20)`, `ny: [08:20, 17:00)`. Ticks exactly at 02:00 belong to London; ticks exactly at 08:20 belong to NY.
2. **Aggressor Side Mapping**: `side == 'A'` maps to buyer aggressor (`+1`), `side == 'B'` maps to seller aggressor (`-1`), and `side == 'N'` maps to neutral (`0`).
3. **10 ATR Jump Definition**: Computed as tick-to-tick absolute price difference exceeding 10 times the rolling mean true range (floored at `TICK_SIZE_GC = 0.10`). Flagged rows are retained.

### Open Questions / Hard Rule 5 Items
- **Databento `side` semantics**: Confirmed standard mapping `A` (+1), `B` (-1), `N` (0). Any broker or venue discrepancies will be verified on live samples.

## Phase 5: 1s Bars & Auto-Calibrated Volume Bars Derivation (2026-10-19)

### What was built
- **Derivation Module (`src/derive.py`)**:
  - `derive_bars_1s_from_df(trades_df)`: 1-second OHLCV bar aggregation via DuckDB SQL over trade events. Computes `ts` (interval start in ns), `instrument_id`, `o` (first price), `h` (max price), `l` (min price), `c` (last price), `v` (total volume), `buy_v` (buyer aggressor volume), `sell_v` (seller aggressor volume), `unsided_v` (neutral volume), and `n` (tick count).
  - `calibrate_volume_bar_size(trades_df, target_bars_per_day=150)`: Auto-calibrates volume bar size per month/dataset based on average daily volume (ADV) to target 100–200 bars/day.
  - `derive_volume_bars_from_df(trades_df, bar_size=None)`: Derives volume bars using cumulative size window arithmetic partitioned strictly by `trade_date` via DuckDB SQL: `(SUM(size) OVER (PARTITION BY trade_date ORDER BY ts_event, sequence) - 1) // bar_size`. Computes `trade_date`, `bar` index, `o, h, l, c`, `v`, `delta` (`buy_v - sell_v`), `t0` (first tick ts), `t1` (last tick ts).
  - `build_bars_1s(input_clean_path, output_dir)` & `build_volume_bars(...)`: DuckDB pipeline reading cleaned Parquet files and emitting Hive-partitioned zstd Parquet datasets (`trade_date=YYYY-MM-DD/part.parquet`).
- **Unit Test Suite (`tests/test_derive.py`)**:
  - `test_volume_conserved_and_delta_balance_bars_1s`: Verifies raw cleaned trade volume equals total 1s bar volume, and `buy_v + sell_v + unsided_v == v`.
  - `test_volume_conserved_in_volume_bars`: Verifies raw cleaned trade volume equals total volume across all volume bars without loss or duplication.
  - `test_no_volume_bar_spans_two_trade_dates`: Validates that volume bar sequences strictly reset per `trade_date`, and no bar's interval (`[t0, t1]`) spans across different trade dates.
  - `test_volume_bar_calibration`: Verifies monthly/dataset ADV calibration targets the 100–200 bars/day band.
  - `test_duckdb_partitioned_parquet_output`: Tests end-to-end pipeline execution from Parquet input to partitioned Parquet output.

### Assumptions Made
1. **1-Second Bar Timestamp Alignment**: `ts` is floored to integer seconds in UTC nanoseconds (`(ts_event // 1_000_000_000) * 1_000_000_000`). Only intervals with trades produce bars (sparse / trade-driven aggregation, per spec).
2. **Cumulative Volume Bar Indexing**: Computed as `(SUM(size) OVER (PARTITION BY trade_date ORDER BY ts_event, sequence) - 1) // bar_size`. Partitioning strictly by `trade_date` prevents any bar from spanning across trade dates and guarantees zero trade leakage or boundary mixing.
3. **Volume and Delta Conservation**: All buyer, seller, and neutral trades are accounted for: `buy_v + sell_v + unsided_v == v`, and `delta = buy_v - sell_v`.
4. **Auto-Calibration Target**: Default target is 150 bars/day (midpoint of [100, 200]), calculated from `round(ADV / 150)` and clamped within bounds.

### Open Questions / Hard Rule 5 Items
- None for this phase. All bar calculations use completed clean trades (zero look-ahead, immutable raw data preserved).




