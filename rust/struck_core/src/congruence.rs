//! N5c: the decoded transaction against the APPROVED plan. Every check is
//! exact (byte-for-byte addresses, integer amounts). Any failed check makes
//! the report `ok = false`; the graph then refuses to request a signature.

use std::collections::BTreeSet;

use pallas_addresses::Address;

use crate::decode::{DecodedOutput, DecodedTx};
use crate::money::{AssetBag, AssetId, Lovelace};
use crate::plan::{bag_of, NetParams, OnchainPlan, PlanOutput, Utxo};
use crate::report::Report;

fn output_bag(out: &DecodedOutput) -> Result<AssetBag, String> {
    let mut bag = AssetBag::default();
    for a in &out.assets {
        bag.add(AssetId::new(&a.policy_hex, &a.name_hex)?, a.qty)?;
    }
    Ok(bag)
}

fn address_bytes(addr: &str) -> Result<Vec<u8>, String> {
    Address::from_bech32(addr)
        .map(|a| a.to_vec())
        .map_err(|e| format!("address {addr:?}: {e}"))
}

fn matches_plan_output(out: &DecodedOutput, want: &PlanOutput) -> Result<bool, String> {
    let want_bytes = address_bytes(&want.address)?;
    let got_bytes = hex::decode(&out.address_hex).map_err(|e| e.to_string())?;
    Ok(got_bytes == want_bytes
        && out.lovelace == want.lovelace
        && output_bag(out)? == bag_of(&want.assets)?
        && !out.has_datum
        && !out.has_script_ref)
}

pub fn check(tx: &DecodedTx, plan: &OnchainPlan, resolved: &[Utxo], net: &NetParams) -> Result<Report, String> {
    let mut r = Report::new();
    let from_bytes = address_bytes(&plan.from_address)?;

    r.push("era", "conway".into(), tx.era.clone(), tx.era == "conway");
    // The selected network, passed in: the plan must name it and the body's
    // network_id (when present) must be its id. A mismatch fails loudly.
    r.push("plan.network", net.name.clone(), plan.network.clone(), plan.network == net.name);
    r.push(
        "network_id",
        format!("absent or {} ({})", net.network_id, net.name),
        format!("{:?}", tx.network_id),
        tx.network_id.is_none() || tx.network_id == Some(net.network_id),
    );

    // Inputs: every input is resolved from the chain index and owned by from_address.
    let mut seen = BTreeSet::new();
    let mut in_lovelace = Lovelace(0);
    let mut in_assets = AssetBag::default();
    for input in &tx.inputs {
        let reference = format!("{}#{}", input.tx_hash, input.index);
        let id = format!("inputs.{reference}");
        if !seen.insert(reference.clone()) {
            r.push(&id, "unique".into(), "duplicate".into(), false);
            continue;
        }
        match resolved.iter().find(|u| u.reference() == reference) {
            None => r.push(&id, "resolved from chain index".into(), "UNRESOLVED".into(), false),
            Some(u) => {
                let owned = address_bytes(&u.address)? == from_bytes;
                r.push(
                    &id,
                    format!("owned by {}", plan.from_address),
                    format!("{} holding {} + {}", u.address, Lovelace(u.lovelace), bag_of(&u.assets)?.describe()),
                    owned,
                );
                in_lovelace = in_lovelace.checked_add(Lovelace(u.lovelace))?;
                in_assets.merge(&bag_of(&u.assets)?)?;
            }
        }
    }
    r.push(
        "inputs.non_empty",
        ">= 1".into(),
        tx.inputs.len().to_string(),
        !tx.inputs.is_empty(),
    );

    // Plan outputs: each matched exactly once, byte-for-byte.
    let mut used = vec![false; tx.outputs.len()];
    for (i, want) in plan.outputs.iter().enumerate() {
        let mut found = None;
        for (k, out) in tx.outputs.iter().enumerate() {
            if !used[k] && matches_plan_output(out, want)? {
                found = Some(k);
                break;
            }
        }
        if let Some(k) = found {
            used[k] = true;
        }
        r.push(
            &format!("plan.outputs[{i}]"),
            format!("{} receives {} + {}", want.address, Lovelace(want.lovelace), bag_of(&want.assets)?.describe()),
            match found {
                Some(k) => format!("tx output #{k} exact match"),
                None => "NO exact matching output".into(),
            },
            found.is_some(),
        );
    }

    // Every other output is change back to from_address, with no datum/script.
    for (k, out) in tx.outputs.iter().enumerate() {
        if used[k] {
            continue;
        }
        let got = hex::decode(&out.address_hex).map_err(|e| e.to_string())?;
        let is_change = got == from_bytes && !out.has_datum && !out.has_script_ref;
        r.push(
            &format!("extra_output[{k}].is_change"),
            format!("change to {}", plan.from_address),
            format!("{} {} + {}", out.address, Lovelace(out.lovelace), output_bag(out)?.describe()),
            is_change,
        );
    }

    // Value conservation (no deposits/withdrawals are allowed, checked below).
    let out_lovelace = Lovelace::sum(tx.outputs.iter().map(|o| Lovelace(o.lovelace)))?;
    let out_plus_fee = out_lovelace.checked_add(Lovelace(tx.fee))?;
    r.push(
        "balance.lovelace",
        format!("inputs {in_lovelace}"),
        format!("outputs {out_lovelace} + fee {}", Lovelace(tx.fee)),
        in_lovelace == out_plus_fee,
    );
    let mut out_assets = AssetBag::default();
    for o in &tx.outputs {
        out_assets.merge(&output_bag(o)?)?;
    }
    r.push(
        "balance.assets",
        in_assets.describe(),
        out_assets.describe(),
        in_assets == out_assets,
    );

    r.push(
        "fee.bound",
        format!("<= {}", Lovelace(plan.max_fee_lovelace)),
        Lovelace(tx.fee).to_string(),
        tx.fee <= plan.max_fee_lovelace,
    );
    r.push(
        "validity.ttl",
        format!("present and <= slot {}", plan.valid_until_slot),
        format!("{:?}", tx.ttl),
        matches!(tx.ttl, Some(t) if t <= plan.valid_until_slot),
    );
    r.push(
        "validity.start",
        "absent or <= ttl".into(),
        format!("{:?}", tx.validity_start),
        match (tx.validity_start, tx.ttl) {
            (None, _) => true,
            (Some(s), Some(t)) => s <= t,
            (Some(_), None) => false,
        },
    );

    push_plain_tx_limits(&mut r, tx);
    Ok(r)
}

