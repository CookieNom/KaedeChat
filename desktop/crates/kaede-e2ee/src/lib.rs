//! Shared RFC 9420 client core for every Kaede client binding.
//!
//! This crate deliberately owns MLS state transitions while host clients own
//! secure persistence and network delivery. No server secret or room key is
//! accepted by this API.

use openmls::{
    credentials::{BasicCredential, CredentialWithKey},
    framing::{MlsMessageBodyIn, MlsMessageIn, MlsMessageOut, ProcessedMessageContent},
    group::{GroupId, MlsGroup, MlsGroupJoinConfig, StagedWelcome},
    key_packages::{KeyPackage, KeyPackageIn},
    prelude::{ProtocolVersion, SignatureScheme},
};
use openmls_basic_credential::SignatureKeyPair;
use openmls_rust_crypto::OpenMlsRustCrypto;
use openmls_traits::{OpenMlsProvider, signatures::Signer, types::Ciphersuite};
use serde::{Deserialize, Serialize};
use tls_codec::{Deserialize as TlsDeserialize, Serialize as TlsSerialize};
use zeroize::{Zeroize, Zeroizing};

#[cfg(target_arch = "wasm32")]
mod wasm;

pub const PROTOCOL: &str = "mls10";
pub const SUITE_NAME: &str = "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519";
pub const CIPHERSUITE: Ciphersuite = Ciphersuite::MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519;
const STATE_FORMAT_VERSION: u8 = 1;
const MAX_STATE_BYTES: usize = 64 * 1024 * 1024;
const MAX_STATE_ENTRIES: usize = 100_000;

#[derive(Debug, thiserror::Error)]
pub enum MlsError {
    #[error("MLS identity creation failed: {0}")]
    Identity(String),
    #[error("MLS key package is invalid: {0}")]
    KeyPackage(String),
    #[error("MLS group operation failed: {0}")]
    Group(String),
    #[error("MLS message is invalid: {0}")]
    Message(String),
    #[error("MLS group does not exist on this device")]
    GroupNotFound,
    #[error("MLS persisted state is invalid: {0}")]
    State(String),
}

#[derive(Debug)]
pub struct DeviceIdentity {
    credential: CredentialWithKey,
    credential_identity: Vec<u8>,
    signer: SignatureKeyPair,
}

impl DeviceIdentity {
    /// Generate a device signing identity bound to the supplied credential.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::Identity`] if the credential is invalid or the
    /// signing key cannot be generated and persisted.
    pub fn generate(provider: &OpenMlsRustCrypto, credential: &[u8]) -> Result<Self, MlsError> {
        if credential.is_empty() || credential.len() > 16_384 {
            return Err(MlsError::Identity("credential length is invalid".into()));
        }
        let signer = SignatureKeyPair::new(SignatureScheme::ED25519)
            .map_err(|error| MlsError::Identity(error.to_string()))?;
        signer
            .store(provider.storage())
            .map_err(|error| MlsError::Identity(error.to_string()))?;
        let credential_identity = credential.to_vec();
        let credential = CredentialWithKey {
            credential: BasicCredential::new(credential_identity.clone()).into(),
            signature_key: signer.public().into(),
        };
        Ok(Self {
            credential,
            credential_identity,
            signer,
        })
    }

    #[must_use]
    pub fn public_identity_key(&self) -> &[u8] {
        self.signer.public()
    }

    /// Sign a server registration challenge with this device identity.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::Identity`] if the signature operation fails.
    pub fn sign_server_challenge(&self, challenge: &[u8]) -> Result<Vec<u8>, MlsError> {
        self.signer
            .sign(challenge)
            .map_err(|error| MlsError::Identity(format!("{error:?}")))
    }
}

#[derive(Debug)]
pub struct PendingCommit {
    pub commit: Vec<u8>,
    pub welcome: Vec<u8>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum ProcessedMessage {
    Application {
        bytes: Vec<u8>,
        aad: Vec<u8>,
        credential: Vec<u8>,
    },
    Proposal,
    Commit,
}

#[derive(Debug)]
pub struct MlsClient {
    provider: OpenMlsRustCrypto,
    identity: DeviceIdentity,
}

impl MlsClient {
    /// Create a new MLS client and device identity.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::Identity`] if device identity creation fails.
    pub fn generate(credential: &[u8]) -> Result<Self, MlsError> {
        let provider = OpenMlsRustCrypto::default();
        let identity = DeviceIdentity::generate(&provider, credential)?;
        Ok(Self { provider, identity })
    }

