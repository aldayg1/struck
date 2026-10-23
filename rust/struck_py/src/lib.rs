//! PyO3 boundary for struck_core. Values cross as str/int only; JSON is str.
//! Errors become `ValueError` (never a default, never a partial result).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use struck_core::money::{
    ada_to_lovelace as core_ada, compare_quantities as core_compare_qty,
    percent_of_quantity as core_percent, Lovelace,
};
use struck_core::plan::{parse_json, select_for_plan, validate_plan, NetParams, OnchainPlan, Utxo};
use struck_core::markets::{parse_fee_tiers, parse_markets};
use struck_core::policy::{self, Policy};
use struck_core::strike_plan::{parse_leg, parse_legs};
use struck_core::strike_request::{self, ApiRequest};
use struck_core::{congruence, decimal, decode, explain, strike_validate};

fn err(msg: String) -> PyErr {
    PyErr::new::<PyValueError, _>(msg)
}

fn to_json<T: serde::Serialize>(value: &T) -> PyResult<String> {
    serde_json::to_string(value).map_err(|e| err(e.to_string()))
}

#[pyfunction]
fn ada_to_lovelace(ada: &str) -> PyResult<u64> {
    core_ada(ada).map(|l| l.0).map_err(err)
}

#[pyfunction]
fn format_lovelace(lovelace: u64) -> String {
    Lovelace(lovelace).to_string()
}

/// Sum a JSON array of non-negative integers. Overflow is an error.
/// Empty array is 0. Lovelace and token quantities; Python does not add them.
#[pyfunction]
fn sum_quantities(json_ints: &str) -> PyResult<u64> {
    let values: Vec<u64> = serde_json::from_str(json_ints)
        .map_err(|e| err(format!("sum_quantities expects a JSON array of integers: {e}")))?;
    Lovelace::sum(values.into_iter().map(Lovelace))
        .map(|total| total.0)
        .map_err(err)
}

/// Exact compare of two non-negative integer quantities (lovelace or token
/// count). `op` is `eq`, `gt`, `lt`, `gte`, or `lte`. A decimal, a sign, or
/// an unknown op is `ValueError`.
#[pyfunction]
fn compare_quantities(left: &str, op: &str, right: &str) -> PyResult<bool> {
    core_compare_qty(left, op, right).map_err(err)
}

/// Whole-quantity percent of an integer amount. A fractional result is an error.
#[pyfunction]
fn percent_of_quantity(amount: &str, percent: &str) -> PyResult<u64> {
    core_percent(amount, percent).map_err(err)
}

#[pyfunction]
fn validate_onchain_plan(plan_json: &str, utxos_json: &str, current_slot: u64, network_json: &str) -> PyResult<String> {
    let net = NetParams::parse(network_json).map_err(err)?;
    let plan: OnchainPlan = parse_json("plan", plan_json).map_err(err)?;
    let utxos: Vec<Utxo> = parse_json("utxos", utxos_json).map_err(err)?;
    to_json(&validate_plan(&plan, &utxos, current_slot, &net))
}

#[pyfunction]
fn select_inputs_for_plan(plan_json: &str, utxos_json: &str) -> PyResult<Vec<String>> {
    let plan: OnchainPlan = parse_json("plan", plan_json).map_err(err)?;
    let utxos: Vec<Utxo> = parse_json("utxos", utxos_json).map_err(err)?;
    select_for_plan(&plan, &utxos).map_err(err)
}

#[pyfunction]
fn decode_tx(cbor_hex: &str) -> PyResult<String> {
    to_json(&decode::decode_tx(cbor_hex).map_err(err)?)
}

#[pyfunction]
fn congruence_check(cbor_hex: &str, plan_json: &str, inputs_json: &str, network_json: &str) -> PyResult<String> {
    let net = NetParams::parse(network_json).map_err(err)?;
    let tx = decode::decode_tx(cbor_hex).map_err(err)?;
    let plan: OnchainPlan = parse_json("plan", plan_json).map_err(err)?;
    let inputs: Vec<Utxo> = parse_json("inputs", inputs_json).map_err(err)?;
    to_json(&congruence::check(&tx, &plan, &inputs, &net).map_err(err)?)
}

