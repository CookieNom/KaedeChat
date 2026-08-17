use js_sys::{Array, Uint8Array};
use wasm_bindgen::prelude::*;

use crate::{MlsClient, MlsError, PendingCommit, ProcessedMessage};

fn js_error(error: MlsError) -> JsError {
    JsError::new(&error.to_string())
}

#[wasm_bindgen(js_name = KaedeMlsPendingCommit)]
pub struct WasmPendingCommit {
    commit: Vec<u8>,
    welcome: Vec<u8>,
}

impl From<PendingCommit> for WasmPendingCommit {
    fn from(value: PendingCommit) -> Self {
        Self {
            commit: value.commit,
            welcome: value.welcome,
        }
    }
}

#[wasm_bindgen(js_class = KaedeMlsPendingCommit)]
impl WasmPendingCommit {
    #[wasm_bindgen(getter)]
    pub fn commit(&self) -> Vec<u8> {
        self.commit.clone()
    }

    #[wasm_bindgen(getter)]
    pub fn welcome(&self) -> Vec<u8> {
        self.welcome.clone()
    }
}

#[wasm_bindgen(js_name = KaedeMlsProcessedMessage)]
pub struct WasmProcessedMessage {
    kind: &'static str,
    application: Option<Vec<u8>>,
    aad: Option<Vec<u8>>,
    credential: Option<Vec<u8>>,
}

#[wasm_bindgen(js_class = KaedeMlsProcessedMessage)]
impl WasmProcessedMessage {
    #[wasm_bindgen(getter)]
    pub fn kind(&self) -> String {
        self.kind.into()
    }

    #[wasm_bindgen(getter)]
    pub fn application(&self) -> Option<Vec<u8>> {
        self.application.clone()
    }

    #[wasm_bindgen(getter)]
    pub fn aad(&self) -> Option<Vec<u8>> {
        self.aad.clone()
    }

    #[wasm_bindgen(getter)]
    pub fn credential(&self) -> Option<Vec<u8>> {
        self.credential.clone()
    }
}

#[wasm_bindgen(js_name = KaedeMlsClient)]
pub struct WasmMlsClient {
    inner: MlsClient,
}

#[wasm_bindgen(js_class = KaedeMlsClient)]
impl WasmMlsClient {
    #[wasm_bindgen(constructor)]
    pub fn new(credential: &[u8]) -> Result<WasmMlsClient, JsError> {
        Ok(Self {
            inner: MlsClient::generate(credential).map_err(js_error)?,
        })
    }

    #[wasm_bindgen(js_name = restoreState)]
    pub fn restore_state(state: &[u8]) -> Result<WasmMlsClient, JsError> {
        Ok(Self {
            inner: MlsClient::restore_state(state).map_err(js_error)?,
        })
    }

    #[wasm_bindgen(js_name = exportState)]
    pub fn export_state(&self) -> Result<Vec<u8>, JsError> {
        self.inner
            .export_state()
            .map(|value| value.to_vec())
            .map_err(js_error)
    }

    #[wasm_bindgen(js_name = publicIdentityKey)]
    pub fn public_identity_key(&self) -> Vec<u8> {
        self.inner.identity().public_identity_key().to_vec()
    }

    #[wasm_bindgen(js_name = signServerChallenge)]
    pub fn sign_server_challenge(&self, challenge: &[u8]) -> Result<Vec<u8>, JsError> {
        self.inner
            .identity()
            .sign_server_challenge(challenge)
            .map_err(js_error)
    }

    #[wasm_bindgen(js_name = generateKeyPackage)]
    pub fn generate_key_package(&self) -> Result<Vec<u8>, JsError> {
        self.inner.generate_key_package().map_err(js_error)
    }

    #[wasm_bindgen(js_name = createGroup)]
    pub fn create_group(&self, group_id: &[u8]) -> Result<(), JsError> {
        self.inner.create_group(group_id).map_err(js_error)
    }

    #[wasm_bindgen(js_name = addMembers)]
    pub fn add_members(
        &self,
        group_id: &[u8],
        key_packages: Array,
    ) -> Result<WasmPendingCommit, JsError> {
        let packages = key_packages
            .iter()
            .map(|value| Uint8Array::new(&value).to_vec())
            .collect::<Vec<_>>();
        self.inner
            .add_members(group_id, &packages)
            .map(WasmPendingCommit::from)
            .map_err(js_error)
    }

    #[wasm_bindgen(js_name = removeAccounts)]
    pub fn remove_accounts(
        &self,
        group_id: &[u8],
        accounts: Array,
    ) -> Result<WasmPendingCommit, JsError> {
        let accounts = accounts
            .iter()
            .map(|value| {
                value
                    .as_string()
                    .ok_or_else(|| JsError::new("account must be a string"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        self.inner
            .remove_accounts(group_id, &accounts)
            .map(WasmPendingCommit::from)
            .map_err(js_error)
    }

    #[wasm_bindgen(js_name = mergePendingCommit)]
    pub fn merge_pending_commit(&self, group_id: &[u8]) -> Result<(), JsError> {
        self.inner.merge_pending_commit(group_id).map_err(js_error)
    }

    #[wasm_bindgen(js_name = joinGroup)]
    pub fn join_group(&self, welcome: &[u8]) -> Result<Vec<u8>, JsError> {
        self.inner.join_group(welcome).map_err(js_error)
    }

    pub fn encrypt(
        &self,
        group_id: &[u8],
        plaintext: &[u8],
        aad: &[u8],
    ) -> Result<Vec<u8>, JsError> {
        self.inner
            .encrypt(group_id, plaintext, aad)
            .map_err(js_error)
    }

    #[wasm_bindgen(js_name = hasGroup)]
    pub fn has_group(&self, group_id: &[u8]) -> Result<bool, JsError> {
        self.inner.has_group(group_id).map_err(js_error)
    }

    #[wasm_bindgen(js_name = memberRoster)]
    pub fn member_roster(&self, group_id: &[u8]) -> Result<Vec<u8>, JsError> {
        self.inner.member_roster(group_id).map_err(js_error)
    }

    pub fn process(
        &self,
        group_id: &[u8],
        wire_message: &[u8],
    ) -> Result<WasmProcessedMessage, JsError> {
        let processed = self
            .inner
            .process(group_id, wire_message)
            .map_err(js_error)?;
        Ok(match processed {
            ProcessedMessage::Application {
                bytes,
                aad,
                credential,
            } => WasmProcessedMessage {
                kind: "application",
                application: Some(bytes),
                aad: Some(aad),
                credential: Some(credential),
            },
            ProcessedMessage::Proposal => WasmProcessedMessage {
                kind: "proposal",
                application: None,
                aad: None,
                credential: None,
            },
            ProcessedMessage::Commit => WasmProcessedMessage {
                kind: "commit",
                application: None,
                aad: None,
                credential: None,
            },
        })
    }

    #[wasm_bindgen(js_name = exportEpochSecret)]
    pub fn export_epoch_secret(
        &self,
        group_id: &[u8],
        label: &str,
        context: &[u8],
        length: usize,
    ) -> Result<Vec<u8>, JsError> {
        self.inner
            .export_epoch_secret(group_id, label, context, length)
            .map(|secret| secret.to_vec())
            .map_err(js_error)
    }
}
