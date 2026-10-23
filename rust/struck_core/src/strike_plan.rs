//! Strike plan legs as they cross the boundary from the approved plan
//! (spec §6.1 `StrikeLeg`; request fields from `openapi/trade-api.yaml:1308`
//! CreateOrderRequest, `:1846` CreateStrategyOrderRequest, `:1924`
//! LeverageRequest, `skills/strike-trading/SKILL.md:211` cancel body).
//!
//! Decimal fields stay the approved STRINGS (they are sent verbatim); the
//! validator parses them exactly with `decimal::parse`.

use serde::{Deserialize, Serialize};

use crate::decimal;

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct OrderSpec {
    pub symbol: String,
    pub side: String,
    #[serde(rename = "type")]
    pub order_type: String,
    pub size: String,
    pub price: Option<String>,
    pub stop_price: Option<String>,
    pub time_in_force: Option<String>,
    pub post_only: bool,
    pub reduce_only: bool,
    pub callback_rate: Option<String>,
    pub activation_price: Option<String>,
    /// Leverage the operator plans with; estimates use it. `None` means the
    /// market `default_leverage` (the account's symbol setting needs the
    /// API wallet, spec B0).
    pub leverage: Option<u32>,
    /// CreateOrderRequest optionals (`skills/strike-trading/SKILL.md` Create Order).
    /// Absent means omitted from the body.
    pub working_type: Option<String>,
    pub close_position: bool,
    pub price_protect: bool,
    pub slippage: Option<String>,
    pub vault_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExitSpec {
    #[serde(rename = "type")]
    pub order_type: String,
    pub stop_price: String,
    pub price: Option<String>,
    /// CreateStrategyOrderRequest exit optionals. Omitted when absent.
    #[serde(default)]
    pub time_in_force: Option<String>,
    #[serde(default)]
    pub working_type: Option<String>,
    #[serde(default)]
    pub post_only: bool,
    #[serde(default)]
    pub price_protect: bool,
}

/// One `POST /v2/order/replace-batch` item: cancel by served `order_id`,
/// then the new CreateOrderRequest.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Replacement {
    pub order_id: String,
    pub order: OrderSpec,
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum StrikeLeg {
    Order { order: OrderSpec },
    Bracket { order: OrderSpec, tp: Option<ExitSpec>, sl: Option<ExitSpec> },
    Leverage { symbol: String, leverage: u32, vault_id: Option<String> },
    Cancel { symbol: String, client_order_id: String, vault_id: Option<String> },
    /// `POST /v2/algo/twap` (`skills/strike-trading/SKILL.md` Create TWAP).
    /// `side` is the served enum `BUY`|`SELL`. `total_size` is the plan's
    /// `size`. `limit_price` is the plan's `price`.
    Twap {
        symbol: String,
        side: String,
        total_size: String,
        duration_sec: u32,
        limit_price: Option<String>,
        reduce_only: bool,
        randomize: bool,
    },
    /// `POST /v2/marginMode`. `margin_mode` is the served value `cross` or `isolated`.
    MarginMode { symbol: String, margin_mode: String, vault_id: Option<String> },
    /// `POST /v2/grid/bot` (`skills/strike-grid-bot/SKILL.md` Create). The
    /// server-side bot; Struck never runs a local grid engine. Decimals are
    /// the approved strings, sent verbatim.
    GridBot {
        symbol: String,
        /// `LONG` | `SHORT` | `NEUTRAL` (exact case, the server is case-sensitive).
        mode: String,
        /// `LIMIT_ONLY` (None) | `MARKET_SEEDED` (LONG/SHORT only).
        initialization_mode: Option<String>,
        lower_price: String,
        upper_price: String,
        grid_count: u32,
        collateral: String,
        sl_price: Option<String>,
        long_sl_price: Option<String>,
        short_sl_price: Option<String>,
    },
    /// `DELETE /v2/grid/bot/{bot_id}` (stop; positions stay open). `bot_id`
    /// is a server-served id, `[A-Za-z0-9_-]+` starting `grid-` (vault bots
    /// serve `grid-vault_…`).
    GridStop { bot_id: String, cancel_orders: bool },
    /// `DELETE /v2/order/cancel-all`.
    CancelAll { symbol: String, vault_id: Option<String> },
    /// `POST /v2/isoMargin`. `modify_type` is `add` or `remove`. `amount` is
    /// the approved decimal string.
    IsoMargin {
        symbol: String,
        amount: String,
        modify_type: String,
        vault_id: Option<String>,
    },
    /// `DELETE /v2/algo/twap/{strategy_id}`. The id is one URL path segment.
    TwapCancel { strategy_id: String },
    /// `POST /v2/order/replace`: cancel the served `order_id`, create `order`.
    Replace { order_id: String, order: OrderSpec },
    /// `POST /v2/orders/batch`. Independent execution; partial success is Strike's.
    Batch { orders: Vec<OrderSpec>, vault_id: Option<String> },
    /// `POST /v2/order/replace-batch`. All-or-nothing cancel, then the new orders.
    ReplaceBatch { replacements: Vec<Replacement>, vault_id: Option<String> },
    /// `POST /v2/copy/subscribe`. Amounts are the approved decimal strings.
    CopySubscribe {
        lead_account_id: String,
        platform: Option<String>,
        copy_mode: String,
        copy_amount: Option<String>,
        margin_per_entry_order: Option<String>,
        ratio_multiplier: Option<String>,
        minimum_entry: Option<String>,
        max_margin_per_symbol: Option<String>,
        max_margin_total: Option<String>,
        excluded_symbols: Vec<String>,
        stop_loss_amount: Option<String>,
        sub_account_id: Option<String>,
        copy_existing_positions: bool,
        auto_create_sub_account: bool,
        request_id: Option<String>,
        source_account_id: Option<String>,
        funding_amount: Option<String>,
        margin_mode_source: Option<String>,
        leverage_source: Option<String>,
        max_leverage: Option<u32>,
        settings_margin_mode: Option<String>,
        settings_leverage: Option<u32>,
        symbol_settings: Vec<FlatSymbolSetting>,
    },
    /// `PATCH /v2/copy/subscribe/{subscription_id}`. Omitted fields stay unchanged.
    CopyPatch {
        subscription_id: String,
        sub_account_id: Option<String>,
        copy_amount: Option<String>,
        margin_per_entry_order: Option<String>,
        ratio_multiplier: Option<String>,
        minimum_entry: Option<String>,
        max_margin_per_symbol: Option<String>,
        max_margin_total: Option<String>,
        excluded_symbols: Vec<String>,
        stop_loss_amount: Option<String>,
        margin_mode_source: Option<String>,
        leverage_source: Option<String>,
        max_leverage: Option<u32>,
    },
    /// `DELETE /v2/copy/subscribe/{subscription_id}`. No body.
    CopyStop { subscription_id: String, sub_account_id: Option<String> },
    /// `POST /v2/copy/saved`.
    CopySave { lead_account_id: String },
    /// `DELETE /v2/copy/saved/{lead_account_id}`.
    CopyUnsave { lead_account_id: String },
    /// `POST /v2/rfq`.
    Rfq {
        client_rfq_id: String,
        symbol: String,
        requested_size: String,
        side: Option<String>,
        vault_id: Option<String>,
        sub_account_id: Option<String>,
    },
    /// `DELETE /v2/rfq/{rfq_id}`. No body.
    RfqClose { rfq_id: String, vault_id: Option<String>, sub_account_id: Option<String> },
    /// `PUT /v2/rfq/{rfq_id}/response`.
    RfqResponse {
        rfq_id: String,
        status: String,
        bid_size: Option<String>,
        ask_size: Option<String>,
    },
    /// `POST /v2/withdraw/quote`. Does not confirm the withdrawal.
    WithdrawQuote { blockchain: String, usd_amount: String, asset: Option<String> },
    /// `POST /v2/withdraw`. `wallet_signature` is the human's signature of the quote message.
    Withdraw { withdraw_id: String, wallet_signature: String },
    /// `POST /v2/vault/deposit`.
    VaultDeposit { vault_id: String, usd_amount: String },
    /// `POST /v2/vault/withdraw`.
    VaultWithdraw { vault_id: String, shares: Option<String>, withdraw_all: bool },
    /// `POST /v2/vault/{vault_id}/withdrawals/{withdrawal_id}/cancel`. No body.
    VaultWithdrawCancel { vault_id: String, withdrawal_id: String },
    /// `POST /v2/vault/create`.
    VaultCreate {
        name: String,
        description: Option<String>,
        initial_deposit: String,
        leader_commission_bps: Option<u32>,
        lockup_days: Option<u32>,
        min_deposit: Option<String>,
        allow_proportional_close: bool,
    },
    /// `POST /v2/vault/{vault_id}/pause`. No body.
    VaultPause { vault_id: String },
    /// `POST /v2/vault/{vault_id}/resume`. No body.
    VaultResume { vault_id: String },
}

/// One row of copy `margin_settings.symbol_settings`, as the plan states it.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FlatSymbolSetting {
    pub symbol: String,
    pub margin_mode: String,
    pub leverage: u32,
}

