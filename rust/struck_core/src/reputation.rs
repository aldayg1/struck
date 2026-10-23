//! Reputation scoring over SERVED Strike evidence only (coordination contract,
//! "Escrow and reputation boundaries"): position-life net realized PnL and ROI,
//! the declared window, history completeness and served balances. Struck does
//! not average ROI percentages, does not equate transient holdings with locked
//! collateral, and never presents the score as lending eligibility: every
//! report carries its rule version and the explicit non-eligibility notice.
//! All arithmetic is `rust_decimal`, exact, checked; no floats.

use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::decimal;

/// The one approved rule. A change to weights, windows or inputs is a NEW
/// version string, never a silent edit of this one.
pub const RULE_VERSION: &str = "struck-reputation-1";

/// Published component weights (sum to 100). Component A is outcome
/// reliability (closed lives that ended net-positive), B is the net realized
/// result over the window, C is loss containment (worst single loss covered by
/// the window's gains).
const WEIGHT_OUTCOME: Decimal = Decimal::from_parts(60, 0, 0, false, 0);
const WEIGHT_RESULT: Decimal = Decimal::from_parts(30, 0, 0, false, 0);
const WEIGHT_CONTAINMENT: Decimal = Decimal::from_parts(10, 0, 0, false, 0);

/// The served position-life fields the rule reads. Extra served fields are
/// ignored here (the Pydantic boundary keeps the full row authoritative).
#[derive(Debug, Deserialize)]
struct PositionLifeInput {
    id: String,
    account_id: String,
    symbol: String,
    status: String,
    net_realized_pnl: String,
    roi_pct: String,
    open_timestamp: i64,
    close_timestamp: Option<i64>,
    data_complete: bool,
}

#[derive(Debug, Deserialize)]
pub struct EvidenceInput {
    rule: String,
    /// Declared evidence window in served epoch ms; end may be open (null).
    window_start_ms: i64,
    window_end_ms: Option<i64>,
    /// True only when every served page has been loaded (no `has_more`).
    history_exhausted: bool,
    /// Served balances snapshot (wallet/available per asset), provenance only.
    balances: Vec<BalanceInput>,
    positions: Vec<PositionLifeInput>,
}

