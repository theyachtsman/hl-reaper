"""Config loading: YAML file + .env secrets."""
import copy
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from hyperliquid.utils import constants

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, path: str | None = None):
        load_dotenv(PROJECT_ROOT / ".env")
        cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
        with open(cfg_path) as f:
            self._raw = yaml.safe_load(f)

        # pristine config.yaml snapshot — the floor/defaults that live_config
        # overrides merge on top of. apply_overrides() always rebuilds _raw
        # from this, so clearing an override restores the file default.
        self._base = copy.deepcopy(self._raw)
        self._overrides: dict = {}

        self.network = self._raw.get("network", "testnet")
        if self.network not in ("testnet", "mainnet"):
            raise ValueError(f"invalid network: {self.network}")

        self.api_url = (
            constants.TESTNET_API_URL if self.network == "testnet"
            else constants.MAINNET_API_URL
        )
        self.account_address = self._raw["account_address"]
        self.secret_key = os.environ.get("HL_REAPER_SECRET", "")
        self.coins = self._raw.get("coins", ["BTC"])
        self.candle_intervals = self._raw.get("candle_intervals", ["1m"])
        self.candle_buffer_size = int(self._raw.get("candle_buffer_size", 500))

        p = self._raw.get("pollers", {})
        self.asset_ctx_seconds = int(p.get("asset_ctx_seconds", 60))
        self.funding_history_minutes = int(p.get("funding_history_minutes", 30))
        self.funding_lookback_hours = int(p.get("funding_lookback_hours", 24))

        hb = self._raw.get("heartbeat", {})
        self.heartbeat_path = hb.get("path", "/tmp/hl_reaper_heartbeat")
        self.heartbeat_interval = int(hb.get("interval_seconds", 30))

        self.stale_feed_seconds = int(self._raw.get("stale_feed_seconds", 30))

        db_rel = self._raw.get("db_path", "data/hl_reaper.db")
        self.db_path = str((PROJECT_ROOT / db_rel).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.log_level = self._raw.get("log_level", "INFO")
        self.test_order = self._raw.get("test_order", {})

    @property
    def longs_enabled(self) -> bool:
        """Direction master switch (Controls page, hot-reloadable). Reads the
        live-merged _raw so apply_overrides() changes take effect each loop."""
        return bool((self._raw.get("trading", {}) or {})
                    .get("longs_enabled", True))

    @property
    def shorts_enabled(self) -> bool:
        return bool((self._raw.get("trading", {}) or {})
                    .get("shorts_enabled", True))

    def apply_overrides(self, overrides: dict):
        """Merge live_config overrides onto the pristine config.yaml base.

        `overrides` maps dotted keys ("section.key", e.g. "risk.min_confidence"
        or "per_coin.SOL.usd_size") to JSON-decoded values. _raw is rebuilt
        from the base each call, so removing an override restores the default.
        Returns the keys whose effective value actually changed since the last
        apply (for audit logging by callers)."""
        new_raw = copy.deepcopy(self._base)
        for dotted, val in (overrides or {}).items():
            if not dotted:
                continue
            parts = str(dotted).split(".")
            node = new_raw
            for p in parts[:-1]:
                nxt = node.get(p)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[p] = nxt
                node = nxt
            node[parts[-1]] = val
        changed = sorted(set(overrides or {}) ^ set(self._overrides)) or [
            k for k in (overrides or {})
            if self._overrides.get(k) != overrides.get(k)]
        self._raw = new_raw
        self._overrides = dict(overrides or {})
        return changed

    def get_dotted(self, dotted: str, default=None):
        """Read an effective value by dotted key from the merged config."""
        node = self._raw
        for p in dotted.split("."):
            if not isinstance(node, dict) or p not in node:
                return default
            node = node[p]
        return node

    def require_secret(self):
        if not self.secret_key or self.secret_key.startswith("0xYOUR"):
            raise RuntimeError(
                "HL_REAPER_SECRET not set. Copy .env.example to .env and add "
                "your API wallet private key."
            )
