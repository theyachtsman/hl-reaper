"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";
import clsx from "clsx";
import { NAV } from "./data";

export default function Sidebar() {
  const path = usePathname();
  const [open, setOpen] = useState(false);

  const links = (
    <nav className="flex flex-col gap-0.5">
      {NAV.map((n) => {
        const active = path === n.href;
        return (
          <Link
            key={n.href}
            href={n.href}
            onClick={() => setOpen(false)}
            className={clsx(
              "px-3 py-2 rounded-lg text-sm border transition-colors",
              active
                ? "border-[#1D9E75]/45 bg-[#1D9E75]/15 text-[#22c98e] font-medium"
                : "border-transparent text-slate-400 hover:text-white hover:bg-[#1D9E75]/8"
            )}
          >
            {n.label}
          </Link>
        );
      })}
    </nav>
  );

  return (
    <>
      {/* mobile: collapsible bar */}
      <div className="md:hidden mb-4">
        <button
          onClick={() => setOpen((o) => !o)}
          className="w-full flex items-center justify-between px-4 py-2.5 rounded-lg card"
        >
          <span className="label" style={{ color: "#22c98e" }}>
            Documentation
          </span>
          <span className="mono text-slate-400">{open ? "▲" : "▼"}</span>
        </button>
        {open && <div className="mt-2 card">{links}</div>}
      </div>

      {/* desktop: persistent sidebar */}
      <aside className="hidden md:block w-52 shrink-0">
        <div className="sticky top-20">
          <div className="label mb-3 px-3" style={{ color: "#22c98e" }}>
            Documentation
          </div>
          {links}
        </div>
      </aside>
    </>
  );
}
