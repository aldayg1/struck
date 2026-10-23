//! Congruence against a REAL preprod tx .
//! The tamper cases change only the approved plan or the resolved inputs;
//! each must flip exactly the check that guards it.

use struck_core::congruence::check;
use struck_core::decode::{decode_tx, DecodedAsset, DecodedTx};
mod common;
use common::net;
use struck_core::plan::{parse_json, NetParams, OnchainPlan, Utxo};

const CBOR: &str = include_str!("golden/send_2ada.cbor.hex");
const PLAN: &str = include_str!("golden/send_2ada.plan.json");
const INPUTS: &str = include_str!("golden/send_2ada.inputs.json");
const OTHER_TESTNET_ADDR: &str =
    "addr_test1qpw0djgj0x59ngrjvqthn7enhvruxnsavsw5th63la3mjel3tkc974sr23jmlzgq5zda4gtv8k9cy38756r9y3qgmkqqjz6aa7";

fn fixtures() -> (OnchainPlan, Vec<Utxo>) {
    (parse_json("plan", PLAN).unwrap(), parse_json("inputs", INPUTS).unwrap())
}

fn failed(plan: &OnchainPlan, inputs: &[Utxo]) -> Vec<String> {
    let tx = decode_tx(CBOR).unwrap();
    check(&tx, plan, inputs, &net("preprod")).unwrap().failed_ids()
}

#[test]
fn real_tx_is_congruent_with_its_approved_plan() {
    let (plan, inputs) = fixtures();
    let tx = decode_tx(CBOR).unwrap();
    assert_eq!(tx.tx_id, "ae9d15e07d94899497c1c5d40ef01c98b5702ac366287c0f88400ae8cf6360dc");
    assert_eq!(tx.fee, 171_573);
    assert_eq!(tx.vkey_witnesses, 0);
    let report = check(&tx, &plan, &inputs, &net("preprod")).unwrap();
    assert!(report.ok, "{:?}", report.failed_ids());
}

#[test]
fn tampered_amount_is_rejected() {
    let (mut plan, inputs) = fixtures();
    plan.outputs[0].lovelace = 2_000_001;
    let f = failed(&plan, &inputs);
    assert!(f.contains(&"plan.outputs[0]".to_string()), "{f:?}");
}

#[test]
fn tampered_payee_is_rejected() {
    let (mut plan, inputs) = fixtures();
    plan.outputs[0].address = OTHER_TESTNET_ADDR.to_string();
    let f = failed(&plan, &inputs);
    assert!(f.contains(&"plan.outputs[0]".to_string()), "{f:?}");
    // The real payee output is now an unexplained extra output, not change.
    assert!(f.contains(&"extra_output[0].is_change".to_string()), "{f:?}");
}

#[test]
fn fee_above_bound_is_rejected() {
    let (mut plan, inputs) = fixtures();
    plan.max_fee_lovelace = 171_572;
    assert_eq!(failed(&plan, &inputs), vec!["fee.bound".to_string()]);
}

#[test]
fn ttl_beyond_window_is_rejected() {
    let (mut plan, inputs) = fixtures();
    plan.valid_until_slot -= 1;
    assert_eq!(failed(&plan, &inputs), vec!["validity.ttl".to_string()]);
}

#[test]
fn input_not_owned_by_wallet_is_rejected() {
    let (plan, mut inputs) = fixtures();
    inputs[0].address = OTHER_TESTNET_ADDR.to_string();
    let f = failed(&plan, &inputs);
    assert!(f.iter().any(|id| id.starts_with("inputs.")), "{f:?}");
}

#[test]
fn misreported_input_value_breaks_balance() {
    let (plan, mut inputs) = fixtures();
    inputs[0].lovelace += 1;
    assert_eq!(failed(&plan, &inputs), vec!["balance.lovelace".to_string()]);
}

#[test]
fn unresolved_input_is_rejected() {
    let (plan, _) = fixtures();
    let f = failed(&plan, &[]);
    assert!(f.iter().any(|id| id.starts_with("inputs.")), "{f:?}");
}

#[test]
fn corrupt_cbor_fails_loudly() {
    assert!(decode_tx("84a4").is_err());
    assert!(decode_tx("zz").is_err());
}

// ---- decoded-tx tampering: each mutation must flip exactly its guard ----

fn tampered(mutate: impl FnOnce(&mut DecodedTx)) -> Vec<String> {
    let (plan, inputs) = fixtures();
    let mut tx = decode_tx(CBOR).unwrap();
    mutate(&mut tx);
    check(&tx, &plan, &inputs, &net("preprod")).unwrap().failed_ids()
}

