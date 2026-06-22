# BASELINE preset / SHORT-gate "not disabling" investigation — 2026-06-19

## Report
BASELINE preset appeared to leave the SHORT structural gate enforced for hours
(`short_blocked_*` audit rows long after BASELINE was applied), suggesting a
key-mapping or hot-reload bug that would invalidate today's BASELINE test.

## Verdict: NO BUG. The preset system, key mapping, and hot-reload all work.

### 1. Key mapping is correct (all 13 keys, all 5 presets)
Every PRESETS key in `dashboard/api.py` maps to a key the bot actually reads:
- `trading.short_structural_gate_enabled` → `scripts/run_bot.py:397`
  (`short_structural_params`, rebuilt every loop) and enforced at
  `run_bot.py:1062` (`if short_struct["enabled"] and ... and not s_allowed`).
- `trading.long_structural_gate_enabled` → `run_bot.py:374`.
- `trading.long_pump_cooldown_enabled` → `run_bot.py:384`.
- `trading.short_dump_cooldown_enabled` → `run_bot.py:407`.
- `risk.funding_hard_block_enabled` → `run_bot.py:651/828`.
- `risk.{min_confidence,min_model_agreement,atr_sl_multiplier,take_profit_r,
  trail_activation_r,max_hold_hours_scalp}` → `reaper/risk/manager.py:90-129`
  (`refresh_params`).
- `trading.{longs,shorts}_enabled` → `reaper/config.py:60-69` properties.

### 2. Hot-reload works (verified live, 6 s pickup both directions)
The bot reads `db.get_live_config()` + `cfg.apply_overrides()` every loop
(`run_bot.py:792-819`), no throttle. Live test through the real dashboard→bot
path: flipping `trading.short_structural_gate_enabled` true↔false was reflected
in the bot's published `short_gates[...].enabled` within **6 seconds** each way.

### 3. The cited blocks happened while the gate was genuinely ENABLED
Reconstructed `short_structural_gate_enabled` timeline (server-local EDT, from
`data/dashboard_api.log`):

| time (EDT) | source | gate |
|---|---|---|
| 12:49:03 | PRESET **SCALPER** applied | **enabled** (SCALPER turns it ON) |
| 15:02:32 | PRESET **BASELINE** applied | disabled |
| 15:11:25 | manual toggle False→True | **enabled** |
| 15:13:40 | manual toggle True→False | disabled |

- During the only clean BASELINE window (15:02:32–15:11:25 EDT) there were
  **ZERO** `short_blocked_*` rows.
- The blocks the report cited (15:11:29 SOL, 15:12:18 ETH, 15:12:29 BTC) all
  landed **after** the 15:11:25 manual re-enable — correct behaviour for an
  enabled gate.
- The earlier 15:02:21 BTC block was **before** BASELINE was applied, during the
  SCALPER period (gate ON).

## Actual root cause
Strategy state was switched repeatedly today and not held on BASELINE:
presets were cycled (SCALPER/SHORT_HUNTER/TREND_RIDER, ending on **SCALPER** at
12:49 — which *enables* both structural gates) and the SHORT gate was also
toggled by hand at 15:11. The bot faithfully tracked each change. The blocks
reflect the gate actually being on, not a silent gate-bypass failure.

## Data status
Today's "BASELINE" data is **NOT a clean continuous BASELINE run** and should be
treated as HYBRID / invalid for the 48 h BASELINE-vs-SCALPER comparison — not
because gates were silently active, but because the active strategy changed
multiple times (SCALPER active 12:49–15:02, BASELINE 15:02–15:11, manual
toggles after). Use `preset_log` + the dashboard audit log to bound any clean
sub-window.

## Clean restart (verified)
BASELINE re-applied and confirmed end-to-end at ~20:16 EDT:
- `/api/presets/active` → BASELINE
- bot-published `long_gates`/`short_gates` `enabled=False` for BTC/ETH/SOL
- zero new `*_blocked_*` rows after re-apply.

A clean BASELINE run starts from this verified point. For attribution, leave
BASELINE active and do not switch presets or hand-toggle gates mid-run.

## Hardening recommendation (optional, not a bug fix)
To prevent this class of confusion, preset apply could be surfaced more loudly in
the audit trail (e.g. write a `trades`/events marker row on apply, already
partially covered by `preset_log`), and the Controls page could warn when a
manual toggle diverges from the active preset (the CUSTOM indicator already does
this — it just isn't in the per-trade audit view).
