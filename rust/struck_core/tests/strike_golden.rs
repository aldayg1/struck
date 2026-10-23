//! Strike lane golden tests on the LIVE testnet `/v2/markets` snapshot
//! (2026-10-21 12:04 CDMX, see markets_golden.rs). ETH-USD rules at that
//! instant: tick 0.01 from 39.86, size grid 0.001, min notional 10,
//! limit band 0.05, mark 2514.69101274, max leverage 25, quote_prec 8.

mod common;
use common::net;
use struck_core::markets::{parse_fee_tiers, parse_markets, Markets};
use struck_core::strike_plan::{parse_leg, StrikeLeg};
use struck_core::strike_request::{build_request, congruence, explain};
use struck_core::strike_validate::validate_leg;

const MARKETS: &str = include_str!("golden/strike_testnet_markets.json");
const FEES: &str = include_str!("golden/strike_testnet_fee_tiers.json");

fn mk() -> Markets {
    parse_markets(MARKETS).unwrap()
}
fn fee() -> struck_core::markets::FeeTier {
    parse_fee_tiers(FEES).unwrap().fee_tiers[0].clone()
}
fn leg(j: &str) -> StrikeLeg {
    parse_leg(j).unwrap()
}

const GOOD: &str = r#"{"kind":"order","symbol":"ETH-USD","side":"buy","type":"limit","size":"0.010","price":"2450.00","stop_price":null,"time_in_force":"GTC","post_only":true,"reduce_only":false,"callback_rate":null,"activation_price":null,"leverage":5,"tp":null,"sl":null,"client_order_id":null}"#;

#[test]
fn good_limit_passes_every_rule() {
    let v = validate_leg(&leg(GOOD), &mk(), Some(&fee())).unwrap();
    assert!(v.report.ok, "{:?}", v.report.failed_ids());
    assert!(v.not_evaluated.iter().any(|s| s.starts_with("admission")));
}

#[test]
fn each_market_rule_rejects_its_violation() {
    let cases = [
        (GOOD.replace("2450.00", "2450.005"), "price.tick_grid"),
        (GOOD.replace("2450.00", "39.00"), "price.min"),
        (GOOD.replace("2450.00", "2000.00"), "price.band"),
        (GOOD.replace("\"0.010\"", "\"0.0105\""), "size.limit_grid"),
        (GOOD.replace("\"0.010\"", "\"0.002\""), "notional.min"),
        (GOOD.replace("\"leverage\":5", "\"leverage\":26"), "leverage.range"),
        (GOOD.replace("ETH-USD", "DOGE-USD"), "market.exists"),
        (GOOD.replace("\"type\":\"limit\"", "\"type\":\"market\""), "order.price_present"),
        (GOOD.replace("\"GTC\"", "\"GTX\""), "order.time_in_force"),
    ];
    for (j, rule) in cases {
        let v = validate_leg(&leg(&j), &mk(), Some(&fee())).unwrap();
        assert!(v.report.failed_ids().contains(&rule.to_string()), "{rule}: {:?}", v.report.failed_ids());
    }
}

#[test]
fn unknown_leg_fields_are_refused() {
    let j = GOOD.replace("\"leverage\":5", "\"leverage\":5,\"extra\":1");
    assert!(parse_leg(&j).is_err());
    assert!(parse_leg(&GOOD.replace("\"kind\":\"order\"", "\"kind\":\"twap\"")).is_err());
}

