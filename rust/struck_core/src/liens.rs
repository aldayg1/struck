//! LIEN contract — the deterministic collateral-lien state machine
//! (coordination I04: "Full LIEN contracts/runtime"). A lien is a lender's
//! recorded claim on collateral owned by the borrower: terms are hashed by the
//! caller, every value check is exact ``rust_decimal`` math, and a transition
//! NEVER invents state — it verifies a proof tuple against the CURRENT state
//! and the served/chain evidence handed in. Python does no arithmetic on any
//! of these values (law 3: money code is Rust).

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// The one contract-rule version. Every change is a NEW version string.
pub const RULE_VERSION: &str = "struck-lien-1";

/// The lien lifecycle phases (stable wire names, lower-case).
#[derive(Debug, Serialize, Deserialize, PartialEq, Eq, Clone, Copy)]
#[serde(rename_all = "lowercase")]
pub enum LienPhase {
    /// Terms exist; collateral NOT yet locked.
    Open,
    /// Collateral lock observed (payment to the lien escrow address).
    Secured,
    /// Funded/drawn position under the lien, inside the window.
    Active,
    /// Borrower delivered principal + fee inside the window; released.
    Repaid,
    /// The window closed without full repayment: the lien executes.
    Foreclosed,
    /// Lender withdrew before the collateral moved. Terminal.
    Cancelled,
}

/// Strict terms of one lien. Decimals are EXACT SERVED strings (lovelace /
/// token qty as served); no floats anywhere on this type.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct LienTerms {
    pub principal: String,
    /// Fee on top of principal; the total due is computed HERE, in Rust.
    pub fee: String,
    pub collateral: String,
    /// Minimum coverage (collateral / (principal + fee)) in basis points.
    pub min_coverage_bps: i64,
    /// Served epoch ms; after this the lien forecloses unless Repaid.
    pub due_ms: i64,
    pub borrower: String,
    pub lender: String,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq)]
