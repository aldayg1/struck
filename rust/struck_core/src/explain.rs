//! N6c: plain words for exactly what the transaction spends and how. Every
//! line is derived from the Pallas decode plus chain-resolved input values;
//! nothing comes from builder-supplied summaries.

use serde::Serialize;

use crate::congruence::{check, check_ada_deposit};
use crate::decode::DecodedTx;
use crate::money::{AssetBag, AssetId, Lovelace};
use crate::plan::{bag_of, same_address, NetParams, OnchainPlan, Utxo};
use crate::report::Report;

#[derive(Debug, Clone, Serialize)]
pub struct Explanation {
    pub tx_id: String,
    pub lines: Vec<String>,
    pub spends_lovelace: u64,
    pub pays_lovelace: u64,
    pub change_lovelace: u64,
    pub fee_lovelace: u64,
    pub congruence: Report,
}

/// Plain words for an ADA deposit whose payee is the Strike exchanger.
/// The exchanger address is reported and not matched to a known directory.
pub fn explain_ada_deposit(
    tx: &DecodedTx,
    user_address: &str,
    deposit_lovelace: u64,
    max_fee_lovelace: u64,
    valid_until_slot: u64,
    resolved: &[Utxo],
    ttl_wallclock: &str,
    net: &NetParams,
) -> Result<Explanation, String> {
    let report = check_ada_deposit(
        tx,
        user_address,
        deposit_lovelace,
        max_fee_lovelace,
        valid_until_slot,
        resolved,
        net,
    )?;
    let mut lines = Vec::new();
    lines.push(format!(
        "SIGNATURE REQUEST (awaiting signature): deposit transaction {} ({})",
        tx.tx_id, net.label
    ));
    let mut spent = Lovelace(0);
    lines.push(format!("It SPENDS {} input(s) from your wallet {}:", tx.inputs.len(), user_address));
    for input in &tx.inputs {
        let reference = format!("{}#{}", input.tx_hash, input.index);
        let u = resolved
            .iter()
            .find(|u| u.reference() == reference)
            .ok_or_else(|| format!("input {reference} is not resolved from the chain index"))?;
        lines.push(format!("  - {reference}: {}", Lovelace(u.lovelace)));
        spent = spent.checked_add(Lovelace(u.lovelace))?;
    }
    lines.push(format!("  Total spent: {spent}."));
    let mut pays = Lovelace(0);
    let mut change = Lovelace(0);
    lines.push(format!("It CREATES {} output(s):", tx.outputs.len()));
    for out in &tx.outputs {
        let is_change = same_address(&out.address, user_address)?;
        let role = if is_change {
            "CHANGE back to you"
        } else {
            "DEPOSIT to the Strike exchanger (payee unverifiable by Struck, the documented limit)"
        };
        lines.push(format!(
            "  - #{} {role}: {} -> {}",
            out.index,
            Lovelace(out.lovelace),
            out.address
        ));
        if is_change {
            change = change.checked_add(Lovelace(out.lovelace))?;
        } else {
            pays = pays.checked_add(Lovelace(out.lovelace))?;
        }
    }
    lines.push(format!(
        "Network fee: {} (approved bound {}).",
        Lovelace(tx.fee),
        Lovelace(max_fee_lovelace)
    ));
    match tx.ttl {
        Some(ttl) => lines.push(format!("Valid until slot {ttl} ({ttl_wallclock}).")),
        None => lines.push("NO validity deadline (TTL absent).".to_string()),
    }
    if report.ok {
        lines.push(format!(
            "Deposit congruence: {}/{} checks passed. Nothing is signed.",
            report.checks.len(),
            report.checks.len()
        ));
    } else {
        lines.push(format!(
            "CONGRUENCE FAILED ({}): {}. Do NOT sign.",
            report.failed_ids().len(),
            report.failed_ids().join(", ")
        ));
    }
    Ok(Explanation {
        tx_id: tx.tx_id.clone(),
        lines,
        spends_lovelace: spent.0,
        pays_lovelace: pays.0,
        change_lovelace: change.0,
        fee_lovelace: tx.fee,
        congruence: report,
    })
}

