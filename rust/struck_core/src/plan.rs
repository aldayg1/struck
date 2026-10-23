//! On-chain plan (the APPROVED shape Python's Pydantic model serializes) and
//! the deterministic N2 plan check. Unknown JSON fields are rejected.

use std::collections::BTreeMap;

use pallas_addresses::{Address, Network};
use serde::{Deserialize, Serialize};

use crate::coinselect::{self, Candidate};
use crate::money::{AssetBag, AssetId, Lovelace};
use crate::report::Report;

/// The ONE selected network, as Python reads it from the per-network table
/// `struck.network.PROFILES` (`NetworkProfile.rust_json`). Rust holds no network
/// literal: name, ids, HRP and labels all arrive here.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct NetParams {
    pub name: String,
    /// Tx body `network_id` == the address header network (CIP-19).
    pub network_id: u8,
    pub address_hrp: String,
    pub label: String,
    pub strike_label: String,
}

impl NetParams {
    pub fn parse(json: &str) -> Result<Self, String> {
        parse_json("network", json)
    }
    pub fn address_network(&self) -> Network {
        Network::from(self.network_id)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PlanAsset {
    pub policy_hex: String,
    pub name_hex: String,
    pub qty: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct PlanOutput {
    pub address: String,
    pub lovelace: u64,
    pub assets: Vec<PlanAsset>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OnchainPlan {
    pub network: String,
    pub from_address: String,
    pub outputs: Vec<PlanOutput>,
    pub max_fee_lovelace: u64,
    pub valid_until_slot: u64,
}

/// One resolved UTxO (values come from the chain index, never from the CBOR).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Utxo {
    pub tx_hash: String,
    pub index: u64,
    pub address: String,
    pub lovelace: u64,
    pub assets: Vec<PlanAsset>,
}

impl Utxo {
    pub fn reference(&self) -> String {
        format!("{}#{}", self.tx_hash.to_ascii_lowercase(), self.index)
    }
}

pub fn parse_json<T: for<'de> Deserialize<'de>>(what: &str, json: &str) -> Result<T, String> {
    serde_json::from_str(json).map_err(|e| format!("{what}: invalid JSON shape: {e}"))
}

pub fn bag_of(assets: &[PlanAsset]) -> Result<AssetBag, String> {
    let mut bag = AssetBag::default();
    for asset in assets {
        bag.add(AssetId::new(&asset.policy_hex, &asset.name_hex)?, asset.qty)?;
    }
    Ok(bag)
}

/// Parse a bech32 address and require the SELECTED network's address network.
pub fn network_address(addr: &str, net: &NetParams) -> Result<Address, String> {
    let parsed =
        Address::from_bech32(addr).map_err(|e| format!("address {addr:?} does not parse: {e}"))?;
    let hrp = parsed.hrp().map_err(|e| format!("address {addr:?}: {e}"))?;
    match parsed.network() {
        Some(n) if n == net.address_network() && hrp == net.address_hrp => Ok(parsed),
        other => Err(format!(
            "address {addr:?} is not a {} address (network {other:?}, hrp {hrp:?})",
            net.name
        )),
    }
}

pub fn same_address(a: &str, b: &str) -> Result<bool, String> {
    let left = Address::from_bech32(a).map_err(|e| format!("address {a:?}: {e}"))?;
    let right = Address::from_bech32(b).map_err(|e| format!("address {b:?}: {e}"))?;
    Ok(left.to_vec() == right.to_vec())
}

pub fn plan_totals(plan: &OnchainPlan) -> Result<(Lovelace, AssetBag), String> {
    let lovelace = Lovelace::sum(plan.outputs.iter().map(|o| Lovelace(o.lovelace)))?;
    let mut bag = AssetBag::default();
    for output in &plan.outputs {
        bag.merge(&bag_of(&output.assets)?)?;
    }
    Ok((lovelace, bag))
}

/// N2: deterministic plan check against the wallet's live UTxO set.
pub fn validate_plan(plan: &OnchainPlan, utxos: &[Utxo], current_slot: u64, net: &NetParams) -> Report {
    let mut r = Report::new();
    let want = net.name.as_str();
    r.push("network", want.to_string(), plan.network.clone(), plan.network == want);
    match network_address(&plan.from_address, net) {
        Ok(addr) => r.push(
            "from_address.key_payment",
            format!("{want} key-payment address"),
            if addr.has_script() { "script address".to_string() } else { "key address".to_string() },
            !addr.has_script(),
        ),
        Err(e) => r.push("from_address.parse", format!("{want} address"), e, false),
    }
    r.push(
        "outputs.non_empty",
        ">= 1 output".to_string(),
        plan.outputs.len().to_string(),
        !plan.outputs.is_empty(),
    );
    for (i, output) in plan.outputs.iter().enumerate() {
        match network_address(&output.address, net) {
            Ok(_) => r.push(&format!("outputs[{i}].address"), format!("{want} address"), output.address.clone(), true),
            Err(e) => r.push(&format!("outputs[{i}].address"), format!("{want} address"), e, false),
        }
        r.push(
            &format!("outputs[{i}].lovelace"),
            "> 0".to_string(),
            output.lovelace.to_string(),
            output.lovelace > 0,
        );
        for (j, asset) in output.assets.iter().enumerate() {
            let id_ok = AssetId::new(&asset.policy_hex, &asset.name_hex);
            r.push(
                &format!("outputs[{i}].assets[{j}]"),
                "valid asset id, qty > 0".to_string(),
                match &id_ok {
                    Ok(id) => format!("{} x {id}", asset.qty),
                    Err(e) => e.clone(),
                },
                id_ok.is_ok() && asset.qty > 0,
            );
        }
    }
    r.push(
        "max_fee_lovelace",
        "> 0".to_string(),
        plan.max_fee_lovelace.to_string(),
        plan.max_fee_lovelace > 0,
    );
    r.push(
        "valid_until_slot",
        format!("> current slot {current_slot}"),
        plan.valid_until_slot.to_string(),
        plan.valid_until_slot > current_slot,
    );

    let mut foreign = Vec::new();
    for u in utxos {
        match same_address(&u.address, &plan.from_address) {
            Ok(true) => {}
            Ok(false) => foreign.push(u.reference()),
            Err(e) => foreign.push(format!("{}: {e}", u.reference())),
        }
    }
    r.push(
        "utxos.owned_by_from_address",
        "every UTxO at from_address".to_string(),
        if foreign.is_empty() { "all owned".to_string() } else { foreign.join(", ") },
        foreign.is_empty(),
    );

    match (plan_totals(plan), available(utxos)) {
        (Ok((need_lovelace, need_assets)), Ok((have_lovelace, have_assets))) => {
            match need_lovelace.checked_add(Lovelace(plan.max_fee_lovelace)) {
                Ok(need_with_fee) => r.push(
                    "funds.lovelace",
                    format!(">= {need_with_fee} (outputs {need_lovelace} + fee bound)"),
                    have_lovelace.to_string(),
                    have_lovelace >= need_with_fee,
                ),
                Err(e) => r.push("funds.lovelace", "no overflow".to_string(), e, false),
            }
            let mut short = Vec::new();
            for (id, qty) in &need_assets.0 {
                let have = have_assets.0.get(id).copied().unwrap_or(0);
                if have < *qty {
                    short.push(format!("{id}: need {qty}, have {have}"));
                }
            }
            r.push(
                "funds.assets",
                need_assets.describe(),
                if short.is_empty() { "covered".to_string() } else { short.join("; ") },
                short.is_empty(),
            );
        }
        (Err(e), _) | (_, Err(e)) => r.push("funds", "computable totals".to_string(), e, false),
    }
    r
}

pub fn available(utxos: &[Utxo]) -> Result<(Lovelace, AssetBag), String> {
    let lovelace = Lovelace::sum(utxos.iter().map(|u| Lovelace(u.lovelace)))?;
    let mut bag = AssetBag::default();
    for u in utxos {
        bag.merge(&bag_of(&u.assets)?)?;
    }
    Ok((lovelace, bag))
}

/// Input selection for the plan through the PORTED 
/// Fails loudly when the selection cannot cover the plan.
pub fn select_for_plan(plan: &OnchainPlan, utxos: &[Utxo]) -> Result<Vec<String>, String> {
    let (need_lovelace, need_assets) = plan_totals(plan)?;
    let mut required: BTreeMap<String, BTreeMap<String, u64>> = BTreeMap::new();
    for (id, qty) in &need_assets.0 {
        required.entry(id.policy_hex.clone()).or_default().insert(id.name_hex.clone(), *qty);
    }
    let mut candidates = Vec::with_capacity(utxos.len());
    for u in utxos {
        let mut assets: BTreeMap<String, BTreeMap<String, u64>> = BTreeMap::new();
        for a in &u.assets {
            let id = AssetId::new(&a.policy_hex, &a.name_hex)?;
            assets.entry(id.policy_hex).or_default().insert(id.name_hex, a.qty);
        }
        candidates.push(Candidate { reference: u.reference(), lovelace: u.lovelace, assets });
    }
    let selection =
        coinselect::select_inputs(&candidates, need_lovelace.0, plan.max_fee_lovelace, &required);
    if selection.short_lovelace || !selection.missing_assets.is_empty() {
        return Err(format!(
            "coin selection cannot cover the plan: short_lovelace={} missing_assets={:?}",
            selection.short_lovelace, selection.missing_assets
        ));
    }
    Ok(selection.references)
}
