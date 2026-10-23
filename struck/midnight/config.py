"""The Midnight settings loader (MID01): thin re-export of
``struck.midnight.settings`` — the ONE settings surface. The per-user file
``midnight.json`` replaces the earlier Cardano-Blockfrost URL table: Midnight
endpoints are operator-entered (never source constants), validated strictly,
and reached live by ``struck.midnight.client``."""

from __future__ import annotations

from .settings import (  # noqa: F401  (re-export: the single import face)
    MIDNIGHT_SETTINGS_FIELDS,
    MIDNIGHT_SETTINGS_PATH,
    MidnightSettings,
    MidnightSettingsError,
    load_midnight_settings,
    redact,
    save_midnight_settings,
)
