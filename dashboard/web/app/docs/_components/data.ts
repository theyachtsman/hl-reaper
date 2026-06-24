// Single source of truth for the docs site. Every number here is mirrored from
// the actual codebase (config.yaml, reaper/aggregator.py, reaper/risk/manager.py,
// scripts/run_bot.py, dashboard/api.py) so the docs can never quietly drift from
// the bot. If you change a default in config.yaml, change it here too.

export const NAV = [
  { href: "/docs", label: "Overview" },
  { href: "/docs/how-it-works", label: "How It Works" },
  { href: "/docs/bands", label: "Bands" },
  { href: "/docs/models", label: "Models" },
  { href: "/docs/risk", label: "Risk Engine" },
  { href: "/docs/presets", label: "Presets" },
  { href: "/docs/controls", label: "Controls" },
  { href: "/docs/dashboard", label: "Dashboard" },
  { href: "/docs/glossary", label: "Glossary" },
] as const;

// ---- the five active directional voters ------------------------------------
// reaper/aggregator.py BASE_WEIGHTS / SCALP_WEIGHTS / TREND_WEIGHTS
export type ModelRow = {
  key: string;
  name: string;
  blurb: string;
  base: number;
  scalp: number;
  trend: number;
};

export const MODELS: ModelRow[] = [
  {
    key: "ta",
    name: "TAModel",
    blurb: "RSI + MACD + EMA cross + Bollinger blended into one score",
    base: 0.225,
    scalp: 0.15,
    trend: 0.3,
  },
  {
    key: "meanrev",
    name: "MeanReversionModel",
    blurb: "z-score of price vs its 20-period mean — fades extremes, RANGING only",
    base: 0.15,
    scalp: 0.45,
    trend: 0.0,
  },
  {
    key: "funding",
    name: "FundingRateModel",
    blurb: "contrarian read of the perp funding rate — fades the crowded side",
    base: 0.15,
    scalp: 0.05,
    trend: 0.2,
  },
  {
    key: "ob",
    name: "OrderbookImbalanceModel",
    blurb: "live bid vs ask depth over the top 10 book levels",
    base: 0.325,
    scalp: 0.2,
    trend: 0.3,
  },
  {
    key: "vwap",
    name: "VWAPModel",
    blurb: "session VWAP with ±1σ bands as dynamic support / resistance",
    base: 0.15,
    scalp: 0.15,
    trend: 0.2,
  },
];

// ---- dual-band geometry (config.yaml risk.*) -------------------------------
export type BandParam = {
  label: string;
  key: string;
  scalp: string;
  trend: string;
  note?: string;
};

export const BAND_PARAMS: BandParam[] = [
  { label: "Candle resolution", key: "interval", scalp: "5m", trend: "1h" },
  { label: "Min confidence", key: "min_confidence", scalp: "0.40", trend: "0.55" },
  { label: "Min model agreement", key: "min_model_agreement", scalp: "2", trend: "3" },
  { label: "Stop distance", key: "atr_sl_multiplier", scalp: "1.0 × ATR", trend: "2.5 × ATR" },
  { label: "Take-profit", key: "take_profit_r", scalp: "1.5 R", trend: "4.0 R" },
  { label: "Trailing activates", key: "trail_activation_r", scalp: "1.0 R", trend: "2.0 R" },
  { label: "Breakeven lock", key: "breakeven_lock_r", scalp: "0.4 R", trend: "0.8 R" },
  { label: "Max hold", key: "max_hold_hours", scalp: "0.5 h (30 min)", trend: "48 h" },
  { label: "Max concurrent", key: "max_concurrent_positions", scalp: "3", trend: "2" },
  { label: "Position size", key: "position_size_usd", scalp: "$30", trend: "$75" },
  {
    label: "Structural gates",
    key: "structural_gates_enabled",
    scalp: "ON",
    trend: "OFF",
    note: "the trend band's own 1h signal IS its gate",
  },
];

// ---- presets (dashboard/api.py PRESETS) ------------------------------------
export type Preset = {
  id: string;
  name: string;
  tagline: string;
  bands: string;
  changes: string[];
  warning?: string;
  when: string;
};

