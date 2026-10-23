//! N7c verification of a wallet-signed tx before anything is submitted.
//!
//! The wallet returns a witness set and the tx is reassembled outside Rust,
//! so the result is checked here against the APPROVED unsigned tx:
//! * the tx id (the hash of the body bytes) is unchanged, which proves the
//!   body was not touched;
//! * there is exactly one vkey witness and no other kind of witness;
//! * blake2b-224(vkey) is the payment key hash of `from_address`;
//! * the ed25519 signature verifies over the tx id.

use pallas_addresses::{Address, ShelleyPaymentPart};
use pallas_crypto::hash::Hasher;
use pallas_crypto::key::ed25519::{PublicKey, Signature};
use pallas_traverse::MultiEraTx;

use serde::Serialize;

use crate::report::Report;

fn payment_key_hash(addr: &str) -> Result<[u8; 28], String> {
    let parsed =
        Address::from_bech32(addr).map_err(|e| format!("address {addr:?} does not parse: {e}"))?;
    match parsed {
        Address::Shelley(sh) => match sh.payment() {
            ShelleyPaymentPart::Key(hash) => {
                let mut out = [0u8; 28];
                out.copy_from_slice(hash.as_ref());
                Ok(out)
            }
            ShelleyPaymentPart::Script(_) => Err(format!("{addr} has a script payment part")),
        },
        _ => Err(format!("{addr} is not a Shelley address")),
    }
}

pub fn verify_signed(signed_hex: &str, approved_tx_id: &str, from_address: &str) -> Report {
    let mut r = Report::new();
    let bytes = match hex::decode(signed_hex.trim()) {
        Ok(b) => b,
        Err(e) => {
            r.push("signed.cbor", "hex CBOR".to_string(), format!("not hex: {e}"), false);
            return r;
        }
    };
    let tx = match MultiEraTx::decode(&bytes) {
        Ok(t) => t,
        Err(e) => {
            r.push("signed.cbor", "Pallas-decodable tx".to_string(), format!("{e}"), false);
            return r;
        }
    };
    let Some(conway) = tx.as_conway() else {
        r.push("signed.era", "Conway".to_string(), format!("{:?}", tx.era()), false);
        return r;
    };
    let tx_id = tx.hash().to_string();
    r.push("signed.tx_id", approved_tx_id.to_string(), tx_id.clone(), tx_id == approved_tx_id);

    let wits = &conway.transaction_witness_set;
    let vkeys: Vec<_> = wits.vkeywitness.iter().flat_map(|s| s.iter()).collect();
    r.push("signed.vkey_witnesses", "1".to_string(), vkeys.len().to_string(), vkeys.len() == 1);
    let others = wits.bootstrap_witness.iter().map(|s| s.len()).sum::<usize>()
        + wits.native_script.iter().map(|s| s.len()).sum::<usize>()
        + wits.plutus_v1_script.iter().map(|s| s.len()).sum::<usize>()
        + wits.plutus_v2_script.iter().map(|s| s.len()).sum::<usize>()
        + wits.plutus_v3_script.iter().map(|s| s.len()).sum::<usize>()
        + wits.plutus_data.iter().map(|s| s.len()).sum::<usize>()
        + usize::from(wits.redeemer.is_some());
    r.push("signed.other_witnesses", "0".to_string(), others.to_string(), others == 0);

    let Some(wit) = vkeys.first() else {
        return r;
    };
    match payment_key_hash(from_address) {
        Ok(want) => {
            let got = Hasher::<224>::hash(wit.vkey.as_ref());
            r.push(
                "signed.key_hash",
                hex::encode(want),
                got.to_string(),
                got.as_ref() == want.as_slice(),
            );
        }
        Err(e) => r.push("signed.key_hash", "key-payment from_address".to_string(), e, false),
    }
    let sig_ok = match (
        PublicKey::try_from(wit.vkey.as_ref() as &[u8]),
        Signature::try_from(wit.signature.as_ref() as &[u8]),
    ) {
        (Ok(pk), Ok(sig)) => pk.verify(tx.hash().as_ref() as &[u8], &sig),
        _ => false,
    };
    r.push(
        "signed.signature",
        "ed25519 signature over the tx id verifies".to_string(),
        if sig_ok { "verifies".to_string() } else { "does NOT verify".to_string() },
        sig_ok,
    );
    r
}

