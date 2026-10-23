//! Stock Pallas decode of the exact CBOR that would be signed. Only the
//! Conway era is accepted (preprod is Conway); anything else is an error.

use pallas_codec::utils::Nullable;
use pallas_traverse::MultiEraTx;
use serde::Serialize;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DecodedAsset {
    pub policy_hex: String,
    pub name_hex: String,
    pub qty: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DecodedInput {
    pub tx_hash: String,
    pub index: u64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DecodedOutput {
    pub index: usize,
    pub address: String,
    pub address_hex: String,
    pub lovelace: u64,
    pub assets: Vec<DecodedAsset>,
    pub has_datum: bool,
    pub has_script_ref: bool,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DecodedTx {
    pub tx_id: String,
    pub era: String,
    pub inputs: Vec<DecodedInput>,
    pub outputs: Vec<DecodedOutput>,
    pub fee: u64,
    pub ttl: Option<u64>,
    pub validity_start: Option<u64>,
    pub network_id: Option<u8>,
    pub mint_policies: usize,
    pub certificates: usize,
    pub withdrawals: usize,
    pub has_script_data_hash: bool,
    pub collateral_inputs: usize,
    pub has_collateral_return: bool,
    pub total_collateral: Option<u64>,
    pub required_signers: usize,
    pub reference_inputs: usize,
    pub voting_procedures: usize,
    pub proposal_procedures: usize,
    pub treasury_value: Option<u64>,
    pub donation: Option<u64>,
    pub has_auxiliary_data_hash: bool,
    pub has_auxiliary_data: bool,
    pub vkey_witnesses: usize,
    pub bootstrap_witnesses: usize,
    pub script_witnesses: usize,
    pub has_redeemers: bool,
    pub has_plutus_data: bool,
    pub is_valid: bool,
}

pub fn decode_tx(cbor_hex: &str) -> Result<DecodedTx, String> {
    let bytes = hex::decode(cbor_hex.trim()).map_err(|e| format!("tx CBOR is not hex: {e}"))?;
    let tx = MultiEraTx::decode(&bytes).map_err(|e| format!("Pallas decode failed: {e}"))?;
    let conway = tx
        .as_conway()
        .ok_or_else(|| format!("tx era is {:?}, only Conway is accepted", tx.era()))?;
    let body = &conway.transaction_body;
    let wits = &conway.transaction_witness_set;

    let inputs = tx
        .inputs()
        .iter()
        .map(|i| DecodedInput { tx_hash: i.hash().to_string(), index: i.index() })
        .collect();

    let mut outputs = Vec::new();
    for (index, out) in tx.outputs().iter().enumerate() {
        let addr = out.address().map_err(|e| format!("output #{index} address: {e}"))?;
        let bech = addr.to_bech32().map_err(|e| format!("output #{index} bech32: {e}"))?;
        let mut assets = Vec::new();
        for policy_assets in out.value().assets() {
            for asset in policy_assets.assets() {
                let qty = asset
                    .output_coin()
                    .ok_or_else(|| format!("output #{index}: asset without output quantity"))?;
                assets.push(DecodedAsset {
                    policy_hex: asset.policy().to_string(),
                    name_hex: hex::encode(asset.name()),
                    qty,
                });
            }
        }
        outputs.push(DecodedOutput {
            index,
            address: bech,
            address_hex: hex::encode(addr.to_vec()),
            lovelace: out.value().coin(),
            assets,
            has_datum: out.datum().is_some(),
            has_script_ref: out.script_ref().is_some(),
        });
    }

    let script_witnesses = wits.native_script.as_ref().map_or(0, |s| s.len())
        + wits.plutus_v1_script.as_ref().map_or(0, |s| s.len())
        + wits.plutus_v2_script.as_ref().map_or(0, |s| s.len())
        + wits.plutus_v3_script.as_ref().map_or(0, |s| s.len());

    Ok(DecodedTx {
        tx_id: tx.hash().to_string(),
        era: "conway".to_string(),
        inputs,
        outputs,
        fee: body.fee,
        ttl: body.ttl,
        validity_start: body.validity_interval_start,
        // Stock pallas conversion (the CBOR wire value of the enum).
        network_id: body.network_id.as_ref().map(|n| u8::from(n.clone())),
        mint_policies: body.mint.as_ref().map_or(0, |m| m.len()),
        certificates: body.certificates.as_ref().map_or(0, |c| c.len()),
        withdrawals: body.withdrawals.as_ref().map_or(0, |w| w.len()),
        has_script_data_hash: body.script_data_hash.is_some(),
        collateral_inputs: body.collateral.as_ref().map_or(0, |c| c.len()),
        has_collateral_return: body.collateral_return.is_some(),
        total_collateral: body.total_collateral,
        required_signers: body.required_signers.as_ref().map_or(0, |s| s.len()),
        reference_inputs: body.reference_inputs.as_ref().map_or(0, |s| s.len()),
        voting_procedures: body.voting_procedures.as_ref().map_or(0, |v| v.len()),
        proposal_procedures: body.proposal_procedures.as_ref().map_or(0, |p| p.len()),
        treasury_value: body.treasury_value,
        donation: body.donation.as_ref().map(|d| u64::from(d)),
        has_auxiliary_data_hash: body.auxiliary_data_hash.is_some(),
        has_auxiliary_data: matches!(conway.auxiliary_data, Nullable::Some(_)),
        vkey_witnesses: wits.vkeywitness.as_ref().map_or(0, |v| v.len()),
        bootstrap_witnesses: wits.bootstrap_witness.as_ref().map_or(0, |b| b.len()),
        script_witnesses,
        has_redeemers: wits.redeemer.is_some(),
        has_plutus_data: wits.plutus_data.is_some(),
        is_valid: conway.success,
    })
}
