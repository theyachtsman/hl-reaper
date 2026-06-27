import {
  PageHeader,
  Section,
  Card,
  Pill,
  Callout,
  CheckRow,
  ACCENT_BRIGHT,
} from "../_components/ui";

export const metadata = { title: "Controls · Docs" };

export default function Controls() {
  return (
    <>
      <PageHeader
        kicker="Controls"
        title="Operating the bot"
        intro="The Controls page is the only place that writes to the live configuration. Every change is hot-reloaded into the running bot within one loop (~10s) — you never restart it. This page explains each control and the few things you should not touch mid-trade."
      />

      <Callout tone="warn" title="Scalp band + structural gates retired — 2026-06-26">
        The Controls page no longer has a Scalp Band section or an Entry Filters
        (structural gates) section — both were removed when the scalp band was
        retired (trend-only operation). The “Reading structural-gate status” notes
        below are historical and no longer apply.
      </Callout>

      <Section title="The control PIN">
        <Card>
          <p className="text-sm text-slate-300 leading-relaxed mb-2">
            Every control that changes state or settings requires a PIN, sent with the request as an{" "}
            <span className="mono">X-Dash-Token</span> header. Without a valid PIN the bridge rejects
            the request with a 401. Enter it once in the PIN field on the Controls page and it is
            remembered in your browser for subsequent actions.
          </p>
          <p className="text-xs text-slate-500">
            The PIN protects the bot from anyone who can merely reach the dashboard on the LAN —
            viewing is open, but acting is gated.
          </p>
        </Card>
      </Section>

      <Section title="Hot-reload, not restart">
        <Callout tone="accent" title="Changes apply within ~10s">
          The trading loop re-reads the merged live configuration at the top of every cycle and
          pushes the new values straight into the risk manager and the loop. There is no caching
          across cycles and no restart — clearing all overrides restores the config.yaml floor.
        </Callout>
      </Section>

      <Section title="Global vs band-specific settings">
        <div className="grid sm:grid-cols-2 gap-3">
          <Card>
            <div className="label mb-2" style={{ color: ACCENT_BRIGHT }}>
              Global
            </div>
            <ul className="text-sm text-slate-400 space-y-1">
              <li>• Longs / shorts master switches</li>
              <li>• Leverage ceiling, default size</li>
              <li>• Drawdown & cascade thresholds</li>
              <li>• Funding hard-block, counter-trend penalty</li>
              <li>• Active coin set</li>
            </ul>
          </Card>
          <Card>
            <div className="label mb-2" style={{ color: "#22d3ee" }}>
              Per band (scalp / trend)
            </div>
            <ul className="text-sm text-slate-400 space-y-1">
              <li>• Band on/off and per-direction toggles</li>
              <li>• Min confidence & agreement</li>
              <li>• SL multiplier, TP R, trailing, breakeven</li>
              <li>• Max hold, max concurrent, position size</li>
              <li>• Structural gates (scalp)</li>
            </ul>
          </Card>
        </div>
        <p className="text-xs text-slate-500 mt-2">
          Effective direction = the global master AND the band's own flag. A band can run long-only
          while the other runs both.
        </p>
      </Section>

      <Section title="Reading structural-gate status">
        <p className="text-slate-400 text-sm mb-3">
          The Controls and Signals views show each gate's signals as live ✓ / ✗ checks per coin.
          All required signals must read ✓ for that side to be allowed to enter:
        </p>
        <Card>
          <CheckRow ok>green ✓ — this signal currently passes</CheckRow>
          <CheckRow ok={false}>red ✗ — this signal is blocking entry right now</CheckRow>
        </Card>
        <p className="text-xs text-slate-500 mt-2">
          A gate switched off in Controls is shown with hazard stripes — it is not evaluating, so
          that side is ungated.
        </p>
      </Section>

      <Section title="HALT vs Pause vs Close-all">
        <div className="space-y-3">
          <Card>
            <Pill tone="short">Emergency HALT</Pill>
            <p className="text-sm text-slate-400 mt-2">
              Closes every open position immediately and freezes the bot in HALTED until you
              manually resume. Use this when something is wrong and you want everything flat now.
            </p>
          </Card>
          <Card>
            <Pill tone="warn">Pause</Pill>
            <p className="text-sm text-slate-400 mt-2">
              Drops to MANAGING: no new entries, but open positions keep their stops, targets,
              trailing and breakeven management. Reversible with Resume. Use this to stop opening
              into conditions you do not trust while letting existing trades play out.
            </p>
          </Card>
          <Card>
            <Pill tone="muted">Close-all</Pill>
            <p className="text-sm text-slate-400 mt-2">
              Closes everything now and drops to MANAGING — lighter than HALT (no long timed
              lockout). You can also close a single coin.
            </p>
          </Card>
        </div>
      </Section>

      <Section title="Presets & per-coin overrides">
        <p className="text-sm text-slate-400 leading-relaxed mb-3">
          Apply a preset to load a whole configuration in one click; changing any value afterward
          flips the active label to CUSTOM (see the Presets page). You can also override size and
          leverage for individual coins — a per-coin size wins over the band's default size for that
          coin.
        </p>
      </Section>

      <Callout tone="warn" title="Do not change these while in a live position">
        Avoid editing the stop multiplier, take-profit R, breakeven R, or position size for a band
        that is currently holding a trade — the in-trade tracker was seeded from the values that
        were live at entry, and shifting the geometry mid-trade can move stops/targets in ways you
        did not intend. Adjust those between trades, or manage an open position with the chart's
        manual SL/TP sliders instead.
      </Callout>
    </>
  );
}
