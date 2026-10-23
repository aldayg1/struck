//! Integer money types. Every operation is checked: an overflow or an
//! underflow is an `Err`, never a wrap, a saturation or a rounding.

use std::collections::BTreeMap;
use std::fmt;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// Lovelace per ADA (ledger constant).
pub const LOVELACE_PER_ADA: u64 = 1_000_000;
const ADA_DECIMALS: u32 = 6;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Lovelace(pub u64);

impl Lovelace {
    pub fn checked_add(self, other: Lovelace) -> Result<Lovelace, String> {
        self.0
            .checked_add(other.0)
            .map(Lovelace)
            .ok_or_else(|| format!("lovelace overflow: {} + {}", self.0, other.0))
    }

    pub fn checked_sub(self, other: Lovelace) -> Result<Lovelace, String> {
        self.0
            .checked_sub(other.0)
            .map(Lovelace)
            .ok_or_else(|| format!("lovelace underflow: {} - {}", self.0, other.0))
    }

    pub fn sum<I: IntoIterator<Item = Lovelace>>(items: I) -> Result<Lovelace, String> {
        items
            .into_iter()
            .try_fold(Lovelace(0), |acc, x| acc.checked_add(x))
    }
}

/// A non-negative integer quantity (lovelace or token count). No decimal point,
/// no sign, no whitespace. Overflow past `u64` is an error.
pub fn parse_quantity(text: &str) -> Result<u64, String> {
    if text.is_empty() || !text.bytes().all(|b| b.is_ascii_digit()) {
        return Err(format!(
            "quantity {text:?} is not a non-negative integer (no sign, no decimal)"
        ));
    }
    text.parse::<u64>()
        .map_err(|e| format!("quantity {text:?}: {e}"))
}

/// `percent` percent of an integer quantity. `percent` is a decimal string,
/// greater than 0 and at most 100. The product must be a whole quantity.
/// A fractional lovelace or token is an error. Nothing is rounded.
pub fn percent_of_quantity(amount: &str, percent: &str) -> Result<u64, String> {
    let qty = parse_quantity(amount)?;
    let pct = crate::decimal::parse("pct_of_incoming", percent)?;
    if pct <= Decimal::ZERO || pct > Decimal::from(100) {
        return Err(format!("pct_of_incoming {percent} must be > 0 and <= 100"));
    }
    let product = Decimal::from(qty)
        .checked_mul(pct)
        .ok_or_else(|| format!("overflow: {qty} * {percent}"))?;
    let whole = product
        .checked_div(Decimal::from(100))
        .ok_or_else(|| format!("overflow: {product} / 100"))?;
    if !whole.fract().is_zero() {
        return Err(format!(
            "pct_of_incoming {percent}% of {amount} is {whole}, not a whole quantity; nothing was rounded"
        ));
    }
    let normalized = whole.normalize();
    if normalized.scale() != 0 || normalized.is_sign_negative() {
        return Err(format!("pct_of_incoming result {normalized} is not a non-negative integer"));
    }
    u64::try_from(normalized.mantissa()).map_err(|e| format!("pct_of_incoming result: {e}"))
}

/// Exact compare of two integer quantities. Ops: `eq`, `gt`, `lt`, `gte`, `lte`.
pub fn compare_quantities(left: &str, op: &str, right: &str) -> Result<bool, String> {
    let a = parse_quantity(left)?;
    let b = parse_quantity(right)?;
    match op {
        "eq" => Ok(a == b),
        "gt" => Ok(a > b),
        "lt" => Ok(a < b),
        "gte" => Ok(a >= b),
        "lte" => Ok(a <= b),
        other => Err(format!("unknown op {other:?} (eq|gt|lt|gte|lte)")),
    }
}

impl fmt::Display for Lovelace {
    /// `2000000` -> `2.000000 ADA` (integer division and remainder only).
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "{}.{:06} ADA",
            self.0 / LOVELACE_PER_ADA,
            self.0 % LOVELACE_PER_ADA
        )
    }
}

/// Exact ADA decimal string -> lovelace. Rejects (never rounds) negative
/// values, more than 6 decimal places, and values that do not fit in u64.
pub fn ada_to_lovelace(ada: &str) -> Result<Lovelace, String> {
    let value = Decimal::from_str_exact(ada.trim())
        .map_err(|e| format!("ADA amount {ada:?} is not an exact decimal: {e}"))?;
    if value.is_sign_negative() {
        return Err(format!("ADA amount {ada:?} is negative"));
    }
    let normalized = value.normalize();
    if normalized.scale() > ADA_DECIMALS {
        return Err(format!(
            "ADA amount {ada:?} has {} decimal places; lovelace allows at most {ADA_DECIMALS} (no rounding)",
            normalized.scale()
        ));
    }
    let scaled = normalized
        .checked_mul(Decimal::from(LOVELACE_PER_ADA))
        .ok_or_else(|| format!("ADA amount {ada:?} overflows"))?;
    if !scaled.fract().is_zero() {
        return Err(format!("ADA amount {ada:?} is not a whole number of lovelace"));
    }
    let mantissa = scaled.normalize().mantissa();
    u64::try_from(mantissa)
        .map(Lovelace)
        .map_err(|_| format!("ADA amount {ada:?} does not fit in u64 lovelace"))
}

