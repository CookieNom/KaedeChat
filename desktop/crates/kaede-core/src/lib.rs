pub mod entities;
pub mod markup;
pub mod reducer;

use async_trait::async_trait;
use serde_json::Value;
use thiserror::Error;

pub use entities::*;
pub use reducer::*;

#[async_trait]
pub trait CryptoProvider: Send + Sync {
    async fn decrypt_message(&self, envelope: &Value) -> Result<String, CryptoError>;
    async fn encrypt_message(&self, plaintext: &str) -> Result<Value, CryptoError>;
}

#[derive(Debug, Error)]
pub enum CryptoError {
    #[error("encrypted messages are not configured for this account")]
    Unavailable,
    #[error("the encrypted message could not be processed")]
    InvalidEnvelope,
}

pub struct OpaqueCryptoProvider;

#[async_trait]
impl CryptoProvider for OpaqueCryptoProvider {
    async fn decrypt_message(&self, _envelope: &Value) -> Result<String, CryptoError> {
        Err(CryptoError::Unavailable)
    }

    async fn encrypt_message(&self, _plaintext: &str) -> Result<Value, CryptoError> {
        Err(CryptoError::Unavailable)
    }
}