/// N7c: the wallet-signed tx vs the APPROVED tx id and from_address (Pallas):
/// tx id unchanged, exactly one vkey witness, key hash of from_address,
/// ed25519 signature over the tx id. A CheckReport JSON; never raises on a bad tx.
#[pyfunction]
fn verify_signed_tx(signed_hex: &str, approved_tx_id: &str, from_address: &str) -> PyResult<String> {
    to_json(&struck_core::signed::verify_signed(signed_hex, approved_tx_id, from_address))
}

/// Post-signature view: the SIGNED tx in plain words (witness count, signer key
/// hash vs the wallet's payment key hash, signature over the body hash, tx id).
#[pyfunction]
fn describe_signed_tx(signed_hex: &str, approved_tx_id: &str, from_address: &str, network_json: &str) -> PyResult<String> {
    let net = NetParams::parse(network_json).map_err(err)?;
    to_json(&struck_core::signed::describe_signed(signed_hex, approved_tx_id, from_address, &net.label).map_err(err)?)
}

#[pyfunction]
fn explain_tx(cbor_hex: &str, plan_json: &str, inputs_json: &str, ttl_wallclock: &str, network_json: &str) -> PyResult<String> {
    let net = NetParams::parse(network_json).map_err(err)?;
    let tx = decode::decode_tx(cbor_hex).map_err(err)?;
    let plan: OnchainPlan = parse_json("plan", plan_json).map_err(err)?;
    let inputs: Vec<Utxo> = parse_json("inputs", inputs_json).map_err(err)?;
    to_json(&explain::explain(&tx, &plan, &inputs, ttl_wallclock, &net).map_err(err)?)
}

/// ADA deposit: one foreign output of `deposit_lovelace` is the unverifiable
/// Strike exchanger. A different amount fails. Nothing is signed.
#[pyfunction]
fn deposit_explain(
    cbor_hex: &str,
    user_address: &str,
    deposit_lovelace: u64,
    max_fee_lovelace: u64,
    valid_until_slot: u64,
    inputs_json: &str,
    ttl_wallclock: &str,
    network_json: &str,
) -> PyResult<String> {
    let net = NetParams::parse(network_json).map_err(err)?;
    let tx = decode::decode_tx(cbor_hex).map_err(err)?;
    let inputs: Vec<Utxo> = parse_json("inputs", inputs_json).map_err(err)?;
    to_json(
        &explain::explain_ada_deposit(
            &tx,
            user_address,
            deposit_lovelace,
            max_fee_lovelace,
            valid_until_slot,
            &inputs,
            ttl_wallclock,
            &net,
        )
        .map_err(err)?,
    )
}

fn fee_tier(fee_tiers_json: &str, tier: u32) -> PyResult<struck_core::markets::FeeTier> {
    let tiers = parse_fee_tiers(fee_tiers_json).map_err(err)?;
    tiers.fee_tiers.into_iter().find(|t| t.tier == tier).ok_or_else(|| err(format!("fee tier {tier} not served")))
}

#[pyfunction]
fn strike_validate_leg(leg_json: &str, markets_json: &str, fee_tiers_json: &str, tier: u32) -> PyResult<String> {
    let leg = parse_leg(leg_json).map_err(err)?;
    let markets = parse_markets(markets_json).map_err(err)?;
    let fee = fee_tier(fee_tiers_json, tier)?;
    to_json(&strike_validate::validate_leg(&leg, &markets, Some(&fee)).map_err(err)?)
}

#[pyfunction]
fn strike_build_request(leg_json: &str, client_order_id: &str) -> PyResult<String> {
    let leg = parse_leg(leg_json).map_err(err)?;
    to_json(&strike_request::build_request(&leg, client_order_id).map_err(err)?)
}

#[pyfunction]
fn strike_request_congruence(request_json: &str, leg_json: &str, client_order_id: &str, markets_json: &str, fee_tiers_json: &str, tier: u32) -> PyResult<String> {
    let req: ApiRequest = parse_json("request", request_json).map_err(err)?;
    let leg = parse_leg(leg_json).map_err(err)?;
    let markets = parse_markets(markets_json).map_err(err)?;
    let fee = fee_tier(fee_tiers_json, tier)?;
    to_json(&strike_request::congruence(&req, &leg, client_order_id, &markets, Some(&fee)).map_err(err)?)
}

