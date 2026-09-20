use std::{future::Future, sync::Arc, time::Duration};

use futures_util::{SinkExt, StreamExt};
use kaede_protocol::{GatewayCloseCode, GatewayEnvelope, GatewayOp, PROTOCOL_VERSION};
use secrecy::{ExposeSecret, SecretString};
use serde_json::{Value, json};
use thiserror::Error;
use tokio::{
    sync::{Mutex, mpsc, watch},
    time,
};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use url::Url;

const IDENTIFY_TIMEOUT: Duration = Duration::from_secs(15);
const MAX_RECONNECT_DELAY: Duration = Duration::from_secs(30);

#[derive(Clone, Debug, Default)]
pub struct ResumeState {
    pub session_id: Option<String>,
    pub sequence: Option<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum GatewayStatus {
    Disconnected,
    Connecting,
    Connected,
    Reconnecting,
    AuthenticationFailed,
}

#[derive(Clone, Debug)]
pub enum GatewayCommand {
    Presence {
        status: String,
        custom_status: Option<String>,
    },
    RequestMembers {
        guild_id: String,
        guild_domain: String,
        query: String,
        limit: u16,
    },
    SubscribeMemberList {
        guild_id: String,
        guild_domain: String,
        ranges: Vec<(u32, u32)>,
    },
    RequestChannelInfo {
        guild_id: String,
        guild_domain: String,
        fields: Vec<String>,
    },
    RequestSoundboardSounds {
        guilds: Vec<(String, String)>,
    },
    VoiceState {
        self_mute: bool,
        self_deaf: bool,
    },
    Shutdown,
}

pub struct GatewayHandle {
    pub events: mpsc::Receiver<GatewayEnvelope>,
    pub commands: mpsc::Sender<GatewayCommand>,
    pub status: watch::Receiver<GatewayStatus>,
    pub resume: Arc<Mutex<ResumeState>>,
}

/// Open the gateway using current account credentials on each connection.
/// The provider receives `true` when the server rejected the previous token
/// and the account session must refresh it before retrying.
#[must_use]
pub fn spawn<F, Fut, E>(url: Url, token_provider: F) -> GatewayHandle
where
    F: Fn(bool) -> Fut + Send + 'static,
    Fut: Future<Output = Result<SecretString, E>> + Send,
    E: Send,
{
    let (event_tx, event_rx) = mpsc::channel(512);
    let (command_tx, command_rx) = mpsc::channel(128);
    let (status_tx, status_rx) = watch::channel(GatewayStatus::Disconnected);
    let resume = Arc::new(Mutex::new(ResumeState::default()));
    tokio::spawn(run(
        url,
        token_provider,
        event_tx,
        command_rx,
        status_tx,
        resume.clone(),
    ));
    GatewayHandle {
        events: event_rx,
        commands: command_tx,
        status: status_rx,
        resume,
    }
}

async fn run<F, Fut, E>(
    url: Url,
    token_provider: F,
    event_tx: mpsc::Sender<GatewayEnvelope>,
    mut command_rx: mpsc::Receiver<GatewayCommand>,
    status_tx: watch::Sender<GatewayStatus>,
    resume: Arc<Mutex<ResumeState>>,
) where
    F: Fn(bool) -> Fut + Send,
    Fut: Future<Output = Result<SecretString, E>> + Send,
    E: Send,
{
    let mut delay = Duration::from_secs(1);
    let mut refresh_token = false;
    loop {
        let _ = status_tx.send(if delay == Duration::from_secs(1) {
            GatewayStatus::Connecting
        } else {
            GatewayStatus::Reconnecting
        });
        let result = match token_provider(refresh_token).await {
            Ok(token) => {
                refresh_token = false;
                connect_once(
                    &url,
                    &token,
                    &event_tx,
                    &mut command_rx,
                    &status_tx,
                    &resume,
                )
                .await
            }
            Err(_) => Ok(ConnectionEnd::AuthenticationFailed),
        };
        if *status_tx.borrow() == GatewayStatus::Connected {
            delay = Duration::from_secs(1);
        }
        match result {
            Ok(ConnectionEnd::Shutdown) => break,
            Ok(ConnectionEnd::AuthenticationFailed) => {
                refresh_token = true;
                let _ = status_tx.send(GatewayStatus::AuthenticationFailed);
                time::sleep(delay).await;
                delay = (delay * 2).min(MAX_RECONNECT_DELAY);
            }
            Ok(ConnectionEnd::Reconnect) | Err(_) => {
                let _ = status_tx.send(GatewayStatus::Reconnecting);
                time::sleep(delay).await;
                delay = (delay * 2).min(MAX_RECONNECT_DELAY);
            }
        }
    }
    let _ = status_tx.send(GatewayStatus::Disconnected);
}

#[allow(clippy::too_many_lines)] // Keep the socket handshake and recovery decisions together.
async fn connect_once(
    url: &Url,
    token: &SecretString,
    event_tx: &mpsc::Sender<GatewayEnvelope>,
    command_rx: &mut mpsc::Receiver<GatewayCommand>,
    status_tx: &watch::Sender<GatewayStatus>,
    resume: &Arc<Mutex<ResumeState>>,
) -> Result<ConnectionEnd, GatewayError> {
    let (socket, _) = connect_async(url.as_str()).await?;
    let (mut writer, mut reader) = socket.split();
    let hello = time::timeout(IDENTIFY_TIMEOUT, reader.next())
        .await
        .map_err(|_| GatewayError::HelloTimeout)?
        .ok_or(GatewayError::Closed)??;
    let hello = decode_text(hello)?;
    if hello.op != GatewayOp::Hello as u8 {
        return Err(GatewayError::ExpectedHello);
    }
    let interval_ms = hello
        .d
        .get("heartbeat_interval")
        .and_then(Value::as_u64)
        .ok_or(GatewayError::InvalidHello)?;
    if !(1_000..=120_000).contains(&interval_ms) {
        return Err(GatewayError::InvalidHello);
    }
    let resume_snapshot = resume.lock().await.clone();
    let auth = if let (Some(session_id), Some(sequence)) =
        (resume_snapshot.session_id, resume_snapshot.sequence)
    {
        json!({
            "op": GatewayOp::Resume as u8,
            "d": {"token": token.expose_secret(), "session_id": session_id, "seq": sequence}
        })
    } else {
        json!({
            "op": GatewayOp::Identify as u8,
            "d": {"token": token.expose_secret(), "v": PROTOCOL_VERSION, "properties": {
                "os": std::env::consts::OS, "client": "kaede-desktop"
            }}
        })
    };
    writer.send(Message::Text(auth.to_string().into())).await?;
    let mut heartbeat = time::interval(Duration::from_millis(interval_ms));
    heartbeat.set_missed_tick_behavior(time::MissedTickBehavior::Delay);
    let mut heartbeat_acknowledged = true;
    let mut ready = false;
    let ready_deadline = time::sleep(IDENTIFY_TIMEOUT);
    tokio::pin!(ready_deadline);

    loop {
        tokio::select! {
            () = &mut ready_deadline, if !ready => return Ok(ConnectionEnd::Reconnect),
            _ = heartbeat.tick() => {
                if !heartbeat_acknowledged {
                    return Ok(ConnectionEnd::Reconnect);
                }
                heartbeat_acknowledged = false;
                let sequence = resume.lock().await.sequence;
                writer.send(Message::Text(json!({
                    "op": GatewayOp::Heartbeat as u8,
                    "d": sequence,
                }).to_string().into())).await?;
            }
            command = command_rx.recv() => {
                let Some(command) = command else { return Ok(ConnectionEnd::Shutdown); };
                if matches!(command, GatewayCommand::Shutdown) {
                    let _ = writer.close().await;
                    return Ok(ConnectionEnd::Shutdown);
                }
                writer.send(Message::Text(command_payload(command).to_string().into())).await?;
            }
            incoming = reader.next() => {
                let Some(incoming) = incoming else { return Ok(ConnectionEnd::Reconnect); };
                let incoming = incoming?;
                if let Message::Close(frame) = incoming {
                    if let Some(frame) = frame {
                        let code = u16::from(frame.code);
                        if code == GatewayCloseCode::AuthenticationFailed as u16
                            || code == GatewayCloseCode::NotAuthenticated as u16
                        {
                            return Ok(ConnectionEnd::AuthenticationFailed);
                        }
                        if code == GatewayCloseCode::InvalidSequence as u16 {
                            *resume.lock().await = ResumeState::default();
                        }
                    }
                    return Ok(ConnectionEnd::Reconnect);
                }
                if incoming.is_ping() {
                    writer.send(Message::Pong(incoming.into_data())).await?;
                    continue;
                }
                if !incoming.is_text() { continue; }
                let envelope = decode_text(incoming)?;
                match envelope.op {
                    op if op == GatewayOp::Dispatch as u8 => {
                        if matches!(envelope.t.as_deref(), Some("READY" | "RESUMED")) {
                            ready = true;
                            let _ = status_tx.send(GatewayStatus::Connected);
                        }
                        if let Some(sequence) = envelope.s {
                            resume.lock().await.sequence = Some(sequence);
                        }
                        if envelope.t.as_deref() == Some("READY")
                            && let Some(session_id) = envelope.d.get("session_id").and_then(Value::as_str)
                        {
                            resume.lock().await.session_id = Some(session_id.to_owned());
                        }
                        if event_tx.send(envelope).await.is_err() {
                            return Ok(ConnectionEnd::Shutdown);
                        }
                    }
                    op if op == GatewayOp::Reconnect as u8 => return Ok(ConnectionEnd::Reconnect),
                    op if op == GatewayOp::InvalidSession as u8 => {
                        *resume.lock().await = ResumeState::default();
                        return Ok(ConnectionEnd::Reconnect);
                    }
                    op if op == GatewayOp::HeartbeatAck as u8 => heartbeat_acknowledged = true,
                    _ => {}
                }
            }
        }
    }
}

fn command_payload(command: GatewayCommand) -> Value {
    match command {
        GatewayCommand::Presence {
            status,
            custom_status,
        } => json!({
            "op": GatewayOp::PresenceUpdate as u8,
            "d": {"status": status, "custom_status": custom_status},
        }),
        GatewayCommand::RequestMembers {
            guild_id,
            guild_domain,
            query,
            limit,
        } => {
            let guild_ref = gateway_guild_reference(&guild_id, &guild_domain);
            json!({
                "op": GatewayOp::RequestMembers as u8,
                "d": {"guild_id": guild_ref, "query": query, "limit": limit.min(100)},
            })
        }
        GatewayCommand::SubscribeMemberList {
            guild_id,
            guild_domain,
            ranges,
        } => {
            let guild_ref = gateway_guild_reference(&guild_id, &guild_domain);
            json!({
                "op": GatewayOp::SubscribeMemberList as u8,
                "d": {"guild_id": guild_ref,
                    "ranges": ranges.into_iter().take(3).map(|(start, end)| [start, end.min(start.saturating_add(99))]).collect::<Vec<_>>()},
            })
        }
        GatewayCommand::RequestChannelInfo {
            guild_id,
            guild_domain,
            fields,
        } => {
            let guild_ref = gateway_guild_reference(&guild_id, &guild_domain);
            json!({
                "op": GatewayOp::RequestChannelInfo as u8,
                "d": {"guild_id": guild_ref, "fields": fields},
            })
        }
        GatewayCommand::RequestSoundboardSounds { guilds } => json!({
            "op": GatewayOp::RequestSoundboardSounds as u8,
            "d": {"guild_ids": guilds.into_iter().map(|(id, domain)| gateway_guild_reference(&id, &domain)).collect::<Vec<_>>()},
        }),
        GatewayCommand::VoiceState {
            self_mute,
            self_deaf,
        } => json!({
            "op": GatewayOp::VoiceStateUpdate as u8,
            "d": {"self_mute": self_mute || self_deaf, "self_deaf": self_deaf},
        }),
        GatewayCommand::Shutdown => Value::Null,
    }
}

fn gateway_guild_reference(guild_id: &str, guild_domain: &str) -> String {
    if guild_domain.is_empty() || guild_id.contains('@') {
        guild_id.to_owned()
    } else {
        format!("{guild_id}@{guild_domain}")
    }
}

fn decode_text(message: Message) -> Result<GatewayEnvelope, GatewayError> {
    let text = message.into_text()?;
    serde_json::from_str(&text).map_err(GatewayError::Decode)
}

enum ConnectionEnd {
    Reconnect,
    AuthenticationFailed,
    Shutdown,
}

#[derive(Debug, Error)]
enum GatewayError {
    #[error("gateway transport failed: {0}")]
    Transport(Box<tokio_tungstenite::tungstenite::Error>),
    #[error("gateway did not send HELLO in time")]
    HelloTimeout,
    #[error("gateway closed the connection")]
    Closed,
    #[error("gateway did not start with HELLO")]
    ExpectedHello,
    #[error("gateway HELLO was invalid")]
    InvalidHello,
    #[error("gateway payload was invalid: {0}")]
    Decode(serde_json::Error),
}

impl From<tokio_tungstenite::tungstenite::Error> for GatewayError {
    fn from(error: tokio_tungstenite::tungstenite::Error) -> Self {
        Self::Transport(Box::new(error))
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::expect_used)]

