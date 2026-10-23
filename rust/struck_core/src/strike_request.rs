//! N4s / N5s / N6s for the Strike lane (spec §5):
//! * `build_request`  — the EXACT method, path and JSON body for one approved
//!   leg (field names from `openapi/trade-api.yaml` CreateOrderRequest :1308,
//!   CreateStrategyOrderRequest :1846, LeverageRequest :1924; cancel
//!   `DELETE /v2/order/cancel` body `skills/strike-trading/SKILL.md:211`). Decimals are the approved strings,
//!   verbatim. Absent optionals are omitted (never `""`, which Strike 400s).
//! * `congruence`     — the request body that would be signed == the body
//!   rebuilt from the APPROVED leg, plus the leg re-validated against a FRESH
//!   `/v2/markets`.
//! * `explain`        — plain-words effects from the request + estimates.
//!
//! Nothing here signs or sends; the Struck API-wallet keyring is spec B0/N8s.

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::markets::{FeeTier, Markets};
use crate::plan::NetParams;
use crate::report::Report;
use crate::strike_plan::{client_id_ok, ExitSpec, FlatSymbolSetting, OrderSpec, StrikeLeg};
use crate::strike_validate::{validate_leg, LegValidation};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ApiRequest {
    pub method: String,
    pub path: String,
    /// The exact body bytes (compact JSON) the signature would cover.
    pub body: String,
    pub client_order_id: Option<String>,
}

fn put(map: &mut Map<String, Value>, key: &str, v: &Option<String>) {
    if let Some(s) = v {
        map.insert(key.to_string(), Value::String(s.clone()));
    }
}

fn order_body(o: &OrderSpec, coid: &str) -> Map<String, Value> {
    let mut b = Map::new();
    b.insert("client_order_id".into(), json!(coid));
    b.insert("symbol".into(), json!(o.symbol));
    b.insert("side".into(), json!(o.side));
    b.insert("type".into(), json!(o.order_type));
    b.insert("size".into(), json!(o.size));
    put(&mut b, "price", &o.price);
    put(&mut b, "stop_price", &o.stop_price);
    put(&mut b, "time_in_force", &o.time_in_force);
    if o.post_only {
        b.insert("post_only".into(), json!(true));
    }
    if o.reduce_only {
        b.insert("reduce_only".into(), json!(true));
    }
    put(&mut b, "callback_rate", &o.callback_rate);
    put(&mut b, "activation_price", &o.activation_price);
    put(&mut b, "working_type", &o.working_type);
    put(&mut b, "slippage", &o.slippage);
    put(&mut b, "vault_id", &o.vault_id);
    if o.close_position {
        b.insert("close_position".into(), json!(true));
    }
    if o.price_protect {
        b.insert("price_protect".into(), json!(true));
    }
    b
}

fn exit_body(e: &ExitSpec, coid: &str, size: &str) -> Value {
    let mut b = Map::new();
    b.insert("client_order_id".into(), json!(coid));
    b.insert("type".into(), json!(e.order_type));
    b.insert("size".into(), json!(size));
    b.insert("stop_price".into(), json!(e.stop_price));
    put(&mut b, "price", &e.price);
    put(&mut b, "time_in_force", &e.time_in_force);
    put(&mut b, "working_type", &e.working_type);
    if e.post_only {
        b.insert("post_only".into(), json!(true));
    }
    if e.price_protect {
        b.insert("price_protect".into(), json!(true));
    }
    Value::Object(b)
}

fn with_vault(mut body: Map<String, Value>, vault_id: &Option<String>) -> Map<String, Value> {
    put(&mut body, "vault_id", vault_id);
    body
}

fn with_query(path: &str, key: &str, value: &Option<String>) -> String {
    match value {
        Some(v) => format!("{path}?{key}={v}"),
        None => path.to_string(),
    }
}

