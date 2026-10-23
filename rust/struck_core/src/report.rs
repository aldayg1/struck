//! The one check-report shape shared by the plan check (N2) and the
//! congruence check (N5c): `{ok, checks: [{id, expected, actual, ok}]}`.

use serde::Serialize;

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Check {
    pub id: String,
    pub expected: String,
    pub actual: String,
    pub ok: bool,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq, Eq)]
pub struct Report {
    pub ok: bool,
    pub checks: Vec<Check>,
}

impl Report {
    pub fn new() -> Report {
        Report { ok: true, checks: Vec::new() }
    }

    pub fn push(&mut self, id: &str, expected: String, actual: String, ok: bool) {
        self.ok = self.ok && ok;
        self.checks.push(Check { id: id.to_string(), expected, actual, ok });
    }

    pub fn failed_ids(&self) -> Vec<String> {
        self.checks.iter().filter(|c| !c.ok).map(|c| c.id.clone()).collect()
    }
}
