//! Narrow C ABI used by Kaede's native mobile clients.
//!
//! The ABI deliberately accepts and returns bounded JSON so Dart never needs
//! to mirror `OpenMLS` structs. Every allocation returned here has one matching
//! `kaede_e2ee_buffer_free` call.

#![allow(unsafe_code)]

use std::{
    collections::HashMap,
    ptr, slice,
    sync::{
        Mutex, OnceLock,
        atomic::{AtomicU64, Ordering},
    },
};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use kaede_e2ee::{MlsClient, PendingCommit, ProcessedMessage};
use serde_json::{Value, json};
use zeroize::{Zeroize, Zeroizing};

const MAX_INPUT_BYTES: usize = 64 * 1024 * 1024;

fn zeroize_json(value: &mut Value) {
    match value {
        Value::String(string) => drop(Zeroizing::new(std::mem::take(string))),
        Value::Array(values) => values.iter_mut().for_each(zeroize_json),
        Value::Object(values) => values.values_mut().for_each(zeroize_json),
        Value::Null | Value::Bool(_) | Value::Number(_) => {}
    }
}

#[repr(C)]
pub struct KaedeE2eeBuffer {
    pub data: *mut u8,
    pub len: usize,
}

impl KaedeE2eeBuffer {
    fn from_json(value: &Value) -> Self {
        let serialized = Zeroizing::new(value.to_string());
        let mut encoded = serialized.as_bytes().to_vec().into_boxed_slice();
        let result = Self {
            data: encoded.as_mut_ptr(),
            len: encoded.len(),
        };
        std::mem::forget(encoded);
        result
    }

    fn success(value: &Value) -> Self {
        let mut wrapped = json!({"ok": true, "result": value});
        let buffer = Self::from_json(&wrapped);
        zeroize_json(&mut wrapped);
        buffer
    }

    fn error(message: &str) -> Self {
        let mut wrapped = json!({"ok": false, "error": message});
        let buffer = Self::from_json(&wrapped);
        zeroize_json(&mut wrapped);
        buffer
    }
}

static CLIENTS: OnceLock<Mutex<HashMap<u64, MlsClient>>> = OnceLock::new();
static NEXT_HANDLE: AtomicU64 = AtomicU64::new(1);

fn clients() -> &'static Mutex<HashMap<u64, MlsClient>> {
    CLIENTS.get_or_init(|| Mutex::new(HashMap::new()))
}

unsafe fn input_bytes<'a>(data: *const u8, len: usize) -> Result<&'a [u8], String> {
    if data.is_null() || len > MAX_INPUT_BYTES {
        return Err("native E2EE input is invalid".into());
    }
    // SAFETY: The Dart FFI caller keeps the input allocation alive for the
    // duration of this synchronous call and the length was bounded above.
    Ok(unsafe { slice::from_raw_parts(data, len) })
}

fn decode(value: &Value, field: &str, maximum: usize) -> Result<Zeroizing<Vec<u8>>, String> {
    let encoded = value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{field} is required"))?;
    let decoded = URL_SAFE_NO_PAD
        .decode(encoded)
        .map_err(|_| format!("{field} is not canonical base64url"))?;
    if decoded.is_empty() || decoded.len() > maximum || URL_SAFE_NO_PAD.encode(&decoded) != encoded
    {
        return Err(format!("{field} is not canonical base64url"));
    }
    Ok(Zeroizing::new(decoded))
}

fn encode(value: &[u8]) -> String {
    URL_SAFE_NO_PAD.encode(value)
}

fn pending(value: &PendingCommit) -> Value {
    json!({"commit": encode(&value.commit), "welcome": encode(&value.welcome)})
}

fn allocate(client: MlsClient) -> Result<u64, String> {
    let handle = NEXT_HANDLE.fetch_add(1, Ordering::Relaxed);
    if handle == 0 {
        return Err("native E2EE handle space was exhausted".into());
    }
    clients()
        .lock()
        .map_err(|_| "native E2EE state lock is poisoned".to_owned())?
        .insert(handle, client);
    Ok(handle)
}

