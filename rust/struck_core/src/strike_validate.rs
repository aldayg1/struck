//! N2 for the Strike lane (spec §5 N2, §6.3 `strike_validate.rs`): every
//! approved leg is checked, rule by rule, against the market rules served by
//! a FRESH `/v2/markets` (`skills/strike-account/SKILL.md:85-91`,
//! `skills/strike-calculations/SKILL.md:41-43`).
//!
//! Rules that need the account (positions, available balance, symbol
//! leverage: API-wallet reads, spec B0 not done) are NOT evaluated; they are
//! listed in `not_evaluated` so the operator sees exactly what was skipped.

use rust_decimal::Decimal;
use serde::Serialize;

use crate::decimal::{self, add, div, mul, on_grid, sub};
use crate::markets::{FeeTier, Market, Markets};
use crate::report::Report;
use crate::strike_plan::{client_id_ok, grid_bot_id_ok, path_segment_ok, ExitSpec, OrderSpec, StrikeLeg};

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct LegValidation {
    pub report: Report,
    pub not_evaluated: Vec<String>,
}

const ORDER_TYPES: [&str; 7] =
    ["limit", "market", "stop", "stop_limit", "take_profit", "take_profit_limit", "trailing_stop_market"];

fn limit_style(t: &str) -> bool {
    matches!(t, "limit" | "stop_limit" | "take_profit_limit")
}

fn needs_price(t: &str) -> bool {
    limit_style(t)
}

fn needs_stop(t: &str) -> bool {
    matches!(t, "stop" | "stop_limit" | "take_profit" | "take_profit_limit")
}

fn opt(s: &Option<String>) -> String {
    s.clone().unwrap_or_else(|| "none".to_string())
}

