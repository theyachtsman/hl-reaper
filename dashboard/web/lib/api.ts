"use client";
import { useEffect, useState } from "react";

export async function api<T = any>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { cache: "no-store", ...init });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}

export function getPin(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("hl_dash_pin") ?? "";
}

export function setPin(pin: string) {
  localStorage.setItem("hl_dash_pin", pin);
}

export function post<T = any>(path: string, body?: any): Promise<T> {
  return api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dash-Token": getPin() },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

/** Poll an endpoint on an interval; survives transient bridge outages. */
export function usePoll<T = any>(path: string, ms = 5000) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    const tick = () =>
      api<T>(path)
        .then((d) => { if (live) { setData(d); setErr(null); } })
        .catch((e) => { if (live) setErr(String(e)); });
    tick();
    const id = setInterval(tick, ms);
    return () => { live = false; clearInterval(id); };
  }, [path, ms]);
  return { data, err };
}

export const fmtUsd = (v: number | null | undefined, dp = 2) =>
  v == null ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;

export const fmtPct = (v: number | null | undefined, dp = 2) =>
  v == null ? "—" : `${(v * 100).toFixed(dp)}%`;

export const fmtTs = (ts: number | null | undefined) =>
  ts ? new Date(ts).toLocaleString("en-US", { hour12: false }) : "—";
