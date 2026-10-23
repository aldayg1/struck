//! PORTED verbatim from 
//! (sha256 db946f7f2ad8b69c701564663b3e509ab041f80c2747022a6f634a56f9fb64b8).
//! Known issue: `saturating_add` on
//! lovelace totals would mask an overflow instead of failing loudly.
//!
//! Input selection for transaction building: largest-first coverage of the
//! requested lovelace (plus a fee headroom), with explicit coverage of every
//! required native asset — the token-aware rule the builder's Python path
//! grew after the real "In and out value not conserved" rejection on a
//! 191-UTxO wallet.

/// One selectable UTxO as plain data.
#[derive(Debug, Clone)]
pub struct Candidate {
    /// Opaque reference (e.g. "txid#ix") echoed back for chosen entries.
    pub reference: String,
    pub lovelace: u64,
    /// policy-id hex -> asset-name hex -> quantity.
    pub assets: std::collections::BTreeMap<String, std::collections::BTreeMap<String, u64>>,
}

#[derive(Debug, Clone)]
pub struct Selection {
    pub references: Vec<String>,
    pub total_lovelace: u64,
    /// Remaining uncovered required assets (empty when fully covered).
    pub missing_assets: std::collections::BTreeMap<String, std::collections::BTreeMap<String, u64>>,
    /// True when the selected lovelace does not cover amount + fee headroom.
    pub short_lovelace: bool,
}

/// Select inputs: every UTxO carrying a required asset first (largest coin
/// first), then largest-first pure-ADA entries until ``amount_lovelace +
/// fee_headroom`` is covered. Mirrors the semantics the Python builder used:
/// token-carrying inputs are added for ASSET coverage even when their coins
/// are unneeded; ADA coverage continues over the whole set.
pub fn select_inputs(
    candidates: &[Candidate],
    amount_lovelace: u64,
    fee_headroom: u64,
    required_assets: &std::collections::BTreeMap<String, std::collections::BTreeMap<String, u64>>,
) -> Selection {
    let mut remaining: std::collections::BTreeMap<String, std::collections::BTreeMap<String, u64>> =
        required_assets.clone();
    let mut chosen: Vec<&Candidate> = Vec::new();
    let mut total: u64 = 0;

    // Pass 1: asset coverage, largest coin first among carriers.
    let mut sorted: Vec<&Candidate> = candidates.iter().collect();
    sorted.sort_by(|a, b| b.lovelace.cmp(&a.lovelace).then_with(|| a.reference.cmp(&b.reference)));

    for candidate in &sorted {
        if remaining.is_empty() {
            break;
        }
        let mut took = false;
        for (policy, names) in &candidate.assets {
            if let Some(needed) = remaining.get_mut(policy) {
                for (name, qty) in names {
                    let need = needed.get(name).copied().unwrap_or(0);
                    if need > 0 && *qty > 0 {
                        took = true;
                        let covered = need.saturating_sub(*qty);
                        if covered == 0 {
                            needed.remove(name);
                        } else {
                            needed.insert(name.clone(), covered);
                        }
                    }
                }
                if needed.is_empty() {
                    remaining.remove(policy);
                }
            }
        }
        if took {
            chosen.push(candidate);
            total = total.saturating_add(candidate.lovelace);
        }
    }

    // Pass 2: ADA coverage, largest first (skip already-chosen).
    let target = amount_lovelace.saturating_add(fee_headroom);
    for candidate in &sorted {
        if total >= target {
            break;
        }
        if chosen.iter().any(|c| c.reference == candidate.reference) {
            continue;
        }
        chosen.push(candidate);
        total = total.saturating_add(candidate.lovelace);
    }

    // Stable output: keep the sorted (largest-first) order of selection.
    let references: Vec<String> = chosen.iter().map(|c| c.reference.clone()).collect();
    Selection {
        references,
        total_lovelace: total,
        missing_assets: remaining,
        short_lovelace: total < target,
    }
}