    /// Restore client state after the host has authenticated and decrypted it.
    ///
    /// Callers must treat both the input and the value returned by
    /// [`Self::export_state`] as secret key material and seal it immediately.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::State`] if the snapshot is malformed, oversized,
    /// internally inconsistent, or cannot be restored.
    pub fn restore_state(state: &[u8]) -> Result<Self, MlsError> {
        if state.is_empty() || state.len() > MAX_STATE_BYTES {
            return Err(MlsError::State("state length is invalid".into()));
        }
        let mut snapshot: StateSnapshot =
            serde_json::from_slice(state).map_err(|error| MlsError::State(error.to_string()))?;
        if snapshot.version != STATE_FORMAT_VERSION
            || snapshot.credential_identity.is_empty()
            || snapshot.credential_identity.len() > 16_384
            || snapshot.signing_public.len() != 32
            || snapshot.entries.len() > MAX_STATE_ENTRIES
        {
            snapshot.zeroize();
            return Err(MlsError::State("state metadata is invalid".into()));
        }

        let provider = OpenMlsRustCrypto::default();
        {
            let mut values = provider
                .storage()
                .values
                .write()
                .map_err(|_| MlsError::State("state lock is poisoned".into()))?;
            for (key, value) in &snapshot.entries {
                if key.is_empty() || key.len() > 64 * 1024 || value.len() > MAX_STATE_BYTES {
                    drop(values);
                    snapshot.zeroize();
                    return Err(MlsError::State("state entry is invalid".into()));
                }
                if values.insert(key.clone(), value.clone()).is_some() {
                    drop(values);
                    snapshot.zeroize();
                    return Err(MlsError::State("state contains a duplicate key".into()));
                }
            }
        }
        let signer = SignatureKeyPair::read(
            provider.storage(),
            &snapshot.signing_public,
            SignatureScheme::ED25519,
        );
        let Some(signer) = signer else {
            snapshot.zeroize();
            return Err(MlsError::State("signing key is missing".into()));
        };
        if signer.public() != snapshot.signing_public {
            snapshot.zeroize();
            return Err(MlsError::State(
                "signing key does not match identity".into(),
            ));
        }
        let credential_identity = snapshot.credential_identity.clone();
        let credential = CredentialWithKey {
            credential: BasicCredential::new(credential_identity.clone()).into(),
            signature_key: signer.public().into(),
        };
        snapshot.zeroize();
        Ok(Self {
            provider,
            identity: DeviceIdentity {
                credential,
                credential_identity,
                signer,
            },
        })
    }

    /// Export all MLS state for immediate authenticated encryption by the host.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::State`] if state cannot be read or safely encoded.
    pub fn export_state(&self) -> Result<Zeroizing<Vec<u8>>, MlsError> {
        let values = self
            .provider
            .storage()
            .values
            .read()
            .map_err(|_| MlsError::State("state lock is poisoned".into()))?;
        if values.len() > MAX_STATE_ENTRIES {
            return Err(MlsError::State("too many state entries".into()));
        }
        let mut snapshot = StateSnapshot {
            version: STATE_FORMAT_VERSION,
            credential_identity: self.identity.credential_identity.clone(),
            signing_public: self.identity.signer.public().to_vec(),
            entries: values
                .iter()
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect(),
        };
        drop(values);
        let encoded =
            serde_json::to_vec(&snapshot).map_err(|error| MlsError::State(error.to_string()))?;
        snapshot.zeroize();
        if encoded.len() > MAX_STATE_BYTES {
            return Err(MlsError::State("serialized state is too large".into()));
        }
        Ok(Zeroizing::new(encoded))
    }

    #[must_use]
    pub fn identity(&self) -> &DeviceIdentity {
        &self.identity
    }

    /// Generate a one-use MLS key package for this device.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::KeyPackage`] if package construction or encoding fails.
    pub fn generate_key_package(&self) -> Result<Vec<u8>, MlsError> {
        let package = KeyPackage::builder()
            .build(
                CIPHERSUITE,
                &self.provider,
                &self.identity.signer,
                self.identity.credential.clone(),
            )
            .map_err(|error| MlsError::KeyPackage(error.to_string()))?;
        package
            .key_package()
            .tls_serialize_detached()
            .map_err(|error| MlsError::KeyPackage(error.to_string()))
    }

