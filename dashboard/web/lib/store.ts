"use client";
import { create } from "zustand";

export type Status = {
  network: string;
  risk_state: string;
  risk_reason: string | null;
  trading_mode?: string;
  directions?: { longs: boolean; shorts: boolean };
  bot_status: string | null;
  phase: string | null;
  control_request: string | null;
  coins_disabled: string[];
  day_open_equity: number;
  week_open_equity: number;
  heartbeat_age_s: number | null;
  recorder_heartbeat_age_s: number | null;
  cache_age_s: number;
  coins: string[];
  regime_history?: {
    ts: number;
    enabled: boolean;
    window: number;
    threshold: number;
    dominant: Record<string, string>;
    coins: Record<string, string[]>;
  } | null;
};

type StatusStore = {
  status: Status | null;
  setStatus: (s: Status) => void;
};

export const useStatusStore = create<StatusStore>((set) => ({
  status: null,
  setStatus: (status) => set({ status }),
}));

// Global band context for the Live page: the SCALP/TREND toggle in the
// Analysis Core drives Open Positions (filter + tag), the chart's default
// timeframe (5m / 1h), and the Analysis Core verdicts — one shared variable
// all three sections subscribe to.
//
// SCALP BAND RETIRED 2026-06-26 — the bot is trend-only, so the SCALP/TREND
// toggle is gone and `activeBand` is permanently "trend". The Band union and the
// store shape are kept so existing subscribers compile, but scalp is never
// selectable and is reported disabled.
export type Band = "scalp" | "trend";
type BandStore = {
  activeBand: Band;
  setActiveBand: (b: Band) => void;
  enabledBands: Record<Band, boolean>;
  setEnabledBands: (e: Record<Band, boolean>) => void;
};
export const useBandStore = create<BandStore>((set) => ({
  activeBand: "trend",
  setActiveBand: (activeBand) => set({ activeBand }),
  enabledBands: { scalp: false, trend: true },
  setEnabledBands: (enabledBands) => set({ enabledBands }),
}));
