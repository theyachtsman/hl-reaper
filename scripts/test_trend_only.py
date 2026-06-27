#!/usr/bin/env python3
"""Trend-only operation tests (SCALP BAND RETIRED 2026-06-26).

Two guarantees the retirement must hold:
  1. The live trading loop evaluates/opens ONLY the trend band — it never
     aggregates a scalp signal or opens a scalp entry, and the structural-gate
     functions that fed the scalp band are gone. open_entry() lives inside
     main() and isn't importable, so the loop is asserted at the source/AST
     level: every open_entry(...) call passes band="trend", and there are no
     references to the removed gate functions or scalp aggregation in the loop.
  2. Applying any strategy preset writes ONLY trend-band + global keys — never a
     scalp or structural-gate config key (asserted against the real PRESETS).

No network, no live services."""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          (f" — {detail}" if detail and not ok else ""))
    PASS += ok
    FAIL += not ok


SCALP_GATE_TOKENS = (
    "long_structural_gate", "short_structural_gate",
    "_momentum_cooldown_ok", "_dump_cooldown_ok",
    "long_structural_params", "short_structural_params",
)

print("--- run_bot: live loop is trend-only ---")

import run_bot  # noqa: E402

# 1. the structural-gate functions are removed from the module entirely.
for tok in SCALP_GATE_TOKENS:
    check(f"run_bot has no {tok}()", not hasattr(run_bot, tok))

# kept: the legacy Change-B confirmation count (dashboard / regression only).
check("long_confirmation_count retained", hasattr(run_bot, "long_confirmation_count"))

src = (ROOT / "scripts" / "run_bot.py").read_text()
tree = ast.parse(src)

# 2. every open_entry(...) CALL passes band="trend" (3rd positional arg), never
#    "scalp". This is the entry path — proves no scalp position is ever opened.
open_entry_bands = []
for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "open_entry"):
        # open_entry(coin, sig, band)
        check("open_entry called with exactly (coin, sig, band)",
              len(node.args) == 3, ast.dump(node))
        band_arg = node.args[2] if len(node.args) == 3 else None
        if isinstance(band_arg, ast.Constant):
            open_entry_bands.append(band_arg.value)
check("open_entry is only ever called with band='trend'",
      open_entry_bands == ["trend"], str(open_entry_bands))

# 3. the loop body no longer calls the removed structural-gate functions.
called_names = {n.func.id for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
for tok in SCALP_GATE_TOKENS:
    check(f"loop makes no call to {tok}()", tok not in called_names)

# 4. open_entry's signature is the trend-only 3-arg form (no gate params).
oe_def = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "open_entry"), None)
check("open_entry def found", oe_def is not None)
if oe_def:
    argnames = [a.arg for a in oe_def.args.args]
    check("open_entry signature is (coin, sig, band)",
          argnames == ["coin", "sig", "band"], str(argnames))
    check("open_entry has no g_allowed/g_detail gate params",
          not ({"g_allowed", "g_detail", "s_allowed", "s_detail"} & set(argnames)))


print("\n--- presets: no scalp / structural-gate keys ever written ---")

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("dash_api", ROOT / "dashboard" / "api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)

BANNED = ("scalp", "structural", "spot_lead", "spot_lag", "oi_rise",
          "pump_cooldown", "dump_cooldown", "pump_threshold", "dump_threshold",
          "ob_bid_threshold", "ob_ask_threshold")

check("retired SCALPER/DUAL_BAND presets are gone",
      "SCALPER" not in api.PRESETS and "DUAL_BAND" not in api.PRESETS,
      str(list(api.PRESETS)))
check("trend presets remain",
      {"TREND_RIDER", "SHORT_HUNTER", "CONSERVATIVE", "BASELINE"} <= set(api.PRESETS),
      str(list(api.PRESETS)))

for name, preset in api.PRESETS.items():
    offenders = [k for k in preset["settings"]
                 if any(b in k for b in BANNED)]
    check(f"preset {name} writes no scalp/structural key", not offenders,
          str(offenders))
    # every preset enables the trend band
    check(f"preset {name} enables the trend band",
          preset["settings"].get("trading.trend_band_enabled") is True)

# the structural-gate config keys are no longer tunable via the bridge.
for k in ("trading.long_structural_gate_enabled",
          "trading.short_structural_gate_enabled",
          "risk.scalp_structural_gates_enabled"):
    check(f"CONFIG_SCHEMA dropped {k}", k not in api.CONFIG_SCHEMA)


print("\n" + "=" * 40)
print(f"RESULT: {PASS}/{PASS + FAIL} trend-only checks passed")
print("TREND-ONLY TEST:", "PASS" if FAIL == 0 else "FAIL")
sys.exit(1 if FAIL else 0)