fn margin_settings(
    mode: &Option<String>,
    leverage: &Option<u32>,
    rows: &[FlatSymbolSetting],
) -> Option<Value> {
    if mode.is_none() && leverage.is_none() && rows.is_empty() {
        return None;
    }
    let mut ms = Map::new();
    put(&mut ms, "default_margin_mode", mode);
    if let Some(l) = leverage {
        ms.insert("default_leverage".into(), json!(l));
    }
    if !rows.is_empty() {
        let mut symbols = Map::new();
        for row in rows {
            symbols.insert(
                row.symbol.clone(),
                json!({"margin_mode": row.margin_mode, "leverage": row.leverage}),
            );
        }
        ms.insert("symbol_settings".into(), Value::Object(symbols));
    }
    Some(Value::Object(ms))
}

fn expected_body(leg: &StrikeLeg, coid: &str) -> (String, String, Value) {
    match leg {
        StrikeLeg::Order { order } => ("POST".into(), "/v2/order".into(), Value::Object(order_body(order, coid))),
        StrikeLeg::Bracket { order, tp, sl } => {
            let mut b = order_body(order, coid);
            b.insert("strategy_id".into(), json!(format!("{coid}-s")));
            if let Some(t) = tp {
                b.insert("tp_order".into(), exit_body(t, &format!("{coid}-tp"), &order.size));
            }
            if let Some(s) = sl {
                b.insert("sl_order".into(), exit_body(s, &format!("{coid}-sl"), &order.size));
            }
            ("POST".into(), "/v2/order/strategy".into(), Value::Object(b))
        }
        StrikeLeg::Leverage { symbol, leverage, vault_id } => {
            let mut b = Map::new();
            b.insert("symbol".into(), json!(symbol));
            b.insert("leverage".into(), json!(leverage));
            ("POST".into(), "/v2/leverage".into(), Value::Object(with_vault(b, vault_id)))
        }
        StrikeLeg::MarginMode { symbol, margin_mode, vault_id } => {
            let mut b = Map::new();
            b.insert("symbol".into(), json!(symbol));
            b.insert("marginMode".into(), json!(margin_mode));
            ("POST".into(), "/v2/marginMode".into(), Value::Object(with_vault(b, vault_id)))
        }
        StrikeLeg::GridBot {
            symbol,
            mode,
            initialization_mode,
            lower_price,
            upper_price,
            grid_count,
            collateral,
            sl_price,
            long_sl_price,
            short_sl_price,
        } => {
            // skills/strike-grid-bot/SKILL.md Create: decimals as the approved
            // strings, grid_count a number, absent optionals omitted.
            let mut b = Map::new();
            b.insert("symbol".into(), json!(symbol));
            b.insert("mode".into(), json!(mode));
            put(&mut b, "initialization_mode", initialization_mode);
            b.insert("lower_price".into(), json!(lower_price));
            b.insert("upper_price".into(), json!(upper_price));
            b.insert("grid_count".into(), json!(grid_count));
            b.insert("collateral".into(), json!(collateral));
            put(&mut b, "sl_price", sl_price);
            put(&mut b, "long_sl_price", long_sl_price);
            put(&mut b, "short_sl_price", short_sl_price);
            ("POST".into(), "/v2/grid/bot".into(), Value::Object(b))
        }
        StrikeLeg::GridStop { bot_id, cancel_orders } => {
            let mut b = Map::new();
            if *cancel_orders {
                b.insert("cancel_orders".into(), json!(true));
            }
            ("DELETE".into(), format!("/v2/grid/bot/{bot_id}"), Value::Object(b))
        }
        StrikeLeg::Cancel { symbol, client_order_id, vault_id } => {
            let mut b = Map::new();
            b.insert("symbol".into(), json!(symbol));
            b.insert("client_order_id".into(), json!(client_order_id));
            ("DELETE".into(), "/v2/order/cancel".into(), Value::Object(with_vault(b, vault_id)))
        }
        StrikeLeg::CancelAll { symbol, vault_id } => {
            let mut b = Map::new();
            b.insert("symbol".into(), json!(symbol));
            ("DELETE".into(), "/v2/order/cancel-all".into(), Value::Object(with_vault(b, vault_id)))
        }
        StrikeLeg::IsoMargin { symbol, amount, modify_type, vault_id } => {
            let mut b = Map::new();
            b.insert("symbol".into(), json!(symbol));
            b.insert("amount".into(), json!(amount));
            b.insert("modify_type".into(), json!(modify_type));
            ("POST".into(), "/v2/isoMargin".into(), Value::Object(with_vault(b, vault_id)))
        }
        StrikeLeg::TwapCancel { strategy_id } => (
            "DELETE".into(),
            format!("/v2/algo/twap/{strategy_id}"),
            Value::Object(Map::new()),
        ),
        StrikeLeg::Replace { order_id, order } => {
            let mut new_order = order_body(order, coid);
            let vault = new_order.remove("vault_id");
            let mut b = Map::new();
            b.insert(
                "cancel".into(),
                json!({"order_id": order_id, "symbol": order.symbol}),
            );
            b.insert("new_order".into(), Value::Object(new_order));
            if let Some(v) = vault {
                b.insert("vault_id".into(), v);
            }
            ("POST".into(), "/v2/order/replace".into(), Value::Object(b))
        }
        StrikeLeg::Batch { orders, vault_id } => {
            let built: Vec<Value> = orders
                .iter()
                .enumerate()
                .map(|(i, o)| Value::Object(order_body(o, &format!("{coid}-{i}"))))
                .collect();
            let mut b = Map::new();
            b.insert("orders".into(), Value::Array(built));
            ("POST".into(), "/v2/orders/batch".into(), Value::Object(with_vault(b, vault_id)))
        }
        StrikeLeg::ReplaceBatch { replacements, vault_id } => {
            let built: Vec<Value> = replacements
                .iter()
                .enumerate()
                .map(|(i, item)| {
                    let mut new_order = order_body(&item.order, &format!("{coid}-{i}"));
                    new_order.remove("vault_id");
                    json!({
                        "cancel": {"order_id": item.order_id, "symbol": item.order.symbol},
                        "new_order": Value::Object(new_order),
                    })
                })
                .collect();
            let mut b = Map::new();
            b.insert("replacements".into(), Value::Array(built));
            (
                "POST".into(),
                "/v2/order/replace-batch".into(),
                Value::Object(with_vault(b, vault_id)),
            )
        }
        StrikeLeg::CopySubscribe {
            lead_account_id,
            platform,
            copy_mode,
            copy_amount,
            margin_per_entry_order,
            ratio_multiplier,
            minimum_entry,
            max_margin_per_symbol,
            max_margin_total,
            excluded_symbols,
            stop_loss_amount,
            sub_account_id,
            copy_existing_positions,
            auto_create_sub_account,
            request_id,
            source_account_id,
            funding_amount,
            margin_mode_source,
            leverage_source,
            max_leverage,
            settings_margin_mode,
            settings_leverage,
            symbol_settings,
        } => {
            let mut b = Map::new();
            b.insert("lead_account_id".into(), json!(lead_account_id));
            b.insert("copy_mode".into(), json!(copy_mode));
            put(&mut b, "platform", platform);
            put(&mut b, "copy_amount", copy_amount);
            put(&mut b, "margin_per_entry_order", margin_per_entry_order);
            put(&mut b, "ratio_multiplier", ratio_multiplier);
            put(&mut b, "minimum_entry", minimum_entry);
            put(&mut b, "max_margin_per_symbol", max_margin_per_symbol);
            put(&mut b, "max_margin_total", max_margin_total);
            put(&mut b, "stop_loss_amount", stop_loss_amount);
            put(&mut b, "sub_account_id", sub_account_id);
            put(&mut b, "request_id", request_id);
            put(&mut b, "source_account_id", source_account_id);
            put(&mut b, "funding_amount", funding_amount);
            put(&mut b, "margin_mode_source", margin_mode_source);
            put(&mut b, "leverage_source", leverage_source);
            if let Some(l) = max_leverage {
                b.insert("max_leverage".into(), json!(l));
            }
            if !excluded_symbols.is_empty() {
                b.insert("excluded_symbols".into(), json!(excluded_symbols));
            }
            if *copy_existing_positions {
                b.insert("copy_existing_positions".into(), json!(true));
            }
            if *auto_create_sub_account {
                b.insert("auto_create_sub_account".into(), json!(true));
            }
            if let Some(ms) = margin_settings(settings_margin_mode, settings_leverage, symbol_settings) {
                b.insert("margin_settings".into(), ms);
            }
            ("POST".into(), "/v2/copy/subscribe".into(), Value::Object(b))
        }
        StrikeLeg::CopyPatch {
            subscription_id,
            sub_account_id,
            copy_amount,
            margin_per_entry_order,
            ratio_multiplier,
            minimum_entry,
            max_margin_per_symbol,
            max_margin_total,
            excluded_symbols,
            stop_loss_amount,
            margin_mode_source,
            leverage_source,
            max_leverage,
        } => {
            let mut b = Map::new();
            put(&mut b, "copy_amount", copy_amount);
            put(&mut b, "margin_per_entry_order", margin_per_entry_order);
            put(&mut b, "ratio_multiplier", ratio_multiplier);
            put(&mut b, "minimum_entry", minimum_entry);
            put(&mut b, "max_margin_per_symbol", max_margin_per_symbol);
            put(&mut b, "max_margin_total", max_margin_total);
            put(&mut b, "stop_loss_amount", stop_loss_amount);
            put(&mut b, "sub_account_id", sub_account_id);
            put(&mut b, "margin_mode_source", margin_mode_source);
            put(&mut b, "leverage_source", leverage_source);
            if let Some(l) = max_leverage {
                b.insert("max_leverage".into(), json!(l));
            }
            if !excluded_symbols.is_empty() {
                b.insert("excluded_symbols".into(), json!(excluded_symbols));
            }
            (
                "PATCH".into(),
                format!("/v2/copy/subscribe/{subscription_id}"),
                Value::Object(b),
            )
        }
        StrikeLeg::CopyStop { subscription_id, sub_account_id } => (
            "DELETE".into(),
            with_query(&format!("/v2/copy/subscribe/{subscription_id}"), "sub_account_id", sub_account_id),
            Value::Object(Map::new()),
        ),
        StrikeLeg::CopySave { lead_account_id } => (
            "POST".into(),
            "/v2/copy/saved".into(),
            json!({"lead_account_id": lead_account_id}),
        ),
        StrikeLeg::CopyUnsave { lead_account_id } => (
            "DELETE".into(),
            format!("/v2/copy/saved/{lead_account_id}"),
            Value::Object(Map::new()),
        ),
        StrikeLeg::Rfq {
            client_rfq_id,
            symbol,
            requested_size,
            side,
            vault_id,
            sub_account_id,
        } => {
            let mut b = Map::new();
            b.insert("client_rfq_id".into(), json!(client_rfq_id));
            b.insert("symbol".into(), json!(symbol));
            b.insert("requested_size".into(), json!(requested_size));
            put(&mut b, "side", side);
            put(&mut b, "vault_id", vault_id);
            put(&mut b, "sub_account_id", sub_account_id);
            ("POST".into(), "/v2/rfq".into(), Value::Object(b))
        }
        StrikeLeg::RfqClose { rfq_id, vault_id, sub_account_id } => {
            let path = if let Some(v) = vault_id {
                format!("/v2/rfq/{rfq_id}?vault_id={v}")
            } else {
                with_query(&format!("/v2/rfq/{rfq_id}"), "sub_account_id", sub_account_id)
            };
            ("DELETE".into(), path, Value::Object(Map::new()))
        }
        StrikeLeg::RfqResponse { rfq_id, status, bid_size, ask_size } => {
            let mut b = Map::new();
            b.insert("status".into(), json!(status));
            put(&mut b, "bid_size", bid_size);
            put(&mut b, "ask_size", ask_size);
            ("PUT".into(), format!("/v2/rfq/{rfq_id}/response"), Value::Object(b))
        }
        StrikeLeg::WithdrawQuote { blockchain, usd_amount, asset } => {
            let mut b = Map::new();
            b.insert("blockchain".into(), json!(blockchain));
            b.insert("usd_amount".into(), json!(usd_amount));
            put(&mut b, "asset", asset);
            ("POST".into(), "/v2/withdraw/quote".into(), Value::Object(b))
        }
        StrikeLeg::Withdraw { withdraw_id, wallet_signature } => (
            "POST".into(),
            "/v2/withdraw".into(),
            json!({"withdraw_id": withdraw_id, "wallet_signature": wallet_signature}),
        ),
        StrikeLeg::VaultDeposit { vault_id, usd_amount } => (
            "POST".into(),
            "/v2/vault/deposit".into(),
            json!({"vault_id": vault_id, "usd_amount": usd_amount}),
        ),
        StrikeLeg::VaultWithdraw { vault_id, shares, withdraw_all } => {
            let mut b = Map::new();
            b.insert("vault_id".into(), json!(vault_id));
            if *withdraw_all {
                b.insert("withdraw_all".into(), json!(true));
            }
            put(&mut b, "shares", shares);
            ("POST".into(), "/v2/vault/withdraw".into(), Value::Object(b))
        }
        StrikeLeg::VaultWithdrawCancel { vault_id, withdrawal_id } => (
            "POST".into(),
            format!("/v2/vault/{vault_id}/withdrawals/{withdrawal_id}/cancel"),
            Value::Object(Map::new()),
        ),
        StrikeLeg::VaultCreate {
            name,
            description,
            initial_deposit,
            leader_commission_bps,
            lockup_days,
            min_deposit,
            allow_proportional_close,
        } => {
            let mut b = Map::new();
            b.insert("name".into(), json!(name));
            b.insert("initial_deposit".into(), json!(initial_deposit));
            put(&mut b, "description", description);
            put(&mut b, "min_deposit", min_deposit);
            if let Some(bips) = leader_commission_bps {
                b.insert("leader_commission_bps".into(), json!(bips));
            }
            if let Some(days) = lockup_days {
                b.insert("lockup_days".into(), json!(days));
            }
            if *allow_proportional_close {
                b.insert("allow_proportional_close".into(), json!(true));
            }
            ("POST".into(), "/v2/vault/create".into(), Value::Object(b))
        }
        StrikeLeg::VaultPause { vault_id } => (
            "POST".into(),
            format!("/v2/vault/{vault_id}/pause"),
            Value::Object(Map::new()),
        ),
        StrikeLeg::VaultResume { vault_id } => (
            "POST".into(),
            format!("/v2/vault/{vault_id}/resume"),
            Value::Object(Map::new()),
        ),
        StrikeLeg::Twap {
            symbol,
            side,
            total_size,
            duration_sec,
            limit_price,
            reduce_only,
            randomize,
        } => {
            let mut b = Map::new();
            b.insert("symbol".into(), json!(symbol));
            b.insert("side".into(), json!(side));
            b.insert("total_size".into(), json!(total_size));
            b.insert("duration_sec".into(), json!(duration_sec));
            put(&mut b, "limit_price", limit_price);
            if *reduce_only {
                b.insert("reduce_only".into(), json!(true));
            }
            if *randomize {
                b.insert("randomize".into(), json!(true));
            }
            ("POST".into(), "/v2/algo/twap".into(), Value::Object(b))
        }
    }
}

