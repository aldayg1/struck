//! Golden: the LIVE Strike MAINNET public `/v2/markets`, `/v2/fee-tiers` and
//! `/v2/time` bodies, fetched 2026-10-21 16:04 CDMX through Struck's
//! RecordedHttp (unsigned GETs, source `strike-mainnet`; see
//! the golden provenance record). Spec B1 parsing and the market rules run on
//! the mainnet snapshot exactly as on the testnet one. ETH-USD at that
//! instant: tick 0.1 from 39.9, size grid 0.001, min notional 10, band 0.05,
//! mark 2490.2, 15 margin tiers, max leverage 100, funding -0.0000206705456061463.

mod common;
use common::net;
use rust_decimal::Decimal;
use struck_core::markets::{parse_fee_tiers, parse_markets, Markets};
use struck_core::strike_plan::{parse_leg, StrikeLeg};
use struck_core::strike_request::{build_request, congruence, explain};
use struck_core::strike_validate::validate_leg;

const MARKETS: &str = include_str!("golden/strike_mainnet_markets.json");
const FEES: &str = include_str!("golden/strike_mainnet_fee_tiers.json");
const TIME: &str = include_str!("golden/strike_mainnet_time.json");

const GOOD: &str = r#"{"kind":"order","symbol":"ETH-USD","side":"buy","type":"limit","size":"0.010","price":"2450.0","stop_price":null,"time_in_force":"GTC","post_only":true,"reduce_only":false,"callback_rate":null,"activation_price":null,"leverage":5,"tp":null,"sl":null,"client_order_id":null}"#;

fn d(s: &str) -> Decimal {
    s.parse().unwrap()
}
fn mk() -> Markets {
    parse_markets(MARKETS).unwrap()
}
fn fee() -> struck_core::markets::FeeTier {
    parse_fee_tiers(FEES).unwrap().fee_tiers[0].clone()
}
fn leg(j: &str) -> StrikeLeg {
    parse_leg(j).unwrap()
}

#[test]
fn mainnet_markets_parse_exactly() {
    let m = mk();
    assert_eq!(m.markets.len(), 32);
    for s in ["ADA-USD", "BTC-USD", "ETH-USD", "SOL-USD", "XAU-USD", "SP500-USD"] {
        assert!(m.markets.contains_key(s), "{s}");
    }
    let eth = &m.markets["ETH-USD"];
    assert_eq!(eth.order_tick_price, d("0.1"));
    assert_eq!(eth.order_min_price, d("39.9"));
    assert_eq!(eth.order_min_notional, d("10"));
    assert_eq!(eth.quote_prec, 8);
    assert_eq!(eth.max_leverage(), 100);
    assert_eq!(eth.margin_tiers.len(), 15);
    assert_eq!(eth.margin_tiers[14].maintenance_amount.to_string(), "2007000.0000");
    assert_eq!(eth.funding_rate.to_string(), "-0.0000206705456061463");
    assert_eq!(eth.tier_index_by_leverage(100).unwrap(), 0);
    assert_eq!(eth.tier_index_by_leverage(75).unwrap(), 1);
    assert!(eth.tier_index_by_leverage(101).is_err());
}

#[test]
fn mainnet_fee_tiers_parse_numbers_exactly() {
    let f = parse_fee_tiers(FEES).unwrap();
    assert_eq!(f.fee_tiers.len(), 7);
    assert_eq!(f.fee_tiers[0].taker_rate, d("0.0005"));
    assert_eq!(f.fee_tiers[0].maker_rate, d("-0.00005"));
    assert_eq!(f.fee_tiers[6].taker_rate, d("0.00028"));
}

#[test]
fn mainnet_time_is_the_served_shape() {
    assert_eq!(TIME, r#"{"serverTime":1792620260967}"#);
}

#[test]
fn mainnet_market_rules_and_congruence() {
    let v = validate_leg(&leg(GOOD), &mk(), Some(&fee())).unwrap();
    assert!(v.report.ok, "{:?}", v.report.failed_ids());
    for (j, rule) in [
        (GOOD.replace("2450.0", "2450.05"), "price.tick_grid"),
        (GOOD.replace("\"leverage\":5", "\"leverage\":101"), "leverage.range"),
        (GOOD.replace("2450.0", "2000.0"), "price.band"),
    ] {
        let v = validate_leg(&leg(&j), &mk(), Some(&fee())).unwrap();
        assert!(v.report.failed_ids().contains(&rule.to_string()), "{rule}: {:?}", v.report.failed_ids());
    }
    let l = leg(GOOD);
    let req = build_request(&l, "struck-0123456789abcdef01234567-0").unwrap();
    assert!(congruence(&req, &l, "struck-0123456789abcdef01234567-0", &mk(), Some(&fee())).unwrap().report.ok);
    let fee = parse_fee_tiers(FEES).unwrap().fee_tiers[0].clone();
    let main = net("mainnet");
    let e = explain(&req, &l, &mk(), &fee, &main).unwrap();
    assert!(e.lines[0].contains(&format!("on {}", main.strike_label)));
    assert!(e.lines.iter().any(|x| x.starts_with("served: funding_rate")));
}