/// ADA deposit built by Strike (strike-deposit skill, Cardano `cardano_cbor`).
///
/// The exchanger address is created at build-tx and is not knowable before
/// that. It is the one documented unverifiable payee: exactly one output that
/// is not the user's address, no datum, no script, no tokens, and lovelace
/// equal to `deposit_lovelace`. Every other output is change to the user.
/// The address itself is not compared to a directory.
pub fn check_ada_deposit(
    tx: &DecodedTx,
    user_address: &str,
    deposit_lovelace: u64,
    max_fee_lovelace: u64,
    valid_until_slot: u64,
    resolved: &[Utxo],
    net: &NetParams,
) -> Result<Report, String> {
    let mut r = Report::new();
    let from_bytes = address_bytes(user_address)?;

    r.push("era", "conway".into(), tx.era.clone(), tx.era == "conway");
    r.push(
        "network_id",
        format!("absent or {} ({})", net.network_id, net.name),
        format!("{:?}", tx.network_id),
        tx.network_id.is_none() || tx.network_id == Some(net.network_id),
    );

    let mut seen = BTreeSet::new();
    let mut in_lovelace = Lovelace(0);
    let mut in_assets = AssetBag::default();
    for input in &tx.inputs {
        let reference = format!("{}#{}", input.tx_hash, input.index);
        let id = format!("inputs.{reference}");
        if !seen.insert(reference.clone()) {
            r.push(&id, "unique".into(), "duplicate".into(), false);
            continue;
        }
        match resolved.iter().find(|u| u.reference() == reference) {
            None => r.push(&id, "resolved from chain index".into(), "UNRESOLVED".into(), false),
            Some(u) => {
                let owned = address_bytes(&u.address)? == from_bytes;
                r.push(
                    &id,
                    format!("owned by {user_address}"),
                    format!("{} holding {}", u.address, Lovelace(u.lovelace)),
                    owned,
                );
                in_lovelace = in_lovelace.checked_add(Lovelace(u.lovelace))?;
                in_assets.merge(&bag_of(&u.assets)?)?;
            }
        }
    }
    r.push("inputs.non_empty", ">= 1".into(), tx.inputs.len().to_string(), !tx.inputs.is_empty());

    let mut foreign: Vec<usize> = Vec::new();
    for (k, out) in tx.outputs.iter().enumerate() {
        let got = hex::decode(&out.address_hex).map_err(|e| e.to_string())?;
        if got == from_bytes {
            let is_change = !out.has_datum && !out.has_script_ref;
            r.push(
                &format!("output[{k}].change"),
                format!("change to {user_address}"),
                format!("{} {}", out.address, Lovelace(out.lovelace)),
                is_change,
            );
        } else {
            foreign.push(k);
        }
    }
    let one = foreign.len() == 1;
    r.push(
        "exchanger.count",
        "exactly one output that is not the user's address".into(),
        foreign.len().to_string(),
        one,
    );
    if let Some(&k) = foreign.first() {
        if foreign.len() == 1 {
            let out = &tx.outputs[k];
            let plain = !out.has_datum && !out.has_script_ref && out.assets.is_empty();
            let amount_ok = out.lovelace == deposit_lovelace;
            r.push(
                "exchanger.payee",
                "unverifiable Strike exchanger (documented limit); amount must match".into(),
                format!("{} {}", out.address, Lovelace(out.lovelace)),
                plain && amount_ok,
            );
        }
    }

    let out_lovelace = Lovelace::sum(tx.outputs.iter().map(|o| Lovelace(o.lovelace)))?;
    let out_plus_fee = out_lovelace.checked_add(Lovelace(tx.fee))?;
    r.push(
        "balance.lovelace",
        format!("inputs {in_lovelace}"),
        format!("outputs {out_lovelace} + fee {}", Lovelace(tx.fee)),
        in_lovelace == out_plus_fee,
    );
    let mut out_assets = AssetBag::default();
    for o in &tx.outputs {
        out_assets.merge(&output_bag(o)?)?;
    }
    r.push(
        "balance.assets",
        in_assets.describe(),
        out_assets.describe(),
        in_assets == out_assets,
    );
    r.push(
        "fee.bound",
        format!("<= {}", Lovelace(max_fee_lovelace)),
        Lovelace(tx.fee).to_string(),
        tx.fee <= max_fee_lovelace,
    );
    r.push(
        "validity.ttl",
        format!("present and <= slot {valid_until_slot}"),
        format!("{:?}", tx.ttl),
        matches!(tx.ttl, Some(t) if t <= valid_until_slot),
    );
    push_plain_tx_limits(&mut r, tx);
    Ok(r)
}

