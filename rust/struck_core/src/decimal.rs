//! Strike decimals (spec §6.3 `decimal.rs`). Strike serves every price, size,
//! rate and money value as a JSON string; Struck parses them EXACTLY into
//! `rust_decimal::Decimal` (never f64), does checked arithmetic only, and
//! rounds half-even to the market's `quote_prec` where the backend rounds
//! (`skills/strike-calculations/SKILL.md:12-13`).
//!
//! OPEN (spec Q7): the backend rounds every intermediate result to <=19
//! significant digits (govalues/decimal); this module does NOT emulate that,
//! so every value computed here is labelled an ESTIMATE by its callers.

use rust_decimal::Decimal;

/// Parse a Strike decimal string: optional '-', digits, optional '.digits'.
/// No exponent, no '+', no whitespace, no empty string (Strike rejects "").
pub fn parse(field: &str, s: &str) -> Result<Decimal, String> {
    let body = s.strip_prefix('-').unwrap_or(s);
    let mut parts = body.splitn(2, '.');
    let int = parts.next().unwrap_or("");
    let frac = parts.next();
    let digits_ok = |x: &str| !x.is_empty() && x.bytes().all(|b| b.is_ascii_digit());
    if !digits_ok(int) || frac.is_some_and(|f| !digits_ok(f)) {
        return Err(format!("{field}: {s:?} is not a decimal string"));
    }
    s.parse::<Decimal>().map_err(|e| format!("{field}: {s:?}: {e}"))
}

pub fn mul(a: Decimal, b: Decimal) -> Result<Decimal, String> {
    a.checked_mul(b).ok_or_else(|| format!("overflow: {a} * {b}"))
}

pub fn div(a: Decimal, b: Decimal) -> Result<Decimal, String> {
    if b.is_zero() {
        return Err(format!("division by zero: {a} / 0"));
    }
    a.checked_div(b).ok_or_else(|| format!("overflow: {a} / {b}"))
}

pub fn add(a: Decimal, b: Decimal) -> Result<Decimal, String> {
    a.checked_add(b).ok_or_else(|| format!("overflow: {a} + {b}"))
}

pub fn sub(a: Decimal, b: Decimal) -> Result<Decimal, String> {
    a.checked_sub(b).ok_or_else(|| format!("overflow: {a} - {b}"))
}

/// `value = origin + k * step` for an integer k >= 0 (tick and size grids).
pub fn on_grid(value: Decimal, origin: Decimal, step: Decimal) -> Result<bool, String> {
    if step <= Decimal::ZERO {
        return Err(format!("grid step must be > 0, got {step}"));
    }
    let offset = sub(value, origin)?;
    if offset < Decimal::ZERO {
        return Ok(false);
    }
    let rem = offset.checked_rem(step).ok_or_else(|| format!("overflow: {offset} % {step}"))?;
    Ok(rem.is_zero())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_is_strict() {
        assert_eq!(parse("x", "0.10").unwrap().to_string(), "0.10");
        assert_eq!(parse("x", "-5").unwrap().to_string(), "-5");
        for bad in ["", "1e5", "+1", " 1", ".5", "5.", "1.2.3", "abc"] {
            assert!(parse("x", bad).is_err(), "{bad:?} must be rejected");
        }
    }

    #[test]
    fn grid_and_rounding() {
        let d = |s: &str| parse("x", s).unwrap();
        assert!(on_grid(d("39.88"), d("39.86"), d("0.01")).unwrap());
        assert!(!on_grid(d("39.885"), d("39.86"), d("0.01")).unwrap());
        assert!(!on_grid(d("39.85"), d("39.86"), d("0.01")).unwrap());
    }
}

/// Exact threshold comparison for trigger conditions on served decimals
/// (spec §3.3 "trigger thresholds on served values (exact decimal
/// comparisons)"). Ops are the ).
pub fn compare(value: &str, op: &str, threshold: &str) -> Result<bool, String> {
    let a = parse("value", value)?;
    let b = parse("threshold", threshold)?;
    match op {
        "gt" => Ok(a > b),
        "lt" => Ok(a < b),
        "gte" => Ok(a >= b),
        "lte" => Ok(a <= b),
        other => Err(format!("unknown op {other:?} (gt|lt|gte|lte)")),
    }
}

#[cfg(test)]
mod compare_tests {
    #[test]
    fn exact() {
        assert!(super::compare("0.30000000000000000001", "gt", "0.3").unwrap());
        assert!(super::compare("2.50", "gte", "2.5").unwrap());
        assert!(!super::compare("2.50", "gt", "2.5").unwrap());
        assert!(super::compare("1", "eq", "1").is_err());
    }
}
