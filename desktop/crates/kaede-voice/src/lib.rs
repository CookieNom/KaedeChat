//! `LiveKit` transport backed exclusively by Kaede's `CPAL` audio graph.

use std::{
    borrow::Cow,
    collections::BTreeSet,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};

use futures_util::StreamExt;
use kaede_api::{ApiClient, ApiClientError};
use kaede_audio::{
    CaptureGate, CaptureSettings, NativeCapture, NativePlayback, ProcessorChain, SpeechProcessor,
    VOICE_CHANNELS, VOICE_SAMPLE_RATE,
};
use kaede_capture::{PackedFrame, PackedPixelFormat};
use kaede_protocol::{
    EntityRef, PRIORITY_SPEAKER_ACTIVE_PAYLOAD, PRIORITY_SPEAKER_INACTIVE_PAYLOAD,
    PRIORITY_SPEAKER_TOPIC,
};
use livekit::{
    E2eeOptions,
    e2ee::{
        EncryptionType,
        key_provider::{KeyProvider, KeyProviderOptions},
    },
    options::{AudioEncoding, TrackPublishOptions, VideoEncoding},
    prelude::{
        DataPacket, DataPacketKind, DisconnectReason, LocalAudioTrack, LocalTrack, LocalVideoTrack,
        Participant, RemoteTrack, Room, RoomEvent, RoomOptions, TrackSource,
    },
    webrtc::{
        audio_frame::AudioFrame,
        audio_source::{AudioSourceOptions, RtcAudioSource, native::NativeAudioSource},
        audio_stream::native::NativeAudioStream,
        desktop_capturer::{
            CaptureError, DesktopCaptureSourceType, DesktopCapturer, DesktopCapturerOptions,
            DesktopFrame,
        },
        video_frame::{I420Buffer, VideoFormatType, VideoFrame, VideoRotation},
        video_source::{RtcVideoSource, VideoResolution, native::NativeVideoSource},
        video_stream::native::NativeVideoStream,
    },
};
use nokhwa::{
    Camera,
    pixel_format::RgbFormat,
    utils::{CameraFormat, CameraIndex, FrameFormat, RequestedFormat, RequestedFormatType},
};
use secrecy::{ExposeSecret, SecretString};
use serde::{Deserialize, Deserializer, Serialize};
use thiserror::Error;
use tokio::{
    sync::{mpsc, watch},
    task::JoinHandle,
    time,
};

const AUDIO_FRAME_TIME: Duration = Duration::from_millis(10);

#[derive(Clone, Debug, Deserialize)]
#[allow(clippy::struct_excessive_bools)] // Wire DTO mirrors independent server grants.
pub struct VoiceGrant {
    pub token: SecretString,
    pub url: String,
    pub room: String,
    pub generation: u64,
    pub expires_at: String,
    pub can_speak: bool,
    pub can_stream: bool,
    #[serde(default)]
    pub can_priority_speak: bool,
    #[serde(default)]
    pub can_use_vad: bool,
    pub bitrate: u64,
    pub user_limit: u64,
    #[serde(deserialize_with = "deserialize_required_nullable_string")]
    pub rtc_region: Option<String>,
    pub video_quality_mode: u8,
    #[serde(default)]
    pub move_session_id: Option<String>,
    pub e2ee: bool,
    #[serde(default)]
    pub channel_id: Option<String>,
    #[serde(default)]
    pub channel_domain: Option<String>,
    #[serde(default)]
    pub encryption_policy_generation: Option<String>,
    #[serde(default)]
    pub encryption_epoch: Option<String>,
    #[serde(default)]
    pub media_protocol: Option<String>,
    #[serde(default)]
    pub media_suite: Option<String>,
    #[serde(default)]
    pub media_session_id: Option<String>,
    #[serde(default)]
    pub media_epoch: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct ExpectedVoicePolicy {
    pub e2ee: bool,
    pub room: String,
    pub channel_id: String,
    pub channel_domain: String,
    pub bitrate: u64,
    pub user_limit: u64,
    #[serde(deserialize_with = "deserialize_required_nullable_string")]
    pub rtc_region: Option<String>,
    pub video_quality_mode: u8,
    pub encryption_policy_generation: Option<String>,
    pub encryption_epoch: Option<String>,
    pub media_protocol: Option<String>,
    pub media_suite: Option<String>,
    pub media_session_id: Option<String>,
    pub media_epoch: Option<String>,
}

impl ExpectedVoicePolicy {
    fn matches(&self, grant: &VoiceGrant) -> bool {
        let context = (
            self.encryption_policy_generation.as_deref(),
            self.encryption_epoch.as_deref(),
            self.media_protocol.as_deref(),
            self.media_suite.as_deref(),
            self.media_session_id.as_deref(),
            self.media_epoch.as_deref(),
        );
        let internally_valid = if self.e2ee {
            context.0.is_some_and(valid_decimal)
                && context.1.is_some_and(valid_decimal)
                && context.2 == Some("livekit-e2ee-v1")
                && context.3 == Some("AES-256-GCM")
                && context.4.is_some_and(valid_media_session_id)
                && context.5 == context.1
        } else {
            [
                context.0, context.1, context.2, context.3, context.4, context.5,
            ]
            .iter()
            .all(Option::is_none)
        };
        let expected_media_valid = valid_voice_media_policy(
            self.bitrate,
            self.user_limit,
            self.rtc_region.as_deref(),
            self.video_quality_mode,
        );
        let grant_media_valid = valid_voice_media_policy(
            grant.bitrate,
            grant.user_limit,
            grant.rtc_region.as_deref(),
            grant.video_quality_mode,
        );
        internally_valid
            && expected_media_valid
            && grant_media_valid
            && !self.room.is_empty()
            && !self.channel_id.is_empty()
            && !self.channel_domain.is_empty()
            && grant.e2ee == self.e2ee
            && grant.room == self.room
            && grant.channel_id.as_deref() == Some(self.channel_id.as_str())
            && grant.channel_domain.as_deref() == Some(self.channel_domain.as_str())
            && grant.encryption_policy_generation == self.encryption_policy_generation
            && grant.encryption_epoch == self.encryption_epoch
            && grant.media_protocol == self.media_protocol
            && grant.media_suite == self.media_suite
            && grant.media_session_id == self.media_session_id
            && grant.media_epoch == self.media_epoch
            && grant.bitrate == self.bitrate
            && grant.user_limit == self.user_limit
            && grant.rtc_region == self.rtc_region
            && grant.video_quality_mode == self.video_quality_mode
    }
}

fn deserialize_required_nullable_string<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    Option::<String>::deserialize(deserializer)
}

fn valid_voice_media_policy(
    bitrate: u64,
    user_limit: u64,
    rtc_region: Option<&str>,
    video_quality_mode: u8,
) -> bool {
    (8_000..=384_000).contains(&bitrate)
        // Discord voice channels cap at 99, while Stage channels support an
        // audience limit up to 10,000. The authoritative channel-type fence
        // lives on the server; the native grant boundary must accept both.
        && user_limit <= 10_000
        && rtc_region.is_none_or(|region| {
            let length = region.chars().count();
            (1..=64).contains(&length)
        })
        && matches!(video_quality_mode, 1 | 2)
}

fn valid_decimal(value: &str) -> bool {
    value == "0"
        || (value
            .bytes()
            .next()
            .is_some_and(|byte| matches!(byte, b'1'..=b'9'))
            && value.bytes().all(|byte| byte.is_ascii_digit()))
}

fn valid_media_session_id(value: &str) -> bool {
    value.len() == 43
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum VoiceStatus {
    Disconnected,
    Connecting,
    Connected {
        room: String,
        can_speak: bool,
        can_stream: bool,
        screen_sharing: bool,
        camera_enabled: bool,
    },
    Reconnecting,
    MediaError {
        message: String,
        room: String,
        can_speak: bool,
        can_stream: bool,
        screen_sharing: bool,
        camera_enabled: bool,
    },
    Failed(String),
}

#[derive(Clone, Debug)]
pub struct RemoteVideoFrame {
    pub participant: String,
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
    pub removed: bool,
}

#[derive(Clone, Debug)]
pub enum VoiceCommand {
    SetMuted(bool),
    SetDeafened(bool),
    SetPushToTalk(bool),
    SetPriorityPushToTalk(bool),
    SetCamera {
        enabled: bool,
        device_id: Option<String>,
    },
    SetScreenShare {
        enabled: bool,
        source_id: Option<String>,
        settings: ScreenShareSettings,
    },
    Leave,
}

#[derive(Clone, Copy, Debug)]
pub struct MediaPublishSettings {
    pub audio_max_bitrate: u64,
}

pub struct VoiceMediaSettings {
    pub capture: CaptureSettings,
    pub output_device: Option<String>,
    pub publish: MediaPublishSettings,
    /// Start the capture graph closed until the caller installs the handle and
    /// reconciles its latest UI state.
    pub initially_muted: bool,
    /// Start playback closed for the same join/install window.
    pub initially_deafened: bool,
}

#[derive(Clone, Copy, Debug)]
pub struct VoiceGrantRequest<'a> {
    pub sender_device_id: Option<&'a str>,
    pub connection_id: &'a str,
    pub takeover: bool,
}