/// ``fee`` is the account's effective fee tier when known; ``None`` means the
/// fee-dependent rules (grid spacing vs fees) land in ``not_evaluated``.
pub fn validate_leg(
    leg: &StrikeLeg,
    markets: &Markets,
    fee: Option<&FeeTier>,
) -> Result<LegValidation, String> {
    let mut r = Report::new();
    let mut skipped = Vec::new();
    if let StrikeLeg::TwapCancel { strategy_id } = leg {
        r.push(
            "twap_cancel.strategy_id",
            "one URL path segment".into(),
            strategy_id.clone(),
            path_segment_ok(strategy_id),
        );
        skipped.push("twap_cancel.exists (needs GET /v2/algo/twap/{id}: API wallet)".into());
        return Ok(LegValidation { report: r, not_evaluated: skipped });
    }
    if leg.symbol().is_empty()
        && !matches!(
            leg,
            StrikeLeg::GridStop { .. } | StrikeLeg::Batch { .. } | StrikeLeg::ReplaceBatch { .. }
        )
    {
        r.push("venue.shape", "checked when the leg was parsed".into(), "ok".into(), true);
        skipped.push("served minima and account balance (API wallet reads)".into());
        return Ok(LegValidation { report: r, not_evaluated: skipped });
    }
    if let StrikeLeg::Batch { orders, .. } = leg {
        r.push("batch.count", ">= 1".into(), orders.len().to_string(), !orders.is_empty());
        for order in orders {
            match markets.markets.get(&order.symbol) {
                Some(m) => check_order(&mut r, &mut skipped, m, order)?,
                None => r.push(
                    "market.exists",
                    "listed in /v2/markets".into(),
                    format!("{} not listed", order.symbol),
                    false,
                ),
            }
        }
        return Ok(LegValidation { report: r, not_evaluated: skipped });
    }
    if let StrikeLeg::ReplaceBatch { replacements, .. } = leg {
        r.push(
            "replace_batch.count",
            ">= 1".into(),
            replacements.len().to_string(),
            !replacements.is_empty(),
        );
        for item in replacements {
            r.push(
                "replace.order_id",
                "one URL path segment".into(),
                item.order_id.clone(),
                path_segment_ok(&item.order_id),
            );
            match markets.markets.get(&item.order.symbol) {
                Some(m) => check_order(&mut r, &mut skipped, m, &item.order)?,
                None => r.push(
                    "market.exists",
                    "listed in /v2/markets".into(),
                    format!("{} not listed", item.order.symbol),
                    false,
                ),
            }
        }
        skipped.push("replace.order_exists (needs GET /v2/order: API wallet)".into());
        return Ok(LegValidation { report: r, not_evaluated: skipped });
    }
    if let StrikeLeg::GridStop { bot_id, .. } = leg {
        r.push(
            "grid_stop.bot_id",
            "served id: grid-..., letters, digits, '-' and '_'".into(),
            bot_id.clone(),
            grid_bot_id_ok(bot_id),
        );
        skipped.push("grid_stop.bot_exists (needs GET /v2/grid/bot/{bot_id}: FlexibleAuth read)".into());
        return Ok(LegValidation { report: r, not_evaluated: skipped });
    }
    let symbol = leg.symbol();
    let Some(m) = markets.markets.get(symbol) else {
        r.push("market.exists", "listed in /v2/markets".into(), format!("{symbol} not listed"), false);
        return Ok(LegValidation { report: r, not_evaluated: skipped });
    };
    r.push("market.exists", "listed in /v2/markets".into(), symbol.to_string(), true);
    match leg {
        StrikeLeg::Order { order } => {
            check_order(&mut r, &mut skipped, m, order)?;
        }
        StrikeLeg::Bracket { order, tp, sl } => {
            check_order(&mut r, &mut skipped, m, order)?;
            r.push(
                "bracket.has_exit",
                "tp or sl present".into(),
                format!("tp={} sl={}", tp.is_some(), sl.is_some()),
                tp.is_some() || sl.is_some(),
            );
            if let Some(t) = tp {
                check_exit(&mut r, m, "tp", t, &["take_profit", "take_profit_limit"])?;
            }
            if let Some(s) = sl {
                check_exit(&mut r, m, "sl", s, &["stop", "stop_limit"])?;
            }
            if let (Some(t), Some(s)) = (tp, sl) {
                // openapi/trade-api.yaml:1846: buy primary needs TP >= SL, sell TP <= SL
                let tps = decimal::parse("tp.stop_price", &t.stop_price)?;
                let sls = decimal::parse("sl.stop_price", &s.stop_price)?;
                let ok = if order.side == "buy" { tps >= sls } else { tps <= sls };
                r.push(
                    "bracket.tp_vs_sl",
                    format!("{} primary: tp {} sl", order.side, if order.side == "buy" { ">=" } else { "<=" }),
                    format!("tp={tps} sl={sls}"),
                    ok,
                );
            }
        }
        StrikeLeg::MarginMode { margin_mode, .. } => {
            let known = margin_mode == "cross" || margin_mode == "isolated";
            r.push(
                "margin_mode.value",
                "cross|isolated".into(),
                margin_mode.clone(),
                known,
            );
            skipped.push(
                "margin_mode.position_and_orders (needs /v2/positions and /v2/openOrders: API wallet)".into(),
            );
        }
        StrikeLeg::Leverage { leverage, .. } => {
            let max = m.max_leverage();
            r.push("leverage.range", format!("1..={max} (margin_tiers[0])"), leverage.to_string(), *leverage >= 1 && *leverage <= max);
            skipped.push("leverage.position_fits_tier (needs /v2/positions: API wallet, spec B0)".into());
        }
        StrikeLeg::Twap {
            side,
            total_size,
            duration_sec,
            limit_price,
            reduce_only,
            ..
        } => {
            check_twap(&mut r, &mut skipped, m, side, total_size, *duration_sec, limit_price, *reduce_only)?;
        }
        StrikeLeg::GridBot {
            mode,
            lower_price,
            upper_price,
            grid_count,
            collateral,
            sl_price,
            long_sl_price,
            short_sl_price,
            ..
        } => {
            check_grid(
                &mut r, &mut skipped, m, fee, mode, lower_price, upper_price, *grid_count,
                collateral, sl_price, long_sl_price, short_sl_price,
            )?;
        }
        StrikeLeg::Cancel { client_order_id, .. } => {
            r.push(
                "market.status",
                "trading|break (cancels allowed)".into(),
                m.status.clone(),
                m.status == "trading" || m.status == "break",
            );
            r.push("cancel.client_order_id", "1..=256 chars, no reserved prefix".into(), client_order_id.clone(), client_id_ok(client_order_id));
            skipped.push("cancel.order_exists (needs GET /v2/order: API wallet, spec B0)".into());
        }
        StrikeLeg::CancelAll { .. } => {
            r.push(
                "market.status",
                "trading|break (cancels allowed)".into(),
                m.status.clone(),
                m.status == "trading" || m.status == "break",
            );
        }
        StrikeLeg::IsoMargin { amount, modify_type, .. } => {
            let known = modify_type == "add" || modify_type == "remove";
            r.push("iso_margin.modify_type", "add|remove".into(), modify_type.clone(), known);
            let n = decimal::parse("amount", amount)?;
            r.push("iso_margin.amount.positive", "> 0".into(), n.to_string(), n > Decimal::ZERO);
            skipped.push("iso_margin.balance (needs GET /v2/account: API wallet)".into());
        }
        StrikeLeg::Replace { order_id, order } => {
            r.push(
                "replace.order_id",
                "one URL path segment".into(),
                order_id.clone(),
                path_segment_ok(order_id),
            );
            check_order(&mut r, &mut skipped, m, order)?;
            skipped.push("replace.order_exists (needs GET /v2/order: API wallet)".into());
        }
        StrikeLeg::Rfq { requested_size, .. } => {
            let size = decimal::parse("requested_size", requested_size)?;
            r.push("rfq.size.positive", "> 0".into(), size.to_string(), size > Decimal::ZERO);
            let frac = requested_size.split_once('.').map(|(_, fr)| fr.len()).unwrap_or(0) as u32;
            r.push(
                "rfq.base_prec",
                format!("<= {} decimal places", m.base_prec),
                frac.to_string(),
                frac <= m.base_prec,
            );
            let max_ok = m.order_limit_max_size.is_zero() || size <= m.order_limit_max_size;
            r.push(
                "rfq.max_size",
                format!("<= {} (0 = none)", m.order_limit_max_size),
                size.to_string(),
                max_ok,
            );
            skipped.push("rfq.quoter_allowlist (Strike's server list)".into());
        }
        StrikeLeg::TwapCancel { .. }
        | StrikeLeg::Batch { .. }
        | StrikeLeg::ReplaceBatch { .. }
        | StrikeLeg::GridStop { .. }
        | StrikeLeg::CopySubscribe { .. }
        | StrikeLeg::CopyPatch { .. }
        | StrikeLeg::CopyStop { .. }
        | StrikeLeg::CopySave { .. }
        | StrikeLeg::CopyUnsave { .. }
        | StrikeLeg::RfqClose { .. }
        | StrikeLeg::RfqResponse { .. }
        | StrikeLeg::WithdrawQuote { .. }
        | StrikeLeg::Withdraw { .. }
        | StrikeLeg::VaultDeposit { .. }
        | StrikeLeg::VaultWithdraw { .. }
        | StrikeLeg::VaultWithdrawCancel { .. }
        | StrikeLeg::VaultCreate { .. }
        | StrikeLeg::VaultPause { .. }
        | StrikeLeg::VaultResume { .. } => unreachable!("handled before the market lookup"),
    }
    Ok(LegValidation { report: r, not_evaluated: skipped })
}