    use super::*;

    #[tokio::test]
    #[allow(clippy::too_many_lines)] // One scripted exchange covers the full reconnect sequence.
    async fn reconnect_refreshes_credentials_and_recovers_a_rejected_resume() {
        use tokio::net::TcpListener;
        use tokio_tungstenite::{accept_async, tungstenite::protocol::CloseFrame};

        let listener = TcpListener::bind("127.0.0.1:0").await.expect("listener");
        let url = Url::parse(&format!(
            "ws://{}/gateway",
            listener.local_addr().expect("address")
        ))
        .expect("URL");
        let server = tokio::spawn(async move {
            for attempt in 0..4 {
                let (stream, _) = listener.accept().await.expect("connection");
                let mut socket = accept_async(stream).await.expect("WebSocket");
                socket
                    .send(Message::Text(
                        json!({
                            "op": GatewayOp::Hello as u8,
                            "d": {"heartbeat_interval": 1000},
                        })
                        .to_string()
                        .into(),
                    ))
                    .await
                    .expect("HELLO");
                let auth = decode_text(socket.next().await.expect("auth").expect("frame"))
                    .expect("auth payload");
                assert_eq!(
                    auth.d["token"],
                    if attempt == 0 { "expired" } else { "refreshed" }
                );
                assert_eq!(
                    auth.op,
                    if attempt == 2 {
                        GatewayOp::Resume
                    } else {
                        GatewayOp::Identify
                    } as u8
                );
                if attempt == 0 || attempt == 2 {
                    let code = if attempt == 0 {
                        GatewayCloseCode::AuthenticationFailed
                    } else {
                        assert_eq!(auth.d["seq"], 7);
                        GatewayCloseCode::InvalidSequence
                    };
                    socket
                        .close(Some(CloseFrame {
                            code: (code as u16).into(),
                            reason: "".into(),
                        }))
                        .await
                        .expect("close");
                    continue;
                }
                socket
                    .send(Message::Text(
                        json!({
                            "op": GatewayOp::Dispatch as u8, "t": "READY", "s": 7,
                            "d": {"session_id": format!("session-{attempt}")},
                        })
                        .to_string()
                        .into(),
                    ))
                    .await
                    .expect("READY");
                if attempt == 1 {
                    socket
                        .send(Message::Text(
                            json!({"op": GatewayOp::Reconnect as u8, "d": null})
                                .to_string()
                                .into(),
                        ))
                        .await
                        .expect("RECONNECT");
                } else {
                    while let Some(Ok(message)) = socket.next().await {
                        if message.is_close() {
                            break;
                        }
                        socket
                            .send(Message::Text(
                                json!({"op": GatewayOp::HeartbeatAck as u8, "d": null})
                                    .to_string()
                                    .into(),
                            ))
                            .await
                            .expect("ACK");
                    }
                }
            }
        });
        let refreshes = Arc::new(Mutex::new(Vec::new()));
        let calls = refreshes.clone();
        let mut gateway = spawn(url, move |refresh| {
            let calls = calls.clone();
            async move {
                let mut calls = calls.lock().await;
                calls.push(refresh);
                Ok::<_, ()>(SecretString::from(if calls.len() == 1 {
                    "expired"
                } else {
                    "refreshed"
                }))
            }
        });
        time::timeout(Duration::from_secs(12), async {
            let first = gateway.events.recv().await.expect("first READY");
            assert_eq!(first.d["session_id"], "session-1");
            let recovered = gateway.events.recv().await.expect("replacement READY");
            assert_eq!(recovered.d["session_id"], "session-3");
            gateway
                .commands
                .send(GatewayCommand::Shutdown)
                .await
                .expect("shutdown");
            server.await.expect("server assertions");
        })
        .await
        .expect("automatic recovery");
        assert_eq!(*refreshes.lock().await, vec![false, true, false, false]);
    }