impl Default for MediaPublishSettings {
    fn default() -> Self {
        Self {
            audio_max_bitrate: 48_000,
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct ScreenShareSettings {
    pub width: u32,
    pub height: u32,
    pub frame_rate: u32,
    pub max_bitrate: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct CameraSettings {
    width: u32,
    height: u32,
    frame_rate: u32,
    max_bitrate: u64,
}

fn camera_settings(video_quality_mode: u8) -> CameraSettings {
    if video_quality_mode == 2 {
        CameraSettings {
            width: 1280,
            height: 720,
            frame_rate: 30,
            max_bitrate: 1_700_000,
        }
    } else {
        CameraSettings {
            width: 640,
            height: 360,
            frame_rate: 20,
            max_bitrate: 450_000,
        }
    }
}

fn effective_microphone_bitrate(preferred: u64, channel_bitrate: u64) -> u64 {
    preferred.clamp(8_000, 128_000).min(channel_bitrate)
}

impl Default for ScreenShareSettings {
    fn default() -> Self {
        Self {
            width: 1280,
            height: 720,
            frame_rate: 30,
            max_bitrate: 2_500_000,
        }
    }
}

pub struct VoiceHandle {
    pub commands: mpsc::UnboundedSender<VoiceCommand>,
    pub status: watch::Receiver<VoiceStatus>,
    /// Becomes true when authoritative local permissions no longer match the
    /// immutable capture/publication graph created from the join grant.
    pub grant_stale: watch::Receiver<bool>,
    pub priority_speakers: watch::Receiver<BTreeSet<String>>,
    pub video_frames: Option<mpsc::Receiver<RemoteVideoFrame>>,
    pub input_level: Option<Arc<CaptureGate>>,
    /// Opaque broker correlation for a federated guild voice session.
    /// Replacement grants must carry the same value as the active handle.
    pub move_session_id: Option<String>,
    status_control: watch::Sender<VoiceStatus>,
    task: JoinHandle<()>,
}

impl VoiceHandle {
    /// Marks a still-current native room terminally failed when a fenced grant
    /// refresh cannot construct its replacement.
    pub fn mark_failed(&self, message: String) {
        let _ = self.status_control.send(VoiceStatus::Failed(message));
    }

    pub async fn leave(self) {
        let _ = self.commands.send(VoiceCommand::Leave);
        let _ = self.task.await;
    }
}

/// Obtains a home-instance grant and joins a guild voice channel.
///
/// # Errors
///
/// Returns an error when authorization, native audio setup, or the `LiveKit`
/// connection fails.
pub async fn join_channel(
    api: ApiClient,
    channel: &EntityRef,
    media: VoiceMediaSettings,
    expected_policy: ExpectedVoicePolicy,
    media_key: Option<Vec<u8>>,
    request: VoiceGrantRequest<'_>,
) -> Result<VoiceHandle, VoiceError> {
    let grant: VoiceGrant = api
        .post(
            &format!("channels/{channel}/voice/token"),
            &serde_json::json!({
                "sender_device_id": request.sender_device_id,
                "connection_id": request.connection_id,
                "takeover": request.takeover,
                "client_kind": "desktop"
            }),
        )
        .await?;
    Box::pin(join(grant, media, expected_policy, media_key, true)).await
}

/// Obtains a home-instance grant and joins a direct-message call.
///
/// # Errors
///
/// Returns an error when authorization, native audio setup, or the `LiveKit`
/// connection fails.
pub async fn join_call(
    api: ApiClient,
    call: &EntityRef,
    media: VoiceMediaSettings,
    expected_policy: ExpectedVoicePolicy,
    media_key: Option<Vec<u8>>,
    request: VoiceGrantRequest<'_>,
) -> Result<VoiceHandle, VoiceError> {
    let grant: VoiceGrant = api
        .post(
            &format!("calls/{call}/voice/token"),
            &serde_json::json!({
                "sender_device_id": request.sender_device_id,
                "connection_id": request.connection_id,
                "takeover": request.takeover,
                "client_kind": "desktop"
            }),
        )
        .await?;
    Box::pin(join(grant, media, expected_policy, media_key, false)).await
}

#[allow(clippy::too_many_lines)] // Join validates and installs one linear media pipeline.
async fn join(
    grant: VoiceGrant,
    media: VoiceMediaSettings,
    expected_policy: ExpectedVoicePolicy,
    media_key: Option<Vec<u8>>,
    allow_priority_speaker: bool,
) -> Result<VoiceHandle, VoiceError> {
    let initially_muted = media.initially_muted;
    let initially_deafened = media.initially_deafened;
    let room_options = media_room_options(&grant, &expected_policy, media_key)?;
    let move_session_id = grant.move_session_id.clone();
    if grant.can_speak
        && media.capture.mode == kaede_audio::InputMode::VoiceActivity
        && !grant.can_use_vad
    {
        return Err(VoiceError::VoiceActivityDenied);
    }
    let priority_speaker_access =
        local_priority_speaker_access(allow_priority_speaker, &grant, media.capture.mode);
    let (status_tx, status_rx) = watch::channel(VoiceStatus::Connecting);
    let status_control = status_tx.clone();
    let (grant_stale_tx, grant_stale_rx) = watch::channel(false);
    let (priority_speakers_tx, priority_speakers_rx) = watch::channel(BTreeSet::new());
    let (command_tx, command_rx) = mpsc::unbounded_channel();
    // Video frames are intentionally lossy, but participant removal must not be.
    // A modest buffer gives several simultaneous tracks a fair chance to publish
    // without retaining a large amount of decoded RGBA data.
    let (video_tx, video_rx) = mpsc::channel(16);
    let mut processor_chain = ProcessorChain::default();
    processor_chain.push(Box::new(SpeechProcessor::from_settings(&media.capture)));
    // Do not open the operating-system microphone when the server grant is
    // listen-only. This avoids an unnecessary privacy prompt and ensures that
    // a missing SPEAK grant cannot accidentally feed a local capture graph.
    let capture = grant
        .can_speak
        .then(|| NativeCapture::open(&media.capture))
        .transpose()?;
    if let Some(capture) = capture.as_ref() {
        capture
            .gate
            .set_muted(initially_muted || initially_deafened);
    }
    let playback = NativePlayback::open(media.output_device.as_deref())?;
    playback.set_deafened(initially_deafened);
    let input_level = capture.as_ref().map(|capture| capture.gate.clone());
    let room_name = grant.room.clone();
    let (room, events) = Box::pin(Room::connect(
        &grant.url,
        grant.token.expose_secret(),
        room_options,
    ))
    .await?;

    let source = if grant.can_speak {
        let source = NativeAudioSource::new(
            AudioSourceOptions::default(),
            VOICE_SAMPLE_RATE,
            u32::from(VOICE_CHANNELS),
            100,
        );
        let track = LocalAudioTrack::create_audio_track(
            "microphone",
            RtcAudioSource::Native(source.clone()),
        );
        room.local_participant()
            .publish_track(
                LocalTrack::Audio(track),
                TrackPublishOptions {
                    source: TrackSource::Microphone,
                    audio_encoding: Some(AudioEncoding {
                        max_bitrate: effective_microphone_bitrate(
                            media.publish.audio_max_bitrate,
                            grant.bitrate,
                        ),
                    }),
                    ..TrackPublishOptions::default()
                },
            )
            .await?;
        Some(source)
    } else {
        None
    };
    let _ = status_tx.send(VoiceStatus::Connected {
        room: room_name,
        can_speak: grant.can_speak,
        can_stream: grant.can_stream,
        screen_sharing: false,
        camera_enabled: false,
    });
    let task = tokio::spawn(run_room(
        room,
        events,
        command_rx,
        status_tx,
        capture,
        playback,
        source,
        grant.can_speak,
        grant.can_stream,
        grant.can_use_vad,
        media.capture.mode,
        initially_muted,
        initially_deafened,
        priority_speaker_access,
        grant.video_quality_mode,
        video_tx,
        priority_speakers_tx,
        grant_stale_tx,
        processor_chain,
    ));
    Ok(VoiceHandle {
        commands: command_tx,
        status: status_rx,
        grant_stale: grant_stale_rx,
        priority_speakers: priority_speakers_rx,
        video_frames: Some(video_rx),
        input_level,
        move_session_id,
        status_control,
        task,
    })
}

fn local_priority_speaker_allowed(
    channel_join: bool,
    grant: &VoiceGrant,
    input_mode: kaede_audio::InputMode,
) -> bool {
    channel_join
        && grant.can_speak
        && grant.can_priority_speak
        && input_mode == kaede_audio::InputMode::PushToTalk
}

fn local_priority_speaker_access(
    allow_priority_speaker: bool,
    grant: &VoiceGrant,
    input_mode: kaede_audio::InputMode,
) -> LocalPrioritySpeakerAccess {
    LocalPrioritySpeakerAccess {
        context_allowed: allow_priority_speaker
            && grant.can_speak
            && input_mode == kaede_audio::InputMode::PushToTalk,
        capability: local_priority_speaker_allowed(allow_priority_speaker, grant, input_mode),
    }
}

fn media_room_options(
    grant: &VoiceGrant,
    expected: &ExpectedVoicePolicy,
    media_key: Option<Vec<u8>>,
) -> Result<RoomOptions, VoiceError> {
    if !expected.matches(grant) {
        if let Some(mut key) = media_key {
            key.fill(0);
        }
        return Err(VoiceError::EncryptionPolicyMismatch);
    }
    match (grant.e2ee, media_key) {
        (false, None) => Ok(RoomOptions::default()),
        (false, Some(mut key)) => {
            key.fill(0);
            Err(VoiceError::EncryptionPolicyMismatch)
        }
        (true, None) => Err(VoiceError::EncryptionKeyMissing),
        (true, Some(mut key)) => {
            if key.len() != 32
                || grant.channel_id.as_deref().is_none_or(str::is_empty)
                || grant.channel_domain.as_deref().is_none_or(str::is_empty)
                || grant
                    .encryption_policy_generation
                    .as_deref()
                    .is_none_or(str::is_empty)
                || grant.encryption_epoch.as_deref().is_none_or(str::is_empty)
                || grant.media_protocol.as_deref() != Some("livekit-e2ee-v1")
                || grant.media_suite.as_deref() != Some("AES-256-GCM")
                || grant
                    .media_session_id
                    .as_deref()
                    .is_none_or(|value| !valid_media_session_id(value))
                || grant.media_epoch != grant.encryption_epoch
            {
                key.fill(0);
                return Err(VoiceError::EncryptionPolicyMismatch);
            }
            let provider = KeyProvider::with_shared_key(
                KeyProviderOptions {
                    ratchet_salt: b"kaede-livekit-v1".to_vec(),
                    ..KeyProviderOptions::default()
                },
                key,
            );
            let mut options = RoomOptions::default();
            options.encryption = Some(E2eeOptions {
                encryption_type: EncryptionType::Gcm,
                key_provider: provider,
            });
            Ok(options)
        }
    }
}

#[allow(clippy::cast_possible_truncation)]
fn float_sample_to_i16(sample: f32) -> i16 {
    // The explicit clamp makes this the standard saturating PCM conversion;
    // rounding can only produce a value in the signed 16-bit range.
    (sample.clamp(-1.0, 1.0) * f32::from(i16::MAX)).round() as i16
}

#[derive(Deserialize)]
struct PrioritySpeakerMetadata {
    user_id: String,
    user_domain: String,
    can_speak: bool,
    #[serde(default)]
    can_stream: Option<bool>,
    #[serde(default)]
    can_use_vad: Option<bool>,
    #[serde(default)]
    can_priority_speak: bool,
}

fn participant_voice_metadata(identity: &str, metadata: &str) -> Option<PrioritySpeakerMetadata> {
    let Ok(metadata) = serde_json::from_str::<PrioritySpeakerMetadata>(metadata) else {
        return None;
    };
    let Ok(user_id) = metadata.user_id.parse::<u64>() else {
        return None;
    };
    (metadata.user_id == user_id.to_string()
        && identity
            == format!(
                "{}@{}",
                metadata.user_id,
                metadata
                    .user_domain
                    .trim_end_matches('.')
                    .to_ascii_lowercase()
            ))
    .then_some(metadata)
}

fn participant_can_priority_speak(identity: &str, metadata: &str) -> bool {
    participant_voice_metadata(identity, metadata)
        .is_some_and(|metadata| metadata.can_speak && metadata.can_priority_speak)
}

fn local_voice_grant_rotation(
    joined_can_speak: bool,
    joined_can_stream: bool,
    joined_can_use_vad: bool,
    input_mode: kaede_audio::InputMode,
    identity: &str,
    metadata: &str,
) -> bool {
    let Some(metadata) = participant_voice_metadata(identity, metadata) else {
        return false;
    };
    metadata.can_speak != joined_can_speak
        || metadata
            .can_stream
            .is_some_and(|can_stream| can_stream != joined_can_stream)
        || (input_mode == kaede_audio::InputMode::VoiceActivity
            && joined_can_speak
            && metadata
                .can_use_vad
                .is_some_and(|can_use_vad| can_use_vad != joined_can_use_vad))
}

fn local_priority_speaker_metadata_allowed(
    context_allowed: bool,
    identity: &str,
    metadata: &str,
) -> bool {
    context_allowed && participant_can_priority_speak(identity, metadata)
}

fn local_priority_speaker_metadata_transition(
    context_allowed: bool,
    active: bool,
    identity: &str,
    metadata: &str,
) -> (bool, bool) {
    let allowed = local_priority_speaker_metadata_allowed(context_allowed, identity, metadata);
    (allowed, active && !allowed)
}

fn decode_priority_speaker_signal(
    identity: &str,
    metadata: &str,
    topic: Option<&str>,
    kind: DataPacketKind,
    payload: &[u8],
) -> Option<bool> {
    if topic != Some(PRIORITY_SPEAKER_TOPIC)
        || kind != DataPacketKind::Reliable
        || !participant_can_priority_speak(identity, metadata)
    {
        return None;
    }
    if payload == PRIORITY_SPEAKER_INACTIVE_PAYLOAD {
        Some(false)
    } else if payload == PRIORITY_SPEAKER_ACTIVE_PAYLOAD {
        Some(true)
    } else {
        None
    }
}

fn priority_speaker_transition(active: bool, requested: bool, authorized: bool) -> Option<[u8; 1]> {
    if !authorized || active == requested {
        return None;
    }
    Some(if requested {
        PRIORITY_SPEAKER_ACTIVE_PAYLOAD
    } else {
        PRIORITY_SPEAKER_INACTIVE_PAYLOAD
    })
}

fn set_priority_speaker(
    priority_speakers: &watch::Sender<BTreeSet<String>>,
    identity: &str,
    active: bool,
) {
    priority_speakers.send_modify(|identities| {
        if active {
            identities.insert(identity.to_owned());
        } else {
            identities.remove(identity);
        }
    });
}

async fn publish_priority_speaker_signal(room: &Room, payload: [u8; 1]) -> bool {
    match room
        .local_participant()
        .publish_data(DataPacket {
            payload: payload.to_vec(),
            topic: Some(PRIORITY_SPEAKER_TOPIC.to_owned()),
            reliable: true,
            ..DataPacket::default()
        })
        .await
    {
        Ok(()) => true,
        Err(error) => {
            tracing::warn!(%error, "priority-speaker signal could not be published");
            false
        }
    }
}

async fn deactivate_local_priority_speaker(
    room: &Room,
    capture: Option<&NativeCapture>,
    active: &mut bool,
    priority_speakers: &watch::Sender<BTreeSet<String>>,
    identity: &str,
) {
    if !std::mem::take(active) {
        return;
    }
    if let Some(capture) = capture {
        capture.gate.set_priority_push_to_talk(false);
    }
    let _ = publish_priority_speaker_signal(room, PRIORITY_SPEAKER_INACTIVE_PAYLOAD).await;
    set_priority_speaker(priority_speakers, identity, false);
}

struct LocalPrioritySpeakerAccess {
    context_allowed: bool,
    capability: bool,
}

#[allow(
    clippy::fn_params_excessive_bools,
    clippy::too_many_arguments,
    clippy::too_many_lines
)]
async fn run_room(
    room: Room,
    mut events: tokio::sync::mpsc::UnboundedReceiver<RoomEvent>,
    mut commands: mpsc::UnboundedReceiver<VoiceCommand>,
    status: watch::Sender<VoiceStatus>,
    capture: Option<NativeCapture>,
    playback: NativePlayback,
    source: Option<NativeAudioSource>,
    can_speak: bool,
    can_stream: bool,
    can_use_vad: bool,
    input_mode: kaede_audio::InputMode,
    initially_muted: bool,
    initially_deafened: bool,
    mut priority_speaker_access: LocalPrioritySpeakerAccess,
    video_quality_mode: u8,
    video_frames: mpsc::Sender<RemoteVideoFrame>,
    priority_speakers: watch::Sender<BTreeSet<String>>,
    grant_stale: watch::Sender<bool>,
    mut processor_chain: ProcessorChain,
) {
    let playback_sink = playback.sink();
    let playback_mixer = playback.mixer();
    let render_reference = playback.render_reference();
    let mut explicitly_muted = initially_muted;
    let mut deafened = initially_deafened;
    let mut priority_push_to_talk_active = false;
    let mut restart_requested = false;
    let local_identity = room.local_participant().identity().to_string();
    let mut capture_tick = time::interval(AUDIO_FRAME_TIME);
    let mut playback_tick = time::interval(AUDIO_FRAME_TIME);
    let mut screen_share: Option<PublishedVideo> = None;
    let mut camera: Option<PublishedVideo> = None;
    capture_tick.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    playback_tick.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            _ = playback_tick.tick() => {
                let frame = playback_mixer.drain((VOICE_SAMPLE_RATE / 100) as usize);
                playback_sink.push_voice_frame(&frame, VOICE_SAMPLE_RATE, VOICE_CHANNELS);
            }
            _ = capture_tick.tick(), if can_speak && source.is_some() && capture.is_some() => {
                let Some(capture) = capture.as_ref() else { continue };
                let render = render_reference.drain((VOICE_SAMPLE_RATE / 100) as usize);
                processor_chain.observe_render(&render, VOICE_SAMPLE_RATE);
                let frame = capture.drain_voice_frame(AUDIO_FRAME_TIME, &mut processor_chain);
                let pcm: Vec<i16> = frame.into_iter()
                    .map(float_sample_to_i16)
                    .collect();
                let audio_frame = AudioFrame {
                    data: Cow::Owned(pcm),
                    sample_rate: VOICE_SAMPLE_RATE,
                    num_channels: u32::from(VOICE_CHANNELS),
                    samples_per_channel: VOICE_SAMPLE_RATE / 100,
                };
                let Some(source) = source.as_ref() else { continue };
                if let Err(error) = time::timeout(Duration::from_millis(100), source.capture_frame(&audio_frame)).await {
                    tracing::warn!(%error, "LiveKit capture backpressure dropped an audio frame");
                }
            }
            command = commands.recv() => {
                match command {
                    Some(VoiceCommand::SetMuted(muted)) => {
                        explicitly_muted = muted;
                        if let Some(capture) = capture.as_ref() {
                            capture.gate.set_muted(explicitly_muted || deafened);
                        }
                        if muted {
                            deactivate_local_priority_speaker(
                                &room,
                                capture.as_ref(),
                                &mut priority_push_to_talk_active,
                                &priority_speakers,
                                &local_identity,
                            )
                            .await;
                        }
                    }
                    Some(VoiceCommand::SetDeafened(next_deafened)) => {
                        deafened = next_deafened;
                        playback.set_deafened(deafened);
                        // Match the familiar voice-client behavior: deafen also
                        // suppresses local publication while it is active.
                        if let Some(capture) = capture.as_ref() {
                            capture.gate.set_muted(explicitly_muted || deafened);
                        }
                        if next_deafened {
                            deactivate_local_priority_speaker(
                                &room,
                                capture.as_ref(),
                                &mut priority_push_to_talk_active,
                                &priority_speakers,
                                &local_identity,
                            )
                            .await;
                        }
                    }
                    Some(VoiceCommand::SetPushToTalk(active)) => {
                        if let Some(capture) = capture.as_ref() {
                            capture.gate.set_push_to_talk(active);
                        }
                    }
                    Some(VoiceCommand::SetPriorityPushToTalk(active)) => {
                        let Some(payload) = priority_speaker_transition(
                            priority_push_to_talk_active,
                            active,
                            priority_speaker_access.capability,
                        ) else {
                            continue;
                        };
                        if active && (explicitly_muted || deafened) {
                            continue;
                        }
                        if !active {
                            priority_push_to_talk_active = false;
                            if let Some(capture) = capture.as_ref() {
                                capture.gate.set_priority_push_to_talk(false);
                            }
                            let _ = publish_priority_speaker_signal(&room, payload).await;
                            set_priority_speaker(&priority_speakers, &local_identity, false);
                        } else if publish_priority_speaker_signal(&room, payload).await {
                            priority_push_to_talk_active = true;
                            if let Some(capture) = capture.as_ref() {
                                capture.gate.set_priority_push_to_talk(true);
                            }
                            set_priority_speaker(
                                &priority_speakers,
                                &local_identity,
                                true,
                            );
                        }
                    }
                    Some(VoiceCommand::SetCamera { enabled, device_id }) => {
                        if enabled && !can_stream {
                            send_media_error(&status, &room, can_speak, can_stream, screen_share.as_ref(), camera.as_ref(),
                                "You do not have permission to use your camera in this channel.".to_owned());
                            continue;
                        }
                        let result = if enabled && camera.is_none() {
                            publish_camera(&room, device_id.as_deref(), video_quality_mode).await.map(|published| {
                                camera = Some(published);
                            })
                        } else if !enabled {
                            stop_published_video(&room, camera.take()).await
                        } else {
                            Ok(())
                        };
                        match result {
                            Ok(()) => {
                                let _ = status.send(VoiceStatus::Connected {
                                    room: room.name(),
                                    can_speak,
                                    can_stream,
                                    screen_sharing: screen_share.is_some(),
                                    camera_enabled: camera.is_some(),
                                });
                            }
                            Err(error) => {
                                tracing::warn!(%error, "camera control failed");
                                send_media_error(&status, &room, can_speak, can_stream, screen_share.as_ref(), camera.as_ref(), error.user_message());
                            }
                        }
                    }
                    Some(VoiceCommand::SetScreenShare { enabled, source_id, settings }) => {
                        if enabled && !can_stream {
                            send_media_error(&status, &room, can_speak, can_stream, screen_share.as_ref(), camera.as_ref(),
                                "You do not have permission to share your screen in this channel.".to_owned());
                            continue;
                        }
                        let result = if enabled && screen_share.is_none() {
                            publish_screen_share(&room, source_id.as_deref(), settings).await.map(|published| {
                                screen_share = Some(published);
                            })
                        } else if !enabled {
                            stop_published_video(&room, screen_share.take()).await
                        } else {
                            Ok(())
                        };
                        match result {
                            Ok(()) => {
                                let _ = status.send(VoiceStatus::Connected {
                                    room: room.name(),
                                    can_speak,
                                    can_stream,
                                    screen_sharing: screen_share.is_some(),
                                    camera_enabled: camera.is_some(),
                                });
                            }
                            Err(error) => {
                                tracing::warn!(%error, "screen-share control failed");
                                send_media_error(&status, &room, can_speak, can_stream, screen_share.as_ref(), camera.as_ref(), error.user_message());
                            }
                        }
                    }
                    Some(VoiceCommand::Leave) | None => {
                        deactivate_local_priority_speaker(
                            &room,
                            capture.as_ref(),
                            &mut priority_push_to_talk_active,
                            &priority_speakers,
                            &local_identity,
                        )
                        .await;
                        break;
                    }
                }
            }
            event = events.recv() => {
                match event {
                    Some(RoomEvent::TrackSubscribed { track: RemoteTrack::Audio(track), publication, participant }) => {
                        let mixer = playback_mixer.clone();
                        let priority_speakers = priority_speakers.clone();
                        let participant_identity = participant.identity().to_string();
                        let track_id = publication.sid().to_string();
                        let priority_eligible = publication.source() == TrackSource::Microphone;
                        mixer.register_track(
                            &participant_identity,
                            &track_id,
                            priority_eligible,
                        );
                        if priority_eligible {
                            mixer.set_priority_capability(
                                &participant_identity,
                                participant_can_priority_speak(
                                    &participant_identity,
                                    &participant.metadata(),
                                ),
                            );
                        }
                        tokio::spawn(async move {
                            let sample_rate = VOICE_SAMPLE_RATE.cast_signed();
                            let mut stream = NativeAudioStream::new(track.rtc_track(), sample_rate, i32::from(VOICE_CHANNELS));
                            while let Some(frame) = stream.next().await {
                                let Ok(channels) = u16::try_from(frame.num_channels) else {
                                    tracing::warn!(channels = frame.num_channels, "remote audio channel count is unsupported");
                                    continue;
                                };
                                let samples: Vec<f32> = frame.data.iter()
                                    .map(|sample| f32::from(*sample) / f32::from(i16::MAX))
                                    .collect();
                                mixer.push_track(
                                    &participant_identity,
                                    &track_id,
                                    priority_eligible,
                                    &samples,
                                    frame.sample_rate,
                                    channels,
                                );
                            }
                            let microphone_remains =
                                mixer.remove_track(&participant_identity, &track_id);
                            if priority_eligible && !microphone_remains {
                                let _ = mixer.set_priority_active(&participant_identity, false);
                                set_priority_speaker(&priority_speakers, &participant_identity, false);
                            }
                        });
                    }
                    Some(RoomEvent::TrackSubscribed { track: RemoteTrack::Video(track), participant, .. }) => {
                        let video_frames = video_frames.clone();
                        let participant = participant.identity().to_string();
                        tokio::spawn(async move {
                            let mut stream = NativeVideoStream::new(track.rtc_track());
                            while let Some(frame) = stream.next().await {
                                let width = frame.buffer.width();
                                let height = frame.buffer.height();
                                let Some(size) = width.checked_mul(height).and_then(|pixels| pixels.checked_mul(4)).and_then(|bytes| usize::try_from(bytes).ok()) else { continue };
                                let mut rgba = vec![0_u8; size];
                                let (Ok(width_i32), Ok(height_i32)) = (i32::try_from(width), i32::try_from(height)) else {
                                    tracing::warn!(width, height, "remote video dimensions exceed decoder limits");
                                    continue;
                                };
                                frame.buffer.to_argb(VideoFormatType::RGBA, &mut rgba, width.saturating_mul(4), width_i32, height_i32);
                                let _ = video_frames.try_send(RemoteVideoFrame { participant: participant.clone(), width, height, rgba, removed: false });
                            }
                            let _ = video_frames
                                .send(RemoteVideoFrame {
                                    participant,
                                    width: 0,
                                    height: 0,
                                    rgba: Vec::new(),
                                    removed: true,
                                })
                                .await;
                        });
                    }
                    Some(RoomEvent::DataReceived { payload, topic, kind, participant }) => {
                        let Some(participant) = participant else { continue };
                        let identity = participant.identity().to_string();
                        let Some(active) = decode_priority_speaker_signal(
                            &identity,
                            &participant.metadata(),
                            topic.as_deref(),
                            kind,
                            payload.as_slice(),
                        ) else {
                            continue;
                        };
                        playback_mixer.set_priority_capability(&identity, true);
                        if playback_mixer.set_priority_active(&identity, active) {
                            set_priority_speaker(&priority_speakers, &identity, active);
                        }
                    }
                    Some(RoomEvent::ParticipantMetadataChanged { participant, metadata, .. }) => {
                        match participant {
                            Participant::Remote(participant) => {
                                let identity = participant.identity().to_string();
                                let capable = participant_can_priority_speak(&identity, &metadata);
                                playback_mixer.set_priority_capability(&identity, capable);
                                if !capable {
                                    set_priority_speaker(&priority_speakers, &identity, false);
                                }
                            }
                            Participant::Local(participant) => {
                                let identity = participant.identity().to_string();
                                if local_voice_grant_rotation(
                                    can_speak,
                                    can_stream,
                                    can_use_vad,
                                    input_mode,
                                    &identity,
                                    &metadata,
                                ) {
                                    if let Some(capture) = capture.as_ref() {
                                        capture.gate.set_muted(true);
                                        capture.gate.set_push_to_talk(false);
                                    }
                                    deactivate_local_priority_speaker(
                                        &room,
                                        capture.as_ref(),
                                        &mut priority_push_to_talk_active,
                                        &priority_speakers,
                                        &local_identity,
                                    )
                                    .await;
                                    restart_requested = true;
                                    let _ = status.send(VoiceStatus::Reconnecting);
                                    let _ = grant_stale.send(true);
                                    break;
                                }
                                let (next_capability, revoke_active) =
                                    local_priority_speaker_metadata_transition(
                                    priority_speaker_access.context_allowed,
                                    priority_push_to_talk_active,
                                    &identity,
                                    &metadata,
                                );
                                if revoke_active {
                                    deactivate_local_priority_speaker(
                                        &room,
                                        capture.as_ref(),
                                        &mut priority_push_to_talk_active,
                                        &priority_speakers,
                                        &local_identity,
                                    )
                                    .await;
                                }
                                priority_speaker_access.capability = next_capability;
                            }
                        }
                    }
                    Some(RoomEvent::ParticipantDisconnected(participant)) => {
                        let identity = participant.identity().to_string();
                        playback_mixer.remove_participant(&identity);
                        set_priority_speaker(&priority_speakers, &identity, false);
                    }
                    Some(RoomEvent::TrackUnsubscribed { track: RemoteTrack::Audio(_), publication, participant }) => {
                        let identity = participant.identity().to_string();
                        let track_id = publication.sid().to_string();
                        let microphone_remains =
                            playback_mixer.remove_track(&identity, &track_id);
                        if publication.source() == TrackSource::Microphone && !microphone_remains {
                            let _ = playback_mixer.set_priority_active(&identity, false);
                            set_priority_speaker(&priority_speakers, &identity, false);
                        }
                    }
                    Some(RoomEvent::Reconnecting) => {
                        priority_push_to_talk_active = false;
                        playback_mixer.clear_priority_active();
                        priority_speakers.send_modify(BTreeSet::clear);
                        if let Some(capture) = capture.as_ref() {
                            capture.gate.set_priority_push_to_talk(false);
                        }
                        let _ = status.send(VoiceStatus::Reconnecting);
                    }
                    Some(RoomEvent::Reconnected) => {
                        let _ = status.send(VoiceStatus::Connected {
                            room: room.name(),
                            can_speak,
                            can_stream,
                            screen_sharing: screen_share.is_some(),
                            camera_enabled: camera.is_some(),
                        });
                    }
                    Some(RoomEvent::Disconnected { reason }) => {
                        tracing::warn!(?reason, "voice room disconnected unexpectedly");
                        let _ = status.send(VoiceStatus::Failed(
                            disconnect_message(reason.into()).to_owned(),
                        ));
                        break;
                    }
                    None => break,
                    _ => {}
                }
            }
        }
    }
    playback_mixer.clear_priority_active();
    priority_speakers.send_modify(BTreeSet::clear);
    if let Err(error) = stop_published_video(&room, screen_share.take()).await {
        tracing::warn!(%error, "screen-share cleanup failed");
    }
    if let Err(error) = stop_published_video(&room, camera.take()).await {
        tracing::warn!(%error, "camera cleanup failed");
    }
    if let Err(error) = room.close().await {
        tracing::warn!(%error, "LiveKit room close failed");
    }
    if !restart_requested {
        let _ = status.send(VoiceStatus::Disconnected);
    }
}