pub fn explain(
    tx: &DecodedTx,
    plan: &OnchainPlan,
    resolved: &[Utxo],
    ttl_wallclock: &str,
    net: &NetParams,
) -> Result<Explanation, String> {
    let report = check(tx, plan, resolved, net)?;
    let mut lines = Vec::new();
    let witnesses = tx.vkey_witnesses + tx.bootstrap_witnesses;
    lines.push(format!(
        "SIGNATURE REQUEST (awaiting signature): transaction {} (Conway era, {}), \
         {witnesses} witness(es) so far.",
        tx.tx_id,
        net.label
    ));

    let mut spent = Lovelace(0);
    let mut spent_assets = AssetBag::default();
    lines.push(format!("It SPENDS {} input(s) from your wallet {}:", tx.inputs.len(), plan.from_address));
    for input in &tx.inputs {
        let reference = format!("{}#{}", input.tx_hash, input.index);
        let u = resolved
            .iter()
            .find(|u| u.reference() == reference)
            .ok_or_else(|| format!("input {reference} is not resolved from the chain index"))?;
        let bag = bag_of(&u.assets)?;
        lines.push(format!("  - {reference}: {} + {}", Lovelace(u.lovelace), bag.describe()));
        spent = spent.checked_add(Lovelace(u.lovelace))?;
        spent_assets.merge(&bag)?;
    }
    lines.push(format!("  Total spent: {spent} + {}.", spent_assets.describe()));

    let mut pays = Lovelace(0);
    let mut change = Lovelace(0);
    lines.push(format!("It CREATES {} output(s):", tx.outputs.len()));
    for out in &tx.outputs {
        let mut bag = AssetBag::default();
        for a in &out.assets {
            bag.add(AssetId::new(&a.policy_hex, &a.name_hex)?, a.qty)?;
        }
        let is_change = same_address(&out.address, &plan.from_address)?
            && !plan.outputs.iter().any(|p| p.address == out.address);
        let role = if is_change { "CHANGE back to you" } else { "PAYMENT" };
        lines.push(format!(
            "  - #{} {role}: {} + {} -> {}",
            out.index,
            Lovelace(out.lovelace),
            bag.describe(),
            out.address
        ));
        if is_change {
            change = change.checked_add(Lovelace(out.lovelace))?;
        } else {
            pays = pays.checked_add(Lovelace(out.lovelace))?;
        }
    }
    lines.push(format!(
        "Network fee: {} (approved bound {}).",
        Lovelace(tx.fee),
        Lovelace(plan.max_fee_lovelace)
    ));
    match tx.ttl {
        Some(ttl) => lines.push(format!("Valid until slot {ttl} ({ttl_wallclock}); after that the network rejects it.")),
        None => lines.push("NO validity deadline (TTL absent).".to_string()),
    }
    let present: Vec<String> = report
        .checks
        .iter()
        .filter(|c| c.id.starts_with("undeclared.") && !c.ok)
        .map(|c| c.id.trim_start_matches("undeclared.").to_string())
        .collect();
    if present.is_empty() {
        lines.push(
            "Nothing else: no minting, no certificates, no withdrawals, no scripts, no datums, no metadata, no governance actions."
                .to_string(),
        );
    } else {
        lines.push(format!("ALSO PRESENT (not in the plan): {}.", present.join(", ")));
    }
    let failed = report.failed_ids();
    if report.ok {
        lines.push(format!(
            "Congruence with the approved plan: {}/{} deterministic checks passed.",
            report.checks.len(),
            report.checks.len()
        ));
    } else {
        lines.push(format!(
            "CONGRUENCE FAILED ({} of {} checks): {}. Do NOT sign.",
            failed.len(),
            report.checks.len(),
            failed.join(", ")
        ));
    }
    Ok(Explanation {
        tx_id: tx.tx_id.clone(),
        lines,
        spends_lovelace: spent.0,
        pays_lovelace: pays.0,
        change_lovelace: change.0,
        fee_lovelace: tx.fee,
        congruence: report,
    })
}