/// What the SIGNED transaction is, in plain words, from the signed CBOR itself
/// (Pallas decode) plus `verify_signed`. Used for every post-signature view.
#[derive(Debug, Clone, Serialize)]
pub struct SignedSummary {
    pub tx_id: String,
    pub vkey_witnesses: usize,
    pub other_witnesses: usize,
    pub signer_key_hashes: Vec<String>,
    pub wallet_key_hash: String,
    pub key_hash_matches: bool,
    pub signature_verifies: bool,
    pub check: Report,
    pub lines: Vec<String>,
}

fn check_ok(report: &Report, id: &str) -> bool {
    report.checks.iter().any(|c| c.id == id && c.ok)
}

pub fn describe_signed(
    signed_hex: &str,
    approved_tx_id: &str,
    from_address: &str,
    network_label: &str,
) -> Result<SignedSummary, String> {
    let report = verify_signed(signed_hex, approved_tx_id, from_address);
    let bytes = hex::decode(signed_hex.trim()).map_err(|e| format!("signed tx is not hex: {e}"))?;
    let tx = MultiEraTx::decode(&bytes).map_err(|e| format!("signed tx does not decode: {e}"))?;
    let conway = tx.as_conway().ok_or_else(|| format!("signed tx is {:?}, not Conway", tx.era()))?;
    let tx_id = tx.hash().to_string();
    let wits = &conway.transaction_witness_set;
    let vkeys: Vec<_> = wits.vkeywitness.iter().flat_map(|s| s.iter()).collect();
    let signer_key_hashes: Vec<String> =
        vkeys.iter().map(|w| Hasher::<224>::hash(w.vkey.as_ref()).to_string()).collect();
    let other_witnesses = wits.bootstrap_witness.iter().map(|s| s.len()).sum::<usize>()
        + wits.native_script.iter().map(|s| s.len()).sum::<usize>()
        + wits.plutus_v1_script.iter().map(|s| s.len()).sum::<usize>()
        + wits.plutus_v2_script.iter().map(|s| s.len()).sum::<usize>()
        + wits.plutus_v3_script.iter().map(|s| s.len()).sum::<usize>()
        + wits.plutus_data.iter().map(|s| s.len()).sum::<usize>()
        + usize::from(wits.redeemer.is_some());
    let wallet_key_hash = hex::encode(payment_key_hash(from_address)?);
    let key_hash_matches = check_ok(&report, "signed.key_hash");
    let signature_verifies = check_ok(&report, "signed.signature");
    let yes_no = |b: bool| if b { "yes" } else { "NO" };
    let lines = vec![
        format!(
            "SIGNED transaction {tx_id} (Conway era, {network_label}): {} vkey witness(es), \
             {other_witnesses} other witness(es).",
            vkeys.len()
        ),
        format!(
            "  Signer key hash(es): {}; your wallet's payment key hash {wallet_key_hash}: match {}.",
            if signer_key_hashes.is_empty() { "none".to_string() } else { signer_key_hashes.join(", ") },
            yes_no(key_hash_matches)
        ),
        format!(
            "  Ed25519 signature over the body hash (tx id) {tx_id}: verifies {}.",
            yes_no(signature_verifies)
        ),
        format!(
            "  Tx id equals the approved, congruence-checked tx id {approved_tx_id}: {}.",
            yes_no(tx_id == approved_tx_id)
        ),
        format!("  Signed-tx check: {}.", if report.ok { "all ok" } else { "FAILED" }),
    ];
    Ok(SignedSummary {
        tx_id,
        vkey_witnesses: vkeys.len(),
        other_witnesses,
        signer_key_hashes,
        wallet_key_hash,
        key_hash_matches,
        signature_verifies,
        check: report,
        lines,
    })
}
