//! Signed-tx verification against REAL preprod txs: the unsigned build must be
//! refused, and the tx Struck signed and submitted on 2026-10-21 (tx id
//! 09f6df51…59754, receipt 20261022T001259Z-koios-send-b5f4913166bc) must
//! pass and be described as SIGNED.

use struck_core::signed::{describe_signed, verify_signed};

const CBOR: &str = include_str!("golden/send_2ada.cbor.hex");
const TX_ID: &str = "ae9d15e07d94899497c1c5d40ef01c98b5702ac366287c0f88400ae8cf6360dc";
const FROM: &str = "addr_test1qrlz9mur7jclv6aawhl5lzwrfrddw3m8qe5m3p8kxq6seacryq78sqzx95jar4ldhfvajcdjf4d7zs3h324p3wwspkks5g8jlw";

#[test]
fn unsigned_tx_is_not_accepted_as_signed() {
    let report = verify_signed(CBOR, TX_ID, FROM);
    assert!(!report.ok);
    assert_eq!(report.failed_ids(), vec!["signed.vkey_witnesses".to_string()]);
}

#[test]
fn a_different_tx_id_is_refused() {
    let other = "00".repeat(32);
    let report = verify_signed(CBOR, &other, FROM);
    assert!(report.failed_ids().contains(&"signed.tx_id".to_string()));
}

#[test]
fn corrupt_cbor_is_refused() {
    let report = verify_signed("zz", TX_ID, FROM);
    assert_eq!(report.failed_ids(), vec!["signed.cbor".to_string()]);
}

const SIGNED: &str = include_str!("golden/send_2ada_signed.cbor.hex");
const SIGNED_TX_ID: &str = "09f6df511e98fcf42657192726672d182e60343e660779d67c56c4c89ba59754";

#[test]
fn the_submitted_signed_tx_verifies() {
    let report = verify_signed(SIGNED, SIGNED_TX_ID, FROM);
    assert!(report.ok, "{:?}", report.failed_ids());
}

#[test]
fn the_signed_tx_is_described_as_signed_with_its_witness() {
    let s = describe_signed(SIGNED, SIGNED_TX_ID, FROM, "preprod").unwrap();
    assert_eq!(s.tx_id, SIGNED_TX_ID);
    assert_eq!((s.vkey_witnesses, s.other_witnesses), (1, 0));
    assert_eq!(s.signer_key_hashes, vec![s.wallet_key_hash.clone()]);
    assert!(s.key_hash_matches && s.signature_verifies && s.check.ok);
    let text = s.lines.join("\n");
    assert!(text.starts_with("SIGNED transaction 09f6df51"));
    assert!(text.contains("1 vkey witness(es)") && text.contains("verifies yes"));
    assert!(!text.to_uppercase().contains("UNSIGNED") && !text.contains("awaiting"));
}

#[test]
fn the_unsigned_build_is_described_with_zero_witnesses_and_a_failed_check() {
    let unsigned = include_str!("golden/send_2ada_signed.unsigned.cbor.hex");
    let s = describe_signed(unsigned, SIGNED_TX_ID, FROM, "preprod").unwrap();
    assert_eq!(s.vkey_witnesses, 0);
    assert!(!s.check.ok && !s.signature_verifies);
    assert!(s.lines[0].contains("0 vkey witness(es)") && s.lines.join("\n").contains("FAILED"));
}
