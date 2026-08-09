//! Restricted in-app Cloudflare Turnstile challenge surface.
//!
//! The challenge `WebView` runs in a short-lived helper process, isolated from
//! the main Tauri window. Credentials never enter the helper; it receives only
//! a public challenge URL and returns one nonce-bound provider token through a
//! private stdout pipe.

use std::{env, process::Stdio};

use async_trait::async_trait;
use kaede_platform::{PlatformError, TurnstileBroker, TurnstileChallenge};
use secrecy::SecretString;
use serde::{Deserialize, Serialize};
#[cfg(feature = "embedded-webview")]
use tao::{
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoopBuilder},
    window::WindowBuilder,
};
use tokio::{io::AsyncReadExt, process::Command, time};
use url::Url;
#[cfg(feature = "embedded-webview")]
use wry::{WebViewBuilder, http::Request};

pub const HELPER_FLAG: &str = "--kaede-native-challenge";
const HELPER_TIMEOUT: time::Duration = time::Duration::from_secs(180);
const MAX_HELPER_OUTPUT: usize = 4096;

#[derive(Clone, Default)]
pub struct EmbeddedTurnstile;

#[derive(Debug, Deserialize, Serialize)]
struct ChallengeMessage {
    kind: String,
    request_id: String,
    value: String,
}

#[async_trait]
impl TurnstileBroker for EmbeddedTurnstile {
    async fn solve(&self, challenge: TurnstileChallenge) -> Result<SecretString, PlatformError> {
        let challenge_url = challenge_url(&challenge)?;
        let executable =
            env::current_exe().map_err(|error| PlatformError::Other(error.to_string()))?;
        let mut child = Command::new(executable)
            .arg(HELPER_FLAG)
            .arg(challenge_url.as_str())
            .arg(challenge.origin.as_str())
            .arg(&challenge.request_id)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|error| PlatformError::Other(error.to_string()))?;
        let output = child.stdout.take().ok_or_else(|| {
            PlatformError::Other("challenge helper has no output pipe".to_owned())
        })?;
        let mut bytes = Vec::new();
        time::timeout(
            HELPER_TIMEOUT,
            output
                .take(MAX_HELPER_OUTPUT as u64)
                .read_to_end(&mut bytes),
        )
        .await
        .map_err(|_| PlatformError::ChallengeCancelled)?
        .map_err(|error| PlatformError::Other(error.to_string()))?;
        let _status = child
            .wait()
            .await
            .map_err(|error| PlatformError::Other(error.to_string()))?;
        let message: ChallengeMessage = serde_json::from_slice(&bytes)
            .map_err(|error| PlatformError::Other(error.to_string()))?;
        if message.kind != "complete" || message.request_id != challenge.request_id {
            return Err(PlatformError::ChallengeCancelled);
        }
        if message.value.is_empty() || message.value.len() > 2048 {
            return Err(PlatformError::Other(
                "challenge returned an invalid token".to_owned(),
            ));
        }
        Ok(SecretString::from(message.value))
    }
}

fn challenge_url(challenge: &TurnstileChallenge) -> Result<Url, PlatformError> {
    let mut url = challenge
        .origin
        .join("/api/v1/auth/native-challenge")
        .map_err(|error| PlatformError::Other(error.to_string()))?;
    url.query_pairs_mut()
        .append_pair("action", &challenge.action)
        .append_pair("request_id", &challenge.request_id);
    Ok(url)
}

#[cfg(feature = "embedded-webview")]
#[derive(Debug)]
enum HelperEvent {
    Ipc(String),
}

/// Run the challenge helper branch. This function owns the process UI loop and
/// therefore must be called before Tauri is initialized.
///
/// # Errors
///
/// Returns [`PlatformError`] when the URL leaves the permitted origins or the
/// platform web view cannot be constructed.
#[cfg(feature = "embedded-webview")]
pub fn run_helper(url: &str, origin: &str, request_id: &str) -> Result<(), PlatformError> {
    let url = Url::parse(url).map_err(|error| PlatformError::Other(error.to_string()))?;
    let origin = Url::parse(origin).map_err(|error| PlatformError::Other(error.to_string()))?;
    if url.scheme() != "https" || !same_origin(&url, &origin) {
        return Err(PlatformError::Other(
            "challenge URL is outside the home instance".to_owned(),
        ));
    }
    let event_loop = EventLoopBuilder::<HelperEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    let window = WindowBuilder::new()
        .with_title("Kaede verification")
        .with_inner_size(tao::dpi::LogicalSize::new(500.0, 430.0))
        .with_resizable(false)
        .build(&event_loop)
        .map_err(|error| PlatformError::Other(error.to_string()))?;
    let expected_origin = origin.clone();
    let builder = WebViewBuilder::new()
        .with_url(url.as_str())
        .with_devtools(false)
        .with_navigation_handler(move |target| {
            Url::parse(&target).is_ok_and(|target| {
                same_origin(&target, &expected_origin)
                    || target.host_str() == Some("challenges.cloudflare.com")
            })
        })
        .with_ipc_handler(move |request: Request<String>| {
            let _ = proxy.send_event(HelperEvent::Ipc(request.body().clone()));
        });

    #[cfg(any(target_os = "windows", target_os = "macos"))]
    let _webview = builder
        .build(&window)
        .map_err(|error| PlatformError::Other(error.to_string()))?;
    #[cfg(target_os = "linux")]
    let _webview = {
        use tao::platform::unix::WindowExtUnix;
        use wry::WebViewBuilderExtUnix;
        let container = window
            .default_vbox()
            .ok_or_else(|| PlatformError::Other("WebView container is unavailable".to_owned()))?;
        builder
            .build_gtk(container)
            .map_err(|error| PlatformError::Other(error.to_string()))?
    };

    let expected_request = request_id.to_owned();
    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => {
                *control_flow = ControlFlow::Exit;
            }
            Event::UserEvent(HelperEvent::Ipc(body)) => {
                if let Ok(message) = serde_json::from_str::<ChallengeMessage>(&body)
                    && message.request_id == expected_request
                    && message.kind == "complete"
                {
                    if let Ok(encoded) = serde_json::to_string(&message) {
                        println!("{encoded}");
                    }
                    *control_flow = ControlFlow::Exit;
                }
            }
            _ => {}
        }
    });
}

/// Report a clear startup error in builds that intentionally omit the native
/// `WebView` dependency (for example, contract-only CI on a minimal Linux host).
///
/// # Errors
///
/// Always returns an error because this build excludes the embedded challenge.
#[cfg(not(feature = "embedded-webview"))]
pub fn run_helper(_url: &str, _origin: &str, _request_id: &str) -> Result<(), PlatformError> {
    Err(PlatformError::Other(
        "this build does not include the embedded verification window".to_owned(),
    ))
}

#[cfg(feature = "embedded-webview")]
fn same_origin(left: &Url, right: &Url) -> bool {
    left.scheme() == right.scheme()
        && left.host_str() == right.host_str()
        && left.port_or_known_default() == right.port_or_known_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn challenge_url_is_bound_to_home_origin_and_nonce() {
        let Ok(origin) = Url::parse("https://chat.example/") else {
            panic!("test origin should parse");
        };
        let challenge = TurnstileChallenge {
            origin,
            site_key: "public-key".to_owned(),
            action: "kaede-login-v1".to_owned(),
            request_id: "nonce-1".to_owned(),
        };
        let Ok(url) = challenge_url(&challenge) else {
            panic!("challenge URL should build");
        };
        assert_eq!(url.host_str(), Some("chat.example"));
        assert!(url.as_str().contains("request_id=nonce-1"));
    }
}
