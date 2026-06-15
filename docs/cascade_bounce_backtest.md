# Cascade-Bounce Backtest — Results (Phase 8.6, Task 3)

_Written 2026-06-14. Script:
[scripts/backtest_cascade_bounce.py](../scripts/backtest_cascade_bounce.py).
Raw output: `data/backtest_cascade_bounce_20260614.json`._

## Verdict

**NO-GO. Fading liquidation-cascade overshoots is not profitable on 192 days of
1m data across 7 coins — and it loses even with zero fees on 6 of 7 coins.** The
core premise (price *reverts* after a cascade stabilizes) does not hold at the
strategy's operating timescale. At 5-20 minutes after a stabilized cascade,
price *continues* in the cascade direction more often than it reverts. A weak
mean-reversion only appears around the 60-minute mark — too slow for a fast
scalp, too small to clear fees, and inconsistent across coins.

Recommendation: **leave `cascade_bounce.enabled: false`.** Do not turn it on
live. The model, risk integration, and tests stay in the tree (they're correct
and tested) but the strategy as designed has no edge. Task 4 (build) is **not**
triggered — and is moot anyway, since the model was already built on 2026-06-12.

## Method

Faithful replay, not a reimplementation: the real
`reaper.models.cascade_bounce.CascadeBounceModel` was fed 192 days of 1m candles
bar-by-bar exactly as `run_bot.py` feeds it live, with its wall-clock timer
monkeypatched to simulated bar time so its detection / stabilization / cooldown /
knife-abandon state machine ran identically to live. Entries taken at the open of
the bar *after* the signal (can't fill a closed bar). Within-bar ties resolved
stop-before-target (conservative). OI/liquidation confirmation is
confidence-boost-only in the model and never affects P&L, so it was run with ctx
empty (the live `liquidations.db` is empty anyway — see assessment doc).

- Data: `data/history/{COIN}_1m.csv`, ~192 days (≈2025-12-01 → 2026-06-11).
- Default config: move ≥1.5% in 5m, vol ≥3× baseline, stabilize 2 bars; exit
  TP +1.0% / SL −0.75% / max-hold 20m.
- Fees reported at three levels: gross 0%, maker 0.045% RT, taker 0.070% RT.

## Trade frequency — the strategy is NOT idle

Cascades meeting the trigger are **common**, not rare: 653 events over 192 days
across 7 coins (~24/week portfolio-wide). So this is not an "opportunistic but
rare" strategy that sits idle — it fires often, and loses often. (More-liquid
coins fire less: BTC 0.66/wk; WIF 7.2/wk.)

## Default-config results (per coin)

| Coin | Trades | /wk | STOP/TP/TIME | avg hold | Gross net | Taker net | Win% | PF(taker) |
|------|-------:|----:|:------------:|---------:|----------:|----------:|-----:|----------:|
| BTC  | 18  | 0.7 | 8/3/7    | 11.2m | −1.7%  | −3.0%   | 39% | 0.61 |
| ETH  | 76  | 2.8 | 32/18/26 | 11.3m | −3.0%  | −8.3%   | 42% | 0.72 |
| SOL  | 79  | 2.9 | 31/19/29 | 10.8m | **+1.2%** | −4.3% | 48% | 0.84 |
| ARB  | 115 | 4.2 | 53/29/33 | 10.7m | −3.7%  | −11.7%  | 44% | 0.74 |
| AVAX | 69  | 2.5 | 35/9/25  | 11.3m | −13.7% | −18.5%  | 35% | 0.42 |
| DOGE | 98  | 3.6 | 53/17/28 | 10.3m | −15.6% | −22.5%  | 33% | 0.51 |
| WIF  | 198 | 7.2 | 109/35/54| 9.7m  | −34.6% | −48.4%  | 36% | 0.48 |
| **Portfolio** | **653** | | | | **−71.1%** | **−116.8%** | **40%** | |

STOP is the dominant exit on every coin. Break-even win rate for a 1.0%/0.75%
TP/SL is 42.9%; realized win rates are 33–48%, mostly below break-even *before*
fees even enter the picture. Only SOL is gross-positive, and it goes negative
after any fee.

## The decisive diagnostic — does a bounce appear at all?

Geometry-free: mean **signed forward return in the bounce direction** at fixed
horizons from entry (positive = the fade was right; this removes any TP/SL
artifact). Pooled across all 653 events:

| Horizon | Pooled mean | vs maker break-even (+0.045%) |
|--------:|------------:|:------------------------------|
| +5 min  | **−0.122%** | continuation, not reversion |
| +15 min | **−0.132%** | continuation, not reversion |
| +30 min | −0.009%     | ~flat |
| +60 min | +0.090%     | weak reversion, but < reliable edge, negative on 3/7 coins |

This is the whole story. **At the strategy's actual hold window (~10m, capped at
20m), the expected move is *against* the fade.** "Wait 2 bars for stabilization,
then fade" enters squarely into ongoing continuation. The overshoot-snaps-back
intuition is real only at ~1h — a different (swing) regime that this
fast-scalp design cannot capture and that doesn't survive fees (WIF stays
negative even at +60m).

## Sensitivity sweep (does any reasonable variant flip positive?)

| Variant | Gross portfolio | Taker portfolio |
|---------|----------------:|----------------:|
| Default (TP1.0/SL0.75/hold20) | −71.1% | −116.8% |
| Longer hold, chase 60m (TP1.2/SL1.0/hold60) | −76.0% | −121.7% |
| More-sensitive trigger (move≥1.0%, vol≥2.5×) | negative every coin | negative every coin |
| Wider stop (TP1.0/SL1.5) | best coin SOL +3.4% gross | SOL −2.2% taker; portfolio negative |

No variant is profitable after fees; most are negative even gross. Widening the
stop helps win rate (you stop out less) but the extra losers when the cascade
*does* keep going more than offset it. Lowering the trigger only adds more
losing trades. This is consistent with the forward-return diagnostic: the signal
itself has the wrong sign at this timescale, so no exit-geometry tuning rescues
it.

## Caveats (honest)

- **No real liquidation confirmation was available.** `liquidations.db` is empty
  (poller has caught 0 backstop events in calm tape). The model treats liq/OI
  confirmation as confidence-only, so it doesn't change these P&L numbers — but
  it remains *possible* that gating entries on a *confirmed* large backstop flush
  (not just price+volume) isolates a rarer, cleaner subset that behaves
  differently. We cannot test that until the poller captures real cascades during
  a volatile window. This is the one un-foreclosed door.
- 192 days, single broad regime. But the result is consistent across 7 coins and
  653 events, and the failure is *directional* (wrong-sign forward return), not a
  thin-sample fluke.
- Binance 1m candles used as the price source (same as all prior backtests); HL
  microstructure during a cascade may differ at sub-minute resolution, but the
  bounce thesis is a minutes-scale claim and that's what we tested.

## Bottom line

This matches the Phase 4.6 pattern from a new angle: another structurally
distinct hypothesis tested honestly and found to have no edge. The specific new
knowledge is sharper than "no edge," though — **after a stabilized cascade,
price continues for ~15 more minutes on average.** That is a *continuation*
signal, the opposite of the bounce thesis. If anything is worth a future look it
is the inverse (momentum-follow on confirmed cascades), not the fade — and only
gated on *real* liquidation confirmation once the poller has caught some.