    /// Create a new MLS group with this device as its initial member.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::Group`] if the group identifier is invalid or group
    /// creation fails.
    pub fn create_group(&self, group_id: &[u8]) -> Result<(), MlsError> {
        if group_id.is_empty() || group_id.len() > 128 {
            return Err(MlsError::Group("group ID length is invalid".into()));
        }
        MlsGroup::builder()
            .ciphersuite(CIPHERSUITE)
            .use_ratchet_tree_extension(true)
            .with_group_id(GroupId::from_slice(group_id))
            .build(
                &self.provider,
                &self.identity.signer,
                self.identity.credential.clone(),
            )
            .map_err(|error| MlsError::Group(error.to_string()))?;
        Ok(())
    }

    /// Stage a commit that adds the supplied device key packages.
    ///
    /// # Errors
    ///
    /// Returns an [`MlsError`] if the group or a key package is invalid, or if
    /// the commit and Welcome messages cannot be created.
    pub fn add_members(
        &self,
        group_id: &[u8],
        key_packages: &[Vec<u8>],
    ) -> Result<PendingCommit, MlsError> {
        let mut group = self.load_group(group_id)?;
        let packages = key_packages
            .iter()
            .map(|bytes| self.decode_key_package(bytes))
            .collect::<Result<Vec<_>, _>>()?;
        let (commit, welcome, _) = group
            .add_members(&self.provider, &self.identity.signer, &packages)
            .map_err(|error| MlsError::Group(error.to_string()))?;
        Ok(PendingCommit {
            commit: serialize_message(&commit)?,
            welcome: serialize_message(&welcome)?,
        })
    }

    /// Stage a commit removing every MLS leaf owned by the named accounts.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::Group`] if the group cannot be loaded, no requested
    /// account is present, or the removal commit fails.
    pub fn remove_accounts(
        &self,
        group_id: &[u8],
        accounts: &[String],
    ) -> Result<PendingCommit, MlsError> {
        let mut group = self.load_group(group_id)?;
        let account_set = accounts
            .iter()
            .map(String::as_str)
            .collect::<std::collections::HashSet<_>>();
        let targets = group
            .members()
            .filter_map(|member| {
                let identity = std::str::from_utf8(member.credential.serialized_content()).ok()?;
                let parsed = serde_json::from_str::<serde_json::Value>(identity).ok()?;
                let account = parsed.get("account")?.as_str()?;
                account_set.contains(account).then_some(member.index)
            })
            .collect::<Vec<_>>();
        if targets.is_empty() {
            return Err(MlsError::Group("no matching MLS members were found".into()));
        }
        let (commit, welcome, _) = group
            .remove_members(&self.provider, &self.identity.signer, &targets)
            .map_err(|error| MlsError::Group(error.to_string()))?;
        Ok(PendingCommit {
            commit: serialize_message(&commit)?,
            welcome: welcome
                .map(|message| serialize_message(&message))
                .transpose()?
                .unwrap_or_default(),
        })
    }

    /// Merge this client's staged local commit into the group state.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::Group`] if the group is missing or no valid staged
    /// commit can be merged.
    pub fn merge_pending_commit(&self, group_id: &[u8]) -> Result<(), MlsError> {
        let mut group = self.load_group(group_id)?;
        group
            .merge_pending_commit(&self.provider)
            .map_err(|error| MlsError::Group(error.to_string()))
    }

    /// Join the group carried by an MLS Welcome message.
    ///
    /// # Errors
    ///
    /// Returns an [`MlsError`] if the wire message is not a valid Welcome or
    /// the referenced key package cannot establish the group.
    pub fn join_group(&self, welcome: &[u8]) -> Result<Vec<u8>, MlsError> {
        let welcome = match deserialize_message(welcome)?.extract() {
            MlsMessageBodyIn::Welcome(welcome) => welcome,
            other => {
                return Err(MlsError::Message(format!(
                    "expected a Welcome message, got {other:?}"
                )));
            }
        };
        let group = StagedWelcome::new_from_welcome(
            &self.provider,
            &MlsGroupJoinConfig::builder().build(),
            welcome,
            None,
        )
        .map_err(|error| MlsError::Group(error.to_string()))?
        .into_group(&self.provider)
        .map_err(|error| MlsError::Group(error.to_string()))?;
        Ok(group.group_id().as_slice().to_vec())
    }

