"""Bot state machine states."""
from enum import Enum


class BotState(Enum):
    ACTIVE       = "ACTIVE"       # all guards green, entries allowed
    MANAGING     = "MANAGING"     # daily drawdown hit, no new entries, SL/TP still active
    HALTED       = "HALTED"       # cascade/weekly limit, all positions closed, loop frozen
    RECONNECTING = "RECONNECTING" # WS dropped, entries paused
    COOLDOWN     = "COOLDOWN"     # voluntary pause with countdown timer
    CASCADE_BOUNCE_ACTIVE = "CASCADE_BOUNCE_ACTIVE"  # bounce trade open: ensemble entries paused, positions managed
