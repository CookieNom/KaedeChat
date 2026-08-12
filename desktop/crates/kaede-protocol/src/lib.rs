mod generated;

use std::{fmt, str::FromStr};

pub use generated::*;
use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use thiserror::Error;

pub const MAX_SNOWFLAKE: u64 = i64::MAX as u64;

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct Snowflake(u64);

impl Snowflake {
    /// Creates a snowflake within Kaede's signed `PostgreSQL` `BIGINT` range.
    ///
    /// # Errors
    ///
    /// Returns [`IdError::OutOfRange`] when the value exceeds the protocol limit.
    pub fn new(value: u64) -> Result<Self, IdError> {
        if value > MAX_SNOWFLAKE {
            return Err(IdError::OutOfRange);
        }
        Ok(Self(value))
    }

    #[must_use]
    pub const fn get(self) -> u64 {
        self.0
    }
}

impl fmt::Display for Snowflake {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

impl FromStr for Snowflake {
    type Err = IdError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        if value.is_empty() || (value.len() > 1 && value.starts_with('0')) {
            return Err(IdError::NonCanonical);
        }
        let parsed = value.parse::<u64>().map_err(|_| IdError::InvalidInteger)?;
        Self::new(parsed)
    }
}

impl Serialize for Snowflake {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.collect_str(self)
    }
}

impl<'de> Deserialize<'de> for Snowflake {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        value.parse().map_err(de::Error::custom)
    }
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct Domain(String);

impl Domain {
    /// Parses and normalizes a DNS domain used in a federated identifier.
    ///
    /// # Errors
    ///
    /// Returns [`IdError::InvalidDomain`] for a malformed or oversized name.
    pub fn parse(value: impl AsRef<str>) -> Result<Self, IdError> {
        let normalized = value
            .as_ref()
            .trim()
            .trim_end_matches('.')
            .to_ascii_lowercase();
        if normalized.is_empty()
            || normalized.len() > 253
            || normalized.contains('/')
            || normalized.contains('@')
            || normalized.split('.').any(|label| {
                label.is_empty()
                    || label.len() > 63
                    || label.starts_with('-')
                    || label.ends_with('-')
                    || !label
                        .chars()
                        .all(|ch| ch.is_ascii_alphanumeric() || ch == '-')
            })
        {
            return Err(IdError::InvalidDomain);
        }
        Ok(Self(normalized))
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for Domain {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

impl FromStr for Domain {
    type Err = IdError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        Self::parse(value)
    }
}

impl<'de> Deserialize<'de> for Domain {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        Self::parse(String::deserialize(deserializer)?).map_err(de::Error::custom)
    }
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct EntityRef {
    pub id: Snowflake,
    pub domain: Domain,
}

impl Serialize for EntityRef {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.collect_str(self)
    }
}

impl<'de> Deserialize<'de> for EntityRef {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum WireRef {
            Canonical(String),
            Object {
                id: Snowflake,
                #[serde(alias = "origin_domain")]
                domain: Domain,
            },
        }

        match WireRef::deserialize(deserializer)? {
            WireRef::Canonical(value) => value.parse().map_err(de::Error::custom),
            WireRef::Object { id, domain } => Ok(Self::new(id, domain)),
        }
    }
}

impl EntityRef {
    #[must_use]
    pub const fn new(id: Snowflake, domain: Domain) -> Self {
        Self { id, domain }
    }
}

impl fmt::Display for EntityRef {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}@{}", self.id, self.domain)
    }
}

impl FromStr for EntityRef {
    type Err = IdError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let (id, domain) = value.split_once('@').ok_or(IdError::MissingDomain)?;
        if domain.contains('@') {
            return Err(IdError::InvalidDomain);
        }
        Ok(Self::new(id.parse()?, Domain::parse(domain)?))
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PermissionBits(#[serde(with = "decimal_u64")] pub u64);

impl PermissionBits {
    #[must_use]
    pub const fn contains(self, bit: u64) -> bool {
        self.0 & bit == bit
    }
}

pub mod decimal_u64 {
    use serde::{Deserialize, Deserializer, Serializer, de};

    /// Serializes an integer using the canonical decimal-string wire form.
    ///
    /// # Errors
    ///
    /// Propagates errors from the selected Serde serializer.
    pub fn serialize<S>(value: &u64, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.collect_str(value)
    }

    /// Deserializes a canonical unsigned decimal string.
    ///
    /// # Errors
    ///
    /// Returns a Serde error for non-canonical or out-of-range values.
    pub fn deserialize<'de, D>(deserializer: D) -> Result<u64, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        if value.is_empty() || (value.len() > 1 && value.starts_with('0')) {
            return Err(de::Error::custom("permission mask is not canonical"));
        }
        value.parse().map_err(de::Error::custom)
    }
}

pub mod optional_decimal_u64 {
    use serde::{Deserialize, Deserializer, Serializer, de};

    /// Serializes an optional integer using the decimal-string wire form.
    ///
    /// # Errors
    ///
    /// Propagates errors from the selected Serde serializer.
    pub fn serialize<S>(value: &Option<u64>, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        match value {
            Some(value) => serializer.serialize_some(&value.to_string()),
            None => serializer.serialize_none(),
        }
    }