fn check_price_grid(r: &mut Report, m: &Market, label: &str, p: Decimal) -> Result<(), String> {
    r.push(
        &format!("{label}.tick_grid"),
        format!("{} + k*{}", m.order_min_price, m.order_tick_price),
        p.to_string(),
        on_grid(p, m.order_min_price, m.order_tick_price)?,
    );
    r.push(&format!("{label}.min"), format!(">= {}", m.order_min_price), p.to_string(), p >= m.order_min_price);
    let max_ok = m.order_max_price.is_zero() || p <= m.order_max_price;
    r.push(&format!("{label}.max"), format!("<= {} (0 = none)", m.order_max_price), p.to_string(), max_ok);
    Ok(())
}

fn check_order(r: &mut Report, skipped: &mut Vec<String>, m: &Market, o: &OrderSpec) -> Result<(), String> {
    r.push("market.status", "trading".into(), m.status.clone(), m.status == "trading");
    r.push(
        "market.reduce_only",
        "market reduce_only => leg reduce_only".into(),
        format!("market={} leg={}", m.reduce_only, o.reduce_only),
        !m.reduce_only || o.reduce_only,
    );
    r.push("order.side", "buy|sell".into(), o.side.clone(), o.side == "buy" || o.side == "sell");
    let known = ORDER_TYPES.contains(&o.order_type.as_str());
    r.push("order.type", ORDER_TYPES.join("|"), o.order_type.clone(), known);
    if !known {
        return Ok(());
    }
    let t = o.order_type.as_str();
    if let Some(tif) = &o.time_in_force {
        r.push("order.time_in_force", "GTC|IOC|FOK".into(), tif.clone(), matches!(tif.as_str(), "GTC" | "IOC" | "FOK"));
    }
    r.push("order.price_present", format!("required={}", needs_price(t)), opt(&o.price), o.price.is_some() == needs_price(t));
    r.push("order.stop_price_present", format!("required={}", needs_stop(t)), opt(&o.stop_price), o.stop_price.is_some() == needs_stop(t));
    let trailing = t == "trailing_stop_market";
    r.push("order.callback_rate_present", format!("required={trailing}"), opt(&o.callback_rate), o.callback_rate.is_some() == trailing);
    if trailing {
        r.push("order.trailing_reduce_only", "true".into(), o.reduce_only.to_string(), o.reduce_only);
    } else {
        r.push("order.activation_price_absent", "none (trailing only)".into(), opt(&o.activation_price), o.activation_price.is_none());
    }
    let size = decimal::parse("size", &o.size)?;
    r.push("size.positive", "> 0".into(), size.to_string(), size > Decimal::ZERO);
    r.push("size.min", format!(">= {} (order_market_min_size)", m.order_market_min_size), size.to_string(), size >= m.order_market_min_size);
    let (origin, step, max, style) = if limit_style(t) {
        (m.order_limit_min_size, m.order_limit_step_size, m.order_limit_max_size, "limit")
    } else {
        (m.order_market_min_size, m.order_market_step_size, m.order_market_max_size, "market")
    };
    r.push(&format!("size.{style}_grid"), format!("{origin} + k*{step}"), size.to_string(), on_grid(size, origin, step)?);
    r.push(&format!("size.{style}_max"), format!("<= {max} (0 rejects all)"), size.to_string(), size <= max && !max.is_zero());

    let mut reference = m.mark_price;
    let mut ref_name = "mark_price";
    if let Some(ps) = &o.price {
        let p = decimal::parse("price", ps)?;
        check_price_grid(r, m, "price", p)?;
        let band = mul(m.order_limit_price_bound, m.mark_price)?;
        let dev = sub(p, m.mark_price)?.abs();
        r.push(
            "price.band",
            format!("|price - mark| <= {} * {} = {band}", m.order_limit_price_bound, m.mark_price),
            dev.to_string(),
            dev <= band,
        );
        reference = p;
        ref_name = "price";
    } else {
        skipped.push("price.band for market-style execution (execution price unknown before the fill)".into());
    }
    if let Some(sp) = &o.stop_price {
        let s = decimal::parse("stop_price", sp)?;
        r.push("stop_price.positive", "> 0".into(), s.to_string(), s > Decimal::ZERO);
        check_price_grid(r, m, "stop_price", s)?;
    }
    if let Some(cr) = &o.callback_rate {
        let c = decimal::parse("callback_rate", cr)?;
        let lo: Decimal = Decimal::new(1, 1);
        let hi: Decimal = Decimal::from(5);
        r.push("callback_rate.range", "0.1..=5 (percent)".into(), c.to_string(), c >= lo && c <= hi);
    }
    if let Some(wt) = &o.working_type {
        r.push(
            "order.working_type",
            "mark_price|contract_price".into(),
            wt.clone(),
            wt == "mark_price" || wt == "contract_price",
        );
    }
    if let Some(sl) = &o.slippage {
        let s = decimal::parse("slippage", sl)?;
        r.push("order.slippage.positive", "> 0".into(), s.to_string(), s > Decimal::ZERO);
    }
    if let Some(ap) = &o.activation_price {
        let a = decimal::parse("activation_price", ap)?;
        check_price_grid(r, m, "activation_price", a)?;
    }
    let notional = mul(reference, size)?;
    if o.reduce_only {
        skipped.push("notional.min (not applied to reduce-only orders)".into());
    } else {
        r.push(
            "notional.min",
            format!(">= {} ({ref_name} * size)", m.order_min_notional),
            notional.to_string(),
            notional >= m.order_min_notional,
        );
    }
    if let Some(lev) = o.leverage {
        let max = m.max_leverage();
        let in_range = lev >= 1 && lev <= max;
        r.push("leverage.range", format!("1..={max} (margin_tiers[0])"), lev.to_string(), in_range);
        if in_range {
            let tier = m.tier_index_by_leverage(lev)?;
            let cap = m.margin_tiers[tier].max_notional;
            let mark_notional = mul(m.mark_price, size)?;
            r.push(
                "leverage.tier_max_notional(order_only)",
                format!("mark*size <= {cap} (tier {tier})"),
                mark_notional.to_string(),
                o.reduce_only || mark_notional <= cap,
            );
        }
    }
    skipped.push("position.post_order_notional (needs /v2/positions: API wallet, spec B0)".into());
    skipped.push("admission.extra_vs_available_balance (needs /v2/account: API wallet, spec B0)".into());
    skipped.push("operator_risk_limits (Struck config values not set by the operator)".into());
    Ok(())
}

