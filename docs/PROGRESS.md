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

