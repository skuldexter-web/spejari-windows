"""Channel / band helpers (2.4 GHz and 5 GHz only)."""

from __future__ import annotations

from typing import Optional

BAND_2G = "2.4"
BAND_5G = "5"


def freq_to_band(freq_mhz: int) -> Optional[str]:
    if 2400 <= freq_mhz <= 2500:
        return BAND_2G
    if 5150 <= freq_mhz <= 5895:
        return BAND_5G
    return None


def in_scope(freq_mhz: int) -> bool:
    """Spejari records 2.4 GHz and 5 GHz networks only (6 GHz is ignored)."""
    return freq_to_band(freq_mhz) is not None


def freq_to_channel(freq_mhz: int) -> int:
    if freq_mhz == 2484:
        return 14
    if 2400 <= freq_mhz <= 2500:
        return (freq_mhz - 2407) // 5
    if 5150 <= freq_mhz <= 5895:
        return (freq_mhz - 5000) // 5
    return 0