/// Native asset identity: 28-byte policy id + asset name bytes (both hex).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct AssetId {
    pub policy_hex: String,
    pub name_hex: String,
}

impl AssetId {
    pub fn new(policy_hex: &str, name_hex: &str) -> Result<AssetId, String> {
        let policy = hex::decode(policy_hex)
            .map_err(|e| format!("asset policy {policy_hex:?} is not hex: {e}"))?;
        if policy.len() != 28 {
            return Err(format!(
                "asset policy {policy_hex:?} must be 28 bytes, got {}",
                policy.len()
            ));
        }
        let name =
            hex::decode(name_hex).map_err(|e| format!("asset name {name_hex:?} is not hex: {e}"))?;
        if name.len() > 32 {
            return Err(format!("asset name {name_hex:?} exceeds 32 bytes"));
        }
        Ok(AssetId {
            policy_hex: policy_hex.to_ascii_lowercase(),
            name_hex: name_hex.to_ascii_lowercase(),
        })
    }
}

impl fmt::Display for AssetId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}", self.policy_hex, self.name_hex)
    }
}

/// Exact multi-asset bag (zero quantities are never stored).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AssetBag(pub BTreeMap<AssetId, u64>);

impl AssetBag {
    pub fn add(&mut self, id: AssetId, qty: u64) -> Result<(), String> {
        if qty == 0 {
            return Ok(());
        }
        let slot = self.0.entry(id.clone()).or_insert(0);
        *slot = slot
            .checked_add(qty)
            .ok_or_else(|| format!("asset quantity overflow for {id}"))?;
        Ok(())
    }

    pub fn merge(&mut self, other: &AssetBag) -> Result<(), String> {
        for (id, qty) in &other.0 {
            self.add(id.clone(), *qty)?;
        }
        Ok(())
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    pub fn describe(&self) -> String {
        if self.0.is_empty() {
            return "no tokens".to_string();
        }
        self.0
            .iter()
            .map(|(id, qty)| format!("{qty} x {id}"))
            .collect::<Vec<_>>()
            .join(", ")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ada_exact_conversion() {
        assert_eq!(ada_to_lovelace("2"), Ok(Lovelace(2_000_000)));
        assert_eq!(ada_to_lovelace("2.5"), Ok(Lovelace(2_500_000)));
        assert_eq!(ada_to_lovelace("0.000001"), Ok(Lovelace(1)));
        assert_eq!(ada_to_lovelace("1.100000"), Ok(Lovelace(1_100_000)));
    }

    #[test]
    fn ada_rejects_instead_of_rounding() {
        assert!(ada_to_lovelace("0.0000001").is_err());
        assert!(ada_to_lovelace("-1").is_err());
        assert!(ada_to_lovelace("1e3").is_err());
        assert!(ada_to_lovelace("abc").is_err());
        assert!(ada_to_lovelace("99999999999999999999").is_err());
    }

    #[test]
    fn lovelace_display_and_checked_math() {
        assert_eq!(Lovelace(2_000_000).to_string(), "2.000000 ADA");
        assert_eq!(Lovelace(171_573).to_string(), "0.171573 ADA");
        assert!(Lovelace(u64::MAX).checked_add(Lovelace(1)).is_err());
        assert!(Lovelace(0).checked_sub(Lovelace(1)).is_err());
    }

    #[test]
    fn percent_of_incoming_is_exact_or_refused() {
        assert_eq!(percent_of_quantity("5000000", "40").unwrap(), 2_000_000);
        assert_eq!(percent_of_quantity("1000000", "100").unwrap(), 1_000_000);
        assert!(percent_of_quantity("3", "1").is_err());
        assert!(percent_of_quantity("100", "0").is_err());
        assert!(percent_of_quantity("100", "100.1").is_err());
        assert!(percent_of_quantity("1.5", "40").is_err());
    }

    #[test]
    fn sum_empty_is_zero_and_overflow_is_an_error() {
        assert_eq!(Lovelace::sum([]).unwrap().0, 0);
        assert_eq!(Lovelace::sum([Lovelace(1), Lovelace(2)]).unwrap().0, 3);
        assert!(Lovelace::sum([Lovelace(u64::MAX), Lovelace(1)]).is_err());
    }

    #[test]
    fn compare_quantities_eq_lt_gt_and_rejects_non_integers() {
        assert!(super::compare_quantities("2", "eq", "2").unwrap());
        assert!(!super::compare_quantities("2", "eq", "3").unwrap());
        assert!(super::compare_quantities("1", "lt", "2").unwrap());
        assert!(super::compare_quantities("3", "gt", "2").unwrap());
        assert!(super::compare_quantities("2", "gte", "2").unwrap());
        assert!(super::compare_quantities("2", "lte", "2").unwrap());
        assert!(super::compare_quantities("1.5", "eq", "2").is_err());
        assert!(super::compare_quantities("-1", "lt", "2").is_err());
        assert!(super::compare_quantities("2", "ne", "2").is_err());
    }

    #[test]
    fn asset_id_validates_policy_length() {
        assert!(AssetId::new("00", "").is_err());
        assert!(AssetId::new(&"ab".repeat(28), "4141").is_ok());
    }
}