impl StrikeLeg {
    pub fn symbol(&self) -> &str {
        match self {
            StrikeLeg::Order { order } | StrikeLeg::Bracket { order, .. } => &order.symbol,
            StrikeLeg::Leverage { symbol, .. }
            | StrikeLeg::Cancel { symbol, .. }
            | StrikeLeg::Twap { symbol, .. }
            | StrikeLeg::MarginMode { symbol, .. }
            | StrikeLeg::GridBot { symbol, .. }
            | StrikeLeg::CancelAll { symbol, .. }
            | StrikeLeg::IsoMargin { symbol, .. }
            | StrikeLeg::Rfq { symbol, .. } => symbol,
            StrikeLeg::Replace { order, .. } => &order.symbol,
            StrikeLeg::Batch { orders, .. } => orders.first().map(|o| o.symbol.as_str()).unwrap_or(""),
            StrikeLeg::ReplaceBatch { replacements, .. } => {
                replacements.first().map(|r| r.order.symbol.as_str()).unwrap_or("")
            }
            // GridStop and TwapCancel carry no market symbol. validate/explain
            // handle them before any market lookup, so "" is never looked up.
            StrikeLeg::GridStop { .. }
            | StrikeLeg::TwapCancel { .. }
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
            | StrikeLeg::VaultResume { .. } => "",
        }
    }
}

/// One path segment safe to interpolate into a Strike URL. No `/`, `?`, or host.
/// `client_rfq_id`: 1..=64 of `[A-Za-z0-9._:-]` (strike-rfq).
pub fn client_rfq_id_ok(id: &str) -> bool {
    let b = id.as_bytes();
    (1..=64).contains(&b.len())
        && b.iter().all(|c| c.is_ascii_alphanumeric() || matches!(*c, b'.' | b'_' | b':' | b'-'))
}

pub fn path_segment_ok(id: &str) -> bool {
    let b = id.as_bytes();
    !b.is_empty()
        && b.len() <= 128
        && b[0].is_ascii_alphanumeric()
        && b.iter().all(|c| c.is_ascii_alphanumeric() || *c == b'-' || *c == b'_')
}

