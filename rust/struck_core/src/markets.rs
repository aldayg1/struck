//! Strike market rules exactly as served by `GET /v2/markets` (spec §6.3
//! `markets.rs`; field list `skills/strike-account/SKILL.md:26-97`) and the
//! public `GET /v2/fee-tiers` (PascalCase JSON numbers, `:109-127`).
//!
//! Decimal fields are JSON strings and are parsed with the strict string
//! serde (a number where a string is served fails loudly). Integer fields
//! (`base_prec`, `quote_prec`, `default_leverage`, `max_leverage`,
//! `next_funding_time`) are JSON numbers. Unknown served fields are ignored:
//! Struck reads the rules it uses and nothing else.

use std::collections::BTreeMap;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct MarginTier {
    #[serde(with = "rust_decimal::serde::str")]
    pub max_notional: Decimal,
    pub max_leverage: u32,
    #[serde(with = "rust_decimal::serde::str")]
    pub maintenance_margin_rate: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub maintenance_amount: Decimal,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct Market {
    pub symbol: String,
    pub status: String,
    pub base_prec: u32,
    pub quote_prec: u32,
    pub default_leverage: u32,
    pub margin_tiers: Vec<MarginTier>,
    pub reduce_only: bool,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_tick_price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_min_price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_max_price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_limit_price_bound: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_market_price_bound: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_limit_step_size: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_limit_min_size: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_limit_max_size: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_market_step_size: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_market_min_size: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_market_max_size: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub order_min_notional: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub liquidation_fee_rate: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub mark_price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub index_price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub bid1_price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub ask1_price: Decimal,
    #[serde(with = "rust_decimal::serde::str")]
    pub funding_rate: Decimal,
    pub next_funding_time: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct Markets {
    pub markets: BTreeMap<String, Market>,
}

/// One bracket of `/v2/fee-tiers.feeTiers` (JSON numbers, exact via
/// serde_json `arbitrary_precision`; mainnet serves `-5e-05`).
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct FeeTier {
    #[serde(rename = "Tier")]
    pub tier: u32,
    #[serde(rename = "MinVolume", with = "rust_decimal::serde::arbitrary_precision")]
    pub min_volume: Decimal,
    #[serde(rename = "TakerRate", with = "rust_decimal::serde::arbitrary_precision")]
    pub taker_rate: Decimal,
    #[serde(rename = "MakerRate", with = "rust_decimal::serde::arbitrary_precision")]
    pub maker_rate: Decimal,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct FeeTiers {
    #[serde(rename = "feeTiers")]
    pub fee_tiers: Vec<FeeTier>,
}

pub fn parse_markets(json: &str) -> Result<Markets, String> {
    let m: Markets = serde_json::from_str(json).map_err(|e| format!("/v2/markets: {e}"))?;
    for (key, market) in &m.markets {
        if key != &market.symbol {
            return Err(format!("/v2/markets: key {key} holds symbol {}", market.symbol));
        }
        check_tiers(market)?;
    }
    Ok(m)
}

pub fn parse_fee_tiers(json: &str) -> Result<FeeTiers, String> {
    let f: FeeTiers = serde_json::from_str(json).map_err(|e| format!("/v2/fee-tiers: {e}"))?;
    if f.fee_tiers.is_empty() {
        return Err("/v2/fee-tiers: feeTiers is empty".to_string());
    }
    Ok(f)
}

/// The served tier invariants (`skills/strike-calculations/SKILL.md:38`).
/// A violation means the served data is not what the formulas assume.
fn check_tiers(m: &Market) -> Result<(), String> {
    let t = &m.margin_tiers;
    if t.is_empty() {
        return Err(format!("{}: margin_tiers is empty", m.symbol));
    }
    for w in t.windows(2) {
        if w[1].max_notional < w[0].max_notional
            || w[1].max_leverage > w[0].max_leverage
            || w[1].maintenance_margin_rate < w[0].maintenance_margin_rate
        {
            return Err(format!("{}: margin_tiers not sorted as documented", m.symbol));
        }
    }
    Ok(())
}

impl Market {
    /// Market max leverage = `margin_tiers[0].max_leverage` (SKILL.md:42).
    pub fn max_leverage(&self) -> u32 {
        self.margin_tiers[0].max_leverage
    }

    /// Highest index with `leverage <= max_leverage[i]` (SKILL.md:43).
    pub fn tier_index_by_leverage(&self, leverage: u32) -> Result<usize, String> {
        if leverage == 0 || leverage > self.max_leverage() {
            return Err(format!("{}: leverage {leverage} outside 1..={}", self.symbol, self.max_leverage()));
        }
        let mut idx = 0;
        for (i, t) in self.margin_tiers.iter().enumerate() {
            if leverage <= t.max_leverage {
                idx = i;
            }
        }
        Ok(idx)
    }
}
