# Mainnet cutover checklist

Things that are deliberately loosened on **testnet** and MUST be restored
before going live. Testnet OI/spot barely move (OI ~0.01–0.04 %/5m vs the
0.1 % mainnet threshold), so the structural gates are zeroed there or nothing
would ever fire. On mainnet those thresholds are what make the gates mean
anything — clear the overrides so config.yaml defaults take effect.

## Clear these `live_config` overrides (set them back to config.yaml defaults)

Stored in `data/hl_reaper.db` table `live_config`. Delete the row (the bot
then falls back to the config.yaml default) or set it to the default value.

| key | testnet override | mainnet default (config.yaml) |
|-----|------------------|-------------------------------|
| `trading.long_oi_rise_threshold`   | 0.0 | 0.001  |
| `trading.long_spot_lead_threshold` | 0.0 | 0.0002 |
| `trading.short_oi_rise_threshold`  | 0.0 | 0.001  |
| `trading.short_spot_lag_threshold` | 0.0 | 0.0002 |

Clear all four:

```bash
sqlite3 data/hl_reaper.db "DELETE FROM live_config WHERE key IN (
  'trading.long_oi_rise_threshold','trading.long_spot_lead_threshold',
  'trading.short_oi_rise_threshold','trading.short_spot_lag_threshold');"
```

Or zero just the SHORT pair via the Controls page (Entry Filters → SHORT
Structural Gate → OI rise / Spot lag thresholds → reset override).

Verify after restart — the bot logs the effective gate thresholds on startup:

```
LONG STRUCTURAL gate ENABLED  — need spot leading (>0.0002) + OI rising (>0.001) + book bid-heavy (>=0.20)
SHORT STRUCTURAL gate ENABLED — need spot lagging (<-0.0002) + OI rising w/ falling price (>0.001) + book ask-heavy (<=-0.20)
```

If either line shows `>0.0000`, an override is still live — clear it.