    #[test]
    fn member_requests_are_bounded_before_serialization() {
        let payload = command_payload(GatewayCommand::RequestMembers {
            guild_id: "1".to_owned(),
            guild_domain: "home.example".to_owned(),
            query: "member".to_owned(),
            limit: u16::MAX,
        });
        assert_eq!(payload["d"]["query"], "member");
        assert_eq!(payload["op"], GatewayOp::RequestMembers as u8);
        assert_eq!(payload["d"]["limit"], 100);
        assert_eq!(payload["d"]["guild_id"], "1@home.example");
        assert!(payload["d"].get("guild_domain").is_none());
    }

    #[test]
    fn member_subscriptions_bound_ranges_and_operation_count() {
        let payload = command_payload(GatewayCommand::SubscribeMemberList {
            guild_id: "1".to_owned(),
            guild_domain: "home.example".to_owned(),
            ranges: vec![(0, 1_000), (100, 500), (200, 300), (400, 500)],
        });
        let ranges = payload["d"]["ranges"].as_array().expect("ranges");
        assert_eq!(ranges.len(), 3);
        assert_eq!(ranges[0], json!([0, 99]));
        assert_eq!(ranges[1], json!([100, 199]));
        assert_eq!(ranges[2], json!([200, 299]));
        assert_eq!(payload["d"]["guild_id"], "1@home.example");
    }