#[pyfunction]
fn strike_explain(request_json: &str, leg_json: &str, markets_json: &str, fee_tiers_json: &str, tier: u32, network_json: &str) -> PyResult<String> {
    let net = NetParams::parse(network_json).map_err(err)?;
    let req: ApiRequest = parse_json("request", request_json).map_err(err)?;
    let leg = parse_leg(leg_json).map_err(err)?;
    let markets = parse_markets(markets_json).map_err(err)?;
    let fee = fee_tier(fee_tiers_json, tier)?;
    to_json(&strike_request::explain(&req, &leg, &markets, &fee, &net).map_err(err)?)
}

#[pyfunction]
fn policy_check_onchain(policy_json: &str, send_json: &str, now_ms: u64) -> PyResult<String> {
    let pol: Policy = parse_json("policy", policy_json).map_err(err)?;
    let send: policy::ProposedSend = parse_json("send", send_json).map_err(err)?;
    to_json(&policy::check_onchain(&pol, &send, now_ms))
}

#[pyfunction]
fn policy_check_strike(policy_json: &str, network: &str, legs_json: &str, markets_json: &str, now_ms: u64) -> PyResult<String> {
    let pol: Policy = parse_json("policy", policy_json).map_err(err)?;
    let legs = parse_legs(legs_json).map_err(err)?;
    let markets = parse_markets(markets_json).map_err(err)?;
    to_json(&policy::check_strike(&pol, network, &legs, &markets, now_ms).map_err(err)?)
}

#[pyfunction]
fn decimal_compare(value: &str, op: &str, threshold: &str) -> PyResult<bool> {
    decimal::compare(value, op, threshold).map_err(err)
}

/// Reputation report over SERVED evidence (rule struck-reputation-1). A
/// report JSON; incomplete evidence is a scored=false report, never a guess.
#[pyfunction]
fn reputation_score(evidence_json: &str) -> PyResult<String> {
    to_json(&struck_core::reputation::score(evidence_json).map_err(err)?)
}

#[pymodule]
fn struck_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(ada_to_lovelace, m)?)?;
    m.add_function(wrap_pyfunction!(verify_signed_tx, m)?)?;
    m.add_function(wrap_pyfunction!(format_lovelace, m)?)?;
    m.add_function(wrap_pyfunction!(sum_quantities, m)?)?;
    m.add_function(wrap_pyfunction!(compare_quantities, m)?)?;
    m.add_function(wrap_pyfunction!(percent_of_quantity, m)?)?;
    m.add_function(wrap_pyfunction!(validate_onchain_plan, m)?)?;
    m.add_function(wrap_pyfunction!(select_inputs_for_plan, m)?)?;
    m.add_function(wrap_pyfunction!(decode_tx, m)?)?;
    m.add_function(wrap_pyfunction!(congruence_check, m)?)?;
    m.add_function(wrap_pyfunction!(explain_tx, m)?)?;
    m.add_function(wrap_pyfunction!(deposit_explain, m)?)?;
    m.add_function(wrap_pyfunction!(describe_signed_tx, m)?)?;
    m.add_function(wrap_pyfunction!(strike_validate_leg, m)?)?;
    m.add_function(wrap_pyfunction!(strike_build_request, m)?)?;
    m.add_function(wrap_pyfunction!(strike_request_congruence, m)?)?;
    m.add_function(wrap_pyfunction!(strike_explain, m)?)?;
    m.add_function(wrap_pyfunction!(policy_check_onchain, m)?)?;
    m.add_function(wrap_pyfunction!(policy_check_strike, m)?)?;
    m.add_function(wrap_pyfunction!(decimal_compare, m)?)?;
    m.add_function(wrap_pyfunction!(reputation_score, m)?)?;
    m.add_function(wrap_pyfunction!(lien_transition, m)?)?;
    Ok(())
}

/// LIEN contract: the exact verdict of one transition attempt against the
/// current stored state. The RESILIENT receipt (ok or refusal reasons) as
/// JSON; it never panics and never guesses. Rule struck-lien-1.
#[pyfunction]
fn lien_transition(input_json: &str) -> PyResult<String> {
    Ok(struck_core::liens::transition_receipt(input_json))
}