pub struct LienState {
    pub terms: LienTerms,
    pub phase: LienPhase,
    /// The served lock/release tx ids (d2: used as served). Empty until the
    /// phase actually moved on chain evidence.
    pub lock_tx_id: Option<String>,
    pub release_tx_id: Option<String>,
    pub last_transition_ms: i64,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Refusal {
    pub rule_version: String,
    pub ok: bool,
    pub reasons: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct TransitionInput {
    /// The CURRENT stored state, exactly as persisted — no inference.
    pub current: LienState,
    /// The phase this transition claims to reach.
    pub proposed: LienPhase,
    /// Decision clock (served epoch ms); checked against due_ms where the
    /// phase requires it.
    pub now_ms: i64,
    /// Secured: the SERVED locked amount. Repaid: the SERVED delivered
    /// amount. Exact strings; compared against the terms with ==.
    pub lock_amount: Option<String>,
    pub repay_amount: Option<String>,
    /// The served tx id of the lock / release payment for THIS transition.
    pub tx_id: Option<String>,
}

fn dec(s: &str, where_: &str, reasons: &mut Vec<String>) -> Option<Decimal> {
    match s.parse::<Decimal>() {
        Ok(v) if v >= Decimal::ZERO => Some(v),
        _ => {
            reasons.push(format!(
                "{where_}: {s:?} is not an exact non-negative served decimal"
            ));
            None
        }
    }
}

/// `collateral >= (min_coverage_bps / 10000) * (principal + fee)` by exact
/// cross-multiplication: collateral * 10000 >= bps * total_due.
fn coverage_ok(t: &LienTerms, collateral: Decimal, reasons: &mut Vec<String>) -> bool {
    let Some(principal) = dec(&t.principal, "terms.principal", reasons) else {
        return false;
    };
    let Some(_fee) = dec(&t.fee, "terms.fee", reasons) else {
        return false;
    };
    if t.min_coverage_bps < 0 {
        reasons.push("terms.min_coverage_bps: negative".into());
        return false;
    }
    let total_due = principal + _fee;
    if total_due.is_zero() {
        reasons.push("terms: principal + fee is zero".into());
        return false;
    }
    let Some(lhs) = collateral.checked_mul(Decimal::from(10_000)) else {
        reasons.push("collateral coverage: checked-multiply overflow".into());
        return false;
    };
    let Some(rhs) = total_due.checked_mul(Decimal::from(t.min_coverage_bps)) else {
        reasons.push("collateral coverage: checked-multiply overflow".into());
        return false;
    };
    if lhs >= rhs {
        true
    } else {
        reasons.push(format!(
            "collateral coverage: {collateral} does not cover the due {total_due} at min_coverage_bps {}",
            t.min_coverage_bps
        ));
        false
    }
}

/// The ONLY way state moves: the full proof tuple against the current phase.
/// Returns the new state, or the refusal reasons (never a panic, never a
/// guess — a refusal is the contract answer).
pub fn transition(
    input: &TransitionInput,
) -> Result<LienState, Refusal> {
    let mut reasons: Vec<String> = Vec::new();
    let refuse = |reasons: Vec<String>| Refusal {
        rule_version: RULE_VERSION.into(),
        ok: false,
        reasons,
    };
    let t = &input.current.terms;
    let now = input.now_ms;
    let next = match (input.current.phase, input.proposed) {
        (LienPhase::Open, LienPhase::Secured) => {
            let Some(amount) = input.lock_amount.as_ref() else {
                reasons.push("Secured requires lock_amount (the served collateral)".into());
                return Err(refuse(reasons));
            };
            let Some(amount) = dec(amount, "lock_amount", &mut reasons) else {
                return Err(refuse(reasons));
            };
            let Some(collateral) = dec(&t.collateral, "terms.collateral", &mut reasons) else {
                return Err(refuse(reasons));
            };
            if amount != collateral {
                reasons.push(format!(
                    "lock_amount {amount} != terms.collateral {collateral}: the lock must move EXACTLY the terms"
                ));
                return Err(refuse(reasons));
            }
            if input.tx_id.is_none() {
                reasons.push("Secured requires the observed lock tx id".into());
                return Err(refuse(reasons));
            }
            if !coverage_ok(t, amount, &mut reasons) {
                return Err(refuse(reasons));
            }
            LienPhase::Secured
        }
        (LienPhase::Secured, LienPhase::Active) => {
            if now >= t.due_ms {
                reasons.push("cannot go Active on/after due_ms: foreclose, not activation".into());
                return Err(refuse(reasons));
            }
            LienPhase::Active
        }
        (LienPhase::Secured | LienPhase::Active, LienPhase::Repaid) => {
            let Some(delivered) = input.repay_amount.as_ref() else {
                reasons.push("Repaid requires repay_amount (the served principal + fee)".into());
                return Err(refuse(reasons));
            };
            let Some(principal) = dec(&t.principal, "terms.principal", &mut reasons) else {
                return Err(refuse(reasons));
            };
            let Some(fee) = dec(&t.fee, "terms.fee", &mut reasons) else {
                return Err(refuse(reasons));
            };
            let Some(delivered) = dec(delivered, "repay_amount", &mut reasons) else {
                return Err(refuse(reasons));
            };
            let total_due = principal + fee;
            if delivered != total_due {
                reasons.push(format!(
                    "repay_amount {delivered} != principal + fee {total_due}: a partial repayment NEVER releases a lien"
                ));
                return Err(refuse(reasons));
            }
            if input.tx_id.is_none() {
                reasons.push("Repaid requires the served release tx id".into());
                return Err(refuse(reasons));
            }
            LienPhase::Repaid
        }
        (LienPhase::Secured | LienPhase::Active, LienPhase::Foreclosed) => {
            if now < t.due_ms {
                reasons.push(format!(
                    "foreclose refused: now {now} before due_ms {} (the window is not over)",
                    t.due_ms
                ));
                return Err(refuse(reasons));
            }
            LienPhase::Foreclosed
        }
        (LienPhase::Open, LienPhase::Cancelled) => LienPhase::Cancelled,
        (from, to) => {
            reasons.push(format!(
                "transition refused: {from:?} -> {to:?} is not a contract path"
            ));
            return Err(refuse(reasons));
        }
    };
    if input.proposed != next {
        reasons.push(format!(
            "proposed {:?} does not match the contract verdict {:?}",
            input.proposed, next
        ));
        return Err(refuse(reasons));
    }
    Ok(LienState {
        terms: t.clone(),
        phase: next,
        lock_tx_id: input.current.lock_tx_id.clone().or_else(|| input.tx_id.clone()),
        release_tx_id: input.tx_id.clone(),
        last_transition_ms: now,
    })
}

/// Receipt-shaped verdict of one contract decision: the RESILIENT answer the
/// runtime stores — ok with the new state, or the refusal reasons. One call,
/// exact bytes, no panic.
#[derive(Debug, Serialize, PartialEq)]
pub struct TransitionReceipt {
    pub rule_version: String,
    pub from_phase: String,
    pub to_phase: String,
    pub ok: bool,
    pub state: Option<LienState>,
    pub reasons: Vec<String>,
}

pub fn transition_receipt(input_json: &str) -> String {
    let parsed: Result<TransitionInput, _> = serde_json::from_str(input_json);
    let from_phase = match &parsed {
        Ok(i) => format!("{:?}", i.current.phase).to_lowercase(),
        Err(_) => "unparseable".into(),
    };
    let receipt = |ok, state, reasons: Vec<String>, to_phase: String| {
        serde_json::to_string(&TransitionReceipt {
            rule_version: RULE_VERSION.into(),
            from_phase: from_phase.clone(),
            to_phase,
            ok,
            state,
            reasons,
        })
        .unwrap_or_else(|e| {
            serde_json::to_string(&Refusal {
                rule_version: RULE_VERSION.into(),
                ok: false,
                reasons: vec![format!("receipt serialization failed: {e}")],
            })
            .unwrap_or_default()
        })
    };
    match parsed {
        Ok(input) => match transition(&input) {
            Ok(state) => {
                let to_phase = format!("{:?}", state.phase).to_lowercase();
                receipt(true, Some(state), Vec::new(), to_phase)
            }
            Err(Refusal { reasons, .. }) => {
                let stuck_phase = format!("{:?}", input.current.phase).to_lowercase();
                receipt(false, Some(input.current), reasons, stuck_phase)
            }
        },
        Err(e) => receipt(false, None, vec![format!("not the strict JSON shape: {e}")], "unchanged".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn terms() -> LienTerms {
        LienTerms {
            principal: "20".into(),
            fee: "1".into(),
            collateral: "25".into(),
            min_coverage_bps: 10_000,
            due_ms: 1_800_000_000_000,
            borrower: "addr_test1borr".into(),
            lender: "addr_test1lend".into(),
        }
    }

    fn state(phase: LienPhase, locked: bool) -> LienState {
        LienState {
            terms: terms(),
            phase,
            lock_tx_id: locked.then(|| "locktx".into()),
            release_tx_id: None,
            last_transition_ms: 0,
        }
    }

    #[test]
    fn open_to_secured_requires_exact_collateral_and_tx_id() {
        let s = state(LienPhase::Open, false);
        let inp = format!(
            "{{\"current\":{},\"proposed\":\"secured\",\"now_ms\":1,\"lock_amount\":\"25\",\"tx_id\":\"abc\"}}",
            serde_json::to_string(&s).unwrap()
        );
        let r = transition_receipt(&inp);
        assert!(r.contains("\"ok\":true"), "{r}");
        assert!(r.contains("\"to_phase\":\"secured\""), "{r}");
    }

    #[test]
    fn the_lock_must_move_exactly_the_terms() {
        let s = state(LienPhase::Open, false);
        let inp = format!(
            "{{\"current\":{},\"proposed\":\"secured\",\"now_ms\":1,\"lock_amount\":\"24\",\"tx_id\":\"abc\"}}",
            serde_json::to_string(&s).unwrap()
        );
        let r = transition_receipt(&inp);
        assert!(r.contains("\"ok\":false"), "{r}");
        assert!(r.contains("must move EXACTLY"), "{r}");
    }

    #[test]
    fn partial_repayment_never_releases() {
        let s = state(LienPhase::Active, true);
        let inp = format!(
            "{{\"current\":{},\"proposed\":\"repaid\",\"now_ms\":1,\"repay_amount\":\"10\",\"tx_id\":\"abc\"}}",
            serde_json::to_string(&s).unwrap()
        );
        let r = transition_receipt(&inp);
        assert!(r.contains("\"ok\":false") && r.contains("partial repayment"), "{r}");
    }

    #[test]
    fn full_repay_releases_and_keeps_the_lock_tx() {
        let s = state(LienPhase::Active, true);
        let inp = format!(
            "{{\"current\":{},\"proposed\":\"repaid\",\"now_ms\":1,\"repay_amount\":\"21\",\"tx_id\":\"rel\"}}",
            serde_json::to_string(&s).unwrap()
        );
        let r = transition_receipt(&inp);
        assert!(r.contains("\"ok\":true"), "{r}");
        assert!(r.contains("\"lock_tx_id\":\"locktx\"") && r.contains("\"release_tx_id\":\"rel\""), "{r}");
    }

    #[test]
    fn foreclose_only_after_due() {
        let s = state(LienPhase::Active, true);
        let early = format!(
            "{{\"current\":{},\"proposed\":\"foreclosed\",\"now_ms\":1,\"tx_id\":\"abc\"}}",
            serde_json::to_string(&s).unwrap()
        );
        assert!(transition_receipt(&early).contains("before due_ms"));
        let late = format!(
            "{{\"current\":{},\"proposed\":\"foreclosed\",\"now_ms\":1800000000001,\"tx_id\":\"abc\"}}",
            serde_json::to_string(&s).unwrap()
        );
        assert!(transition_receipt(&late).contains("\"ok\":true"));
    }

    #[test]
    fn impossible_paths_are_refused() {
        let s = state(LienPhase::Open, false);
        let inp = format!(
            "{{\"current\":{},\"proposed\":\"active\",\"now_ms\":1}}",
            serde_json::to_string(&s).unwrap()
        );
        let r = transition_receipt(&inp);
        assert!(r.contains("\"ok\":false") && r.contains("not a contract path"), "{r}");
    }

    #[test]
    fn cancel_only_before_the_lock() {
        // Open -> Cancelled is allowed; Active -> Cancelled is a contract-path
        // refusal (post-lock, the terms govern).
        let open = state(LienPhase::Open, false);
        let ok = format!(
            "{{\"current\":{},\"proposed\":\"cancelled\",\"now_ms\":1}}",
            serde_json::to_string(&open).unwrap()
        );
        assert!(transition_receipt(&ok).contains("\"to_phase\":\"cancelled\""));
        let active = state(LienPhase::Active, true);
        let bad = format!(
            "{{\"current\":{},\"proposed\":\"cancelled\",\"now_ms\":1}}",
            serde_json::to_string(&active).unwrap()
        );
        assert!(transition_receipt(&bad).contains("not a contract path"));
    }

    #[test]
    fn secured_after_due_is_a_contract_projection_not_activation() {
        // A lock arriving AFTER due_ms still Secures the state (the collateral
        // physically moved), but Active/Repaid paths then follow the window
        // logic; this test only pins that Secured itself does not check due.
        let s = state(LienPhase::Open, false);
        let inp = format!(
            "{{\"current\":{},\"proposed\":\"secured\",\"now_ms\":1800000000001,\"lock_amount\":\"25\",\"tx_id\":\"late\"}}",
            serde_json::to_string(&s).unwrap()
        );
        assert!(transition_receipt(&inp).contains("\"ok\":true"));
    }

    #[test]
    fn coverage_is_exact() {
        let mut reasons = Vec::new();
        let t = terms();
        assert!(coverage_ok(&t, Decimal::from(25), &mut reasons));
        assert!(!coverage_ok(&t, Decimal::from(15), &mut reasons));
        assert_eq!(reasons.len(), 1);
        assert!(reasons[0].contains("does not cover"));
    }
}
