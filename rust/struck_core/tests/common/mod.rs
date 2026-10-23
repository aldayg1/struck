//! Test fixture: the selected-network JSON exactly as Python sends it
//! (`struck.network.NetworkProfile.rust_json()` of each PROFILES row; the
//! Python suite asserts the same values in tests/test_network.py).
use struck_core::plan::NetParams;

pub fn net(name: &str) -> NetParams {
    let json = match name {
        "preprod" => r#"{"name":"preprod","label":"preprod (Cardano preprod + Strike TESTNET)","strike_label":"Strike TESTNET","network_id":0,"address_hrp":"addr_test"}"#,
        "mainnet" => r#"{"name":"mainnet","label":"mainnet (Cardano mainnet + Strike MAINNET)","strike_label":"Strike MAINNET","network_id":1,"address_hrp":"addr"}"#,
        other => panic!("no fixture for network {other:?}"),
    };
    NetParams::parse(json).expect("fixture parses")
}