    #[test]
    fn resource_requests_match_channel_info_and_soundboard_gateway_contracts() {
        let channel_info = command_payload(GatewayCommand::RequestChannelInfo {
            guild_id: "1".to_owned(),
            guild_domain: "home.example".to_owned(),
            fields: vec!["status".to_owned(), "voice_start_time".to_owned()],
        });
        assert_eq!(channel_info["op"], GatewayOp::RequestChannelInfo as u8);
        assert_eq!(
            channel_info["d"],
            json!({
                "guild_id": "1@home.example",
                "fields": ["status", "voice_start_time"],
            })
        );

        let soundboard = command_payload(GatewayCommand::RequestSoundboardSounds {
            guilds: (1..=101)
                .map(|id| (id.to_string(), "home.example".to_owned()))
                .collect(),
        });
        assert_eq!(soundboard["op"], GatewayOp::RequestSoundboardSounds as u8);
        let guild_ids = soundboard["d"]["guild_ids"].as_array().expect("guild IDs");
        // Invalid oversized requests remain oversized so every transport gets
        // the same authoritative rejection; callers must never get a partial
        // native-only result through silent truncation.
        assert_eq!(guild_ids.len(), 101);
        assert_eq!(guild_ids[0], "1@home.example");
        assert_eq!(guild_ids[99], "100@home.example");
        assert_eq!(guild_ids[100], "101@home.example");
    }

    #[test]
    fn voice_state_matches_the_gateway_self_state_contract_exactly() {
        for (self_mute, self_deaf) in [(false, false), (true, false), (false, true), (true, true)] {
            let payload = command_payload(GatewayCommand::VoiceState {
                self_mute,
                self_deaf,
            });
            assert_eq!(payload["op"], GatewayOp::VoiceStateUpdate as u8);
            assert_eq!(
                payload["d"],
                json!({"self_mute": self_mute || self_deaf, "self_deaf": self_deaf})
            );
        }
    }
}
