"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect } from "react";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useStatusStore } from "@/lib/store";
import ModeBadge from "./ModeBadge";
import StateBadge from "./StateBadge";

const NAV = [
  { href: "/", label: "Live" },
  { href: "/signals", label: "Signals" },
  { href: "/risk", label: "Risk" },
  { href: "/history", label: "History" },
  { href: "/controls", label: "Controls" },
];

export default function Header() {
  const path = usePathname();
  const { status, setStatus } = useStatusStore();

  useEffect(() => {
    const tick = () => api("/api/status").then(setStatus).catch(() => {});
    tick();
    const id = setInterval(tick, 5000);
    return () => clearInterval(id);
  }, [setStatus]);

  const hbOk = status?.heartbeat_age_s != null && status.heartbeat_age_s < 90;
  return (
    <header className="border-b border-edge bg-panel/70 backdrop-blur sticky top-0 z-10">
      <div className="max-w-6xl mx-auto px-3 md:px-4 py-2.5 md:py-3 flex items-center gap-3 md:gap-6">
        <div className="font-bold text-base md:text-lg tracking-tight whitespace-nowrap">
          HL <span className="text-glow">REAPER</span>
        </div>
        {/* desktop nav — phones use the bottom tab bar */}
        <nav className="hidden md:flex gap-1">
          {NAV.map((n) => (
            <Link
              key={n.href}
              href={n.href}
              className={clsx(
                "px-3 py-1.5 rounded-lg text-sm",
                path === n.href
                  ? "bg-edge text-white"
                  : "text-slate-400 hover:text-white hover:bg-edge/50"
              )}
            >
              {n.label}
            </Link>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-2 md:gap-3 text-xs">
          {status && (
            <>
              <span className="hidden sm:inline text-slate-400 uppercase">{status.network}</span>
              <span className={hbOk ? "text-emerald-400" : "text-red-400"}>
                ♥ {status.heartbeat_age_s != null ? `${status.heartbeat_age_s}s` : "—"}
              </span>
              <ModeBadge mode={status.trading_mode} />
              <StateBadge state={status.risk_state} />
            </>
          )}
        </div>
      </div>
    </header>
  );
}