    /// Deserializes an optional canonical unsigned decimal string.
    ///
    /// # Errors
    ///
    /// Returns a Serde error for non-canonical or out-of-range values.
    pub fn deserialize<'de, D>(deserializer: D) -> Result<Option<u64>, D::Error>
    where
        D: Deserializer<'de>,
    {
        let value = Option::<String>::deserialize(deserializer)?;
        value
            .map(|value| {
                if value.is_empty() || (value.len() > 1 && value.starts_with('0')) {
                    return Err(de::Error::custom("integer is not canonical"));
                }
                value.parse().map_err(de::Error::custom)
            })
            .transpose()
    }
}

/// Opaque optimistic-concurrency token supplied by the server.
///
/// Versions are deliberately not parsed as numbers: database-backed objects use
/// RFC 3339 timestamps while revision counters use canonical decimal strings.
pub type ResourceVersion = String;

#[derive(Debug, Error)]
pub enum IdError {
    #[error("identifier is not a canonical decimal string")]
    NonCanonical,
    #[error("identifier is not an unsigned integer")]
    InvalidInteger,
    #[error("identifier exceeds PostgreSQL BIGINT")]
    OutOfRange,
    #[error("federated identifier is missing an origin domain")]
    MissingDomain,
    #[error("origin domain is invalid")]
    InvalidDomain,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ApiError {
    pub code: String,
    pub message: String,
    pub trace_id: Option<String>,
    pub permissions: Option<PermissionBits>,
    pub retry_after_ms: Option<u64>,
    #[serde(default)]
    pub max_bytes: Option<u64>,
    #[serde(default)]
    pub timeout_until: Option<String>,
    #[serde(default)]
    pub timeout_indefinite: Option<bool>,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub errors: Vec<ValidationIssue>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ValidationIssue {
    #[serde(default, rename = "location", alias = "loc")]
    pub loc: Vec<serde_json::Value>,
    #[serde(rename = "message", alias = "msg")]
    pub msg: String,
    #[serde(rename = "type")]
    pub kind: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct GatewayEnvelope {
    pub op: u8,
    #[serde(default)]
    pub d: serde_json::Value,
    pub s: Option<u64>,
    pub t: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_are_canonical_and_origin_scoped() {
        assert!("0".parse::<Snowflake>().is_ok());
        assert!("01".parse::<Snowflake>().is_err());
        assert!("9223372036854775808".parse::<Snowflake>().is_err());
        let Ok(reference) = "42@Chat.Example.".parse::<EntityRef>() else {
            panic!("reference should be valid");
        };
        assert_eq!(reference.to_string(), "42@chat.example");
        let Ok(serialized) = serde_json::to_string(&reference) else {
            panic!("reference should serialize");
        };
        assert_eq!(serialized, "\"42@chat.example\"");
        let Ok(object): Result<EntityRef, _> =
            serde_json::from_str(r#"{"id":"42","origin_domain":"chat.example"}"#)
        else {
            panic!("object reference should deserialize");
        };
        assert_eq!(object, reference);
    }

    #[test]
    fn permission_bits_are_json_strings() {
        let Ok(bits): Result<PermissionBits, _> = serde_json::from_str("\"2048\"") else {
            panic!("permission mask should be valid");
        };
        assert!(bits.contains(permission::SEND_MESSAGES));
        let Ok(serialized) = serde_json::to_string(&bits) else {
            panic!("permission mask should serialize");
        };
        assert_eq!(serialized, "\"2048\"");
    }

    #[test]
    fn current_and_legacy_validation_issues_decode() {
        let current = serde_json::from_value::<ApiError>(serde_json::json!({
            "code": "VALIDATION_ERROR",
            "message": "The password field is required.",
            "trace_id": "trace.current-123",
            "retry_after_ms": 1250,
            "max_bytes": 5_242_880,
            "timeout_until": "2026-08-12T12:30:00Z",
            "timeout_indefinite": false,
            "reason": "Repeated spam",
            "errors": [{
                "location": ["body", "password"],
                "message": "Field required",
                "type": "missing"
            }]
        }));
        let Ok(current) = current else {
            panic!("current error envelope should decode");
        };
        assert_eq!(
            current.errors[0].loc,
            vec![serde_json::json!("body"), serde_json::json!("password")]
        );
        assert_eq!(current.errors[0].msg, "Field required");
        assert_eq!(current.retry_after_ms, Some(1_250));
        assert_eq!(current.max_bytes, Some(5_242_880));
        assert_eq!(
            current.timeout_until.as_deref(),
            Some("2026-08-12T12:30:00Z")
        );
        assert_eq!(current.timeout_indefinite, Some(false));
        assert_eq!(current.reason.as_deref(), Some("Repeated spam"));
        let serialized = serde_json::to_value(&current.errors[0]);
        let Ok(serialized) = serialized else {
            panic!("validation issue should encode");
        };
        assert_eq!(serialized["location"][1], "password");
        assert_eq!(serialized["message"], "Field required");
        assert!(serialized.get("loc").is_none());
        assert!(serialized.get("msg").is_none());

        let legacy = serde_json::from_value::<ApiError>(serde_json::json!({
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "trace_id": "legacy-trace",
            "errors": [{
                "loc": ["body", "display_name"],
                "msg": "Field is required",
                "type": "missing"
            }]
        }));
        let Ok(legacy) = legacy else {
            panic!("legacy error envelope should decode");
        };
        assert_eq!(legacy.errors[0].msg, "Field is required");
    }
}