    /// Encrypt an MLS application message with authenticated application data.
    ///
    /// # Errors
    ///
    /// Returns an [`MlsError`] if the authenticated context is invalid, the
    /// group is missing, or encryption or serialization fails.
    pub fn encrypt(
        &self,
        group_id: &[u8],
        plaintext: &[u8],
        aad: &[u8],
    ) -> Result<Vec<u8>, MlsError> {
        if aad.is_empty() || aad.len() > 4096 {
            return Err(MlsError::Message(
                "authenticated context length is invalid".into(),
            ));
        }
        let mut group = self.load_group(group_id)?;
        group.set_aad(aad.to_vec());
        let message = group
            .create_message(&self.provider, &self.identity.signer, plaintext)
            .map_err(|error| MlsError::Message(error.to_string()))?;
        serialize_message(&message)
    }

    /// Report whether this client currently stores the named MLS group.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::Group`] if persistent group state cannot be read.
    pub fn has_group(&self, group_id: &[u8]) -> Result<bool, MlsError> {
        MlsGroup::load(self.provider.storage(), &GroupId::from_slice(group_id))
            .map(|group| group.is_some())
            .map_err(|error| MlsError::Group(error.to_string()))
    }

    /// Return the deterministic credential and signature-key roster as JSON.
    ///
    /// # Errors
    ///
    /// Returns an [`MlsError`] if the group is missing or its roster cannot be
    /// encoded.
    pub fn member_roster(&self, group_id: &[u8]) -> Result<Vec<u8>, MlsError> {
        let group = self.load_group(group_id)?;
        let mut members = group
            .members()
            .map(|member| {
                (
                    member.credential.serialized_content().to_vec(),
                    member.signature_key,
                )
            })
            .collect::<Vec<_>>();
        members.sort();
        serde_json::to_vec(&members).map_err(|error| MlsError::State(error.to_string()))
    }

    /// Authenticate and process an incoming MLS application, proposal, or commit.
    ///
    /// # Errors
    ///
    /// Returns an [`MlsError`] if the group is missing, the wire message is
    /// invalid, authentication fails, or the resulting state cannot be stored.
    pub fn process(
        &self,
        group_id: &[u8],
        wire_message: &[u8],
    ) -> Result<ProcessedMessage, MlsError> {
        let mut group = self.load_group(group_id)?;
        let protocol = deserialize_message(wire_message)?
            .try_into_protocol_message()
            .map_err(|error| MlsError::Message(error.to_string()))?;
        let processed = group
            .process_message(&self.provider, protocol)
            .map_err(|error| MlsError::Message(error.to_string()))?;
        let aad = processed.aad().to_vec();
        let credential = processed.credential().serialized_content().to_vec();
        match processed.into_content() {
            ProcessedMessageContent::ApplicationMessage(message) => {
                Ok(ProcessedMessage::Application {
                    bytes: message.into_bytes(),
                    aad,
                    credential,
                })
            }
            ProcessedMessageContent::ProposalMessage(proposal)
            | ProcessedMessageContent::ExternalJoinProposalMessage(proposal) => {
                group
                    .store_pending_proposal(self.provider.storage(), *proposal)
                    .map_err(|error| MlsError::Group(error.to_string()))?;
                Ok(ProcessedMessage::Proposal)
            }
            ProcessedMessageContent::StagedCommitMessage(commit) => {
                group
                    .merge_staged_commit(&self.provider, *commit)
                    .map_err(|error| MlsError::Group(error.to_string()))?;
                Ok(ProcessedMessage::Commit)
            }
        }
    }

    /// Derive a domain-separated secret from the current MLS epoch.
    ///
    /// # Errors
    ///
    /// Returns [`MlsError::Group`] if exporter parameters are unsafe, the group
    /// is missing, or secret derivation fails.
    pub fn export_epoch_secret(
        &self,
        group_id: &[u8],
        label: &str,
        context: &[u8],
        length: usize,
    ) -> Result<Zeroizing<Vec<u8>>, MlsError> {
        if !(16..=64).contains(&length) || !label.starts_with("kaede ") {
            return Err(MlsError::Group("exporter parameters are invalid".into()));
        }
        let group = self.load_group(group_id)?;
        group
            .export_secret(self.provider.crypto(), label, context, length)
            .map(Zeroizing::new)
            .map_err(|error| MlsError::Group(error.to_string()))
    }

    fn load_group(&self, group_id: &[u8]) -> Result<MlsGroup, MlsError> {
        MlsGroup::load(self.provider.storage(), &GroupId::from_slice(group_id))
            .map_err(|error| MlsError::Group(error.to_string()))?
            .ok_or(MlsError::GroupNotFound)
    }

