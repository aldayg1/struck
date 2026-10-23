//! Golden: the LIVE testnet `/v2/markets` and `/v2/fee-tiers` bodies fetched
//! 2026-10-21 12:04 CDMX through Struck's RecordedHttp (see
//! the recorded call history). Both networks are
//! supported ( mainnet goldens are not fetched yet.

use rust_decimal::Decimal;
use struck_core::markets::{parse_fee_tiers, parse_markets};

const MARKETS: &str = include_str!("golden/strike_testnet_markets.json");
const FEES: &str = include_str!("golden/strike_testnet_fee_tiers.json");

fn d(s: &str) -> Decimal {
    s.parse().unwrap()
}

#[test]
fn testnet_markets_parse_exactly() {
    let m = parse_markets(MARKETS).unwrap();
    let syms: Vec<&str> = m.markets.keys().map(String::as_str).collect();
    assert_eq!(syms, ["ADA-USD", "BTC-USD", "ETH-USD", "SOL-USD"]);
    let eth = &m.markets["ETH-USD"];
    assert_eq!(eth.order_tick_price, d("0.01"));
    assert_eq!(eth.order_min_price, d("39.86"));
    assert_eq!(eth.order_min_notional, d("10"));
    assert_eq!(eth.quote_prec, 8);
    assert_eq!(eth.max_leverage(), 25);
    assert_eq!(eth.margin_tiers.len(), 4);
    assert_eq!(eth.margin_tiers[1].maintenance_amount, d("100.00"));
    // trailing zeros are kept exactly as served
    assert_eq!(eth.funding_rate.to_string(), "0.0000125000000000000");
    assert_eq!(m.markets["BTC-USD"].order_tick_price.to_string(), "0.10");
}

#[test]
fn tier_lookups_follow_the_skill() {
    let m = parse_markets(MARKETS).unwrap();
    let eth = &m.markets["ETH-USD"];
    // boundary stays in the lower tier; above the last tier -> last tier
    assert_eq!(eth.tier_index_by_leverage(25).unwrap(), 0);
    assert_eq!(eth.tier_index_by_leverage(20).unwrap(), 1);
    assert!(eth.tier_index_by_leverage(0).is_err());
    assert!(eth.tier_index_by_leverage(26).is_err());
}

#[test]
fn testnet_fee_tiers_parse_numbers_exactly() {
    let f = parse_fee_tiers(FEES).unwrap();
    assert_eq!(f.fee_tiers[0].tier, 0);
    assert_eq!(f.fee_tiers[0].taker_rate, d("0.0005"));
    assert_eq!(f.fee_tiers[0].maker_rate, d("0"));
    assert_eq!(f.fee_tiers[1].taker_rate, d("0.00045"));
}

#[test]
fn exponent_fee_numbers_parse_without_floats() {
    // the mainnet shape documented in the surface map §1 (S3): -5e-05
    let f = parse_fee_tiers(r#"{"feeTiers":[{"Tier":0,"MinVolume":0,"TakerRate":0.0005,"MakerRate":-5e-05}]}"#).unwrap();
    assert_eq!(f.fee_tiers[0].maker_rate, d("-0.00005"));
}

#[test]
fn number_where_string_is_served_fails_loudly() {
    let bad = MARKETS.replacen("\"order_tick_price\":\"0.01\"", "\"order_tick_price\":0.01", 1);
    assert_ne!(bad, MARKETS);
    assert!(parse_markets(&bad).is_err());
}