#[test]
fn extra_foreign_output_is_rejected() {
    let other = struck_core::plan::network_address(OTHER_TESTNET_ADDR, &net("preprod")).unwrap();
    let f = tampered(|tx| {
        let mut out = tx.outputs[0].clone();
        out.index = tx.outputs.len();
        out.address = OTHER_TESTNET_ADDR.to_string();
        out.address_hex = hex::encode(other.to_vec());
        out.lovelace = 1_000_000;
        tx.outputs.push(out);
    });
    let k = format!("extra_output[{}].is_change", decode_tx(CBOR).unwrap().outputs.len());
    assert!(f.contains(&k), "{f:?}");
    assert!(f.contains(&"balance.lovelace".to_string()), "{f:?}");
}

#[test]
fn duplicate_input_is_rejected() {
    let f = tampered(|tx| {
        let dup = tx.inputs[0].clone();
        tx.inputs.push(dup);
    });
    let tx = decode_tx(CBOR).unwrap();
    let id = format!("inputs.{}#{}", tx.inputs[0].tx_hash, tx.inputs[0].index);
    assert!(f.contains(&id), "{f:?}");
}

#[test]
fn network_id_1_is_rejected_on_preprod() {
    let f = tampered(|tx| tx.network_id = Some(1));
    assert_eq!(f, vec!["network_id".to_string()]);
}

#[test]
fn network_id_0_is_accepted_on_preprod() {
    assert!(tampered(|tx| tx.network_id = Some(0)).is_empty());
}

#[test]
fn selected_mainnet_rejects_a_preprod_plan_loudly() {
    let (plan, inputs) = fixtures();
    let tx = decode_tx(CBOR).unwrap();
    let f = check(&tx, &plan, &inputs, &net("mainnet")).unwrap().failed_ids();
    assert!(f.contains(&"plan.network".to_string()), "{f:?}");
    let mut tx1 = tx.clone();
    tx1.network_id = Some(0);
    let f = check(&tx1, &plan, &inputs, &net("mainnet")).unwrap().failed_ids();
    assert!(f.contains(&"network_id".to_string()), "{f:?}");
}

#[test]
fn every_forbidden_field_is_rejected() {
    let cases: Vec<(&str, Box<dyn Fn(&mut DecodedTx)>)> = vec![
        ("mint", Box::new(|t| t.mint_policies = 1)),
        ("certificates", Box::new(|t| t.certificates = 1)),
        ("withdrawals", Box::new(|t| t.withdrawals = 1)),
        ("script_data_hash", Box::new(|t| t.has_script_data_hash = true)),
        ("collateral", Box::new(|t| t.collateral_inputs = 1)),
        ("collateral_return", Box::new(|t| t.has_collateral_return = true)),
        ("total_collateral", Box::new(|t| t.total_collateral = Some(1))),
        ("required_signers", Box::new(|t| t.required_signers = 1)),
        ("reference_inputs", Box::new(|t| t.reference_inputs = 1)),
        ("voting_procedures", Box::new(|t| t.voting_procedures = 1)),
        ("proposal_procedures", Box::new(|t| t.proposal_procedures = 1)),
        ("treasury_value", Box::new(|t| t.treasury_value = Some(1))),
        ("donation", Box::new(|t| t.donation = Some(1))),
        ("scripts_or_redeemers", Box::new(|t| t.has_redeemers = true)),
        ("auxiliary_data", Box::new(|t| t.has_auxiliary_data_hash = true)),
    ];
    for (name, mutate) in cases {
        let f = tampered(|t| mutate(t));
        assert_eq!(f, vec![format!("undeclared.{name}")], "{name}");
    }
}

#[test]
fn asset_only_imbalance_is_rejected() {
    // An asset appears in the change output that no input holds: lovelace still balance.
    let (plan, _) = fixtures();
    let f = tampered(|tx| {
        let k = tx
            .outputs
            .iter()
            .position(|o| o.address == plan.from_address)
            .expect("the golden tx has a change output");
        tx.outputs[k].assets.push(DecodedAsset {
            policy_hex: "ab".repeat(28),
            name_hex: "".into(),
            qty: 1,
        });
    });
    assert_eq!(f, vec!["balance.assets".to_string()]);
}

#[test]
fn validity_start_after_ttl_is_rejected() {
    let f = tampered(|tx| tx.validity_start = tx.ttl.map(|t| t + 1));
    assert_eq!(f, vec!["validity.start".to_string()]);
}

#[test]
fn plan_check_uses_the_selected_network() {
    use struck_core::plan::validate_plan;
    let (plan, inputs) = fixtures();
    let ok = validate_plan(&plan, &inputs, 0, &net("preprod")).failed_ids();
    assert!(!ok.contains(&"network".to_string()), "{ok:?}");
    let f = validate_plan(&plan, &inputs, 0, &net("mainnet")).failed_ids();
    assert!(f.contains(&"network".to_string()), "{f:?}");
    assert!(f.contains(&"from_address.parse".to_string()), "{f:?}"); // addr_test1 is not mainnet
    assert!(f.contains(&"outputs[0].address".to_string()), "{f:?}");
    // No fallback: an unknown/incomplete network JSON is an error, not a default.
    assert!(NetParams::parse(r#"{"name":"x"}"#).is_err());
}
