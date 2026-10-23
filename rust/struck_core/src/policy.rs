//! Optional opt-in pre-approval policy (spec D5 / Q1 CLOSED, §6.3
//! `policy.rs`). The operator approves a `Policy` once; a proposal that is
//! FULLY inside it may skip the fresh approval. Anything outside, any unknown
//! leg kind, an expired policy, or a policy with no section for the lane:
//! not inside, so Struck asks (the default). Deterministic, exact integers
//! and decimals only.

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal::{self, mul};
use crate::markets::Markets;
use crate::plan::PlanOutput;
use crate::report::Report;
use crate::strike_plan::StrikeLeg;

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OnchainPolicy {
    /// Exact bech32 addresses outputs may pay (change to `from_address` is
    /// built by Struck, it is not a plan output).
    pub allowed_addresses: Vec<String>,
    pub max_lovelace_per_output: u64,
    pub max_lovelace_total: u64,
    pub max_fee_lovelace: u64,
    pub allow_native_assets: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StrikePolicy {
    pub allowed_symbols: Vec<String>,
    pub allowed_kinds: Vec<String>,
    pub allowed_order_types: Vec<String>,
    pub allowed_sides: Vec<String>,
    /// Decimal string, USD notional at the order price (mark for market-style).
    pub max_notional: String,
    pub max_leverage: u32,
    pub reduce_only_required: bool,
}

/// A proposed send as a fired trigger produces it (before N2 picks the
/// validity slot): only what the policy bounds.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProposedSend {
    pub network: String,
    pub from_address: String,
    pub outputs: Vec<PlanOutput>,
    pub max_fee_lovelace: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub policy_id: String,
    pub network: String,
    pub expires_at_unix_ms: u64,
    pub onchain: Option<OnchainPolicy>,
    pub strike: Option<StrikePolicy>,
}

fn common(r: &mut Report, p: &Policy, network: &str, now_ms: u64) {
    r.push("policy.network", p.network.clone(), network.to_string(), p.network == network);
    r.push("policy.not_expired", format!("now < {}", p.expires_at_unix_ms), now_ms.to_string(), now_ms < p.expires_at_unix_ms);
}

pub fn check_onchain(p: &Policy, plan: &ProposedSend, now_ms: u64) -> Report {
    let mut r = Report::new();
    common(&mut r, p, &plan.network, now_ms);
    let Some(op) = &p.onchain else {
        r.push("policy.onchain_section", "present".into(), "absent".into(), false);
        return r;
    };
    r.push("policy.max_fee", format!("<= {}", op.max_fee_lovelace), plan.max_fee_lovelace.to_string(), plan.max_fee_lovelace <= op.max_fee_lovelace);
    let mut total: u128 = 0;
    for (i, o) in plan.outputs.iter().enumerate() {
        r.push(&format!("output[{i}].address_allowed"), "in allowed_addresses".into(), o.address.clone(), op.allowed_addresses.contains(&o.address));
        r.push(&format!("output[{i}].lovelace_cap"), format!("<= {}", op.max_lovelace_per_output), o.lovelace.to_string(), o.lovelace <= op.max_lovelace_per_output);
        r.push(
            &format!("output[{i}].assets"),
            format!("allowed={}", op.allow_native_assets),
            o.assets.len().to_string(),
            op.allow_native_assets || o.assets.is_empty(),
        );
        total += u128::from(o.lovelace);
    }
    r.push("outputs.total_cap", format!("<= {}", op.max_lovelace_total), total.to_string(), total <= u128::from(op.max_lovelace_total));
    r
}