/// A served grid bot id: it is interpolated into the URL path
/// `DELETE /v2/grid/bot/{bot_id}`, so it may contain no path metacharacters.
/// Vault bots serve ids like `grid-vault_<vault_id>-<symbol>-<uuidv7>`
/// (captured live on mainnet 2026-10-22), so `_` is a served character.
pub fn grid_bot_id_ok(id: &str) -> bool {
    id.starts_with("grid-")
        && id.len() <= 200
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// Reserved `client_order_id` prefixes (`skills/strike-trading/SKILL.md:98`).
pub const RESERVED_PREFIXES: [&str; 7] =
    ["copy:", "liq_", "bankrupt_", "adl_", "if_transfer_", "if_adl_", "vault_wd_"];

pub fn client_id_ok(id: &str) -> bool {
    !id.is_empty() && id.len() <= 256 && !RESERVED_PREFIXES.iter().any(|p| id.starts_with(p))
}

/// One CreateOrderRequest inside `batch` or `replace_batch` (flat JSON, same
/// field names as a single order leg, without `kind`).
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FlatOrder {
    pub symbol: String,
    pub side: String,
    #[serde(rename = "type")]
    pub order_type: String,
    pub size: String,
    #[serde(default)]
    pub price: Option<String>,
    #[serde(default)]
    pub stop_price: Option<String>,
    #[serde(default)]
    pub callback_rate: Option<String>,
    #[serde(default)]
    pub activation_price: Option<String>,
    #[serde(default)]
    pub time_in_force: Option<String>,
    #[serde(default)]
    pub post_only: bool,
    #[serde(default)]
    pub reduce_only: bool,
    #[serde(default)]
    pub leverage: Option<u32>,
    #[serde(default)]
    pub working_type: Option<String>,
    #[serde(default)]
    pub close_position: bool,
    #[serde(default)]
    pub price_protect: bool,
    #[serde(default)]
    pub slippage: Option<String>,
}

/// `POST /v2/order/replace-batch` item as it crosses the boundary.
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FlatReplacement {
    pub order_id: String,
    pub symbol: String,
    pub new_order: FlatOrder,
}

/// The FLAT boundary shape of spec §3.2 `StrikeLeg` (`kind`, `symbol`,
/// `side`, `type`, `size`, `price`, `stop_price`, `callback_rate`,
/// `activation_price`, `reduce_only`, `tp`, `sl`, `leverage`), plus
/// `time_in_force`/`post_only` (CreateOrderRequest) and, for `cancel` only,
/// `client_order_id` (the cancel body needs it; the spec table lists none).
    /// `duration_sec` and `randomize` belong to `twap` (strike-trading Create TWAP).
    /// `margin_mode` is `cross` or `isolated` (`POST /v2/marginMode`, skill field `marginMode`).
#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FlatLeg {
    pub kind: String,
    pub symbol: String,
    pub side: Option<String>,
    #[serde(rename = "type")]
    pub order_type: Option<String>,
    pub size: Option<String>,
    pub price: Option<String>,
    pub stop_price: Option<String>,
    pub callback_rate: Option<String>,
    pub activation_price: Option<String>,
    pub time_in_force: Option<String>,
    pub post_only: bool,
    pub reduce_only: bool,
    pub tp: Option<ExitSpec>,
    pub sl: Option<ExitSpec>,
    pub leverage: Option<u32>,
    pub client_order_id: Option<String>,
    #[serde(default)]
    pub duration_sec: Option<u32>,
    #[serde(default)]
    pub randomize: bool,
    /// `cross` or `isolated`. Only kind `margin_mode` may set it.
    #[serde(default)]
    pub margin_mode: Option<String>,
    /// `LONG`|`SHORT`|`NEUTRAL`. Only kind `grid_bot` may set it.
    #[serde(default)]
    pub grid_mode: Option<String>,
    /// `LIMIT_ONLY`|`MARKET_SEEDED`. Only kind `grid_bot`.
    #[serde(default)]
    pub initialization_mode: Option<String>,
    #[serde(default)]
    pub lower_price: Option<String>,
    #[serde(default)]
    pub upper_price: Option<String>,
    #[serde(default)]
    pub grid_count: Option<u32>,
    #[serde(default)]
    pub collateral: Option<String>,
    #[serde(default)]
    pub sl_price: Option<String>,
    #[serde(default)]
    pub long_sl_price: Option<String>,
    #[serde(default)]
    pub short_sl_price: Option<String>,
    /// Only kind `grid_stop`.
    #[serde(default)]
    pub bot_id: Option<String>,
    #[serde(default)]
    pub cancel_orders: bool,
    /// CreateOrderRequest optionals. Only order, bracket, and replace.
    #[serde(default)]
    pub working_type: Option<String>,
    #[serde(default)]
    pub close_position: bool,
    #[serde(default)]
    pub price_protect: bool,
    #[serde(default)]
    pub slippage: Option<String>,
    /// Present on the routes whose skill body lists `vault_id`.
    #[serde(default)]
    pub vault_id: Option<String>,
    /// `POST /v2/isoMargin` amount, decimal string.
    #[serde(default)]
    pub amount: Option<String>,
    /// `add` or `remove`. Only kind `iso_margin`.
    #[serde(default)]
    pub modify_type: Option<String>,
    /// Served TWAP id. Only kind `twap_cancel`.
    #[serde(default)]
    pub strategy_id: Option<String>,
    /// Served order id. Only kind `replace`.
    #[serde(default)]
    pub order_id: Option<String>,
    /// `POST /v2/orders/batch`. Only kind `batch`.
    #[serde(default)]
    pub orders: Option<Vec<FlatOrder>>,
    /// `POST /v2/order/replace-batch`. Only kind `replace_batch`.
    #[serde(default)]
    pub replacements: Option<Vec<FlatReplacement>>,
    #[serde(default)]
    pub lead_account_id: Option<String>,
    #[serde(default)]
    pub platform: Option<String>,
    #[serde(default)]
    pub copy_mode: Option<String>,
    #[serde(default)]
    pub copy_amount: Option<String>,
    #[serde(default)]
    pub margin_per_entry_order: Option<String>,
    #[serde(default)]
    pub ratio_multiplier: Option<String>,
    #[serde(default)]
    pub minimum_entry: Option<String>,
    #[serde(default)]
    pub max_margin_per_symbol: Option<String>,
    #[serde(default)]
    pub max_margin_total: Option<String>,
    #[serde(default)]
    pub excluded_symbols: Vec<String>,
    #[serde(default)]
    pub stop_loss_amount: Option<String>,
    #[serde(default)]
    pub subscription_id: Option<String>,
    #[serde(default)]
    pub sub_account_id: Option<String>,
    #[serde(default)]
    pub copy_existing_positions: bool,
    #[serde(default)]
    pub auto_create_sub_account: bool,
    #[serde(default)]
    pub request_id: Option<String>,
    #[serde(default)]
    pub source_account_id: Option<String>,
    #[serde(default)]
    pub funding_amount: Option<String>,
    #[serde(default)]
    pub margin_mode_source: Option<String>,
    #[serde(default)]
    pub leverage_source: Option<String>,
    #[serde(default)]
    pub max_leverage: Option<u32>,
    #[serde(default)]
    pub settings_margin_mode: Option<String>,
    #[serde(default)]
    pub settings_leverage: Option<u32>,
    #[serde(default)]
    pub symbol_settings: Vec<FlatSymbolSetting>,
    #[serde(default)]
    pub client_rfq_id: Option<String>,
    #[serde(default)]
    pub requested_size: Option<String>,
    #[serde(default)]
    pub rfq_id: Option<String>,
    #[serde(default)]
    pub response_status: Option<String>,
    #[serde(default)]
    pub bid_size: Option<String>,
    #[serde(default)]
    pub ask_size: Option<String>,
    #[serde(default)]
    pub blockchain: Option<String>,
    #[serde(default)]
    pub usd_amount: Option<String>,
    #[serde(default)]
    pub asset: Option<String>,
    #[serde(default)]
    pub withdraw_id: Option<String>,
    #[serde(default)]
    pub wallet_signature: Option<String>,
    #[serde(default)]
    pub shares: Option<String>,
    #[serde(default)]
    pub withdraw_all: bool,
    #[serde(default)]
    pub vault_name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    #[serde(default)]
    pub initial_deposit: Option<String>,
    #[serde(default)]
    pub leader_commission_bps: Option<u32>,
    #[serde(default)]
    pub lockup_days: Option<u32>,
    #[serde(default)]
    pub min_deposit: Option<String>,
    #[serde(default)]
    pub allow_proportional_close: bool,
    #[serde(default)]
    pub withdrawal_id: Option<String>,
}

pub fn parse_leg(json: &str) -> Result<StrikeLeg, String> {
    let f: FlatLeg = serde_json::from_str(json).map_err(|e| format!("leg: invalid JSON shape: {e}"))?;
    from_flat(f)
}

pub fn parse_legs(json: &str) -> Result<Vec<StrikeLeg>, String> {
    let fs: Vec<FlatLeg> = serde_json::from_str(json).map_err(|e| format!("legs: invalid JSON shape: {e}"))?;
    fs.into_iter().map(from_flat).collect()
}

fn spec_from_flat_order(o: FlatOrder) -> Result<OrderSpec, String> {
    if o.side != "buy" && o.side != "sell" {
        return Err("order: side is buy or sell".into());
    }
    if let Some(wt) = &o.working_type {
        if wt != "mark_price" && wt != "contract_price" {
            return Err("working_type is mark_price or contract_price".into());
        }
    }
    if let Some(s) = &o.slippage {
        decimal::parse("slippage", s)?;
    }
    Ok(OrderSpec {
        symbol: o.symbol,
        side: o.side,
        order_type: o.order_type,
        size: o.size,
        price: o.price,
        stop_price: o.stop_price,
        time_in_force: o.time_in_force,
        post_only: o.post_only,
        reduce_only: o.reduce_only,
        callback_rate: o.callback_rate,
        activation_price: o.activation_price,
        leverage: o.leverage,
        working_type: o.working_type,
        close_position: o.close_position,
        price_protect: o.price_protect,
        slippage: o.slippage,
        vault_id: None,
    })
}