#[allow(clippy::too_many_lines)] // One bounded dispatcher keeps the C ABI narrow and auditable.
fn invoke(method: &str, handle: u64, value: &Value) -> Result<Value, String> {
    if method == "generate" {
        let credential = decode(value, "credential", 16_384)?;
        let client = MlsClient::generate(&credential).map_err(|error| error.to_string())?;
        return allocate(client).map(|created| json!({"handle": created.to_string()}));
    }
    if method == "restore" {
        let state = decode(value, "state", MAX_INPUT_BYTES)?;
        let client = MlsClient::restore_state(&state).map_err(|error| error.to_string())?;
        return allocate(client).map(|created| json!({"handle": created.to_string()}));
    }

    let guard = clients()
        .lock()
        .map_err(|_| "native E2EE state lock is poisoned".to_owned())?;
    let client = guard
        .get(&handle)
        .ok_or_else(|| "native E2EE client is closed".to_owned())?;
    match method {
        "export_state" => client
            .export_state()
            .map(|state| json!({"state": encode(&state)}))
            .map_err(|error| error.to_string()),
        "public_identity_key" => {
            Ok(json!({"bytes": encode(client.identity().public_identity_key())}))
        }
        "sign" => {
            let input = decode(value, "input", MAX_INPUT_BYTES)?;
            client
                .identity()
                .sign_server_challenge(&input)
                .map(|signature| json!({"bytes": encode(&signature)}))
                .map_err(|error| error.to_string())
        }
        "generate_key_package" => client
            .generate_key_package()
            .map(|package| json!({"bytes": encode(&package)}))
            .map_err(|error| error.to_string()),
        "inspect_key_package" => {
            let package = decode(value, "key_package", 32_768)?;
            client
                .inspect_key_package(&package)
                .map(|identity| {
                    json!({
                        "credential": encode(&identity.credential),
                        "signature_key": encode(&identity.signature_key),
                    })
                })
                .map_err(|error| error.to_string())
        }
        "create_group" => {
            let group_id = decode(value, "group_id", 128)?;
            client
                .create_group(&group_id)
                .map(|()| json!({}))
                .map_err(|error| error.to_string())
        }
        "add_members" => {
            let group_id = decode(value, "group_id", 128)?;
            let packages = value
                .get("key_packages")
                .and_then(Value::as_array)
                .ok_or_else(|| "key_packages is required".to_owned())?
                .iter()
                .map(|item| {
                    let encoded = item
                        .as_str()
                        .ok_or_else(|| "key package must be a string".to_owned())?;
                    let package = URL_SAFE_NO_PAD
                        .decode(encoded)
                        .map_err(|_| "key package is invalid".to_owned())?;
                    if package.is_empty()
                        || package.len() > 32_768
                        || URL_SAFE_NO_PAD.encode(&package) != encoded
                    {
                        return Err("key package is invalid".to_owned());
                    }
                    Ok(Zeroizing::new(package))
                })
                .collect::<Result<Vec<_>, _>>()?;
            client
                .add_members(&group_id, &packages)
                .map(|commit| pending(&commit))
                .map_err(|error| error.to_string())
        }
        "remove_accounts" => {
            let group_id = decode(value, "group_id", 128)?;
            let accounts = value
                .get("accounts")
                .and_then(Value::as_array)
                .ok_or_else(|| "accounts is required".to_owned())?
                .iter()
                .map(|item| {
                    item.as_str()
                        .map(str::to_owned)
                        .ok_or_else(|| "account must be a string".to_owned())
                })
                .collect::<Result<Vec<_>, _>>()?;
            client
                .remove_accounts(&group_id, &accounts)
                .map(|commit| pending(&commit))
                .map_err(|error| error.to_string())
        }
        "merge_pending_commit" => {
            let group_id = decode(value, "group_id", 128)?;
            client
                .merge_pending_commit(&group_id)
                .map(|()| json!({}))
                .map_err(|error| error.to_string())
        }
        "join_group" => {
            let welcome = decode(value, "welcome", 64 * 1024)?;
            client
                .join_group(&welcome)
                .map(|group_id| json!({"group_id": encode(&group_id)}))
                .map_err(|error| error.to_string())
        }
        "encrypt" => {
            let group_id = decode(value, "group_id", 128)?;
            let plaintext = decode(value, "plaintext", MAX_INPUT_BYTES)?;
            let aad = decode(value, "aad", 4096)?;
            client
                .encrypt(&group_id, &plaintext, &aad)
                .map(|ciphertext| json!({"bytes": encode(&ciphertext)}))
                .map_err(|error| error.to_string())
        }
        "has_group" => {
            let group_id = decode(value, "group_id", 128)?;
            client
                .has_group(&group_id)
                .map(|exists| json!({"exists": exists}))
                .map_err(|error| error.to_string())
        }
        "member_roster" => {
            let group_id = decode(value, "group_id", 128)?;
            client
                .member_roster(&group_id)
                .map(|roster| json!({"bytes": encode(&roster)}))
                .map_err(|error| error.to_string())
        }
        "group_epoch" => {
            let group_id = decode(value, "group_id", 128)?;
            client
                .group_epoch(&group_id)
                .map(|epoch| json!({"epoch": epoch.to_string()}))
                .map_err(|error| error.to_string())
        }
        "process" => {
            let group_id = decode(value, "group_id", 128)?;
            let message = decode(value, "message", 64 * 1024)?;
            client
                .process(&group_id, &message)
                .map(|processed| match processed {
                    ProcessedMessage::Application {
                        bytes,
                        aad,
                        credential,
                    } => {
                        json!({
                            "kind": "application",
                            "application": encode(&bytes),
                            "aad": encode(&aad),
                            "credential": encode(&credential),
                        })
                    }
                    ProcessedMessage::Proposal => json!({"kind": "proposal"}),
                    ProcessedMessage::Commit => json!({"kind": "commit"}),
                })
                .map_err(|error| error.to_string())
        }
        "export_epoch_secret" => {
            let group_id = decode(value, "group_id", 128)?;
            let context = decode(value, "context", 4096)?;
            let label = value
                .get("label")
                .and_then(Value::as_str)
                .ok_or_else(|| "label is required".to_owned())?;
            let length = value
                .get("length")
                .and_then(Value::as_u64)
                .and_then(|raw| usize::try_from(raw).ok())
                .ok_or_else(|| "length is invalid".to_owned())?;
            client
                .export_epoch_secret(&group_id, label, &context, length)
                .map(|secret| json!({"bytes": encode(&secret)}))
                .map_err(|error| error.to_string())
        }
        _ => Err("native E2EE method is unsupported".into()),
    }
}