#[derive(Debug, Deserialize)]
pub struct BalanceInput {
    asset: String,
    #[serde(rename = "walletBalance")]
    wallet_balance: String,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct ScoreReport {
    pub rule_version: String,
    pub status: String,
    pub reasons: Vec<String>,
    pub score: Option<i64>,
    pub components: Option<Components>,
    pub totals: Totals,
    pub evidence: EvidenceNote,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Components {
    /// 0..60: 60 × closed-wins / (closed-wins + closed-losses).
    pub outcome_reliability: String,
    /// 0..30: 30 net gain, 15 exactly flat, 0 net loss over the window.
    pub net_result: String,
    /// 0..10: 10 when the worst single net loss is covered by the window's
    /// profitable lives' gains, else 0.
    pub loss_containment: String,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct Totals {
    pub window_start_ms: i64,
    pub window_end_ms: Option<i64>,
    pub complete_closed_lives: u64,
    pub profitable: u64,
    pub losing: u64,
    pub break_even: u64,
    /// Exact decimal sum of net realized PnL over complete closed lives.
    pub net_realized_pnl_sum: String,
    pub worst_single_net_loss: String,
    /// Distinct markets traded inside the window (provenance, not scored).
    pub markets: Vec<String>,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct EvidenceNote {
    pub history_exhausted: bool,
    pub served_balances: Vec<String>,
    pub notice: &'static str,
}

const NOTICE: &str = "Score of served closed-position outcomes only; NOT creditworthiness or lending eligibility. Balances are not locked collateral. ROI percentages are never averaged.";

fn is_zero(d: &Decimal) -> bool {
    d.is_zero()
}

pub fn score(input_json: &str) -> Result<ScoreReport, String> {
    let input: EvidenceInput = serde_json::from_str(input_json)
        .map_err(|e| format!("evidence: invalid JSON: {e}"))?;
    if input.rule != RULE_VERSION {
        return Err(format!(
            "evidence.rule: expected {RULE_VERSION:?}, got {:?}",
            input.rule
        ));
    }

    let mut reasons: Vec<String> = Vec::new();
    let mut totals = Totals {
        window_start_ms: input.window_start_ms,
        window_end_ms: input.window_end_ms,
        complete_closed_lives: 0,
        profitable: 0,
        losing: 0,
        break_even: 0,
        net_realized_pnl_sum: "0".to_string(),
        worst_single_net_loss: "0".to_string(),
        markets: Vec::new(),
    };

    let mut pnl_sum = Decimal::ZERO;
    let mut worst_loss = Decimal::ZERO;
    let mut gains_sum = Decimal::ZERO;
    let mut markets: Vec<String> = Vec::new();
    let mut identities: Vec<String> = Vec::new();
    const WINDOW_IDENTITY_LIMIT: usize = 1;

    for life in &input.positions {
        if !life.data_complete {
            reasons.push(format!("life {} excluded: partial history", life.id));
            continue;
        }
        if life.status != "closed" {
            reasons.push(format!("life {} excluded: status {}", life.id, life.status));
            continue;
        }
        let closed = life.close_timestamp.ok_or_else(|| {
            format!("life {}: closed without a close timestamp (served shape violation)", life.id)
        })?;
        if closed < input.window_start_ms
            || input.window_end_ms.is_some_and(|end| closed > end)
        {
            reasons.push(format!("life {} excluded: closed outside the window", life.id));
            continue;
        }
        // Served shape: a life must open before it closes (exact integers).
        let opened = life.open_timestamp;
        if opened > closed {
            return Err(format!(
                "life {}: opened {} after close {} (served shape violation)",
                life.id, opened, closed
            ));
        }
        let net = decimal::parse("net_realized_pnl", &life.net_realized_pnl)?;
        pnl_sum = decimal::add(pnl_sum, net)?;
        // REP01 identity discipline: one window may mix exactly one account.
        if !identities.contains(&life.account_id) {
            identities.push(life.account_id.clone());
        }
        if net < Decimal::ZERO {
            totals.losing += 1;
            if net < worst_loss {
                worst_loss = net;
            }
        } else if is_zero(&net) {
            totals.break_even += 1;
        } else {
            totals.profitable += 1;
            gains_sum = decimal::add(gains_sum, net)?;
        }
        totals.complete_closed_lives += 1;
        let roi = decimal::parse("roi_pct", &life.roi_pct)?;
        let _ = roi; // served provenance only; ROI is never averaged
        if !markets.contains(&life.symbol) {
            markets.push(life.symbol.clone());
        }
    }

    totals.net_realized_pnl_sum = pnl_sum.to_string();
    totals.worst_single_net_loss = worst_loss.to_string();
    totals.markets = markets;

    if !input.history_exhausted {
        reasons.push("incomplete: served history not exhausted (more pages exist)".to_string());
    }
    if input.history_exhausted && totals.complete_closed_lives == 0 {
        reasons.push("incomplete: no complete closed lives inside the window".to_string());
    }
    if identities.len() > WINDOW_IDENTITY_LIMIT {
        for id in identities
            .iter()
            .skip(WINDOW_IDENTITY_LIMIT)
        {
            reasons.push(format!("incomplete: mixed identities in one window ({id})"));
        }
    }

    let evidence = EvidenceNote {
        history_exhausted: input.history_exhausted,
        served_balances: input
            .balances
            .iter()
            .map(|b| format!("{} wallet {}", b.asset, b.wallet_balance))
            .collect(),
        notice: NOTICE,
    };

    if !reasons.iter().any(|r| r.starts_with("incomplete:")) {
        let decided = totals.losing + totals.profitable;
        let outcome = if decided == 0 {
            Decimal::ZERO
        } else {
            decimal::mul(
                WEIGHT_OUTCOME,
                decimal::div(Decimal::from(totals.profitable), Decimal::from(decided))?,
            )?
        };
        let net_result = if pnl_sum > Decimal::ZERO {
            WEIGHT_RESULT
        } else if is_zero(&pnl_sum) {
            WEIGHT_RESULT / Decimal::from(2)
        } else {
            Decimal::ZERO
        };
        let containment = if is_zero(&worst_loss) || worst_loss >= Decimal::ZERO {
            WEIGHT_CONTAINMENT
        } else if gains_sum + worst_loss >= Decimal::ZERO {
            WEIGHT_CONTAINMENT
        } else {
            Decimal::ZERO
        };
        let total = decimal::add(decimal::add(outcome, net_result)?, containment)?;
        // Bounded to 0..100 by the published weights; half-even to whole points.
        let rounded = total.round_dp(0);
        let score_value = rounded
            .to_i64()
            .ok_or_else(|| format!("score out of range: {total}"))?;
        return Ok(ScoreReport {
            rule_version: RULE_VERSION.to_string(),
            status: "scored".to_string(),
            reasons,
            score: Some(score_value),
            components: Some(Components {
                outcome_reliability: outcome.to_string(),
                net_result: net_result.to_string(),
                loss_containment: containment.to_string(),
            }),
            totals,
            evidence,
        });
    }

    Ok(ScoreReport {
        rule_version: RULE_VERSION.to_string(),
        status: "incomplete_evidence".to_string(),
        reasons,
        score: None,
        components: None,
        totals,
        evidence,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn life(id: &str, net: &str, roi: &str, closed_ms: i64, complete: bool) -> String {
        format!(
            r#"{{"id":"{id}","account_id":"a1","symbol":"BTC-USD","status":"closed","net_realized_pnl":"{net}",
                "roi_pct":"{roi}","open_timestamp":0,"close_timestamp":{closed_ms},
                "data_complete":{complete}}}"#
        )
    }

    fn evidence(positions: String, exhausted: bool) -> String {
        format!(
            r#"{{"rule":"{RULE_VERSION}","window_start_ms":0,"window_end_ms":null,
                "history_exhausted":{exhausted},"balances":[{{"asset":"USD","walletBalance":"0"}}],
                "positions":[{positions}]}}"#
        )
    }

    #[test]
    fn scores_a_clean_window() {
        let report = score(&evidence(
            format!(
                "{},{}",
                life("p1", "12.5", "5.0", 100, true),
                life("p2", "-3.0", "-1.0", 200, true)
            ),
            true,
        ))
        .unwrap();
        assert_eq!(report.status, "scored");
        // outcome 60*(1/2)=30, result 30, containment 10 -> 70
        assert_eq!(report.score, Some(70));
        assert_eq!(report.totals.net_realized_pnl_sum, "9.5");
        assert_eq!(report.totals.worst_single_net_loss, "-3.0");
    }

    #[test]
    fn partial_history_is_never_scored() {
        let report = score(&evidence(life("p1", "5", "1", 100, false), true)).unwrap();
        assert_eq!(report.status, "incomplete_evidence");
        assert_eq!(report.score, None);
    }

    #[test]
    fn unexhausted_history_is_never_scored() {
        let report = score(&evidence(life("p1", "5", "1", 100, true), false)).unwrap();
        assert_eq!(report.status, "incomplete_evidence");
    }

    #[test]
    fn wrong_rule_version_is_rejected() {
        let body = evidence(life("p1", "5", "1", 100, true), true).replace(RULE_VERSION, "other");
        assert!(score(&body).is_err());
    }

    #[test]
    fn window_excludes_lives_closed_outside() {
        let body = evidence(life("p1", "5", "1", 5000, true), true)
            .replace("\"window_end_ms\":null", "\"window_end_ms\":1000");
        let report = score(&body).unwrap();
        assert_eq!(report.status, "incomplete_evidence");
        assert_eq!(report.totals.complete_closed_lives, 0);
    }

    #[test]
    fn mixed_identities_are_never_scored() {
        let other = life("p2", "4", "1", 200, true).replace("a1", "a2");
        let report = score(&evidence(format!("{},{}", life("p1", "5", "1", 100, true), other), true)).unwrap();
        assert_eq!(report.status, "incomplete_evidence");
        assert_eq!(report.score, None);
    }

    #[test]
    fn a_life_that_opens_after_closing_is_a_shape_violation() {
        let bad = life("p1", "5", "1", 100, true).replace("close_timestamp\":100", "close_timestamp\":100");
        // open_timestamp 0 close 100 is fine; flip them via open 1000 (replace open value)
        let bad = bad.replace("open_timestamp\":0", "open_timestamp\":1000");
        assert!(score(&evidence(bad, true))
            .unwrap_err()
            .contains("served shape violation"));
    }
}
