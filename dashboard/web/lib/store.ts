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
// all three sections subscribe to. Defaults to SCALP (higher-frequency band).
export type Band = "scalp" | "trend";
type BandStore = {
  activeBand: Band;
  setActiveBand: (b: Band) => void;
};
export const useBandStore = create<BandStore>((set) => ({
  activeBand: "scalp",
  setActiveBand: (activeBand) => set({ activeBand }),
}));