fn gt0(name: &str, s: &str) -> Result<(), String> {
    let d = decimal::parse(name, s)?;
    if d <= rust_decimal::Decimal::ZERO {
        return Err(format!("{name} must be > 0"));
    }
    Ok(())
}

fn ge0(name: &str, s: &str) -> Result<(), String> {
    let d = decimal::parse(name, s)?;
    if d < rust_decimal::Decimal::ZERO {
        return Err(format!("{name} must be >= 0"));
    }
    Ok(())
}

fn source_ok(name: &str, s: &str) -> Result<(), String> {
    if s == "account" || s == "lead" {
        Ok(())
    } else {
        Err(format!("{name} is account or lead"))
    }
}

fn venue_fields_set(f: &FlatLeg) -> bool {
    f.lead_account_id.is_some()
        || f.platform.is_some()
        || f.copy_mode.is_some()
        || f.copy_amount.is_some()
        || f.margin_per_entry_order.is_some()
        || f.ratio_multiplier.is_some()
        || f.minimum_entry.is_some()
        || f.max_margin_per_symbol.is_some()
        || f.max_margin_total.is_some()
        || !f.excluded_symbols.is_empty()
        || f.stop_loss_amount.is_some()
        || f.subscription_id.is_some()
        || f.sub_account_id.is_some()
        || f.copy_existing_positions
        || f.auto_create_sub_account
        || f.request_id.is_some()
        || f.source_account_id.is_some()
        || f.funding_amount.is_some()
        || f.margin_mode_source.is_some()
        || f.leverage_source.is_some()
        || f.max_leverage.is_some()
        || f.settings_margin_mode.is_some()
        || f.settings_leverage.is_some()
        || !f.symbol_settings.is_empty()
        || f.client_rfq_id.is_some()
        || f.requested_size.is_some()
        || f.rfq_id.is_some()
        || f.response_status.is_some()
        || f.bid_size.is_some()
        || f.ask_size.is_some()
        || f.blockchain.is_some()
        || f.usd_amount.is_some()
        || f.asset.is_some()
        || f.withdraw_id.is_some()
        || f.wallet_signature.is_some()
        || f.shares.is_some()
        || f.withdraw_all
        || f.vault_name.is_some()
        || f.description.is_some()
        || f.initial_deposit.is_some()
        || f.leader_commission_bps.is_some()
        || f.lockup_days.is_some()
        || f.min_deposit.is_some()
        || f.allow_proportional_close
        || f.withdrawal_id.is_some()
}

fn venue_kind(k: &str) -> bool {
    matches!(
        k,
        "copy_subscribe"
            | "copy_patch"
            | "copy_stop"
            | "copy_save"
            | "copy_unsave"
            | "rfq"
            | "rfq_close"
            | "rfq_response"
            | "withdraw_quote"
            | "withdraw"
            | "vault_deposit"
            | "vault_withdraw"
            | "vault_withdraw_cancel"
            | "vault_create"
            | "vault_pause"
            | "vault_resume"
    )
}

