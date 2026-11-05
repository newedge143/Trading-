# Test Results

Run: `python -m pytest tests/ -v` on 2026-11-05T02:33Z, Python 3.13, pytest 9.2.0.

**Result: 42 passed in 257.74s (0:04:17)**

Synthetic price series inside `tests/` are test fixtures only; they verify the code's mechanics and are never written to `reports/` or reported as backtest results.

| Requirement | Tests |
|---|---|
| Funding calculations | test_funding_long_pays_positive_rate, test_funding_short_receives_positive_rate, test_funding_adverse_stress_always_costs, test_engine_funding_applied_only_at_funding_timestamps |
| Fee calculations | test_taker_fee, test_maker_fee_and_multiplier, test_fees_reduce_wallet, test_fill_price_spread_and_slippage_direction |
| Position sizing | test_gross_notional_equity_vs_fixed, test_round_qty_down_to_step |
| Leverage | test_leverage_cap_enforced, test_engine_leverage_never_exceeds_cap |
| P&L accounting | test_long_round_trip_pnl, test_short_partial_close_and_flip, test_engine_equity_identity |
| Liquidation | test_isolated_liquidation_price, test_engine_liquidates_on_crash, test_stop_loss_prevents_liquidation |
| Signal generation | test_momentum_value, test_signal_long_winner_short_loser, test_funding_component_tilts_toward_receiving_side, test_weight_drift |
| Historical data alignment | test_missing_intervals_detected, test_align_panel_keeps_gaps_as_nan |
| No look-ahead | test_assert_no_future_raises, test_trailing_funding_excludes_future, test_no_lookahead_future_shock_does_not_change_past_decisions, test_orders_fill_at_next_open_not_signal_close |
| Drawdown / risk controls | test_max_drawdown, test_daily_loss_halt_and_drawdown_shutdown_trigger |
| Duplicate order prevention | test_duplicate_orders_rejected, test_engine_never_holds_two_positions_per_symbol |
| Agents | test_evaluator_rejects_overfit, test_decision_log_is_structured_jsonl |
| Pipeline | test_pipeline_end_to_end_offline, test_pipeline_reports_missing_data |

## Raw output

```
============================= test session starts ==============================
platform linux -- Python 3.13.18, pytest-9.2.0, pluggy-1.7.0 -- /usr/local/bin/python
rootdir: /home/user/Trading-
plugins: anyio-4.15.1
collecting ... collected 42 items

tests/test_indicators.py::test_roc_length PASSED                         [  2%]
tests/test_indicators.py::test_atr_positive PASSED                       [  4%]
tests/test_indicators.py::test_bollinger_bands_order PASSED              [  7%]
tests/test_indicators.py::test_momentum_histogram PASSED                 [  9%]
tests/test_indicators.py::test_bb_squeeze_returns_bool PASSED            [ 11%]
tests/test_tradinggym.py::test_taker_fee PASSED                          [ 14%]
tests/test_tradinggym.py::test_maker_fee_and_multiplier PASSED           [ 16%]
tests/test_tradinggym.py::test_fill_price_spread_and_slippage_direction PASSED [ 19%]
tests/test_tradinggym.py::test_funding_long_pays_positive_rate PASSED    [ 21%]
tests/test_tradinggym.py::test_funding_short_receives_positive_rate PASSED [ 23%]
tests/test_tradinggym.py::test_funding_adverse_stress_always_costs PASSED [ 26%]
tests/test_tradinggym.py::test_engine_funding_applied_only_at_funding_timestamps PASSED [ 28%]
tests/test_tradinggym.py::test_gross_notional_equity_vs_fixed PASSED     [ 30%]
tests/test_tradinggym.py::test_leverage_cap_enforced PASSED              [ 33%]
tests/test_tradinggym.py::test_engine_leverage_never_exceeds_cap PASSED  [ 35%]
tests/test_tradinggym.py::test_round_qty_down_to_step PASSED             [ 38%]
tests/test_tradinggym.py::test_long_round_trip_pnl PASSED                [ 40%]
tests/test_tradinggym.py::test_short_partial_close_and_flip PASSED       [ 42%]
tests/test_tradinggym.py::test_fees_reduce_wallet PASSED                 [ 45%]
tests/test_tradinggym.py::test_engine_equity_identity PASSED             [ 47%]
tests/test_tradinggym.py::test_isolated_liquidation_price PASSED         [ 50%]
tests/test_tradinggym.py::test_engine_liquidates_on_crash PASSED         [ 52%]
tests/test_tradinggym.py::test_stop_loss_prevents_liquidation PASSED     [ 54%]
tests/test_tradinggym.py::test_momentum_value PASSED                     [ 57%]
tests/test_tradinggym.py::test_signal_long_winner_short_loser PASSED     [ 59%]
tests/test_tradinggym.py::test_funding_component_tilts_toward_receiving_side PASSED [ 61%]
tests/test_tradinggym.py::test_weight_drift PASSED                       [ 64%]
tests/test_tradinggym.py::test_missing_intervals_detected PASSED         [ 66%]
tests/test_tradinggym.py::test_align_panel_keeps_gaps_as_nan PASSED      [ 69%]
tests/test_tradinggym.py::test_assert_no_future_raises PASSED            [ 71%]
tests/test_tradinggym.py::test_trailing_funding_excludes_future PASSED   [ 73%]
tests/test_tradinggym.py::test_no_lookahead_future_shock_does_not_change_past_decisions PASSED [ 76%]
tests/test_tradinggym.py::test_orders_fill_at_next_open_not_signal_close PASSED [ 78%]
tests/test_tradinggym.py::test_max_drawdown PASSED                       [ 80%]
tests/test_tradinggym.py::test_profit_factor_and_sharpe_signs PASSED     [ 83%]
tests/test_tradinggym.py::test_daily_loss_halt_and_drawdown_shutdown_trigger PASSED [ 85%]
tests/test_tradinggym.py::test_duplicate_orders_rejected PASSED          [ 88%]
tests/test_tradinggym.py::test_engine_never_holds_two_positions_per_symbol PASSED [ 90%]
tests/test_tradinggym.py::test_evaluator_rejects_overfit PASSED          [ 92%]
tests/test_tradinggym.py::test_decision_log_is_structured_jsonl PASSED   [ 95%]
tests/test_tradinggym.py::test_pipeline_end_to_end_offline PASSED        [ 97%]
tests/test_tradinggym.py::test_pipeline_reports_missing_data PASSED      [100%]

======================== 42 passed in 257.74s (0:04:17) ========================
```
