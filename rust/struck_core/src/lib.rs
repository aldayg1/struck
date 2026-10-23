//! Struck money core (law 3: all money code is Rust, integer base units
//! or `rust_decimal`, never floats, never rounding).
//!
//! * `money`       — `Lovelace`, `AssetId`, `AssetBag`, exact ADA <-> lovelace.
//! * `coinselect`  — PORT of 
//! * `plan`        — on-chain plan types + the deterministic N2 plan check.
//! * `decode`      — stock Pallas Conway decode of the exact CBOR to be signed.
//! * `congruence`  — N5c: decoded tx vs the APPROVED plan, check by check.
//! * `explain`     — N6c: plain-words explanation derived ONLY from the decode.
//! * `decimal`, `markets`, `strike_plan`, `strike_validate`,
//!   `strike_request` — the Strike lane (spec §6.3, B1): exact decimals,
//!   served market rules, N2 rule checks, N4s/N5s/N6s (no local Strike math:
//!   rule 19, Strike serves those values).
//! * `policy`      — spec D5 optional opt-in pre-approval policy.

pub mod coinselect;
pub mod congruence;
pub mod decode;
pub mod explain;
pub mod money;
pub mod plan;
pub mod report;
pub mod signed;
pub mod decimal;
pub mod markets;
pub mod strike_plan;
pub mod strike_request;
pub mod strike_validate;
pub mod policy;
pub mod reputation;
pub mod liens;