fn venue_from_flat(k: &str, f: &FlatLeg) -> Result<Option<StrikeLeg>, String> {
    if !venue_kind(k) {
        if venue_fields_set(f) {
            return Err(format!("{k}: copy, vault, rfq and withdraw fields do not belong here"));
        }
        return Ok(None);
    }
    if !f.symbol.is_empty() && k != "rfq" {
        return Err(format!("{k}: symbol is empty; this route has no market symbol"));
    }
    if f.sub_account_id.as_ref().is_some_and(|s| !path_segment_ok(s)) {
        return Err(format!("{k}: sub_account_id must be one path segment"));
    }
    if f.vault_id.is_some()
        && !matches!(
            k,
            "rfq"
                | "rfq_close"
                | "vault_deposit"
                | "vault_withdraw"
                | "vault_withdraw_cancel"
                | "vault_pause"
                | "vault_resume"
        )
    {
        return Err(format!("{k}: vault_id is not a field of this route"));
    }
    if k != "rfq" && f.side.is_some() {
        return Err(format!("{k}: side does not belong on this route"));
    }
    let order_shaped = f.order_type.is_some()
        || f.size.is_some()
        || f.price.is_some()
        || f.stop_price.is_some()
        || f.callback_rate.is_some()
        || f.activation_price.is_some()
        || f.time_in_force.is_some()
        || f.post_only
        || f.reduce_only
        || f.tp.is_some()
        || f.sl.is_some()
        || f.leverage.is_some()
        || f.client_order_id.is_some()
        || f.duration_sec.is_some()
        || f.randomize
        || f.margin_mode.is_some()
        || f.grid_mode.is_some()
        || f.initialization_mode.is_some()
        || f.lower_price.is_some()
        || f.upper_price.is_some()
        || f.grid_count.is_some()
        || f.collateral.is_some()
        || f.sl_price.is_some()
        || f.long_sl_price.is_some()
        || f.short_sl_price.is_some()
        || f.bot_id.is_some()
        || f.cancel_orders
        || f.working_type.is_some()
        || f.close_position
        || f.price_protect
        || f.slippage.is_some()
        || f.amount.is_some()
        || f.modify_type.is_some()
        || f.strategy_id.is_some()
        || f.order_id.is_some()
        || f.orders.is_some()
        || f.replacements.is_some();
    if order_shaped {
        return Err(format!("{k}: order fields do not belong on this route"));
    }
    match k {
        "copy_subscribe" => {
            let lead_account_id = f.lead_account_id.clone().filter(|s| !s.is_empty()).ok_or("copy_subscribe: lead_account_id is required")?;
            let copy_mode = f.copy_mode.clone().ok_or("copy_subscribe: copy_mode is required")?;
            if copy_mode != "fixed_ratio" && copy_mode != "fixed_amount" {
                return Err("copy_subscribe: copy_mode is fixed_ratio or fixed_amount".into());
            }
            if let Some(p) = &f.platform {
                if p != "strike" && p != "hyperliquid" {
                    return Err("copy_subscribe: platform is strike or hyperliquid".into());
                }
            }
            match copy_mode.as_str() {
                "fixed_ratio" => {
                    let amt = f.copy_amount.clone().ok_or("copy_subscribe: copy_amount is required")?;
                    gt0("copy_amount", &amt)?;
                    if f.margin_per_entry_order.is_some() || f.funding_amount.is_some() {
                        return Err("copy_subscribe: margin_per_entry_order and funding_amount belong to fixed_amount".into());
                    }
                }
                _ => {
                    let amt = f.margin_per_entry_order.clone().ok_or("copy_subscribe: margin_per_entry_order is required")?;
                    gt0("margin_per_entry_order", &amt)?;
                    if f.copy_amount.is_some() || f.ratio_multiplier.is_some() || f.minimum_entry.is_some() {
                        return Err("copy_subscribe: copy_amount, ratio_multiplier and minimum_entry belong to fixed_ratio".into());
                    }
                }
            }
            if let Some(m) = &f.ratio_multiplier {
                let d = decimal::parse("ratio_multiplier", m)?;
                if d.fract() != rust_decimal::Decimal::ZERO || d < rust_decimal::Decimal::ONE || d > rust_decimal::Decimal::TEN {
                    return Err("copy_subscribe: ratio_multiplier is an integer 1..=10".into());
                }
            }
            if let Some(m) = &f.minimum_entry {
                ge0("minimum_entry", m)?;
            }
            if let Some(m) = &f.max_margin_per_symbol {
                gt0("max_margin_per_symbol", m)?;
            }
            if let Some(m) = &f.max_margin_total {
                gt0("max_margin_total", m)?;
            }
            if let Some(m) = &f.stop_loss_amount {
                ge0("stop_loss_amount", m)?;
            }
            if let Some(m) = &f.funding_amount {
                gt0("funding_amount", m)?;
            }
            if f.excluded_symbols.len() > 500 || f.excluded_symbols.iter().any(|s| s.is_empty() || s.len() > 32) {
                return Err("copy_subscribe: excluded_symbols has at most 500 items of 1..=32 chars".into());
            }
            if let Some(s) = &f.margin_mode_source {
                source_ok("margin_mode_source", s)?;
            }
            if let Some(s) = &f.leverage_source {
                source_ok("leverage_source", s)?;
            }
            if let Some(mode) = &f.settings_margin_mode {
                if mode != "cross" && mode != "isolated" {
                    return Err("copy_subscribe: settings_margin_mode is cross or isolated".into());
                }
            }
            for row in &f.symbol_settings {
                if row.margin_mode != "cross" && row.margin_mode != "isolated" {
                    return Err("copy_subscribe: symbol margin_mode is cross or isolated".into());
                }
            }
            if f.auto_create_sub_account {
                if f.request_id.as_ref().is_none_or(|s| s.is_empty()) || f.source_account_id.as_ref().is_none_or(|s| s.is_empty()) {
                    return Err("copy_subscribe: auto_create_sub_account needs request_id and source_account_id".into());
                }
                if f.sub_account_id.is_some() {
                    return Err("copy_subscribe: sub_account_id cannot be combined with auto_create_sub_account".into());
                }
            }
            Ok(Some(StrikeLeg::CopySubscribe {
                lead_account_id,
                platform: f.platform.clone(),
                copy_mode,
                copy_amount: f.copy_amount.clone(),
                margin_per_entry_order: f.margin_per_entry_order.clone(),
                ratio_multiplier: f.ratio_multiplier.clone(),
                minimum_entry: f.minimum_entry.clone(),
                max_margin_per_symbol: f.max_margin_per_symbol.clone(),
                max_margin_total: f.max_margin_total.clone(),
                excluded_symbols: f.excluded_symbols.clone(),
                stop_loss_amount: f.stop_loss_amount.clone(),
                sub_account_id: f.sub_account_id.clone(),
                copy_existing_positions: f.copy_existing_positions,
                auto_create_sub_account: f.auto_create_sub_account,
                request_id: f.request_id.clone(),
                source_account_id: f.source_account_id.clone(),
                funding_amount: f.funding_amount.clone(),
                margin_mode_source: f.margin_mode_source.clone(),
                leverage_source: f.leverage_source.clone(),
                max_leverage: f.max_leverage,
                settings_margin_mode: f.settings_margin_mode.clone(),
                settings_leverage: f.settings_leverage,
                symbol_settings: f.symbol_settings.clone(),
            }))
        }
        "copy_patch" => {
            let subscription_id = f.subscription_id.clone().filter(|s| path_segment_ok(s)).ok_or("copy_patch: subscription_id must be one path segment")?;
            let any = f.copy_amount.is_some()
                || f.margin_per_entry_order.is_some()
                || f.ratio_multiplier.is_some()
                || f.minimum_entry.is_some()
                || f.max_margin_per_symbol.is_some()
                || f.max_margin_total.is_some()
                || !f.excluded_symbols.is_empty()
                || f.stop_loss_amount.is_some()
                || f.sub_account_id.is_some()
                || f.margin_mode_source.is_some()
                || f.leverage_source.is_some()
                || f.max_leverage.is_some();
            if !any {
                return Err("copy_patch: at least one field to change".into());
            }
            if let Some(m) = &f.copy_amount { gt0("copy_amount", m)?; }
            if let Some(m) = &f.margin_per_entry_order { gt0("margin_per_entry_order", m)?; }
            if let Some(s) = &f.margin_mode_source { source_ok("margin_mode_source", s)?; }
            if let Some(s) = &f.leverage_source { source_ok("leverage_source", s)?; }
            Ok(Some(StrikeLeg::CopyPatch {
                subscription_id,
                sub_account_id: f.sub_account_id.clone(),
                copy_amount: f.copy_amount.clone(),
                margin_per_entry_order: f.margin_per_entry_order.clone(),
                ratio_multiplier: f.ratio_multiplier.clone(),
                minimum_entry: f.minimum_entry.clone(),
                max_margin_per_symbol: f.max_margin_per_symbol.clone(),
                max_margin_total: f.max_margin_total.clone(),
                excluded_symbols: f.excluded_symbols.clone(),
                stop_loss_amount: f.stop_loss_amount.clone(),
                margin_mode_source: f.margin_mode_source.clone(),
                leverage_source: f.leverage_source.clone(),
                max_leverage: f.max_leverage,
            }))
        }
        "copy_stop" => {
            let subscription_id = f.subscription_id.clone().filter(|s| path_segment_ok(s)).ok_or("copy_stop: subscription_id must be one path segment")?;
            Ok(Some(StrikeLeg::CopyStop { subscription_id, sub_account_id: f.sub_account_id.clone() }))
        }
        "copy_save" | "copy_unsave" => {
            let lead_account_id = f.lead_account_id.clone().filter(|s| path_segment_ok(s)).ok_or(format!("{k}: lead_account_id must be one path segment"))?;
            if k == "copy_save" {
                Ok(Some(StrikeLeg::CopySave { lead_account_id }))
            } else {
                Ok(Some(StrikeLeg::CopyUnsave { lead_account_id }))
            }
        }
        "rfq" => {
            let client_rfq_id = f.client_rfq_id.clone().ok_or("rfq: client_rfq_id is required")?;
            if !client_rfq_id_ok(&client_rfq_id) {
                return Err("rfq: client_rfq_id is 1..=64 of [A-Za-z0-9._:-]".into());
            }
            if f.symbol.is_empty() {
                return Err("rfq: symbol is required".into());
            }
            let requested_size = f.requested_size.clone().ok_or("rfq: requested_size is required")?;
            gt0("requested_size", &requested_size)?;
            if let Some(side) = &f.side {
                if side != "buy" && side != "sell" {
                    return Err("rfq: side is buy or sell, or omitted".into());
                }
            }
            if f.vault_id.is_some() && f.sub_account_id.is_some() {
                return Err("rfq: vault_id and sub_account_id together are refused".into());
            }
            Ok(Some(StrikeLeg::Rfq {
                client_rfq_id,
                symbol: f.symbol.clone(),
                requested_size,
                side: f.side.clone(),
                vault_id: f.vault_id.clone(),
                sub_account_id: f.sub_account_id.clone(),
            }))
        }
        "rfq_close" => {
            let rfq_id = f.rfq_id.clone().filter(|s| path_segment_ok(s)).ok_or("rfq_close: rfq_id must be one path segment")?;
            if f.vault_id.is_some() && f.sub_account_id.is_some() {
                return Err("rfq_close: vault_id and sub_account_id together are refused".into());
            }
            Ok(Some(StrikeLeg::RfqClose { rfq_id, vault_id: f.vault_id.clone(), sub_account_id: f.sub_account_id.clone() }))
        }
        "rfq_response" => {
            let rfq_id = f.rfq_id.clone().filter(|s| path_segment_ok(s)).ok_or("rfq_response: rfq_id must be one path segment")?;
            let status = f.response_status.clone().ok_or("rfq_response: response_status is required")?;
            if status != "acked" && status != "quoted" && status != "uninterested" {
                return Err("rfq_response: response_status is acked, quoted or uninterested".into());
            }
            if status != "quoted" && (f.bid_size.is_some() || f.ask_size.is_some()) {
                return Err("rfq_response: bid_size and ask_size belong to quoted".into());
            }
            if let Some(s) = &f.bid_size { gt0("bid_size", s)?; }
            if let Some(s) = &f.ask_size { gt0("ask_size", s)?; }
            Ok(Some(StrikeLeg::RfqResponse { rfq_id, status, bid_size: f.bid_size.clone(), ask_size: f.ask_size.clone() }))
        }
        "withdraw_quote" => {
            let blockchain = f.blockchain.clone().ok_or("withdraw_quote: blockchain is required")?;
            if blockchain != "ethereum" && blockchain != "solana" && blockchain != "cardano" {
                return Err("withdraw_quote: blockchain is ethereum, solana or cardano".into());
            }
            let usd_amount = f.usd_amount.clone().ok_or("withdraw_quote: usd_amount is required")?;
            gt0("usd_amount", &usd_amount)?;
            Ok(Some(StrikeLeg::WithdrawQuote { blockchain, usd_amount, asset: f.asset.clone() }))
        }
        "withdraw" => {
            let withdraw_id = f.withdraw_id.clone().filter(|s| path_segment_ok(s)).ok_or("withdraw: withdraw_id must be one path segment")?;
            let wallet_signature = f.wallet_signature.clone().filter(|s| !s.is_empty() && s.len() <= 4096).ok_or("withdraw: wallet_signature is the human signature of the quote message")?;
            Ok(Some(StrikeLeg::Withdraw { withdraw_id, wallet_signature }))
        }
        "vault_deposit" => {
            let vault_id = f.vault_id.clone().filter(|s| path_segment_ok(s)).ok_or("vault_deposit: vault_id must be one path segment")?;
            let usd_amount = f.usd_amount.clone().ok_or("vault_deposit: usd_amount is required")?;
            gt0("usd_amount", &usd_amount)?;
            Ok(Some(StrikeLeg::VaultDeposit { vault_id, usd_amount }))
        }
        "vault_withdraw" => {
            let vault_id = f.vault_id.clone().filter(|s| path_segment_ok(s)).ok_or("vault_withdraw: vault_id must be one path segment")?;
            if f.withdraw_all == f.shares.is_some() {
                return Err("vault_withdraw: shares or withdraw_all, not both and not neither".into());
            }
            if let Some(s) = &f.shares {
                gt0("shares", s)?;
                let frac = s.split_once('.').map(|(_, fr)| fr.len()).unwrap_or(0);
                if frac > 8 {
                    return Err("vault_withdraw: shares has at most 8 decimal places".into());
                }
            }
            Ok(Some(StrikeLeg::VaultWithdraw { vault_id, shares: f.shares.clone(), withdraw_all: f.withdraw_all }))
        }
        "vault_withdraw_cancel" => {
            let vault_id = f.vault_id.clone().filter(|s| path_segment_ok(s)).ok_or("vault_withdraw_cancel: vault_id must be one path segment")?;
            let withdrawal_id = f.withdrawal_id.clone().filter(|s| path_segment_ok(s)).ok_or("vault_withdraw_cancel: withdrawal_id must be one path segment")?;
            Ok(Some(StrikeLeg::VaultWithdrawCancel { vault_id, withdrawal_id }))
        }
        "vault_create" => {
            let name = f.vault_name.clone().filter(|s| !s.is_empty() && s.len() <= 255).ok_or("vault_create: vault_name is required (1..=255)")?;
            let initial_deposit = f.initial_deposit.clone().ok_or("vault_create: initial_deposit is required")?;
            let d = decimal::parse("initial_deposit", &initial_deposit)?;
            if d < rust_decimal::Decimal::ONE_HUNDRED {
                return Err("vault_create: initial_deposit must be at least 100".into());
            }
            if let Some(b) = f.leader_commission_bps {
                if b > 5000 {
                    return Err("vault_create: leader_commission_bps is 0..=5000".into());
                }
            }
            if let Some(d) = f.lockup_days {
                if !(1..=30).contains(&d) {
                    return Err("vault_create: lockup_days is 1..=30".into());
                }
            }
            if let Some(m) = &f.min_deposit {
                gt0("min_deposit", m)?;
            }
            Ok(Some(StrikeLeg::VaultCreate {
                name,
                description: f.description.clone(),
                initial_deposit,
                leader_commission_bps: f.leader_commission_bps,
                lockup_days: f.lockup_days,
                min_deposit: f.min_deposit.clone(),
                allow_proportional_close: f.allow_proportional_close,
            }))
        }
        "vault_pause" => {
            let vault_id = f.vault_id.clone().filter(|s| path_segment_ok(s)).ok_or("vault_pause: vault_id must be one path segment")?;
            Ok(Some(StrikeLeg::VaultPause { vault_id }))
        }
        "vault_resume" => {
            let vault_id = f.vault_id.clone().filter(|s| path_segment_ok(s)).ok_or("vault_resume: vault_id must be one path segment")?;
            Ok(Some(StrikeLeg::VaultResume { vault_id }))
        }
        _ => Ok(None),
    }
}

