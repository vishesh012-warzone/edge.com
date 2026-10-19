# Pre-Registration: GC/XAU Order-Flow & Session Profile Hypotheses

**Status**: Pre-registered (Frozen)  
**Target Instruments**: CME GC (Gold Futures, tick size 0.10) & Retail Broker XAUUSD (MT5 ticks)  
**Governing Documents**: `AGENTS.md`, `docs/SPEC.md`, `docs/acceptance.yaml`

---

## 1. Research Protocol & Integrity

To eliminate data snooping, p-hacking, and hindsight bias:
1. All hypotheses, state identifiers, outcome definitions, and statistical tests in this document are specified prior to inspecting out-of-sample data.
2. Under `AGENTS.md`, this file is immutable once created. Any proposed alterations must be submitted through `docs/PROPOSALS.md`.
3. Every hypothesis test executed in code must call `trials.log(test_name)` in `src/trials.py` to record the exact trial counter $N_{trials}$, which directly scales multiple testing adjustments (Holm-Bonferroni, FDR) and computes the Deflated Sharpe Ratio (Bailey & López de Prado).

---

## 2. Market Microstructure & Core Hypotheses

### Hypothesis 1 (H1): Session Value Area Mean Reversion (Auction Market Theory)
* **Concept**: Auction Market Theory posits that Value Area (VAH to VAL, covering 70% of session volume) represents agreed two-sided fair value. When an opening session trades outside the previous completed session's Value Area and then re-enters, market consensus has rejected the price exploration outside value, inducing mean-reversion toward the opposing Value Area boundary.
* **Null Hypothesis ($H_{1,0}$)**: Conditional on price opening outside the previous session's Value Area $[VAL_{t-1}, VAH_{t-1}]$ and subsequently crossing back inside the Value Area, the probability of price reaching the opposite Value Area boundary before exiting the Value Area on the entry side is equal to random chance:
  $$P(\text{Reach Opposing Boundary} \mid \text{Re-entry}) \le 0.50$$
* **Alternative Hypothesis ($H_{1,A}$)**: Re-entry into prior session Value Area exhibits a statistically significant directional drift toward the opposing Value Area boundary:
  $$P(\text{Reach Opposing Boundary} \mid \text{Re-entry}) \ge 0.65 \quad (\alpha = 0.01)$$
* **Event State**: `OPEN_OUT_VA_REENTER`
* **Conditioning Sub-sessions**: London open relative to Asia VA; NY open relative to London VA and Combined Asia+London VA.

---

### Hypothesis 2 (H2): Delta Exhaustion & Absorption at Session Extremes
* **Concept**: When price pushes to a new session extreme (high or low) on elevated volume but exhibits negative delta divergence or rapid delta absorption followed by price rejection, it indicates passive institutional liquidity absorbing aggressive market orders, predicting immediate mean reversion.
* **Null Hypothesis ($H_{2,0}$)**: New session extremes accompanied by high volume and absorption/divergence exhibit zero conditional forward return over horizon $k$:
  $$\mathbb{E}[R_{t+k} \mid \text{Absorption High}] \ge 0, \quad \mathbb{E}[R_{t+k} \mid \text{Absorption Low}] \le 0$$
* **Alternative Hypothesis ($H_{2,A}$)**: Absorption at session extremes reliably predicts profitable mean reversion over the subsequent 5 to 20 volume bars after accounting for bid-ask spread and transaction costs:
  $$\mathbb{E}[R_{t+k} \mid \text{Absorption High}] < 0, \quad \mathbb{E}[R_{t+k} \mid \text{Absorption Low}] > 0 \quad (\alpha = 0.01, |t| \ge 2.8)$$
* **Event States**: `ABSORPTION_HIGH`, `ABSORPTION_LOW`
* **Features**: Delta imbalance ratio, volume percentiles across 100-200 daily volume bars, distance from Session POC.

---

### Hypothesis 3 (H3): CME GC Futures Order Flow Leads Broker XAUUSD Spot
* **Concept**: Centralized price discovery for gold occurs predominantly on CME GC futures. Aggressive order flow (buy/sell volume delta) in CME GC leads retail broker OTC XAUUSD tick quote adjustments over short intraday latency windows (1s to 5s).
* **Null Hypothesis ($H_{3,0}$)**: Order flow delta on CME GC does not Granger-cause mid-quote price adjustments on broker XAUUSD at 1-second to 5-second intervals ($p \ge 0.01$).
* **Alternative Hypothesis ($H_{3,A}$)**: CME GC trade volume delta Granger-causes XAUUSD tick quote adjustments, establishing positive cross-asset predictive information transfer from futures to spot:
  $$\beta_{\text{GC\_delta} \to \text{XAUUSD\_ret}} > 0 \quad (\text{Wald test } p < 0.001)$$
* **Event State**: `GC_DELTA_IMBALANCE_SPIKE`
* **Features**: 1s aggregated buy/sell volume on GC, contemporaneous and lagged XAUUSD mid-price changes.

---

## 3. Data Representation & Event State Schema

Per `docs/SPEC.md`, event studies evaluate Parquet table `events`:
* **Columns**: `trade_date`, `state`, `outcomes`
* **Event States**:
  1. `OPEN_OUT_VA_REENTER_HIGH`: Opened above prior VAH, closed volume bar below prior VAH.
  2. `OPEN_OUT_VA_REENTER_LOW`: Opened below prior VAL, closed volume bar above prior VAL.
  3. `ABSORPTION_HIGH`: Volume in top 10th percentile, positive delta exhaustion, rejected at session high.
  4. `ABSORPTION_LOW`: Volume in top 10th percentile, negative delta exhaustion, rejected at session low.
  5. `POC_TEST_BOUNCE`: Price tests prior session Point of Control (POC) with declining volume/delta absorption and bounces.
* **Outcomes Vector**:
  - `ret_5b`: Forward return across 5 volume bars.
  - `ret_20b`: Forward return across 20 volume bars.
  - `ret_session_close`: Forward return to session completion.
  - `mae`: Maximum Adverse Excursion.
  - `mfe`: Maximum Favorable Excursion.
  - `target_hit`: Boolean indicator for touching opposite VA boundary before initial invalidation stop.

---

## 4. Statistical Testing Protocol

1. **Significance Level**: $\alpha = 0.01$ (conservative baseline to safeguard against spurious discovery).
2. **Multiple Hypothesis Correction**: Holm-Bonferroni step-down adjustment and Benjamini-Hochberg False Discovery Rate (FDR) applied across all trial registrations in `trials.log`.
3. **Resampling**: Stationary bootstrap and block bootstrap (block size = 1 session) to account for volatility clustering, intraday periodicity, and autocorrelation.
4. **Validation**: Purged and Embargoed Walk-Forward splits (zero look-ahead). Out-of-sample data strictly isolated until acceptance gates pass.