    fn decode_key_package(&self, bytes: &[u8]) -> Result<KeyPackage, MlsError> {
        let mut input = bytes;
        let package = KeyPackageIn::tls_deserialize(&mut input)
            .map_err(|error| MlsError::KeyPackage(error.to_string()))?;
        if !input.is_empty() {
            return Err(MlsError::KeyPackage("trailing bytes are forbidden".into()));
        }
        package
            .validate(self.provider.crypto(), ProtocolVersion::Mls10)
            .map_err(|error| MlsError::KeyPackage(error.to_string()))
    }
}

#[derive(Serialize, Deserialize, Zeroize)]
struct StateSnapshot {
    version: u8,
    credential_identity: Vec<u8>,
    signing_public: Vec<u8>,
    entries: Vec<(Vec<u8>, Vec<u8>)>,
}

fn serialize_message(message: &MlsMessageOut) -> Result<Vec<u8>, MlsError> {
    message
        .tls_serialize_detached()
        .map_err(|error| MlsError::Message(error.to_string()))
}

fn deserialize_message(bytes: &[u8]) -> Result<MlsMessageIn, MlsError> {
    let mut input = bytes;
    let message = MlsMessageIn::tls_deserialize(&mut input)
        .map_err(|error| MlsError::Message(error.to_string()))?;
    if !input.is_empty() {
        return Err(MlsError::Message("trailing bytes are forbidden".into()));
    }
    Ok(message)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn two_devices_share_messages_and_domain_separated_epoch_secrets() -> Result<(), MlsError> {
        let alice = MlsClient::generate(b"alice@example.test/device-a")?;
        let bob = MlsClient::generate(b"bob@example.test/device-b")?;
        let group_id = b"kaede-test-group";

        alice.create_group(group_id)?;
        let pending = alice.add_members(group_id, &[bob.generate_key_package()?])?;
        alice.merge_pending_commit(group_id)?;
        assert_eq!(bob.join_group(&pending.welcome)?, group_id);

        let ciphertext = alice.encrypt(group_id, b"private hello", b"message-context")?;
        assert_eq!(
            bob.process(group_id, &ciphertext)?,
            ProcessedMessage::Application {
                bytes: b"private hello".to_vec(),
                aad: b"message-context".to_vec(),
                credential: b"alice@example.test/device-a".to_vec(),
            }
        );

        let alice_file_key =
            alice.export_epoch_secret(group_id, "kaede attachment v1", b"attachment-id", 32)?;
        let bob_file_key =
            bob.export_epoch_secret(group_id, "kaede attachment v1", b"attachment-id", 32)?;
        assert_eq!(*alice_file_key, *bob_file_key);
        assert_ne!(
            *alice_file_key,
            *alice.export_epoch_secret(group_id, "kaede livekit v1", b"call-id", 32)?
        );

        let alice_state = alice.export_state()?;
        let restored_alice = MlsClient::restore_state(&alice_state)?;
        assert_eq!(
            bob.process(
                group_id,
                &restored_alice.encrypt(group_id, b"after restart", b"restart-context")?
            )?,
            ProcessedMessage::Application {
                bytes: b"after restart".to_vec(),
                aad: b"restart-context".to_vec(),
                credential: b"alice@example.test/device-a".to_vec(),
            }
        );
        Ok(())
    }

    #[test]
    fn removing_an_account_rotates_the_epoch_and_excludes_it() -> Result<(), MlsError> {
        let alice_credential = br#"{"version":1,"account":"alice@example.test","nonce":"a"}"#;
        let bob_credential = br#"{"version":1,"account":"bob@example.test","nonce":"b"}"#;
        let alice = MlsClient::generate(alice_credential)?;
        let bob = MlsClient::generate(bob_credential)?;
        let group_id = b"kaede-removal-group";
        alice.create_group(group_id)?;
        let added = alice.add_members(group_id, &[bob.generate_key_package()?])?;
        alice.merge_pending_commit(group_id)?;
        bob.join_group(&added.welcome)?;

        let removed = alice.remove_accounts(group_id, &["bob@example.test".into()])?;
        assert!(removed.welcome.is_empty());
        alice.merge_pending_commit(group_id)?;
        assert!(bob.process(group_id, &removed.commit).is_ok());
        assert!(
            bob.process(
                group_id,
                &alice.encrypt(group_id, b"after removal", b"removal-context")?
            )
            .is_err()
        );
        Ok(())
    }
}
