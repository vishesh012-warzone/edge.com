Sessions (ET wall-clock): asia 18:00-02:00, london 02:00-08:20, ny 08:20-17:00. trade_date = date of (ET + 6h).
Tick size GC = 0.10. Volume bars target 100-200 bars/day. Value area = 70%.
Tables (Parquet, UTC):
 bars_1s: ts, instrument_id, o,h,l,c, v, buy_v, sell_v, n
 volume_bars: trade_date, bar, o,h,l,c, v, delta, t0, t1
 sessions: trade_date, sub, instrument_id, o,h,l,c, vol, buy_v, sell_v, n_trades, poc, val, vah, t_start, t_end
 events: trade_date, state, outcomes (see prereg.md)