fn check_twap(
    r: &mut Report,
    skipped: &mut Vec<String>,
    m: &Market,
    side: &str,
    total_size: &str,
    duration_sec: u32,
    limit_price: &Option<String>,
    reduce_only: bool,
) -> Result<(), String> {
    // skills/strike-trading/SKILL.md Create TWAP: side BUY|SELL, duration
    // 300..=86400, slices clamp(duration/30, 5, 120), each slice meets min
    // size and min notional. total_size must already sit on the market step
    // so Strike's round-down does not change the approved size.
    r.push("market.status", "trading".into(), m.status.clone(), m.status == "trading");
    r.push(
        "market.reduce_only",
        "market reduce_only => leg reduce_only".into(),
        format!("market={} leg={reduce_only}", m.reduce_only),
        !m.reduce_only || reduce_only,
    );
    r.push("twap.side", "BUY|SELL".into(), side.to_string(), side == "BUY" || side == "SELL");
    r.push(
        "twap.duration_sec",
        "300..=86400".into(),
        duration_sec.to_string(),
        (300..=86_400).contains(&duration_sec),
    );
    let size = decimal::parse("size", total_size)?;
    r.push("size.positive", "> 0".into(), size.to_string(), size > Decimal::ZERO);
    r.push(
        "size.market_grid",
        format!("{} + k*{}", m.order_market_min_size, m.order_market_step_size),
        size.to_string(),
        on_grid(size, m.order_market_min_size, m.order_market_step_size)?,
    );
    let slices = (duration_sec / 30).clamp(5, 120);
    let slice = div(size, Decimal::from(slices))?;
    r.push(
        "twap.slice_size",
        format!(">= {} (order_market_min_size), slices={slices}", m.order_market_min_size),
        slice.to_string(),
        slice >= m.order_market_min_size && on_grid(slice, m.order_market_min_size, m.order_market_step_size)?,
    );
    let mut reference = m.mark_price;
    let mut ref_name = "mark_price";
    if let Some(px) = limit_price {
        let p = decimal::parse("price", px)?;
        r.push("twap.limit_price.positive", "> 0".into(), p.to_string(), p > Decimal::ZERO);
        check_price_grid(r, m, "twap.limit_price", p)?;
        reference = p;
        ref_name = "limit_price";
    }
    if reduce_only {
        skipped.push("twap.slice_notional (not applied to reduce-only)".into());
    } else {
        let notional = mul(reference, slice)?;
        r.push(
            "twap.slice_notional",
            format!(">= {} ({ref_name} * slice)", m.order_min_notional),
            notional.to_string(),
            notional >= m.order_min_notional,
        );
    }
    skipped.push("twap.concurrent_limit (needs GET /v2/algo/twap: API wallet)".into());
    skipped.push("position.post_order_notional (needs /v2/positions: API wallet, spec B0)".into());
    skipped.push("admission.extra_vs_available_balance (needs /v2/account: API wallet, spec B0)".into());
    Ok(())
}

