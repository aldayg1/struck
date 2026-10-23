"""Typed contract for the struck_py PyO3 module (Rust struck_core).

Every value crosses the boundary as ``str`` or ``int``; JSON documents are
``str``. Every function raises ``ValueError`` on bad input (fail loudly).
"""

def ada_to_lovelace(ada: str) -> int: ...
def format_lovelace(lovelace: int) -> str: ...
def sum_quantities(json_ints: str) -> int: ...
# Integer quantities only (lovelace, token count). op in eq|gt|lt|gte|lte.
def compare_quantities(left: str, op: str, right: str) -> bool: ...
def percent_of_quantity(amount: str, percent: str) -> int: ...

# network_json: the SELECTED network row of struck.network.PROFILES
# (NetworkProfile.rust_json()); missing/unknown fields raise. No default.
def validate_onchain_plan(
    plan_json: str, utxos_json: str, current_slot: int, network_json: str
) -> str: ...
def select_inputs_for_plan(plan_json: str, utxos_json: str) -> list[str]: ...
def decode_tx(cbor_hex: str) -> str: ...
def congruence_check(cbor_hex: str, plan_json: str, inputs_json: str, network_json: str) -> str: ...

# N7c: signed tx vs the approved tx id + from_address; CheckReport JSON.
def verify_signed_tx(signed_hex: str, approved_tx_id: str, from_address: str) -> str: ...

# Post-signature view of the SIGNED tx (SignedSummary JSON); ValueError if undecodable.
def describe_signed_tx(
    signed_hex: str, approved_tx_id: str, from_address: str, network_json: str
) -> str: ...
def deposit_explain(
    cbor_hex: str,
    user_address: str,
    deposit_lovelace: int,
    max_fee_lovelace: int,
    valid_until_slot: int,
    inputs_json: str,
    ttl_wallclock: str,
    network_json: str,
) -> str: ...
def explain_tx(
    cbor_hex: str, plan_json: str, inputs_json: str, ttl_wallclock: str, network_json: str
) -> str: ...

# Strike lane (spec §6.3, B1). Decimals cross as str; JSON results.
def strike_validate_leg(
    leg_json: str, markets_json: str, fee_tiers_json: str, tier: int
) -> str: ...
def strike_build_request(leg_json: str, client_order_id: str) -> str: ...
def strike_request_congruence(
    request_json: str,
    leg_json: str,
    client_order_id: str,
    markets_json: str,
    fee_tiers_json: str,
    tier: int,
) -> str: ...
def strike_explain(
    request_json: str,
    leg_json: str,
    markets_json: str,
    fee_tiers_json: str,
    tier: int,
    network_json: str,
) -> str: ...

# Spec D5 optional pre-approval policy.
def policy_check_onchain(policy_json: str, send_json: str, now_ms: int) -> str: ...
def policy_check_strike(
    policy_json: str, network: str, legs_json: str, markets_json: str, now_ms: int
) -> str: ...

# Trigger thresholds on served decimals: exact, op in gt|lt|gte|lte.
def decimal_compare(value: str, op: str, threshold: str) -> bool: ...

# Reputation report over served evidence (rule struck-reputation-1): a report
# JSON string with status, score and reasons; errors raise ValueError.
def reputation_score(evidence_json: str) -> str: ...

def lien_transition(input_json: str) -> str: ...