/// Invoke one synchronous MLS operation.
///
/// # Safety
/// Both pointer/length pairs must reference readable memory for this call.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kaede_e2ee_invoke(
    handle: u64,
    method_data: *const u8,
    method_len: usize,
    input_data: *const u8,
    input_len: usize,
) -> KaedeE2eeBuffer {
    let result = (|| {
        let method = std::str::from_utf8(unsafe { input_bytes(method_data, method_len)? })
            .map_err(|_| "native E2EE method is invalid".to_owned())?;
        let input = Zeroizing::new(unsafe { input_bytes(input_data, input_len)? }.to_vec());
        let mut value: Value =
            serde_json::from_slice(&input).map_err(|_| "native E2EE JSON is invalid".to_owned())?;
        let result = invoke(method, handle, &value);
        zeroize_json(&mut value);
        result
    })();
    match result {
        Ok(mut value) => {
            let buffer = KaedeE2eeBuffer::success(&value);
            zeroize_json(&mut value);
            buffer
        }
        Err(error) => KaedeE2eeBuffer::error(&error),
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn kaede_e2ee_close(handle: u64) {
    if let Ok(mut guard) = clients().lock() {
        guard.remove(&handle);
    }
}

/// Release a buffer returned by [`kaede_e2ee_invoke`].
///
/// # Safety
/// `buffer` must be an unmodified buffer returned by this library and must be
/// released exactly once.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kaede_e2ee_buffer_free(buffer: KaedeE2eeBuffer) {
    if buffer.data.is_null() || buffer.len == 0 {
        return;
    }
    // SAFETY: Ownership of this exact boxed slice was transferred from
    // `KaedeE2eeBuffer::from_json` and the ABI requires a single matching free.
    let mut allocation =
        unsafe { Box::from_raw(ptr::slice_from_raw_parts_mut(buffer.data, buffer.len)) };
    allocation.zeroize();
    drop(allocation);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn group_epoch_is_a_decimal_string_from_the_native_state() -> Result<(), String> {
        let generated = invoke(
            "generate",
            0,
            &json!({"credential": encode(b"voice-bot@example.test")}),
        )?;
        let handle = generated["handle"]
            .as_str()
            .ok_or_else(|| "native handle is not a string".to_owned())?
            .parse::<u64>()
            .map_err(|error| error.to_string())?;
        let group_id = b"voice-media-group";
        invoke(
            "create_group",
            handle,
            &json!({"group_id": encode(group_id)}),
        )?;

        assert_eq!(
            invoke(
                "group_epoch",
                handle,
                &json!({"group_id": encode(group_id)}),
            )?,
            json!({"epoch": "0"})
        );
        kaede_e2ee_close(handle);
        Ok(())
    }

    #[test]
    fn nested_json_strings_are_zeroized_before_drop() {
        let mut value = json!({
            "plaintext": "secret",
            "nested": ["credential", {"state": "private"}],
        });
        zeroize_json(&mut value);
        assert_eq!(
            value,
            json!({"plaintext": "", "nested": ["", {"state": ""}]})
        );
    }
}
