"""Config loading: YAML file + .env secrets."""
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

    def require_secret(self):
        if not self.secret_key or self.secret_key.startswith("0xYOUR"):
            raise RuntimeError(
                "HL_REAPER_SECRET not set. Copy .env.example to .env and add "
                "your API wallet private key."
            )