fn push_plain_tx_limits(r: &mut Report, tx: &DecodedTx) {
    let forbidden: [(&str, bool); 15] = [
        ("mint", tx.mint_policies > 0),
        ("certificates", tx.certificates > 0),
        ("withdrawals", tx.withdrawals > 0),
        ("script_data_hash", tx.has_script_data_hash),
        ("collateral", tx.collateral_inputs > 0),
        ("collateral_return", tx.has_collateral_return),
        ("total_collateral", tx.total_collateral.is_some()),
        ("required_signers", tx.required_signers > 0),
        ("reference_inputs", tx.reference_inputs > 0),
        ("voting_procedures", tx.voting_procedures > 0),
        ("proposal_procedures", tx.proposal_procedures > 0),
        ("treasury_value", tx.treasury_value.is_some()),
        ("donation", tx.donation.is_some()),
        ("scripts_or_redeemers", tx.script_witnesses > 0 || tx.has_redeemers || tx.has_plutus_data),
        ("auxiliary_data", tx.has_auxiliary_data || tx.has_auxiliary_data_hash),
    ];
    for (name, present) in forbidden {
        r.push(
            &format!("undeclared.{name}"),
            "absent (plan declares none)".into(),
            if present { "PRESENT".into() } else { "absent".into() },
            !present,
        );
    }
    r.push(
        "unsigned",
        "0 witnesses (signature not yet requested)".into(),
        format!("{} vkey + {} bootstrap", tx.vkey_witnesses, tx.bootstrap_witnesses),
        tx.vkey_witnesses == 0 && tx.bootstrap_witnesses == 0,
    );
    r.push("is_valid", "true".into(), tx.is_valid.to_string(), tx.is_valid);
}

#[cfg(test)]
mod deposit_tests {
    use super::check_ada_deposit;
    use crate::decode::decode_tx;
    use crate::plan::{NetParams, Utxo};
    use serde::Deserialize;
    use std::fs;
    use std::path::PathBuf;

    #[derive(Deserialize)]
    struct Unsigned {
        #[serde(rename = "cborHex")]
        cbor_hex: String,
    }

    fn fixture() -> (String, String, Vec<Utxo>, NetParams) {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../receipts/20261021T165319Z-send-ae0d1b7a4d32");
        let unsigned: Unsigned =
            serde_json::from_str(&fs::read_to_string(dir.join("unsigned.tx.json")).unwrap()).unwrap();
        let plan: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(dir.join("plan.approved.json")).unwrap()).unwrap();
        let utxos: Vec<Utxo> =
            serde_json::from_str(&fs::read_to_string(dir.join("wallet.utxos.json")).unwrap()).unwrap();
        let net = NetParams::parse(
            r#"{"name":"preprod","label":"preprod (Cardano preprod + Strike TESTNET)","strike_label":"Strike TESTNET","network_id":0,"address_hrp":"addr_test"}"#,
        )
        .unwrap();
        (unsigned.cbor_hex, plan["from_address"].as_str().unwrap().to_string(), utxos, net)
    }

    #[test]
    fn one_foreign_ada_output_of_the_deposit_amount_is_the_unverifiable_payee() {
        let (cbor, user, utxos, net) = fixture();
        let tx = decode_tx(&cbor).unwrap();
        let ok = check_ada_deposit(&tx, &user, 2_000_000, 300_000, 136925572, &utxos, &net).unwrap();
        assert!(ok.ok, "{:?}", ok.failed_ids());
        let bad = check_ada_deposit(&tx, &user, 2_000_001, 300_000, 136925572, &utxos, &net).unwrap();
        assert!(!bad.ok);
        assert!(bad.failed_ids().iter().any(|id| id == "exchanger.payee"));
    }
}