fn check_exit(r: &mut Report, m: &Market, label: &str, e: &ExitSpec, types: &[&str]) -> Result<(), String> {
    r.push(&format!("{label}.type"), types.join("|"), e.order_type.clone(), types.contains(&e.order_type.as_str()));
    let s = decimal::parse(&format!("{label}.stop_price"), &e.stop_price)?;
    r.push(&format!("{label}.stop_price.positive"), "> 0".into(), s.to_string(), s > Decimal::ZERO);
    check_price_grid(r, m, &format!("{label}.stop_price"), s)?;
    let needs = e.order_type.ends_with("_limit");
    r.push(&format!("{label}.price_present"), format!("required={needs}"), opt(&e.price), e.price.is_some() == needs);
    if let Some(ps) = &e.price {
        let p = decimal::parse(&format!("{label}.price"), ps)?;
        check_price_grid(r, m, &format!("{label}.price"), p)?;
    }
    Ok(())
}

/// Grid create rules the grid service itself enforces
/// (`skills/strike-grid-bot/SKILL.md` Create + validation): the market-level
/// ones are checked here against the served rows; sizing against the account
/// (leverage snapshot, balance) needs the API wallet and is not_evaluated.
#[allow(clippy::too_many_arguments)]
fn check_grid(
    r: &mut Report,
    skipped: &mut Vec<String>,
    m: &Market,
    fee: Option<&FeeTier>,
    mode: &str,
    lower_price: &str,
    upper_price: &str,
    grid_count: u32,
    collateral: &str,
    sl_price: &Option<String>,
    long_sl_price: &Option<String>,
    short_sl_price: &Option<String>,
) -> Result<(), String> {
    r.push("market.status", "trading".into(), m.status.clone(), m.status == "trading");
    let lower = decimal::parse("lower_price", lower_price)?;
    let upper = decimal::parse("upper_price", upper_price)?;
    let col = decimal::parse("collateral", collateral)?;
    r.push("grid.lower_price.positive", "> 0".into(), lower.to_string(), lower > Decimal::ZERO);
    r.push("grid.upper_price.positive", "> 0".into(), upper.to_string(), upper > Decimal::ZERO);
    r.push("grid.upper_above_lower", "upper > lower".into(), format!("{upper} > {lower}"), upper > lower);
    r.push("grid.grid_count.range", "2..=200".into(), grid_count.to_string(), (2..=200).contains(&grid_count));
    r.push("grid.collateral.positive", "> 0".into(), col.to_string(), col > Decimal::ZERO);
    r.push(
        "grid.mark_strictly_inside",
        format!("{} < mark < {}", lower, upper),
        m.mark_price.to_string(),
        m.mark_price > lower && m.mark_price < upper,
    );
    // SL ordering rules the service 400s on (exact messages in the skill).
    if mode == "LONG" {
        if let Some(s) = sl_price {
            let sl = decimal::parse("sl_price", s)?;
            r.push("grid.sl_below_lower", "sl_price < lower_price (LONG)".into(), format!("{sl} < {lower}"), sl < lower);
        }
    } else if mode == "SHORT" {
        if let Some(s) = sl_price {
            let sl = decimal::parse("sl_price", s)?;
            r.push("grid.sl_above_upper", "sl_price > upper_price (SHORT)".into(), format!("{sl} > {upper}"), sl > upper);
        }
    } else {
        if let Some(s) = long_sl_price {
            let sl = decimal::parse("long_sl_price", s)?;
            r.push("grid.long_sl_below_lower", "long_sl_price < lower_price (NEUTRAL)".into(), format!("{sl} < {lower}"), sl < lower);
        }
        if let Some(s) = short_sl_price {
            let sl = decimal::parse("short_sl_price", s)?;
            r.push("grid.short_sl_above_upper", "short_sl_price > upper_price (NEUTRAL)".into(), format!("{sl} > {upper}"), sl > upper);
        }
    }
    // spacing > mid * (maker + taker) with the effective fee tier.
    let spacing = div(sub(upper, lower)?, Decimal::from(grid_count))?;
    let mid = div(add(upper, lower)?, Decimal::from(2))?;
    match fee {
        Some(f) => {
            let floor = mul(mid, add(f.maker_rate, f.taker_rate)?)?;
            r.push(
                "grid.spacing_vs_fees",
                format!("spacing {spacing} > mid*(maker+taker) = {floor} (tier {})", f.tier),
                spacing.to_string(),
                spacing > floor,
            );
        }
        None => skipped.push("grid.spacing_vs_fees (needs the account fee tier: API wallet)".into()),
    }
    skipped.push("grid.size_per_grid (leverage is the account's symbol snapshot: API wallet; the service re-checks sizing)".into());
    skipped.push("admission.extra_vs_available_balance (needs /v2/account: API wallet, spec B0)".into());
    Ok(())
}