/// `client_order_id` is chosen by Struck and passed in (deterministic from the
/// approved plan hash); it must satisfy the served rules.
pub fn build_request(leg: &StrikeLeg, client_order_id: &str) -> Result<ApiRequest, String> {
    if !client_id_ok(client_order_id) {
        return Err(format!("client_order_id {client_order_id:?}: 1..=256 chars, no reserved prefix"));
    }
    let (method, path, body) = expected_body(leg, client_order_id);
    let coid = match leg {
        StrikeLeg::Order { .. } | StrikeLeg::Bracket { .. } | StrikeLeg::Replace { .. } => {
            Some(client_order_id.to_string())
        }
        StrikeLeg::Leverage { .. }
        | StrikeLeg::Cancel { .. }
        | StrikeLeg::Twap { .. }
        | StrikeLeg::MarginMode { .. }
        | StrikeLeg::GridBot { .. }
        | StrikeLeg::GridStop { .. }
        | StrikeLeg::CancelAll { .. }
        | StrikeLeg::IsoMargin { .. }
        | StrikeLeg::TwapCancel { .. }
        | StrikeLeg::Batch { .. }
        | StrikeLeg::ReplaceBatch { .. }
        | StrikeLeg::CopySubscribe { .. }
        | StrikeLeg::CopyPatch { .. }
        | StrikeLeg::CopyStop { .. }
        | StrikeLeg::CopySave { .. }
        | StrikeLeg::CopyUnsave { .. }
        | StrikeLeg::Rfq { .. }
        | StrikeLeg::RfqClose { .. }
        | StrikeLeg::RfqResponse { .. }
        | StrikeLeg::WithdrawQuote { .. }
        | StrikeLeg::Withdraw { .. }
        | StrikeLeg::VaultDeposit { .. }
        | StrikeLeg::VaultWithdraw { .. }
        | StrikeLeg::VaultWithdrawCancel { .. }
        | StrikeLeg::VaultCreate { .. }
        | StrikeLeg::VaultPause { .. }
        | StrikeLeg::VaultResume { .. } => None,
    };
    let body = serde_json::to_string(&body).map_err(|e| e.to_string())?;
    Ok(ApiRequest { method, path, body, client_order_id: coid })
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RequestCongruence {
    pub report: Report,
    pub fresh_validation: LegValidation,
}

pub fn congruence(req: &ApiRequest, leg: &StrikeLeg, client_order_id: &str, fresh: &Markets, fee: Option<&FeeTier>) -> Result<RequestCongruence, String> {
    let mut r = Report::new();
    let (method, path, expected) = expected_body(leg, client_order_id);
    r.push("request.method", method.clone(), req.method.clone(), req.method == method);
    r.push("request.path", path.clone(), req.path.clone(), req.path == path);
    let actual: Value = serde_json::from_str(&req.body).map_err(|e| format!("request body: {e}"))?;
    let (Value::Object(exp), Value::Object(act)) = (&expected, &actual) else {
        return Err("request body is not a JSON object".into());
    };
    let mut keys: Vec<&String> = exp.keys().chain(act.keys()).collect();
    keys.sort();
    keys.dedup();
    for k in keys {
        let e = exp.get(k).map_or("absent".to_string(), Value::to_string);
        let a = act.get(k).map_or("absent".to_string(), Value::to_string);
        r.push(&format!("body.{k}"), e.clone(), a.clone(), e == a);
    }
    r.push("body.equals_approved", "body rebuilt from approved leg".into(), "request body".into(), expected == actual);
    let fresh_validation = validate_leg(leg, fresh, fee)?;
    r.push(
        "fresh_markets.revalidated",
        "every N2 rule ok on fresh /v2/markets".into(),
        format!("failed: {:?}", fresh_validation.report.failed_ids()),
        fresh_validation.report.ok,
    );
    Ok(RequestCongruence { report: r, fresh_validation })
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct RequestExplanation {
    pub lines: Vec<String>,
}

/// Plain words from the request plus values exactly as served by
/// `/v2/markets` and `/v2/fees` (rule 19: nothing is recomputed here; Strike
/// serves positions' upnl/margin/liquidation on `/v2/positions`).
/// `net` is the selected network's row of `struck.network.PROFILES` (its
/// `strike_label` names the venue); Rust holds no venue literal.
pub fn explain(req: &ApiRequest, leg: &StrikeLeg, markets: &Markets, fee: &FeeTier, net: &NetParams) -> Result<RequestExplanation, String> {
    let venue = &net.strike_label;
    let mut lines = vec![format!("{} {} on {venue}, body {}", req.method, req.path, req.body)];
    if let StrikeLeg::TwapCancel { strategy_id } = leg {
        lines.push(format!(
            "cancels TWAP {strategy_id}. Strike moves it to cancelling; confirm with GET /v2/algo/twap/{{id}}. Nothing is sent here."
        ));
        return Ok(RequestExplanation { lines });
    }
    if leg.symbol().is_empty() && !matches!(leg, StrikeLeg::GridStop { .. }) {
        lines.push(match leg {
            StrikeLeg::CopySubscribe { lead_account_id, copy_mode, .. } => {
                format!("copy {copy_mode} of lead {lead_account_id}. Strike sizes the copies. Nothing is sent here.")
            }
            StrikeLeg::CopyPatch { subscription_id, .. } => {
                format!("patches copy subscription {subscription_id}. Omitted fields stay as Strike has them.")
            }
            StrikeLeg::CopyStop { subscription_id, .. } => {
                format!("stops copy {subscription_id}. Strike closes that subscription's positions.")
            }
            StrikeLeg::CopySave { lead_account_id } => {
                format!("saves lead {lead_account_id}. This does not copy trades.")
            }
            StrikeLeg::CopyUnsave { lead_account_id } => format!("removes saved lead {lead_account_id}"),
            StrikeLeg::RfqClose { rfq_id, .. } => format!("closes RFQ {rfq_id}"),
            StrikeLeg::RfqResponse { rfq_id, status, .. } => format!("reports RFQ {rfq_id} as {status}"),
            StrikeLeg::WithdrawQuote { blockchain, usd_amount, .. } => {
                format!("quotes a {usd_amount} USD withdrawal on {blockchain}. This does not withdraw.")
            }
            StrikeLeg::Withdraw { withdraw_id, .. } => {
                format!("confirms withdrawal {withdraw_id} with the signature the human supplied")
            }
            StrikeLeg::VaultDeposit { vault_id, usd_amount } => {
                format!("deposits {usd_amount} USD into vault {vault_id}")
            }
            StrikeLeg::VaultWithdraw { vault_id, withdraw_all, shares, .. } => {
                if *withdraw_all {
                    format!("withdraws every available share from vault {vault_id}")
                } else {
                    format!("withdraws {} shares from vault {vault_id}", shares.clone().unwrap_or_default())
                }
            }
            StrikeLeg::VaultWithdrawCancel { vault_id, withdrawal_id } => {
                format!("cancels vault withdrawal {withdrawal_id} on {vault_id}")
            }
            StrikeLeg::VaultCreate { name, initial_deposit, .. } => {
                format!("creates vault {name} with initial deposit {initial_deposit} USD")
            }
            StrikeLeg::VaultPause { vault_id } => format!("pauses vault {vault_id}"),
            StrikeLeg::VaultResume { vault_id } => format!("resumes vault {vault_id}"),
            StrikeLeg::TwapCancel { .. } => "cancels a TWAP".into(),
            _ => "this leg has no market symbol".into(),
        });
        return Ok(RequestExplanation { lines });
    }
    if let StrikeLeg::GridStop { bot_id, cancel_orders } = leg {
        lines.push(format!(
            "stops grid bot {bot_id}{}",
            if *cancel_orders { " AND cancels its live orders (positions stay open)" } else { " (its orders stay on the book UNMANAGED)" }
        ));
        lines.push(
            "DELETE answers STOPPING, not STOPPED; watch GET /v2/grid/bot/{bot_id} or GRID_BOT_STOPPED".into(),
        );
        return Ok(RequestExplanation { lines });
    }
    let m = markets.markets.get(leg.symbol()).ok_or_else(|| format!("{} not in /v2/markets", leg.symbol()))?;
    match leg {
        StrikeLeg::Order { order } | StrikeLeg::Bracket { order, .. } => {
            lines.push(format!(
                "{} {} {} {} (mark {}), async: 201 'pending' only means sequenced",
                order.side, order.size, order.symbol, order.order_type, m.mark_price
            ));
            lines.push(format!(
                "served: funding_rate {} (/v2/markets); fee tier {} taker_rate {} / maker_rate {}",
                m.funding_rate, fee.tier, fee.taker_rate, fee.maker_rate
            ));
        }
        StrikeLeg::Leverage { symbol, leverage, .. } => {
            lines.push(format!("sets {symbol} leverage to {leverage}x (also re-margins any open position)"));
        }
        StrikeLeg::MarginMode { symbol, margin_mode, .. } => {
            lines.push(format!(
                "sets {symbol} margin mode to {margin_mode}. Strike can return 200 and still drop the change when a position or orders are open; confirm with GET /v2/symbols. Nothing is sent here."
            ));
        }
        StrikeLeg::Cancel { symbol, client_order_id, .. } => {
            lines.push(format!("cancels {symbol} order {client_order_id}"));
        }
        StrikeLeg::CancelAll { symbol, .. } => {
            lines.push(format!("cancels every open order on {symbol}"));
        }
        StrikeLeg::IsoMargin { symbol, amount, modify_type, .. } => {
            lines.push(format!("isolated margin {modify_type} {amount} USD on {symbol}"));
        }
        StrikeLeg::Replace { order_id, order } => {
            lines.push(format!(
                "replaces order {order_id} with {} {} {} {} (mark {})",
                order.side, order.size, order.symbol, order.order_type, m.mark_price
            ));
        }
        StrikeLeg::Batch { orders, .. } => {
            lines.push(format!(
                "batch of {} order(s); Strike executes them independently (partial success is served)",
                orders.len()
            ));
        }
        StrikeLeg::ReplaceBatch { replacements, .. } => {
            lines.push(format!(
                "replaces {} order(s) as one batch; if any cancel fails Strike rejects the batch",
                replacements.len()
            ));
        }
        StrikeLeg::TwapCancel { .. }
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
        StrikeLeg::Rfq { client_rfq_id, symbol, requested_size, side, .. } => {
            lines.push(format!(
                "opens RFQ {client_rfq_id} for {requested_size} {symbol} side {}. Quoters are Strike's. Nothing is sent here.",
                side.clone().unwrap_or_else(|| "both".into())
            ));
        }
        StrikeLeg::Twap {
            side,
            total_size,
            symbol,
            duration_sec,
            limit_price,
            ..
        } => {
            let limit = limit_price.clone().unwrap_or_else(|| "none".to_string());
            lines.push(format!(
                "TWAP {side} {total_size} {symbol} over {duration_sec}s, limit {limit}. Child orders are Strike's, id twap-{{strategy_id}}-n. Nothing is sent here."
            ));
        }
        StrikeLeg::GridBot {
            symbol,
            mode,
            initialization_mode,
            lower_price,
            upper_price,
            grid_count,
            collateral,
            ..
        } => {
            let init = initialization_mode
                .clone()
                .unwrap_or_else(|| "LIMIT_ONLY".to_string());
            lines.push(format!(
                "creates a server-side {mode} grid bot on {symbol}: {grid_count} levels {lower_price}..={upper_price} (mark {}), collateral {collateral} USD, {init}",
                m.mark_price
            ));
            lines.push(format!(
                "served: fee tier {} taker_rate {} / maker_rate {} (/v2/fees); spacing must beat mid*(maker+taker)",
                fee.tier, fee.taker_rate, fee.maker_rate
            ));
            lines.push(
                "async: 201 SUBMITTED is not RUNNING; poll GET /v2/grid/bot, a silent reject stays 404 forever. Nothing is sent here.".into(),
            );
        }
        // GridStop returned early above (it explains without a market row).
        StrikeLeg::GridStop { .. } => unreachable!("grid_stop handled before the market lookup"),
    }
    Ok(RequestExplanation { lines })
}