pub fn check_strike(p: &Policy, network: &str, legs: &[StrikeLeg], markets: &Markets, now_ms: u64) -> Result<Report, String> {
    let mut r = Report::new();
    common(&mut r, p, network, now_ms);
    let Some(sp) = &p.strike else {
        r.push("policy.strike_section", "present".into(), "absent".into(), false);
        return Ok(r);
    };
    let cap = decimal::parse("policy.max_notional", &sp.max_notional)?;
    for (i, leg) in legs.iter().enumerate() {
        let kind = match leg {
            StrikeLeg::Order { .. } => "order",
            StrikeLeg::Bracket { .. } => "bracket",
            StrikeLeg::Leverage { .. } => "leverage",
            StrikeLeg::Cancel { .. } => "cancel",
            StrikeLeg::Twap { .. } => "twap",
            StrikeLeg::MarginMode { .. } => "margin_mode",
            StrikeLeg::GridBot { .. } => "grid_bot",
            StrikeLeg::GridStop { .. } => "grid_stop",
            StrikeLeg::CancelAll { .. } => "cancel_all",
            StrikeLeg::IsoMargin { .. } => "iso_margin",
            StrikeLeg::TwapCancel { .. } => "twap_cancel",
            StrikeLeg::Replace { .. } => "replace",
            StrikeLeg::Batch { .. } => "batch",
            StrikeLeg::ReplaceBatch { .. } => "replace_batch",
            StrikeLeg::CopySubscribe { .. } => "copy_subscribe",
            StrikeLeg::CopyPatch { .. } => "copy_patch",
            StrikeLeg::CopyStop { .. } => "copy_stop",
            StrikeLeg::CopySave { .. } => "copy_save",
            StrikeLeg::CopyUnsave { .. } => "copy_unsave",
            StrikeLeg::Rfq { .. } => "rfq",
            StrikeLeg::RfqClose { .. } => "rfq_close",
            StrikeLeg::RfqResponse { .. } => "rfq_response",
            StrikeLeg::WithdrawQuote { .. } => "withdraw_quote",
            StrikeLeg::Withdraw { .. } => "withdraw",
            StrikeLeg::VaultDeposit { .. } => "vault_deposit",
            StrikeLeg::VaultWithdraw { .. } => "vault_withdraw",
            StrikeLeg::VaultWithdrawCancel { .. } => "vault_withdraw_cancel",
            StrikeLeg::VaultCreate { .. } => "vault_create",
            StrikeLeg::VaultPause { .. } => "vault_pause",
            StrikeLeg::VaultResume { .. } => "vault_resume",
        };
        r.push(&format!("leg[{i}].kind"), sp.allowed_kinds.join("|"), kind.into(), sp.allowed_kinds.iter().any(|k| k == kind));
        if !leg.symbol().is_empty() {
            // a grid stop addresses a bot id, not a market symbol
            let sym = leg.symbol().to_string();
            r.push(&format!("leg[{i}].symbol"), sp.allowed_symbols.join("|"), sym.clone(), sp.allowed_symbols.contains(&sym));
        }
        match leg {
            StrikeLeg::Order { order: o }
            | StrikeLeg::Bracket { order: o, .. }
            | StrikeLeg::Replace { order: o, .. } => {
                r.push(&format!("leg[{i}].type"), sp.allowed_order_types.join("|"), o.order_type.clone(), sp.allowed_order_types.contains(&o.order_type));
                r.push(&format!("leg[{i}].side"), sp.allowed_sides.join("|"), o.side.clone(), sp.allowed_sides.contains(&o.side));
                r.push(
                    &format!("leg[{i}].reduce_only"),
                    format!("required={}", sp.reduce_only_required),
                    o.reduce_only.to_string(),
                    !sp.reduce_only_required || o.reduce_only,
                );
                let size = decimal::parse("size", &o.size)?;
                let reference = match &o.price {
                    Some(px) => decimal::parse("price", px)?,
                    None => markets
                        .markets
                        .get(&o.symbol)
                        .map(|m| m.mark_price)
                        .ok_or_else(|| format!("{} not in /v2/markets", o.symbol))?,
                };
                let notional: Decimal = mul(reference, size)?;
                r.push(&format!("leg[{i}].notional_cap"), format!("<= {cap}"), notional.to_string(), notional <= cap);
                match o.leverage {
                    Some(l) => r.push(&format!("leg[{i}].leverage_cap"), format!("<= {}", sp.max_leverage), l.to_string(), l <= sp.max_leverage),
                    None => r.push(&format!("leg[{i}].leverage_cap"), "explicit leverage in plan".into(), "none".into(), false),
                }
            }
            StrikeLeg::Leverage { leverage, .. } => {
                r.push(&format!("leg[{i}].leverage_cap"), format!("<= {}", sp.max_leverage), leverage.to_string(), *leverage <= sp.max_leverage);
            }
            StrikeLeg::Cancel { .. } | StrikeLeg::MarginMode { .. } | StrikeLeg::CancelAll { .. } => {}
            StrikeLeg::IsoMargin { amount, .. } => {
                let committed = decimal::parse("amount", amount)?;
                r.push(
                    &format!("leg[{i}].notional_cap"),
                    format!("<= {cap}"),
                    committed.to_string(),
                    committed <= cap,
                );
            }
            StrikeLeg::TwapCancel { .. } => {}
            StrikeLeg::Batch { orders, .. } => {
                for o in orders {
                    let size = decimal::parse("size", &o.size)?;
                    let reference = match &o.price {
                        Some(px) => decimal::parse("price", px)?,
                        None => markets
                            .markets
                            .get(&o.symbol)
                            .map(|m| m.mark_price)
                            .ok_or_else(|| format!("{} not in /v2/markets", o.symbol))?,
                    };
                    let notional: Decimal = mul(reference, size)?;
                    r.push(
                        &format!("leg[{i}].notional_cap"),
                        format!("<= {cap}"),
                        notional.to_string(),
                        notional <= cap,
                    );
                }
            }
            StrikeLeg::ReplaceBatch { replacements, .. } => {
                for item in replacements {
                    let o = &item.order;
                    let size = decimal::parse("size", &o.size)?;
                    let reference = match &o.price {
                        Some(px) => decimal::parse("price", px)?,
                        None => markets
                            .markets
                            .get(&o.symbol)
                            .map(|m| m.mark_price)
                            .ok_or_else(|| format!("{} not in /v2/markets", o.symbol))?,
                    };
                    let notional: Decimal = mul(reference, size)?;
                    r.push(
                        &format!("leg[{i}].notional_cap"),
                        format!("<= {cap}"),
                        notional.to_string(),
                        notional <= cap,
                    );
                }
            }
            StrikeLeg::Twap {
                side,
                total_size,
                limit_price,
                reduce_only,
                symbol,
                ..
            } => {
                r.push(
                    &format!("leg[{i}].side"),
                    sp.allowed_sides.join("|"),
                    side.clone(),
                    sp.allowed_sides.contains(side),
                );
                r.push(
                    &format!("leg[{i}].reduce_only"),
                    format!("required={}", sp.reduce_only_required),
                    reduce_only.to_string(),
                    !sp.reduce_only_required || *reduce_only,
                );
                let size = decimal::parse("size", total_size)?;
                let reference = match limit_price {
                    Some(px) => decimal::parse("price", px)?,
                    None => markets
                        .markets
                        .get(symbol)
                        .map(|m| m.mark_price)
                        .ok_or_else(|| format!("{symbol} not in /v2/markets"))?,
                };
                let notional: Decimal = mul(reference, size)?;
                r.push(
                    &format!("leg[{i}].notional_cap"),
                    format!("<= {cap}"),
                    notional.to_string(),
                    notional <= cap,
                );
            }
            StrikeLeg::GridBot { collateral, .. } => {
                // The policy caps the USD the plan commits; the leverage the
                // service snapshots for sizing is account-side, not in the plan.
                let committed = decimal::parse("collateral", collateral)?;
                r.push(
                    &format!("leg[{i}].notional_cap"),
                    format!("collateral <= {cap}"),
                    committed.to_string(),
                    committed <= cap,
                );
            }
            StrikeLeg::GridStop { .. }
            | StrikeLeg::CopyStop { .. }
            | StrikeLeg::CopySave { .. }
            | StrikeLeg::CopyUnsave { .. }
            | StrikeLeg::CopyPatch { .. }
            | StrikeLeg::RfqClose { .. }
            | StrikeLeg::RfqResponse { .. }
            | StrikeLeg::Withdraw { .. }
            | StrikeLeg::VaultWithdraw { .. }
            | StrikeLeg::VaultWithdrawCancel { .. }
            | StrikeLeg::VaultPause { .. }
            | StrikeLeg::VaultResume { .. }
            | StrikeLeg::TwapCancel { .. } => {}
            StrikeLeg::Rfq { symbol, requested_size, .. } => {
                let size = decimal::parse("requested_size", requested_size)?;
                let reference = markets
                    .markets
                    .get(symbol)
                    .map(|m| m.mark_price)
                    .ok_or_else(|| format!("{symbol} not in /v2/markets"))?;
                let notional = mul(reference, size)?;
                r.push(
                    &format!("leg[{i}].notional_cap"),
                    format!("<= {cap}"),
                    notional.to_string(),
                    notional <= cap,
                );
            }
            StrikeLeg::WithdrawQuote { usd_amount, .. } | StrikeLeg::VaultDeposit { usd_amount, .. } => {
                let n = decimal::parse("usd_amount", usd_amount)?;
                r.push(&format!("leg[{i}].notional_cap"), format!("<= {cap}"), n.to_string(), n <= cap);
            }
            StrikeLeg::VaultCreate { initial_deposit, .. } => {
                let n = decimal::parse("initial_deposit", initial_deposit)?;
                r.push(&format!("leg[{i}].notional_cap"), format!("<= {cap}"), n.to_string(), n <= cap);
            }
            StrikeLeg::CopySubscribe { copy_amount, margin_per_entry_order, funding_amount, .. } => {
                for (name, value) in [
                    ("copy_amount", copy_amount),
                    ("margin_per_entry_order", margin_per_entry_order),
                    ("funding_amount", funding_amount),
                ] {
                    if let Some(v) = value {
                        let n = decimal::parse(name, v)?;
                        r.push(
                            &format!("leg[{i}].notional_cap"),
                            format!("<= {cap}"),
                            n.to_string(),
                            n <= cap,
                        );
                    }
                }
            }
        }
    }
    Ok(r)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::plan::PlanOutput;

    fn pol() -> Policy {
        Policy {
            policy_id: "p1".into(),
            network: "preprod".into(),
            expires_at_unix_ms: 2_000,
            onchain: Some(OnchainPolicy {
                allowed_addresses: vec!["addr_test1qa".into()],
                max_lovelace_per_output: 5_000_000,
                max_lovelace_total: 6_000_000,
                max_fee_lovelace: 400_000,
                allow_native_assets: false,
            }),
            strike: None,
        }
    }

    fn plan(addr: &str, lovelace: u64) -> ProposedSend {
        ProposedSend {
            network: "preprod".into(),
            from_address: "addr_test1from".into(),
            outputs: vec![PlanOutput { address: addr.into(), lovelace, assets: vec![] }],
            max_fee_lovelace: 300_000,
        }
    }

    #[test]
    fn inside_and_outside() {
        assert!(check_onchain(&pol(), &plan("addr_test1qa", 2_000_000), 1_000).ok);
        assert_eq!(check_onchain(&pol(), &plan("addr_test1qb", 2_000_000), 1_000).failed_ids(), ["output[0].address_allowed"]);
        assert_eq!(check_onchain(&pol(), &plan("addr_test1qa", 5_000_001), 1_000).failed_ids(), ["output[0].lovelace_cap"]);
        assert_eq!(check_onchain(&pol(), &plan("addr_test1qa", 1), 2_000).failed_ids(), ["policy.not_expired"]);
        let mut no_section = pol();
        no_section.onchain = None;
        assert!(!check_onchain(&no_section, &plan("addr_test1qa", 1), 1_000).ok);
    }
}