fn from_flat(f: FlatLeg) -> Result<StrikeLeg, String> {
    let k = f.kind.clone();
    if k != "twap" && (f.duration_sec.is_some() || f.randomize) {
        return Err(format!("{k}: duration_sec and randomize belong to kind twap"));
    }
    if k != "margin_mode" && f.margin_mode.is_some() {
        return Err(format!("{k}: margin_mode belongs to kind margin_mode"));
    }
    let grid_fields = f.grid_mode.is_some()
        || f.initialization_mode.is_some()
        || f.lower_price.is_some()
        || f.upper_price.is_some()
        || f.grid_count.is_some()
        || f.collateral.is_some()
        || f.sl_price.is_some()
        || f.long_sl_price.is_some()
        || f.short_sl_price.is_some();
    if k != "grid_bot" && grid_fields {
        return Err(format!("{k}: grid fields belong to kind grid_bot"));
    }
    if k != "grid_stop" && (f.bot_id.is_some() || f.cancel_orders) {
        return Err(format!("{k}: bot_id and cancel_orders belong to kind grid_stop"));
    }
    if k != "batch" && f.orders.is_some() {
        return Err(format!("{k}: orders belong to kind batch"));
    }
    if k != "replace_batch" && f.replacements.is_some() {
        return Err(format!("{k}: replacements belong to kind replace_batch"));
    }
    if k != "twap_cancel" && f.strategy_id.is_some() {
        return Err(format!("{k}: strategy_id belongs to kind twap_cancel"));
    }
    if k != "replace" && f.order_id.is_some() {
        return Err(format!("{k}: order_id belongs to kind replace"));
    }
    if k != "iso_margin" && (f.amount.is_some() || f.modify_type.is_some()) {
        return Err(format!("{k}: amount and modify_type belong to kind iso_margin"));
    }
    let order_extras =
        f.working_type.is_some() || f.close_position || f.price_protect || f.slippage.is_some();
    if order_extras && !matches!(k.as_str(), "order" | "bracket" | "replace") {
        return Err(format!(
            "{k}: working_type, close_position, price_protect and slippage belong to an order"
        ));
    }
    if f.vault_id.is_some()
        && !matches!(
            k.as_str(),
            "order"
                | "bracket"
                | "replace"
                | "cancel"
                | "cancel_all"
                | "leverage"
                | "margin_mode"
                | "iso_margin"
                | "batch"
                | "replace_batch"
                | "rfq"
                | "rfq_close"
                | "vault_deposit"
                | "vault_withdraw"
                | "vault_withdraw_cancel"
                | "vault_pause"
                | "vault_resume"
        )
    {
        return Err(format!("{k}: vault_id is not a field of this route"));
    }
    let none_order_fields = f.side.is_none()
        && f.order_type.is_none()
        && f.size.is_none()
        && f.price.is_none()
        && f.stop_price.is_none()
        && f.callback_rate.is_none()
        && f.activation_price.is_none()
        && f.time_in_force.is_none()
        && !f.post_only
        && !f.reduce_only
        && f.tp.is_none()
        && f.sl.is_none();
    let order = |f: FlatLeg| -> Result<OrderSpec, String> {
        if let Some(wt) = &f.working_type {
            if wt != "mark_price" && wt != "contract_price" {
                return Err("working_type is mark_price or contract_price".into());
            }
        }
        if let Some(s) = &f.slippage {
            decimal::parse("slippage", s)?;
        }
        Ok(OrderSpec {
            symbol: f.symbol,
            side: f.side.ok_or(format!("{k}: side is required"))?,
            order_type: f.order_type.ok_or(format!("{k}: type is required"))?,
            size: f.size.ok_or(format!("{k}: size is required"))?,
            price: f.price,
            stop_price: f.stop_price,
            time_in_force: f.time_in_force,
            post_only: f.post_only,
            reduce_only: f.reduce_only,
            callback_rate: f.callback_rate,
            activation_price: f.activation_price,
            leverage: f.leverage,
            working_type: f.working_type,
            close_position: f.close_position,
            price_protect: f.price_protect,
            slippage: f.slippage,
            vault_id: f.vault_id,
        })
    };
    if let Some(leg) = venue_from_flat(&k, &f)? {
        return Ok(leg);
    }
    match f.kind.as_str() {
        "order" => {
            if f.tp.is_some() || f.sl.is_some() || f.client_order_id.is_some() {
                return Err("order: tp/sl belong to kind bracket; client_order_id is chosen by Struck".into());
            }
            Ok(StrikeLeg::Order { order: order(f)? })
        }
        "bracket" => {
            if f.client_order_id.is_some() {
                return Err("bracket: client_order_id is chosen by Struck".into());
            }
            let (tp, sl) = (f.tp.clone(), f.sl.clone());
            Ok(StrikeLeg::Bracket { order: order(f)?, tp, sl })
        }
        "leverage" => match (none_order_fields && f.client_order_id.is_none(), f.leverage) {
            (true, Some(leverage)) => Ok(StrikeLeg::Leverage {
                symbol: f.symbol,
                leverage,
                vault_id: f.vault_id,
            }),
            _ => Err("leverage: exactly symbol + leverage".into()),
        },
        "cancel" => match (none_order_fields && f.leverage.is_none(), f.client_order_id) {
            (true, Some(client_order_id)) => Ok(StrikeLeg::Cancel {
                symbol: f.symbol,
                client_order_id,
                vault_id: f.vault_id,
            }),
            _ => Err("cancel: exactly symbol + client_order_id".into()),
        },
        "twap" => {
            if f.order_type.is_some()
                || f.stop_price.is_some()
                || f.callback_rate.is_some()
                || f.activation_price.is_some()
                || f.time_in_force.is_some()
                || f.post_only
                || f.tp.is_some()
                || f.sl.is_some()
                || f.leverage.is_some()
                || f.client_order_id.is_some()
            {
                return Err(
                    "twap: only symbol, side, size, duration_sec, price, reduce_only, randomize"
                        .into(),
                );
            }
            let side = f.side.clone().ok_or("twap: side is required")?;
            if side != "BUY" && side != "SELL" {
                return Err("twap: side is BUY or SELL".into());
            }
            let total_size = f.size.clone().ok_or("twap: size is required")?;
            let duration_sec = f.duration_sec.ok_or("twap: duration_sec is required")?;
            if !(300..=86_400).contains(&duration_sec) {
                return Err("twap: duration_sec must be 300..=86400".into());
            }
            Ok(StrikeLeg::Twap {
                symbol: f.symbol,
                side,
                total_size,
                duration_sec,
                limit_price: f.price,
                reduce_only: f.reduce_only,
                randomize: f.randomize,
            })
        }
        "margin_mode" => {
            let mode = f.margin_mode.clone().ok_or("margin_mode: margin_mode is required")?;
            if mode != "cross" && mode != "isolated" {
                return Err("margin_mode: marginMode is cross or isolated".into());
            }
            if !none_order_fields || f.leverage.is_some() || f.client_order_id.is_some() {
                return Err("margin_mode: exactly symbol + margin_mode".into());
            }
            Ok(StrikeLeg::MarginMode { symbol: f.symbol, margin_mode: mode, vault_id: f.vault_id })
        }
        "grid_bot" => {
            if !none_order_fields
                || f.leverage.is_some()
                || f.client_order_id.is_some()
                || f.duration_sec.is_some()
                || f.randomize
                || f.margin_mode.is_some()
            {
                return Err(
                    "grid_bot: only symbol, grid_mode, initialization_mode, lower_price, upper_price, grid_count, collateral, sl_price, long_sl_price, short_sl_price"
                        .into(),
                );
            }
            let mode = f.grid_mode.clone().ok_or("grid_bot: grid_mode is required")?;
            if !matches!(mode.as_str(), "LONG" | "SHORT" | "NEUTRAL") {
                return Err("grid_bot: grid_mode is LONG, SHORT or NEUTRAL (exact case)".into());
            }
            if let Some(im) = &f.initialization_mode {
                if !matches!(im.as_str(), "LIMIT_ONLY" | "MARKET_SEEDED") {
                    return Err("grid_bot: initialization_mode is LIMIT_ONLY or MARKET_SEEDED".into());
                }
                if mode == "NEUTRAL" {
                    return Err(
                        "grid_bot: initialization_mode MARKET_SEEDED is only supported for LONG or SHORT"
                            .into(),
                    );
                }
            }
            let lower_price =
                f.lower_price.clone().ok_or("grid_bot: lower_price is required")?;
            let upper_price =
                f.upper_price.clone().ok_or("grid_bot: upper_price is required")?;
            decimal::parse("lower_price", &lower_price)?;
            decimal::parse("upper_price", &upper_price)?;
            let grid_count = f.grid_count.ok_or("grid_bot: grid_count is required")?;
            if !(2..=200).contains(&grid_count) {
                return Err("grid_bot: grid_count must be between 2 and 200".into());
            }
            let collateral = f.collateral.clone().ok_or("grid_bot: collateral is required")?;
            decimal::parse("collateral", &collateral)?;
            match mode.as_str() {
                "NEUTRAL" => {
                    if f.sl_price.is_some() {
                        return Err(
                            "grid_bot: sl_price is not supported for NEUTRAL mode; use long_sl_price and/or short_sl_price"
                                .into(),
                        );
                    }
                    if let Some(l) = &f.long_sl_price {
                        decimal::parse("long_sl_price", l)?;
                    }
                    if let Some(s) = &f.short_sl_price {
                        decimal::parse("short_sl_price", s)?;
                    }
                }
                _ => {
                    if f.long_sl_price.is_some() || f.short_sl_price.is_some() {
                        return Err(
                            "grid_bot: long_sl_price and short_sl_price are only supported for NEUTRAL mode"
                                .into(),
                        );
                    }
                    if let Some(s) = &f.sl_price {
                        decimal::parse("sl_price", s)?;
                    }
                }
            }
            Ok(StrikeLeg::GridBot {
                symbol: f.symbol,
                mode,
                initialization_mode: f.initialization_mode,
                lower_price,
                upper_price,
                grid_count,
                collateral,
                sl_price: f.sl_price,
                long_sl_price: f.long_sl_price,
                short_sl_price: f.short_sl_price,
            })
        }
        "grid_stop" => {
            if !none_order_fields
                || f.leverage.is_some()
                || f.client_order_id.is_some()
                || f.duration_sec.is_some()
                || f.randomize
                || f.margin_mode.is_some()
                || grid_fields
            {
                return Err("grid_stop: exactly bot_id + cancel_orders".into());
            }
            let bot_id = f.bot_id.clone().ok_or("grid_stop: bot_id is required")?;
            if !grid_bot_id_ok(&bot_id) {
                return Err(
                    "grid_stop: bot_id must be a served id (grid-..., letters, digits, '-' and '_')"
                        .into(),
                );
            }
            Ok(StrikeLeg::GridStop { bot_id, cancel_orders: f.cancel_orders })
        }
        "cancel_all" => {
            if !none_order_fields || f.leverage.is_some() || f.client_order_id.is_some() {
                return Err("cancel_all: exactly symbol, optional vault_id".into());
            }
            if f.symbol.is_empty() {
                return Err("cancel_all: symbol is required".into());
            }
            Ok(StrikeLeg::CancelAll { symbol: f.symbol, vault_id: f.vault_id })
        }
        "iso_margin" => {
            if !none_order_fields || f.leverage.is_some() || f.client_order_id.is_some() {
                return Err("iso_margin: exactly symbol, amount, modify_type, optional vault_id".into());
            }
            let amount = f.amount.clone().ok_or("iso_margin: amount is required")?;
            decimal::parse("amount", &amount)?;
            let modify_type = f.modify_type.clone().ok_or("iso_margin: modify_type is required")?;
            if modify_type != "add" && modify_type != "remove" {
                return Err("iso_margin: modify_type is add or remove".into());
            }
            Ok(StrikeLeg::IsoMargin {
                symbol: f.symbol,
                amount,
                modify_type,
                vault_id: f.vault_id,
            })
        }
        "twap_cancel" => {
            if !none_order_fields
                || f.leverage.is_some()
                || f.client_order_id.is_some()
                || !f.symbol.is_empty()
            {
                return Err("twap_cancel: exactly strategy_id (symbol is empty)".into());
            }
            let strategy_id = f.strategy_id.clone().ok_or("twap_cancel: strategy_id is required")?;
            if !path_segment_ok(&strategy_id) {
                return Err("twap_cancel: strategy_id must be one URL path segment".into());
            }
            Ok(StrikeLeg::TwapCancel { strategy_id })
        }
        "replace" => {
            if f.tp.is_some() || f.sl.is_some() || f.client_order_id.is_some() {
                return Err("replace: tp/sl belong to kind bracket; client_order_id is chosen by Struck".into());
            }
            let order_id = f.order_id.clone().ok_or("replace: order_id is required")?;
            if !path_segment_ok(&order_id) {
                return Err("replace: order_id must be one path segment".into());
            }
            Ok(StrikeLeg::Replace { order_id, order: order(f)? })
        }
        "batch" => {
            if !none_order_fields || f.leverage.is_some() || f.client_order_id.is_some() {
                return Err("batch: exactly orders, optional vault_id".into());
            }
            let raw = f.orders.clone().ok_or("batch: orders is required")?;
            if raw.is_empty() {
                return Err("batch: orders must contain at least one order".into());
            }
            let orders = raw.into_iter().map(spec_from_flat_order).collect::<Result<Vec<_>, _>>()?;
            Ok(StrikeLeg::Batch { orders, vault_id: f.vault_id })
        }
        "replace_batch" => {
            if !none_order_fields || f.leverage.is_some() || f.client_order_id.is_some() {
                return Err("replace_batch: exactly replacements, optional vault_id".into());
            }
            let raw = f.replacements.clone().ok_or("replace_batch: replacements is required")?;
            if raw.is_empty() {
                return Err("replace_batch: replacements must contain at least one item".into());
            }
            let mut replacements = Vec::with_capacity(raw.len());
            for item in raw {
                if !path_segment_ok(&item.order_id) {
                    return Err("replace_batch: order_id must be one path segment".into());
                }
                if item.symbol != item.new_order.symbol {
                    return Err("replace_batch: cancel symbol must equal new_order.symbol".into());
                }
                replacements.push(Replacement {
                    order_id: item.order_id,
                    order: spec_from_flat_order(item.new_order)?,
                });
            }
            Ok(StrikeLeg::ReplaceBatch { replacements, vault_id: f.vault_id })
        }
        other => Err(format!("unknown leg kind {other:?}")),
    }
}