fn send_media_error(
    status: &watch::Sender<VoiceStatus>,
    room: &Room,
    can_speak: bool,
    can_stream: bool,
    screen_share: Option<&PublishedVideo>,
    camera: Option<&PublishedVideo>,
    message: String,
) {
    let _ = status.send(VoiceStatus::MediaError {
        message,
        room: room.name(),
        can_speak,
        can_stream,
        screen_sharing: screen_share.is_some(),
        camera_enabled: camera.is_some(),
    });
}

struct PublishedVideo {
    track: LocalVideoTrack,
    stop: Arc<AtomicBool>,
    capture_thread: Option<thread::JoinHandle<()>>,
}

async fn publish_screen_share(
    room: &Room,
    source_id: Option<&str>,
    settings: ScreenShareSettings,
) -> Result<PublishedVideo, VoiceError> {
    let settings = ScreenShareSettings {
        width: settings.width.clamp(640, 3840),
        height: settings.height.clamp(360, 2160),
        frame_rate: settings.frame_rate.clamp(5, 60),
        max_bitrate: settings.max_bitrate.clamp(300_000, 12_000_000),
    };
    let source = NativeVideoSource::new(
        VideoResolution {
            width: settings.width,
            height: settings.height,
        },
        true,
    );
    let track =
        LocalVideoTrack::create_video_track("screen", RtcVideoSource::Native(source.clone()));
    let stop = Arc::new(AtomicBool::new(false));
    let thread_stop = stop.clone();
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    let capture_thread = thread::Builder::new()
        .name("kaede-screen-capture".to_owned())
        .spawn({
            let source_id = source_id.map(str::to_owned);
            move || {
                run_screen_capture(
                    source,
                    thread_stop,
                    source_id.as_deref(),
                    settings,
                    ready_tx,
                );
            }
        })
        .map_err(VoiceError::CaptureThread)?;
    match time::timeout(Duration::from_secs(120), ready_rx).await {
        Ok(Ok(Ok(()))) => {}
        Ok(Ok(Err(error))) => {
            stop.store(true, Ordering::Release);
            let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
            return Err(VoiceError::ScreenWorker(error));
        }
        Ok(Err(error)) => {
            stop.store(true, Ordering::Release);
            let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
            return Err(VoiceError::ScreenWorker(error.to_string()));
        }
        Err(_) => {
            stop.store(true, Ordering::Release);
            let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
            return Err(VoiceError::ScreenWorker(
                "screen chooser timed out before capture started".to_owned(),
            ));
        }
    }
    if let Err(error) = room
        .local_participant()
        .publish_track(
            LocalTrack::Video(track.clone()),
            TrackPublishOptions {
                source: TrackSource::Screenshare,
                video_encoding: Some(VideoEncoding {
                    max_bitrate: settings.max_bitrate,
                    max_framerate: f64::from(settings.frame_rate),
                }),
                ..TrackPublishOptions::default()
            },
        )
        .await
    {
        stop.store(true, Ordering::Release);
        let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
        return Err(error.into());
    }
    Ok(PublishedVideo {
        track,
        stop,
        capture_thread: Some(capture_thread),
    })
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CameraDevice {
    pub id: String,
    pub label: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ScreenSource {
    pub id: String,
    pub label: String,
    pub kind: &'static str,
}

#[derive(Clone, Debug)]
pub struct ScreenThumbnail {
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
}

/// Enumerate displays and individual windows without starting capture. Some
/// macOS and Wayland sessions intentionally expose no list and instead show a
/// secure operating-system picker when sharing begins.
#[must_use]
pub fn screen_sources() -> Vec<ScreenSource> {
    #[cfg(target_os = "macos")]
    {
        // ScreenCaptureKit owns source disclosure and selection. Returning an
        // application-enumerated list here would make the UI imply it can
        // bypass the secure system chooser when capture deliberately cannot.
        return Vec::new();
    }
    #[cfg(target_os = "linux")]
    if is_wayland_session() {
        // The XDG desktop portal owns source enumeration and consent. Asking
        // XWayland for a parallel source list would bypass that privacy model
        // and commonly produces unusable numeric IDs.
        return Vec::new();
    }
    let mut result = Vec::new();
    for (kind, prefix) in [
        (DesktopCaptureSourceType::Screen, "screen"),
        (DesktopCaptureSourceType::Window, "window"),
    ] {
        let Some(capturer) = DesktopCapturer::new(DesktopCapturerOptions::new(kind)) else {
            continue;
        };
        for source in capturer.get_source_list() {
            let title = source.title();
            result.push(ScreenSource {
                id: format!("{prefix}:{}", source.id()),
                label: format!(
                    "{}: {}",
                    if prefix == "screen" {
                        "Display"
                    } else {
                        "Window"
                    },
                    if title.trim().is_empty() {
                        source.id().to_string()
                    } else {
                        title
                    }
                ),
                kind: if prefix == "screen" {
                    "screen"
                } else {
                    "application"
                },
            });
        }
    }
    result.sort_by(|left, right| left.label.to_lowercase().cmp(&right.label.to_lowercase()));
    result
}

/// Captures one bounded preview frame for a source that was explicitly listed
/// to the user. macOS intentionally returns no application-owned thumbnail:
/// `ScreenCaptureKit`'s secure picker remains the privacy authority there.
#[must_use]
pub fn screen_source_thumbnail(source_id: &str) -> Option<ScreenThumbnail> {
    #[cfg(target_os = "macos")]
    {
        let _ = source_id;
        return None;
    }
    #[cfg(not(target_os = "macos"))]
    {
        #[cfg(target_os = "linux")]
        if is_wayland_session() {
            let _ = source_id;
            return None;
        }
        let (kind, requested_id) = source_id
            .split_once(':')
            .and_then(|(kind, id)| id.parse::<u64>().ok().map(|id| (kind, id)))?;
        let source_type = if kind == "window" {
            DesktopCaptureSourceType::Window
        } else if kind == "screen" {
            DesktopCaptureSourceType::Screen
        } else {
            return None;
        };
        let mut capturer = DesktopCapturer::new(DesktopCapturerOptions::new(source_type))?;
        let selected = capturer
            .get_source_list()
            .into_iter()
            .find(|source| source.id() == requested_id)?;
        let (sender, receiver) = std::sync::mpsc::sync_channel(1);
        capturer.start_capture(Some(selected), move |result| {
            let Ok(frame) = result else { return };
            let Ok(width) = u32::try_from(frame.width()) else {
                return;
            };
            let Ok(height) = u32::try_from(frame.height()) else {
                return;
            };
            let (preview_width, preview_height) = bounded_dimensions(width, height, 384, 216);
            let width_usize = usize::try_from(width).unwrap_or(0);
            let height_usize = usize::try_from(height).unwrap_or(0);
            let preview_width_usize = usize::try_from(preview_width).unwrap_or(0);
            let preview_height_usize = usize::try_from(preview_height).unwrap_or(0);
            let stride = frame.stride() as usize;
            if width_usize == 0
                || height_usize == 0
                || preview_width_usize == 0
                || preview_height_usize == 0
                || stride < width_usize.saturating_mul(4)
                || frame.data().len() < stride.saturating_mul(height_usize)
            {
                return;
            }
            let mut rgba = vec![0_u8; preview_width_usize * preview_height_usize * 4];
            for target_y in 0..preview_height_usize {
                let source_y = target_y * height_usize / preview_height_usize;
                for target_x in 0..preview_width_usize {
                    let source_x = target_x * width_usize / preview_width_usize;
                    let source_offset = source_y * stride + source_x * 4;
                    let target_offset = (target_y * preview_width_usize + target_x) * 4;
                    rgba[target_offset] = frame.data()[source_offset + 2];
                    rgba[target_offset + 1] = frame.data()[source_offset + 1];
                    rgba[target_offset + 2] = frame.data()[source_offset];
                    rgba[target_offset + 3] = 255;
                }
            }
            let _ = sender.try_send(ScreenThumbnail {
                width: preview_width,
                height: preview_height,
                rgba,
            });
        });
        capturer.capture_frame();
        receiver.recv_timeout(Duration::from_millis(800)).ok()
    }
}

/// Enumerate cameras without opening one. Opening remains tied to an explicit
/// user action so merely visiting settings cannot trigger a privacy prompt.
/// Lists cameras currently visible to the native capture backend.
///
/// # Errors
///
/// Returns an error when the operating system camera backend cannot be queried.
pub fn camera_devices() -> Result<Vec<CameraDevice>, VoiceError> {
    let mut cameras = nokhwa::query(nokhwa::utils::ApiBackend::Auto)?
        .into_iter()
        .map(|camera| CameraDevice {
            id: camera.index().as_string(),
            label: camera.human_name(),
        })
        .collect::<Vec<_>>();
    cameras.sort_by(|left, right| left.label.to_lowercase().cmp(&right.label.to_lowercase()));
    Ok(cameras)
}

async fn publish_camera(
    room: &Room,
    device_id: Option<&str>,
    video_quality_mode: u8,
) -> Result<PublishedVideo, VoiceError> {
    let settings = camera_settings(video_quality_mode);
    let camera_id = device_id.map(str::to_owned);
    let source = NativeVideoSource::new(
        VideoResolution {
            width: settings.width,
            height: settings.height,
        },
        false,
    );
    let track =
        LocalVideoTrack::create_video_track("camera", RtcVideoSource::Native(source.clone()));
    let stop = Arc::new(AtomicBool::new(false));
    let thread_stop = stop.clone();
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    let capture_thread = match thread::Builder::new()
        .name("kaede-camera-capture".to_owned())
        .spawn(move || run_camera_capture(source, thread_stop, camera_id, settings, ready_tx))
    {
        Ok(thread) => thread,
        Err(error) => return Err(VoiceError::CaptureThread(error)),
    };
    match ready_rx.await {
        Ok(Ok(())) => {}
        Ok(Err(error)) => {
            stop.store(true, Ordering::Release);
            let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
            return Err(VoiceError::CameraWorker(error));
        }
        Err(error) => {
            stop.store(true, Ordering::Release);
            let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
            return Err(VoiceError::CameraWorker(error.to_string()));
        }
    }
    if let Err(error) = room
        .local_participant()
        .publish_track(
            LocalTrack::Video(track.clone()),
            TrackPublishOptions {
                source: TrackSource::Camera,
                video_encoding: Some(VideoEncoding {
                    max_bitrate: settings.max_bitrate,
                    max_framerate: f64::from(settings.frame_rate),
                }),
                ..TrackPublishOptions::default()
            },
        )
        .await
    {
        stop.store(true, Ordering::Release);
        let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
        return Err(error.into());
    }
    Ok(PublishedVideo {
        track,
        stop,
        capture_thread: Some(capture_thread),
    })
}

fn open_camera(
    device_id: Option<String>,
    settings: CameraSettings,
) -> Result<Camera, nokhwa::NokhwaError> {
    let index = match device_id {
        Some(value) => value
            .parse::<u32>()
            .map_or_else(|_| CameraIndex::String(value), CameraIndex::Index),
        None => CameraIndex::Index(0),
    };
    let format =
        RequestedFormat::new::<RgbFormat>(RequestedFormatType::Closest(CameraFormat::new_from(
            settings.width,
            settings.height,
            FrameFormat::MJPEG,
            settings.frame_rate,
        )));
    let mut camera = Camera::new(index.clone(), format).or_else(|_| {
        Camera::new(
            index,
            RequestedFormat::new::<RgbFormat>(RequestedFormatType::None),
        )
    })?;
    camera.open_stream()?;
    Ok(camera)
}

#[allow(clippy::needless_pass_by_value)]
fn run_camera_capture(
    source: NativeVideoSource,
    stop: Arc<AtomicBool>,
    device_id: Option<String>,
    settings: CameraSettings,
    ready: tokio::sync::oneshot::Sender<Result<(), String>>,
) {
    let mut camera = match open_camera(device_id, settings) {
        Ok(camera) => camera,
        Err(error) => {
            let _ = ready.send(Err(error.to_string()));
            return;
        }
    };
    let _ = ready.send(Ok(()));
    while !stop.load(Ordering::Acquire) {
        match camera.frame() {
            Ok(frame) => {
                let resolution = frame.resolution();
                let width = resolution.width();
                let height = resolution.height();
                let Some(size) = width
                    .checked_mul(height)
                    .and_then(|pixels| pixels.checked_mul(3))
                    .and_then(|bytes| usize::try_from(bytes).ok())
                else {
                    tracing::warn!(width, height, "camera returned an invalid resolution");
                    continue;
                };
                let Ok(stride_width) = usize::try_from(width) else {
                    tracing::warn!(width, height, "camera returned an invalid width");
                    continue;
                };
                let mut rgb = vec![0_u8; size];
                if let Err(error) = frame.decode_image_to_buffer::<RgbFormat>(&mut rgb) {
                    tracing::warn!(%error, "camera frame decode failed");
                    continue;
                }
                let Some(converted) = (PackedFrame {
                    width,
                    height,
                    stride: stride_width.saturating_mul(3),
                    format: PackedPixelFormat::Rgb,
                    data: &rgb,
                })
                .to_i420() else {
                    tracing::warn!(width, height, "camera produced an invalid frame");
                    continue;
                };
                let mut buffer = I420Buffer::new(converted.width, converted.height);
                let (y, u, v) = buffer.data_mut();
                y.copy_from_slice(&converted.y);
                u.copy_from_slice(&converted.u);
                v.copy_from_slice(&converted.v);
                source.capture_frame(&VideoFrame::new(VideoRotation::VideoRotation0, buffer));
            }
            Err(error) => {
                tracing::warn!(%error, "camera frame capture failed");
                thread::sleep(Duration::from_millis(100));
            }
        }
    }
}

async fn stop_published_video(
    room: &Room,
    publication: Option<PublishedVideo>,
) -> Result<(), VoiceError> {
    let Some(mut publication) = publication else {
        return Ok(());
    };
    publication.stop.store(true, Ordering::Release);
    if let Some(capture_thread) = publication.capture_thread.take() {
        let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
    }
    room.local_participant()
        .unpublish_track(&publication.track.sid())
        .await?;
    Ok(())
}

#[allow(clippy::needless_pass_by_value)]
fn run_screen_capture(
    source: NativeVideoSource,
    stop: Arc<AtomicBool>,
    requested: Option<&str>,
    settings: ScreenShareSettings,
    ready: tokio::sync::oneshot::Sender<Result<(), String>>,
) {
    let (kind, requested_id) = requested
        .and_then(|value| value.split_once(':'))
        .and_then(|(kind, id)| id.parse::<u64>().ok().map(|id| (kind, id)))
        .map_or((DesktopCaptureSourceType::Screen, None), |(kind, id)| {
            (
                if kind == "window" {
                    DesktopCaptureSourceType::Window
                } else {
                    DesktopCaptureSourceType::Screen
                },
                Some(id),
            )
        });
    #[cfg(target_os = "linux")]
    let kind = if is_wayland_session() {
        DesktopCaptureSourceType::Generic
    } else {
        kind
    };
    let mut options = DesktopCapturerOptions::new(kind);
    options.set_include_cursor(true);
    let Some(mut capturer) = DesktopCapturer::new(options) else {
        let _ = ready.send(Err(
            "screen capture is unavailable in this desktop session".to_owned()
        ));
        tracing::error!("screen capture is not available on this platform/session");
        return;
    };

    #[cfg(target_os = "macos")]
    let selected = {
        let _ = requested_id;
        // ScreenCaptureKit presents its privacy-preserving system picker. A
        // cached numeric source must not bypass that picker on macOS.
        None
    };
    #[cfg(not(target_os = "macos"))]
    let selected = {
        let sources = capturer.get_source_list();
        let selected = match requested_id {
            Some(id) => sources.into_iter().find(|source| source.id() == id),
            None => sources.into_iter().next(),
        };
        if requested_id.is_some() && selected.is_none() {
            let _ = ready.send(Err(
                "the selected screen-share source is no longer available".to_owned(),
            ));
            return;
        }
        selected
    };

    let mut ready = Some(ready);
    capturer.start_capture(selected, move |result| match result {
        Ok(frame) => {
            let Some(buffer) = prepare_screen_frame(&frame, settings) else {
                return;
            };
            source.capture_frame(&VideoFrame::new(VideoRotation::VideoRotation0, buffer));
            if let Some(ready) = ready.take() {
                let _ = ready.send(Ok(()));
            }
        }
        Err(error) => {
            if error == CaptureError::Permanent
                && let Some(ready) = ready.take()
            {
                let _ = ready.send(Err(
                    "screen capture permission was denied or the selected source closed".to_owned(),
                ));
            }
            tracing::warn!(?error, "screen capture frame failed");
        }
    });

    while !stop.load(Ordering::Acquire) {
        capturer.capture_frame();
        thread::sleep(Duration::from_millis(
            1000_u64 / u64::from(settings.frame_rate.max(1)),
        ));
    }
}

fn prepare_screen_frame(frame: &DesktopFrame, settings: ScreenShareSettings) -> Option<I420Buffer> {
    let width = u32::try_from(frame.width()).ok()?;
    let height = u32::try_from(frame.height()).ok()?;
    let converted = (PackedFrame {
        width,
        height,
        stride: usize::try_from(frame.stride()).ok()?,
        format: PackedPixelFormat::Bgra,
        data: frame.data(),
    })
    .to_i420();
    let Some(converted) = converted else {
        tracing::warn!(width, height, "screen capture produced an invalid frame");
        return None;
    };
    let mut buffer = I420Buffer::new(converted.width, converted.height);
    let (y, u, v) = buffer.data_mut();
    y.copy_from_slice(&converted.y);
    u.copy_from_slice(&converted.u);
    v.copy_from_slice(&converted.v);
    let (scaled_width, scaled_height) = bounded_dimensions(
        converted.width,
        converted.height,
        settings.width,
        settings.height,
    );
    if scaled_width == converted.width && scaled_height == converted.height {
        Some(buffer)
    } else {
        Some(buffer.scale(
            i32::try_from(scaled_width).unwrap_or(i32::MAX),
            i32::try_from(scaled_height).unwrap_or(i32::MAX),
        ))
    }
}

#[cfg(target_os = "linux")]
fn is_wayland_session() -> bool {
    is_wayland_environment(
        std::env::var_os("WAYLAND_DISPLAY").is_some(),
        std::env::var("XDG_SESSION_TYPE").ok().as_deref(),
    )
}

#[cfg(target_os = "linux")]
fn is_wayland_environment(has_wayland_display: bool, session_type: Option<&str>) -> bool {
    has_wayland_display || session_type.is_some_and(|value| value.eq_ignore_ascii_case("wayland"))
}

fn bounded_dimensions(width: u32, height: u32, max_width: u32, max_height: u32) -> (u32, u32) {
    if width <= max_width && height <= max_height {
        return (width.max(2) & !1, height.max(2) & !1);
    }
    let (scaled_width, scaled_height) =
        if u64::from(max_width) * u64::from(height) <= u64::from(max_height) * u64::from(width) {
            let height = u64::from(height) * u64::from(max_width) / u64::from(width.max(1));
            (max_width, u32::try_from(height).unwrap_or(max_height))
        } else {
            let width = u64::from(width) * u64::from(max_height) / u64::from(height.max(1));
            (u32::try_from(width).unwrap_or(max_width), max_height)
        };
    let scaled_width = scaled_width.max(2) & !1;
    let scaled_height = scaled_height.max(2) & !1;
    (scaled_width, scaled_height)
}

#[derive(Debug, Error)]
pub enum VoiceError {
    #[error(transparent)]
    Api(#[from] ApiClientError),
    #[error(transparent)]
    Audio(#[from] kaede_audio::AudioError),
    #[error("LiveKit failed: {0}")]
    LiveKit(#[from] livekit::RoomError),
    #[error("camera capture failed: {0}")]
    Camera(#[from] nokhwa::NokhwaError),
    #[error("camera capture worker failed: {0}")]
    CameraWorker(String),
    #[error("screen capture worker failed: {0}")]
    ScreenWorker(String),
    #[error("voice activity is disabled in this channel; select push to talk")]
    VoiceActivityDenied,
    #[error("the voice grant did not match the current channel policy")]
    EncryptionPolicyMismatch,
    #[error("this encrypted voice room requires a device media key")]
    EncryptionKeyMissing,
    #[error("failed to start native capture thread: {0}")]
    CaptureThread(std::io::Error),
}

impl VoiceError {
    /// Returns recovery-oriented wording suitable for the voice UI.
    #[must_use]
    pub fn user_message(&self) -> String {
        match self {
            Self::Api(error) => error.user_message(),
            Self::Audio(kaede_audio::AudioError::DeviceNotFound) =>
                "The selected microphone or speaker is no longer available. Choose another audio device and try again."
                    .to_owned(),
            Self::Audio(kaede_audio::AudioError::UnsupportedFormat(_)) =>
                "The selected audio device uses a format Kaede does not support. Choose another device and try again."
                    .to_owned(),
            Self::Audio(kaede_audio::AudioError::Backend(_)) =>
                "Kaede could not open the selected audio device. Check your system audio permissions, then choose the device again."
                    .to_owned(),
            Self::LiveKit(_) =>
                "The voice service could not complete the connection. Check your connection and try joining voice again."
                    .to_owned(),
            Self::Camera(_) =>
                "Kaede could not use the selected camera. Check camera permission and whether another app is using it, then try again."
                    .to_owned(),
            Self::CameraWorker(_) =>
                "The camera stopped unexpectedly. Turn the camera off and on; if it keeps failing, choose another camera."
                    .to_owned(),
            Self::ScreenWorker(_) =>
                "Screen sharing did not start. Approve the system chooser and screen-recording permission, then try again."
                    .to_owned(),
            Self::VoiceActivityDenied =>
                "Voice activity is not allowed in this channel. Switch your input mode to push to talk and try again."
                    .to_owned(),
            Self::EncryptionPolicyMismatch =>
                "The voice channel policy changed before Kaede could join. Refresh the conversation and try again."
                    .to_owned(),
            Self::EncryptionKeyMissing =>
                "This call is encrypted, but this device does not have its media key. Restore or re-enroll this encryption device and try again."
                    .to_owned(),
            Self::CaptureThread(_) =>
                "Kaede could not start screen capture. Check screen-recording permission and try sharing again."
                    .to_owned(),
        }
    }
}

fn disconnect_message(reason: DisconnectReason) -> &'static str {
    match reason {
        DisconnectReason::ClientInitiated => "You left voice.",
        DisconnectReason::DuplicateIdentity => {
            "Voice moved to another device. This device will stay disconnected unless you explicitly move voice back here."
        }
        DisconnectReason::ServerShutdown => {
            "The voice server restarted. Wait a moment and join voice again."
        }
        DisconnectReason::ParticipantRemoved => {
            "This voice connection was ended from another device or by a moderator. It will not reconnect automatically."
        }
        DisconnectReason::RoomDeleted | DisconnectReason::RoomClosed => {
            "This voice session has ended and is no longer available."
        }
        DisconnectReason::StateMismatch | DisconnectReason::Migration => {
            "The voice session changed while you were connected. Join voice again."
        }
        DisconnectReason::JoinFailure => {
            "The voice service could not finish joining. Check your connection and try again."
        }
        DisconnectReason::SignalClose | DisconnectReason::ConnectionTimeout => {
            "The voice connection was lost. Check your connection and join again."
        }
        DisconnectReason::UserUnavailable => "The person you called is unavailable.",
        DisconnectReason::UserRejected => "The person you called declined the call.",
        DisconnectReason::SipTrunkFailure => {
            "The phone connection failed. Wait a moment and try the call again."
        }
        DisconnectReason::MediaFailure => {
            "Voice media stopped working. Check your audio devices and connection, then join again."
        }
        DisconnectReason::AgentError => {
            "The voice assistant stopped unexpectedly. Try joining again."
        }
        DisconnectReason::UnknownReason => {
            "The voice session ended unexpectedly. Check your connection and join again."
        }
    }
}

#[cfg(test)]
mod tests {
    use secrecy::SecretString;

    #[cfg(target_os = "linux")]
    use super::is_wayland_environment;
    use super::{
        DataPacketKind, DisconnectReason, ExpectedVoicePolicy, VoiceError, VoiceGrant,
        bounded_dimensions, camera_settings, decode_priority_speaker_signal, disconnect_message,
        effective_microphone_bitrate, local_priority_speaker_allowed,
        local_priority_speaker_metadata_allowed, local_priority_speaker_metadata_transition,
        local_voice_grant_rotation, media_room_options, participant_can_priority_speak,
        priority_speaker_transition,
    };

    fn grant(e2ee: bool) -> VoiceGrant {
        VoiceGrant {
            token: SecretString::from("x".repeat(32)),
            url: "wss://chat.example/livekit".to_owned(),
            room: "g.1.2".to_owned(),
            generation: 0,
            expires_at: "2026-08-18T12:00:00Z".to_owned(),
            can_speak: true,
            can_stream: true,
            can_priority_speak: true,
            can_use_vad: true,
            bitrate: 64_000,
            user_limit: 0,
            rtc_region: None,
            video_quality_mode: 1,
            move_session_id: None,
            e2ee,
            channel_id: Some("2".to_owned()),
            channel_domain: Some("chat.example".to_owned()),
            encryption_policy_generation: e2ee.then(|| "4".to_owned()),
            encryption_epoch: e2ee.then(|| "7".to_owned()),
            media_protocol: e2ee.then(|| "livekit-e2ee-v1".to_owned()),
            media_suite: e2ee.then(|| "AES-256-GCM".to_owned()),
            media_session_id: e2ee.then(|| "a".repeat(43)),
            media_epoch: e2ee.then(|| "7".to_owned()),
        }
    }

    fn expected(e2ee: bool) -> ExpectedVoicePolicy {
        ExpectedVoicePolicy {
            e2ee,
            room: "g.1.2".to_owned(),
            channel_id: "2".to_owned(),
            channel_domain: "chat.example".to_owned(),
            bitrate: 64_000,
            user_limit: 0,
            rtc_region: None,
            video_quality_mode: 1,
            encryption_policy_generation: e2ee.then(|| "4".to_owned()),
            encryption_epoch: e2ee.then(|| "7".to_owned()),
            media_protocol: e2ee.then(|| "livekit-e2ee-v1".to_owned()),
            media_suite: e2ee.then(|| "AES-256-GCM".to_owned()),
            media_session_id: e2ee.then(|| "a".repeat(43)),
            media_epoch: e2ee.then(|| "7".to_owned()),
        }
    }

    #[test]
    fn voice_errors_explain_the_recovery_action() {
        assert_eq!(
            VoiceError::Audio(kaede_audio::AudioError::DeviceNotFound).user_message(),
            "The selected microphone or speaker is no longer available. Choose another audio device and try again."
        );
        assert!(
            VoiceError::VoiceActivityDenied
                .user_message()
                .contains("push to talk")
        );
    }

    #[test]
    fn disconnect_reasons_do_not_leak_debug_enum_names() {
        assert_eq!(
            disconnect_message(DisconnectReason::DuplicateIdentity),
            "Voice moved to another device. This device will stay disconnected unless you explicitly move voice back here."
        );
        assert!(disconnect_message(DisconnectReason::ConnectionTimeout).contains("join again"));
    }

    #[test]
    fn native_voice_policy_is_bidirectional_before_media_setup() {
        assert!(media_room_options(&grant(false), &expected(false), None).is_ok());
        assert!(media_room_options(&grant(true), &expected(true), Some(vec![7; 32])).is_ok());
        assert!(matches!(
            media_room_options(&grant(false), &expected(true), None),
            Err(VoiceError::EncryptionPolicyMismatch)
        ));
        assert!(matches!(
            media_room_options(&grant(true), &expected(false), Some(vec![7; 32])),
            Err(VoiceError::EncryptionPolicyMismatch)
        ));
        let mut changed = grant(true);
        changed.media_session_id = Some("b".repeat(43));
        assert!(matches!(
            media_room_options(&changed, &expected(true), Some(vec![7; 32])),
            Err(VoiceError::EncryptionPolicyMismatch)
        ));

        let mut configured = ExpectedVoicePolicy {
            bitrate: 32_000,
            user_limit: 17,
            rtc_region: Some("future-region/alpha".to_owned()),
            video_quality_mode: 2,
            ..expected(false)
        };
        let mut configured_grant = grant(false);
        configured_grant.bitrate = 32_000;
        configured_grant.user_limit = 17;
        configured_grant.rtc_region = Some("future-region/alpha".to_owned());
        configured_grant.video_quality_mode = 2;
        assert!(media_room_options(&configured_grant, &configured, None).is_ok());

        configured.user_limit = 10_000;
        configured_grant.user_limit = 10_000;
        assert!(media_room_options(&configured_grant, &configured, None).is_ok());
        configured_grant.rtc_region = Some("other".to_owned());
        assert!(matches!(
            media_room_options(&configured_grant, &configured, None),
            Err(VoiceError::EncryptionPolicyMismatch)
        ));

        let mut invalid_expected = expected(false);
        let mut invalid_grant = grant(false);
        invalid_expected.bitrate = 7_999;
        invalid_grant.bitrate = 7_999;
        assert!(matches!(
            media_room_options(&invalid_grant, &invalid_expected, None),
            Err(VoiceError::EncryptionPolicyMismatch)
        ));
        invalid_expected = expected(false);
        invalid_grant = grant(false);
        invalid_expected.user_limit = 10_001;
        invalid_grant.user_limit = 10_001;
        assert!(matches!(
            media_room_options(&invalid_grant, &invalid_expected, None),
            Err(VoiceError::EncryptionPolicyMismatch)
        ));
        invalid_expected = expected(false);
        invalid_grant = grant(false);
        invalid_expected.video_quality_mode = 3;
        invalid_grant.video_quality_mode = 3;
        assert!(matches!(
            media_room_options(&invalid_grant, &invalid_expected, None),
            Err(VoiceError::EncryptionPolicyMismatch)
        ));
    }

    #[test]
    fn native_voice_grant_requires_an_explicit_encryption_mode() {
        let value = serde_json::json!({
            "token": "x".repeat(32),
            "url": "wss://chat.example/livekit",
            "room": "g.1.2",
            "generation": 0,
            "expires_at": "2026-08-18T12:00:00Z",
            "can_speak": true,
            "can_stream": true,
            "bitrate": 64000,
            "user_limit": 0,
            "rtc_region": null,
            "video_quality_mode": 1,
            "channel_id": "2",
            "channel_domain": "chat.example"
        });
        assert!(serde_json::from_value::<VoiceGrant>(value).is_err());
    }

    #[test]
    fn native_voice_grant_requires_and_preserves_media_policy() {
        let value = serde_json::json!({
            "token": "x".repeat(32),
            "url": "wss://chat.example/livekit",
            "room": "g.1.2",
            "generation": 0,
            "expires_at": "2026-08-18T12:00:00Z",
            "can_speak": true,
            "can_stream": true,
            "bitrate": 32000,
            "user_limit": 17,
            "rtc_region": "future-region/alpha",
            "video_quality_mode": 2,
            "e2ee": false,
            "channel_id": "2",
            "channel_domain": "chat.example"
        });
        let parsed = serde_json::from_value::<VoiceGrant>(value.clone());
        assert!(parsed.is_ok());
        if let Ok(parsed) = parsed {
            assert_eq!(parsed.bitrate, 32_000);
            assert_eq!(parsed.user_limit, 17);
            assert_eq!(parsed.rtc_region.as_deref(), Some("future-region/alpha"));
            assert_eq!(parsed.video_quality_mode, 2);
            assert!(!parsed.can_priority_speak);
        }

        for field in ["bitrate", "user_limit", "rtc_region", "video_quality_mode"] {
            let mut missing = value.clone();
            assert!(missing.is_object());
            if let Some(object) = missing.as_object_mut() {
                object.remove(field);
            }
            assert!(serde_json::from_value::<VoiceGrant>(missing).is_err());
        }
    }

    #[test]
    fn priority_speaker_signals_are_exact_and_server_metadata_bound() {
        let metadata = r#"{"user_id":"42","user_domain":"Chat.Example","can_speak":true,"can_priority_speak":true}"#;
        assert!(participant_can_priority_speak("42@chat.example", metadata));
        assert_eq!(
            decode_priority_speaker_signal(
                "42@chat.example",
                metadata,
                Some(super::PRIORITY_SPEAKER_TOPIC),
                DataPacketKind::Reliable,
                &[1],
            ),
            Some(true)
        );
        assert_eq!(
            decode_priority_speaker_signal(
                "42@chat.example",
                metadata,
                Some(super::PRIORITY_SPEAKER_TOPIC),
                DataPacketKind::Reliable,
                &[0],
            ),
            Some(false)
        );

        for (identity, candidate_metadata, topic, kind, payload) in [
            (
                "43@chat.example",
                metadata,
                Some(super::PRIORITY_SPEAKER_TOPIC),
                DataPacketKind::Reliable,
                &[1][..],
            ),
            (
                "42@chat.example",
                r#"{"user_id":"42","user_domain":"chat.example","can_speak":true,"can_priority_speak":false}"#,
                Some(super::PRIORITY_SPEAKER_TOPIC),
                DataPacketKind::Reliable,
                &[1][..],
            ),
            (
                "42@chat.example",
                metadata,
                Some("kaede.priority-speaker.v2"),
                DataPacketKind::Reliable,
                &[1][..],
            ),
            (
                "42@chat.example",
                metadata,
                Some(super::PRIORITY_SPEAKER_TOPIC),
                DataPacketKind::Lossy,
                &[1][..],
            ),
            (
                "42@chat.example",
                metadata,
                Some(super::PRIORITY_SPEAKER_TOPIC),
                DataPacketKind::Reliable,
                &[1, 0][..],
            ),
        ] {
            assert_eq!(
                decode_priority_speaker_signal(identity, candidate_metadata, topic, kind, payload,),
                None
            );
        }
        assert!(!participant_can_priority_speak(
            "42@chat.example",
            r#"{"user_id":"42","user_domain":"chat.example","can_speak":false,"can_priority_speak":true}"#,
        ));
    }

    #[test]
    fn priority_speaker_publication_is_authorized_and_edge_triggered() {
        assert_eq!(priority_speaker_transition(false, true, false), None);
        assert_eq!(priority_speaker_transition(false, true, true), Some([1]));
        assert_eq!(priority_speaker_transition(true, true, true), None);
        assert_eq!(priority_speaker_transition(true, false, true), Some([0]));
        assert_eq!(priority_speaker_transition(false, false, true), None);
    }

    #[test]
    fn local_priority_speaker_requires_a_guild_ptt_grant() {
        let priority_grant = grant(false);
        assert!(local_priority_speaker_allowed(
            true,
            &priority_grant,
            kaede_audio::InputMode::PushToTalk,
        ));
        assert!(!local_priority_speaker_allowed(
            true,
            &priority_grant,
            kaede_audio::InputMode::VoiceActivity,
        ));
        assert!(!local_priority_speaker_allowed(
            false,
            &priority_grant,
            kaede_audio::InputMode::PushToTalk,
        ));
        let mut denied = priority_grant;
        denied.can_priority_speak = false;
        assert!(!local_priority_speaker_allowed(
            true,
            &denied,
            kaede_audio::InputMode::PushToTalk,
        ));
    }

    #[test]
    fn local_priority_capability_follows_authoritative_metadata_rotation() {
        let granted = r#"{"user_id":"42","user_domain":"chat.example","can_speak":true,"can_priority_speak":true}"#;
        let revoked = r#"{"user_id":"42","user_domain":"chat.example","can_speak":true,"can_priority_speak":false}"#;
        assert!(local_priority_speaker_metadata_allowed(
            true,
            "42@chat.example",
            granted,
        ));
        assert!(!local_priority_speaker_metadata_allowed(
            true,
            "42@chat.example",
            revoked,
        ));
        assert!(!local_priority_speaker_metadata_allowed(
            false,
            "42@chat.example",
            granted,
        ));
        assert_eq!(
            local_priority_speaker_metadata_transition(true, true, "42@chat.example", revoked,),
            (false, true)
        );
        assert_eq!(
            local_priority_speaker_metadata_transition(true, false, "42@chat.example", granted,),
            (true, false)
        );
    }

    #[test]
    fn immutable_local_grant_rotation_requires_a_fresh_native_grant() {
        let granted = r#"{"user_id":"42","user_domain":"chat.example","can_speak":true,"can_stream":true,"can_use_vad":true}"#;
        let listen_only = r#"{"user_id":"42","user_domain":"chat.example","can_speak":false,"can_stream":true,"can_use_vad":true}"#;
        let stream_revoked = r#"{"user_id":"42","user_domain":"chat.example","can_speak":true,"can_stream":false,"can_use_vad":true}"#;
        let vad_revoked = r#"{"user_id":"42","user_domain":"chat.example","can_speak":true,"can_stream":true,"can_use_vad":false}"#;

        assert!(local_voice_grant_rotation(
            false,
            true,
            true,
            kaede_audio::InputMode::PushToTalk,
            "42@chat.example",
            granted,
        ));
        assert!(local_voice_grant_rotation(
            true,
            true,
            true,
            kaede_audio::InputMode::PushToTalk,
            "42@chat.example",
            listen_only,
        ));
        assert!(local_voice_grant_rotation(
            true,
            true,
            true,
            kaede_audio::InputMode::PushToTalk,
            "42@chat.example",
            stream_revoked,
        ));
        assert!(local_voice_grant_rotation(
            true,
            true,
            true,
            kaede_audio::InputMode::VoiceActivity,
            "42@chat.example",
            vad_revoked,
        ));
        assert!(!local_voice_grant_rotation(
            true,
            true,
            true,
            kaede_audio::InputMode::PushToTalk,
            "42@chat.example",
            vad_revoked,
        ));
        assert!(!local_voice_grant_rotation(
            true,
            true,
            true,
            kaede_audio::InputMode::VoiceActivity,
            "42@chat.example",
            granted,
        ));
        assert!(!local_voice_grant_rotation(
            false,
            true,
            true,
            kaede_audio::InputMode::PushToTalk,
            "43@chat.example",
            granted,
        ));
    }

    #[test]
    fn media_policy_caps_microphone_and_selects_camera_defaults() {
        assert_eq!(effective_microphone_bitrate(128_000, 32_000), 32_000);
        assert_eq!(effective_microphone_bitrate(24_000, 96_000), 24_000);
        assert_eq!(effective_microphone_bitrate(4_000, 8_000), 8_000);

        let automatic = camera_settings(1);
        let full = camera_settings(2);
        assert_eq!((automatic.width, automatic.height), (640, 360));
        assert_eq!((full.width, full.height), (1280, 720));
        assert!(automatic.max_bitrate < full.max_bitrate);
    }

    #[test]
    fn screen_share_scaling_preserves_aspect_ratio_and_even_dimensions() {
        assert_eq!(bounded_dimensions(1920, 1080, 1280, 720), (1280, 720));
        assert_eq!(bounded_dimensions(3440, 1440, 1920, 1080), (1920, 802));
        assert_eq!(bounded_dimensions(1279, 719, 1920, 1080), (1278, 718));
        assert_eq!(bounded_dimensions(1, 1, 1280, 720), (2, 2));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn wayland_detection_accepts_either_authoritative_session_signal() {
        assert!(is_wayland_environment(true, Some("x11")));
        assert!(is_wayland_environment(false, Some("Wayland")));
        assert!(!is_wayland_environment(false, Some("x11")));
        assert!(!is_wayland_environment(false, None));
    }
}
