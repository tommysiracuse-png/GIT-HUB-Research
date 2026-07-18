"""Paper-only breakout quality helpers.

These helpers are intentionally side-effect free and do not place orders.
They are designed for scanner/report ranking and paper-trading research only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BreakoutQualityConfig:
    mode: str = "paper_only"
    base_breakout_buffer_pct: float = 0.2
    high_atr_pct_threshold: float = 4.5
    high_vol_breakout_buffer_pct: float = 0.35
    min_rel_volume: float = 1.8
    max_spread_pct: float = 0.35
    require_above_vwap: bool = True
    require_above_20ema: bool = True


def breakout_confirmation_buffer_pct(atr_pct: float, config: BreakoutQualityConfig | None = None) -> float:
    cfg = config or BreakoutQualityConfig()
    if atr_pct > cfg.high_atr_pct_threshold:
        return cfg.high_vol_breakout_buffer_pct
    return cfg.base_breakout_buffer_pct


def is_high_conviction_breakout(
    *,
    price_above_vwap: bool,
    price_above_20ema: bool,
    rel_volume: float,
    spread_pct: float,
    atr_pct: float,
    config: BreakoutQualityConfig | None = None,
) -> bool:
    cfg = config or BreakoutQualityConfig()
    if cfg.require_above_vwap and not price_above_vwap:
        return False
    if cfg.require_above_20ema and not price_above_20ema:
        return False
    if rel_volume < cfg.min_rel_volume:
        return False
    if spread_pct > cfg.max_spread_pct:
        return False
    return breakout_confirmation_buffer_pct(atr_pct, cfg) >= cfg.base_breakout_buffer_pct


def score_breakout_quality(
    *,
    price_above_vwap: bool,
    price_above_20ema: bool,
    rel_volume: float,
    spread_pct: float,
    atr_pct: float,
    config: BreakoutQualityConfig | None = None,
) -> float:
    """Return a 0-100 paper-only quality score.

    Higher is better; this is only for ranking candidates.
    """
    cfg = config or BreakoutQualityConfig()
    score = 100.0

    if cfg.require_above_vwap and not price_above_vwap:
        score -= 35.0
    if cfg.require_above_20ema and not price_above_20ema:
        score -= 25.0

    score -= max(0.0, (cfg.min_rel_volume - rel_volume) * 20.0)
    score -= max(0.0, (spread_pct - cfg.max_spread_pct) * 250.0)
    if atr_pct > cfg.high_atr_pct_threshold:
        score -= 10.0

    return max(0.0, min(100.0, score))