export const PRESETS: Preset[] = [
  {
    id: "DUAL_BAND",
    name: "DUAL BAND",
    tagline: "The flagship — 5m scalp + 1h trend running simultaneously.",
    bands: "Scalp + Trend",
    changes: [
      "Both bands enabled, both directions",
      "Scalp tight defaults, trend wide defaults",
      "Counter-trend penalty 0.7",
      "Funding hard-block on",
    ],
    when: "The default all-rounder. Mixed or unclear conditions where you want both fast scalps and patient trend trades.",
  },
  {
    id: "SCALPER",
    name: "SCALPER",
    tagline: "Pure 5m scalp band. Tight stops, fast exits, high frequency.",
    bands: "Scalp only",
    changes: [
      "Trend band disabled",
      "Scalp tight defaults (conf 0.40, agree 2)",
      "Funding hard-block on",
    ],
    when: "Choppy, range-bound tape where many small mean-reversion trades beat holding.",
  },
  {
    id: "TREND_RIDER",
    name: "TREND RIDER",
    tagline: "Pure 1h trend band. Wide stops, lets winners run, low frequency.",
    bands: "Trend only",
    changes: [
      "Scalp band disabled",
      "Trend size raised to $175",
      "Trend min confidence lowered to 0.49",
      "Counter-trend penalty 1.0 (none)",
    ],
    when: "Strong, sustained directional moves where you want to size up and hold.",
  },
  {
    id: "SHORT_HUNTER",
    name: "SHORT HUNTER",
    tagline: "Both bands, shorts only. Optimized for downtrends.",
    bands: "Scalp + Trend",
    changes: [
      "Global longs disabled (shorts only)",
      "Both bands enabled",
      "Counter-trend penalty 0.7",
    ],
    when: "Risk-off, bleeding markets — the historically stronger side of this book.",
  },
  {
    id: "CONSERVATIVE",
    name: "CONSERVATIVE",
    tagline: "Both bands, highest-quality entries only.",
    bands: "Scalp + Trend",
    changes: [
      "Scalp bar raised to conf 0.55 / agree 3",
      "Trend bar raised to conf 0.62 / agree 4",
      "Counter-trend penalty 0.6 (strict)",
    ],
    when: "When you want fewer, higher-conviction trades and can tolerate sitting out.",
  },
  {
    id: "BASELINE",
    name: "BASELINE",
    tagline: "Both bands, structural gates off. Max frequency.",
    bands: "Scalp + Trend",
    changes: [
      "Structural gates OFF (long + short)",
      "Pump / dump cooldowns off",
      "Scalp confidence floored to 0.35",
      "Counter-trend penalty 0.85",
    ],
    warning:
      "Disables structural gates and increases trade frequency. Use only in trending markets.",
    when: "Strong trends only — the unfiltered baseline used to measure what the gates are worth.",
  },
];

// ---- risk state machine (reaper/risk/state.py) -----------------------------
export const STATES = [
  { name: "ACTIVE", desc: "All guards green — new entries allowed." },
  { name: "MANAGING", desc: "Daily drawdown hit (or paused): no new entries, open positions still managed." },
  { name: "HALTED", desc: "Cascade / severe drawdown / manual halt: everything closed, loop frozen until the window expires or you resume." },
  { name: "RECONNECTING", desc: "Market-data feed went stale (>30s): entries paused until it recovers." },
  { name: "COOLDOWN", desc: "Weekly drawdown hit: a 48h timed lockout." },
  { name: "CASCADE_BOUNCE_ACTIVE", desc: "A cascade-bounce trade is open: ensemble entries paused, the bounce position is managed." },
];

// ---- worked vote example (used on /docs/how-it-works) ----------------------
// A real SCALP-band aggregation (RANGING regime, so MeanReversion is awake).
// Numbers follow reaper/aggregator.py exactly.
export const VOTE_EXAMPLE = {
  band: "SCALP",
  regime: "RANGING",
  rows: [
    { model: "TAModel", weight: 0.15, dir: "SHORT", conf: 0.62 },
    { model: "MeanReversionModel", weight: 0.45, dir: "SHORT", conf: 0.78 },
    { model: "FundingRateModel", weight: 0.05, dir: "FLAT", conf: 0.0 },
    { model: "OrderbookImbalanceModel", weight: 0.2, dir: "SHORT", conf: 0.66 },
    { model: "VWAPModel", weight: 0.15, dir: "LONG", conf: 0.55 },
  ],
  // active_weight excludes the FLAT abstention (0.05): 0.95
  activeWeight: 0.95,
  // score = -(.15*.62) -(.45*.78) -(.20*.66) + (.15*.55) = -0.4935
  score: -0.4935,
  // confidence = 0.4935 / 0.95 = 0.5195
  confidence: 0.52,
  direction: "SHORT",
  longVotes: 1,
  shortVotes: 3,
  flatVotes: 1,
} as const;
