"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";

const TABS = [
  { href: "/", label: "Live", icon: "📈" },
  { href: "/signals", label: "Signals", icon: "🎯" },
  { href: "/risk", label: "Risk", icon: "🛡️" },
  { href: "/controls", label: "Controls", icon: "🎛️" },
];

/** Phone-only bottom tab bar (hidden on md+, where the header nav shows). */
export default function BottomNav() {
  const path = usePathname();
  return (
    <nav className="md:hidden fixed bottom-0 inset-x-0 z-20 bg-panel/95 backdrop-blur border-t border-edge pb-[env(safe-area-inset-bottom)]">
      <div className="grid grid-cols-4">
        {TABS.map((t) => (
          <Link
            key={t.href}
            href={t.href}
            className={clsx(
              "flex flex-col items-center gap-0.5 py-2 text-[11px]",
              path === t.href ? "text-glow" : "text-slate-400"
            )}
          >
            <span className="text-lg leading-none">{t.icon}</span>
            {t.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}