#[test]
fn bracket_rules() {
    let b = r#"{"kind":"bracket","symbol":"ETH-USD","side":"buy","type":"limit","size":"0.010","price":"2450.00","stop_price":null,"time_in_force":null,"post_only":false,"reduce_only":false,"callback_rate":null,"activation_price":null,"leverage":null,"tp":{"type":"take_profit","stop_price":"2600.00","price":null},"sl":{"type":"stop","stop_price":"2300.00","price":null},"client_order_id":null}"#;
    assert!(validate_leg(&leg(b), &mk(), Some(&fee())).unwrap().report.ok);
    let swapped = b.replace("2600.00", "2200.00");
    assert!(validate_leg(&leg(&swapped), &mk(), Some(&fee())).unwrap().report.failed_ids().contains(&"bracket.tp_vs_sl".to_string()));
    let req = build_request(&leg(b), "struck-abc-0").unwrap();
    assert_eq!(req.path, "/v2/order/strategy");
    assert!(req.body.contains(r#""tp_order":{"client_order_id":"struck-abc-0-tp","size":"0.010","stop_price":"2600.00","type":"take_profit"}"#), "{}", req.body);
}

#[test]
fn exact_body_and_congruence() {
    let l = leg(GOOD);
    let req = build_request(&l, "struck-0123456789abcdef01234567-0").unwrap();
    assert_eq!(req.method, "POST");
    assert_eq!(req.path, "/v2/order");
    assert_eq!(
        req.body,
        r#"{"client_order_id":"struck-0123456789abcdef01234567-0","post_only":true,"price":"2450.00","side":"buy","size":"0.010","symbol":"ETH-USD","time_in_force":"GTC","type":"limit"}"#
    );
    let c = congruence(&req, &l, "struck-0123456789abcdef01234567-0", &mk(), Some(&fee())).unwrap();
    assert!(c.report.ok, "{:?}", c.report.failed_ids());
    // a body that differs from the approved leg in one byte is incongruent
    let mut tampered = req.clone();
    tampered.body = tampered.body.replace("\"0.010\"", "\"0.100\"");
    let c = congruence(&tampered, &l, "struck-0123456789abcdef01234567-0", &mk(), Some(&fee())).unwrap();
    assert_eq!(c.report.failed_ids(), ["body.size", "body.equals_approved"]);
    // reserved prefixes are refused
    assert!(build_request(&l, "liq_x").is_err());
}

#[test]
fn explanation_uses_served_values_only() {
    let l = leg(GOOD);
    let fee = parse_fee_tiers(FEES).unwrap().fee_tiers[0].clone();
    let req = build_request(&l, "struck-x-0").unwrap();
    let pre = net("preprod");
    let e = explain(&req, &l, &mk(), &fee, &pre).unwrap();
    assert!(e.lines.iter().any(|x| x.starts_with("served: funding_rate")));
    assert!(e.lines[0].contains(&format!("on {}", pre.strike_label)));
    let main = net("mainnet");
    let m = explain(&req, &l, &mk(), &fee, &main).unwrap();
    assert!(m.lines[0].contains(&format!("on {}", main.strike_label)));
    assert!(main.strike_label.contains("Strike MAINNET"));
}

#[test]
fn twap_builds_the_algo_body_and_checks_slices() {
    let j = r#"{"kind":"twap","symbol":"ETH-USD","side":"BUY","type":null,"size":"0.040","price":null,"stop_price":null,"time_in_force":null,"post_only":false,"reduce_only":false,"callback_rate":null,"activation_price":null,"leverage":null,"tp":null,"sl":null,"client_order_id":null,"duration_sec":300,"randomize":false}"#;
    let l = leg(j);
    let v = validate_leg(&l, &mk(), Some(&fee())).unwrap();
    assert!(v.report.ok, "{:?}", v.report.failed_ids());
    let req = build_request(&l, "struck-twap-0").unwrap();
    assert_eq!(req.method, "POST");
    assert_eq!(req.path, "/v2/algo/twap");
    assert_eq!(req.client_order_id, None);
    assert_eq!(
        req.body,
        r#"{"duration_sec":300,"side":"BUY","symbol":"ETH-USD","total_size":"0.040"}"#
    );
    let short = j.replace("300", "299");
    assert!(parse_leg(&short).is_err());
    assert!(parse_leg(&GOOD.replace("\"kind\":\"order\"", "\"kind\":\"twap\"")).is_err());
}

#[test]
fn margin_mode_builds_the_skill_body() {
    let j = r#"{"kind":"margin_mode","symbol":"ETH-USD","margin_mode":"isolated","side":null,"type":null,"size":null,"price":null,"stop_price":null,"time_in_force":null,"post_only":false,"reduce_only":false,"callback_rate":null,"activation_price":null,"leverage":null,"tp":null,"sl":null,"client_order_id":null}"#;
    let l = leg(j);
    let v = validate_leg(&l, &mk(), Some(&fee())).unwrap();
    assert!(v.report.ok, "{:?}", v.report.failed_ids());
    assert!(v.not_evaluated.iter().any(|s| s.contains("/v2/positions")));
    let req = build_request(&l, "struck-margin-0").unwrap();
    assert_eq!(req.path, "/v2/marginMode");
    assert_eq!(req.client_order_id, None);
    assert_eq!(req.body, r#"{"marginMode":"isolated","symbol":"ETH-USD"}"#);
    assert!(parse_leg(&j.replace("isolated", "hedge")).is_err());
}

/// Oracle: `.codex/skills/strike-grid-bot/SKILL.md` Create — the exact body
/// shape (decimals as strings, grid_count a number, absent optionals omitted)
/// and the service's documented validation errors (SL mode rules, 2..=200,
/// NEUTRAL has no sl_price, MARKET_SEEDED only LONG/SHORT). Mark price at the
/// snapshot instant is 2514.69101274; the range must strictly contain it.
#[test]
fn grid_bot_builds_the_skill_body_and_rules() {
    let j = r#"{"kind":"grid_bot","symbol":"ETH-USD","grid_mode":"LONG","initialization_mode":"LIMIT_ONLY","lower_price":"2400","upper_price":"2600","grid_count":20,"collateral":"500","sl_price":"2300","long_sl_price":null,"short_sl_price":null,"side":null,"type":null,"size":null,"price":null,"stop_price":null,"time_in_force":null,"post_only":false,"reduce_only":false,"callback_rate":null,"activation_price":null,"leverage":null,"tp":null,"sl":null,"client_order_id":null}"#;
    let l = leg(j);
    let v = validate_leg(&l, &mk(), Some(&fee())).unwrap();
    assert!(v.report.ok, "{:?}", v.report.failed_ids());
    assert!(v.report.checks.iter().any(|c| c.id == "grid.spacing_vs_fees" && c.ok));
    let req = build_request(&l, "struck-grid-0").unwrap();
    assert_eq!((req.method.as_str(), req.path.as_str()), ("POST", "/v2/grid/bot"));
    assert_eq!(req.client_order_id, None);
    assert_eq!(
        req.body,
        r#"{"collateral":"500","grid_count":20,"initialization_mode":"LIMIT_ONLY","lower_price":"2400","mode":"LONG","sl_price":"2300","symbol":"ETH-USD","upper_price":"2600"}"#
    );
    // LONG sl above lower: parses (shape ok) but the Report refuses it (the
    // service 400s "sl_price must be below lower_price for LONG mode")
    let bad_sl = parse_leg(&j.replace("2300", "2450")).unwrap();
    let v_bad = validate_leg(&bad_sl, &mk(), Some(&fee())).unwrap();
    assert!(v_bad.report.failed_ids().contains(&"grid.sl_below_lower".to_string()));
    assert!(parse_leg(&j.replace("\"LONG\"", "\"NEUTRAL\"")).is_err()); // sl_price not for NEUTRAL
    assert!(parse_leg(&j.replace("20", "1")).is_err()); // grid_count 2..=200
    let flat = parse_leg(&j.replace("2400", "2600")).unwrap(); // lower == upper: shape ok
    assert!(!validate_leg(&flat, &mk(), Some(&fee())).unwrap().report.ok); // rules refuse it
    // a bot id with a path metacharacter can never enter the URL
    assert!(parse_leg(
        r#"{"kind":"grid_stop","symbol":"ETH-USD","bot_id":"grid-../../evil","cancel_orders":true,"side":null,"type":null,"size":null,"price":null,"stop_price":null,"time_in_force":null,"post_only":false,"reduce_only":false,"callback_rate":null,"activation_price":null,"leverage":null,"tp":null,"sl":null,"client_order_id":null}"#
    )
    .is_err());
    let stop = parse_leg(
        r#"{"kind":"grid_stop","symbol":"ETH-USD","bot_id":"grid-019d06d9-d337-7169-af03-2ea759f7a6aa-ETH-USD-019e63da-5684-7c16-b990-6f0c4c5637ce","cancel_orders":true,"side":null,"type":null,"size":null,"price":null,"stop_price":null,"time_in_force":null,"post_only":false,"reduce_only":false,"callback_rate":null,"activation_price":null,"leverage":null,"tp":null,"sl":null,"client_order_id":null}"#
    )
    .unwrap();
    let stop_req = build_request(&stop, "struck-gridstop-0").unwrap();
    assert_eq!(stop_req.method, "DELETE");
    assert_eq!(
        stop_req.path,
        "/v2/grid/bot/grid-019d06d9-d337-7169-af03-2ea759f7a6aa-ETH-USD-019e63da-5684-7c16-b990-6f0c4c5637ce"
    );
    assert_eq!(stop_req.body, r#"{"cancel_orders":true}"#);
}
