use std::{sync::Arc, time::Duration};

use futures_util::{SinkExt, StreamExt};
use kaede_protocol::{GatewayEnvelope, GatewayOp, PROTOCOL_VERSION};
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
    VoiceState {
        guild_id: Option<String>,
        guild_domain: Option<String>,
        channel_id: Option<String>,
        channel_domain: Option<String>,
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

#[must_use]
pub fn spawn(url: Url, token: SecretString) -> GatewayHandle {
    let (event_tx, event_rx) = mpsc::channel(512);
    let (command_tx, command_rx) = mpsc::channel(128);
    let (status_tx, status_rx) = watch::channel(GatewayStatus::Disconnected);
    let resume = Arc::new(Mutex::new(ResumeState::default()));
    tokio::spawn(run(
        url,
        token,
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

async fn run(
    url: Url,
    token: SecretString,
    event_tx: mpsc::Sender<GatewayEnvelope>,
    mut command_rx: mpsc::Receiver<GatewayCommand>,
    status_tx: watch::Sender<GatewayStatus>,
    resume: Arc<Mutex<ResumeState>>,
) {
    let mut delay = Duration::from_secs(1);
    loop {
        let _ = status_tx.send(if delay == Duration::from_secs(1) {
            GatewayStatus::Connecting
        } else {
            GatewayStatus::Reconnecting
        });
        match connect_once(
            &url,
            &token,
            &event_tx,
            &mut command_rx,
            &status_tx,
            &resume,
        )
        .await
        {
            Ok(ConnectionEnd::Shutdown) => break,
            Ok(ConnectionEnd::Reconnect) | Err(_) => {
                let _ = status_tx.send(GatewayStatus::Reconnecting);
                time::sleep(delay).await;
                delay = (delay * 2).min(MAX_RECONNECT_DELAY);
            }
        }
    }
    let _ = status_tx.send(GatewayStatus::Disconnected);
}

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
    let _ = status_tx.send(GatewayStatus::Connected);

    loop {
        tokio::select! {
            _ = heartbeat.tick() => {
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
                if incoming.is_close() {
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
        } => json!({
            "op": GatewayOp::RequestMembers as u8,
            "d": {"guild_id": guild_id, "guild_domain": guild_domain, "query": query, "limit": limit.min(100)},
        }),
        GatewayCommand::SubscribeMemberList {
            guild_id,
            guild_domain,
            ranges,
        } => json!({
            "op": GatewayOp::SubscribeMemberList as u8,
            "d": {"guild_id": guild_id, "guild_domain": guild_domain,
                "ranges": ranges.into_iter().take(3).map(|(start, end)| [start, end.min(start.saturating_add(99))]).collect::<Vec<_>>()},
        }),
        GatewayCommand::VoiceState {
            guild_id,
            guild_domain,
            channel_id,
            channel_domain,
            self_mute,
            self_deaf,
        } => json!({
            "op": GatewayOp::VoiceStateUpdate as u8,
            "d": {"guild_id": guild_id, "guild_domain": guild_domain, "channel_id": channel_id,
                "channel_domain": channel_domain, "self_mute": self_mute, "self_deaf": self_deaf},
        }),
        GatewayCommand::Shutdown => Value::Null,
    }
}

fn decode_text(message: Message) -> Result<GatewayEnvelope, GatewayError> {
    let text = message.into_text()?;
    serde_json::from_str(&text).map_err(GatewayError::Decode)
}

enum ConnectionEnd {
    Reconnect,
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

    #[test]
    fn member_requests_are_bounded_before_serialization() {
        let payload = command_payload(GatewayCommand::RequestMembers {
            guild_id: "1".to_owned(),
            guild_domain: "home.example".to_owned(),
            query: "member".to_owned(),
            limit: u16::MAX,
        });
        assert_eq!(payload["op"], GatewayOp::RequestMembers as u8);
        assert_eq!(payload["d"]["limit"], 100);
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
    }

    #[test]
    fn gateway_envelopes_decode_decimal_identifiers_without_number_coercion() {
        let message = Message::Text(
            json!({
                "op": GatewayOp::Dispatch as u8,
                "t": "CHANNEL_ACCESS_REVOKED",
                "s": 18,
                "d": {
                    "channel_id": "76426998884343809",
                    "channel_domain": "remote.example"
                }
            })
            .to_string()
            .into(),
        );
        let envelope = decode_text(message).expect("gateway envelope");
        assert_eq!(envelope.d["channel_id"], "76426998884343809");
        assert_eq!(envelope.t.as_deref(), Some("CHANNEL_ACCESS_REVOKED"));
    }
}
