//! `LiveKit` transport backed exclusively by Kaede's `CPAL` audio graph.

mod camera_catalog;
mod native_video_probe;
mod screen_audio;
pub use kaede_capture::system_audio::{
    AudioApplication, applications as screen_audio_applications,
};

use std::{
    borrow::Cow,
    collections::{BTreeMap, BTreeSet},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU32, Ordering},
    },
    thread,
    time::{Duration, Instant},
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
        key_provider::{KeyDerivationAlgorithm, KeyProvider, KeyProviderOptions},
    },
    options::{
        AudioEncoding, TrackPublishOptions, VideoCodec, VideoEncoding, compute_video_encodings,
    },
    prelude::{
        DataPacket, DataPacketKind, DisconnectReason, LocalAudioTrack, LocalParticipant,
        LocalTrack, LocalVideoTrack, Participant, RemoteTrack, Room, RoomEvent, RoomOptions,
        TrackKind, TrackSource,
    },
    webrtc::{
        audio_frame::AudioFrame,
        audio_source::{AudioSourceOptions, RtcAudioSource, native::NativeAudioSource},
        audio_stream::native::NativeAudioStream,
        desktop_capturer::{
            CaptureError, DesktopCaptureSourceType, DesktopCapturer, DesktopCapturerOptions,
            DesktopFrame,
        },
        rtp_parameters::RtpEncodingParameters,
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
    pub screen_share: bool,
    pub width: u32,
    pub height: u32,
    pub rgba: Vec<u8>,
    pub removed: bool,
}

#[derive(Clone, Debug)]
pub enum VoiceCommand {
    SetVolume {
        identity: String,
        stream: bool,
        volume: f32,
    },
    SetMuted(bool),
    SetVideoVisible(bool),
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
    pub opus_dtx: bool,
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
            opus_dtx: true,
        }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct ScreenShareSettings {
    pub share_audio: bool,
    /// Explicit Linux portal audio choice: zero for desktop, otherwise an app PID.
    pub audio_process: Option<u32>,
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
            share_audio: false,
            audio_process: None,
            width: 1280,
            height: 720,
            frame_rate: 30,
            max_bitrate: 2_500_000,
        }
    }
}

pub struct VoiceHandle {
    pub video_degraded: Arc<AtomicBool>,
    pub commands: mpsc::UnboundedSender<VoiceCommand>,
    pub status: watch::Receiver<VoiceStatus>,
    /// Becomes true when authoritative local permissions no longer match the
    /// immutable capture/publication graph created from the join grant, or an
    /// audio-device failure requires rebuilding that graph.
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
    let started = Instant::now();
    tracing::info!("voice join: requesting grant");
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
    tracing::info!(
        elapsed_ms = started.elapsed().as_millis(),
        "voice join: grant received"
    );
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
    let started = Instant::now();
    tracing::info!("voice join: requesting grant");
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
    tracing::info!(
        elapsed_ms = started.elapsed().as_millis(),
        "voice join: grant received"
    );
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
    // Cumulative milestones distinguish cold native setup from network delays.
    let started = Instant::now();
    tracing::info!("voice join: preparing media");
    let initially_muted = media.initially_muted;
    let initially_deafened = media.initially_deafened;
    let room_options = media_room_options(&grant, &expected_policy, media_key)?;
    tracing::info!(
        elapsed_ms = started.elapsed().as_millis(),
        "voice join: room options ready"
    );
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
    tracing::info!(
        elapsed_ms = started.elapsed().as_millis(),
        "voice join: speech processor ready"
    );
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
    tracing::info!(
        elapsed_ms = started.elapsed().as_millis(),
        "voice join: capture ready"
    );
    let playback = NativePlayback::open(media.output_device.as_deref())?;
    playback.set_deafened(initially_deafened);
    let input_level = capture.as_ref().map(|capture| capture.gate.clone());
    tracing::info!(
        elapsed_ms = started.elapsed().as_millis(),
        "voice join: playback ready, initializing WebRTC"
    );
    // Retain the runtime through connect so timing it cannot initialize it twice.
    let runtime = livekit::rtc_engine::lk_runtime::LkRuntime::instance();
    tracing::info!(
        elapsed_ms = started.elapsed().as_millis(),
        "voice join: WebRTC ready, connecting room"
    );
    let room_name = grant.room.clone();
    let (room, events) = Box::pin(Room::connect(
        &grant.url,
        grant.token.expose_secret(),
        room_options,
    ))
    .await?;

    drop(runtime);
    tracing::info!(
        elapsed_ms = started.elapsed().as_millis(),
        "voice join: room connected"
    );
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
        tracing::info!(
            elapsed_ms = started.elapsed().as_millis(),
            "voice join: microphone track ready, publishing"
        );
        room.local_participant()
            .publish_track(
                LocalTrack::Audio(track),
                TrackPublishOptions {
                    source: TrackSource::Microphone,
                    dtx: media.publish.opus_dtx,
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
    tracing::info!(
        elapsed_ms = started.elapsed().as_millis(),
        "voice join: media ready"
    );
    let _ = status_tx.send(VoiceStatus::Connected {
        room: room_name,
        can_speak: grant.can_speak,
        can_stream: grant.can_stream,
        screen_sharing: false,
        camera_enabled: false,
    });
    let video_degraded = Arc::new(AtomicBool::new(false));
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
        video_degraded.clone(),
    ));
    Ok(VoiceHandle {
        video_degraded,
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
        (false, None) => {
            let mut options = RoomOptions::default();
            options.auto_subscribe = false;
            options.dynacast = true;
            Ok(options)
        }
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
                    key_derivation_algorithm: KeyDerivationAlgorithm::HKDF,
                    ..KeyProviderOptions::default()
                },
                key,
            );
            let mut options = RoomOptions::default();
            options.auto_subscribe = false;
            options.dynacast = true;
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
    video_degraded: Arc<AtomicBool>,
) {
    let playback_sink = playback.sink();
    let playback_mixer = playback.mixer();
    let render_reference = playback.render_reference();
    let mut explicitly_muted = initially_muted;
    let mut deafened = initially_deafened;
    let mut priority_push_to_talk_active = false;
    let mut restart_requested = false;
    let local_identity = room.local_participant().identity().to_string();
    let encrypted = room.e2ee_manager().encryption_type() != EncryptionType::None;
    let allow_h264 = Arc::new(AtomicBool::new(!encrypted));
    let mut capture_tick = time::interval(AUDIO_FRAME_TIME);
    let mut playback_tick = time::interval(AUDIO_FRAME_TIME);
    let mut screen_share: Option<PublishedVideo> = None;
    let mut camera: Option<PublishedVideo> = None;
    let mut screen_start: Option<PendingVideo<'_>> = None;
    let mut camera_start: Option<PendingVideo<'_>> = None;
    let mut video_tick = time::interval(Duration::from_secs(2));
    video_tick.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    let mut subscriptions = BTreeSet::new();
    let mut video_visible = true;
    let mut budget = VideoBudget::default();
    let mut upload = UploadPressure::default();
    let mut encode_headroom = EncodeHeadroom::default();
    let send_budget = Arc::new(tokio::sync::Mutex::new(VideoSendBudget::default()));
    let variant_backoff = Arc::new(Mutex::new(VideoBackoff::default()));
    let mut first_seen: BTreeMap<String, Instant> = BTreeMap::new();
    let (mut force_announce, mut last_announced, mut last_announce_at) =
        (true, Vec::new(), None::<Instant>);
    // Per published track: first sample time and consecutive software encodes.
    let publisher_tracks = Arc::new(Mutex::new(BTreeMap::<String, (Instant, u8)>::new()));
    let mut video_peers: BTreeMap<String, VideoPeer> = BTreeMap::new();
    let publisher_samples = Arc::new(Mutex::new(
        BTreeMap::<String, (u32, f64, std::time::Instant)>::new(),
    ));
    let receiver_health = Arc::new(Mutex::new(BTreeMap::<String, ReceiverHealth>::new()));
    let (performance_tx, mut performance_rx) = mpsc::channel::<PerformanceReport>(1);
    let mut performance_job: Option<JoinHandle<()>> = None;
    native_video_probe::warm_hevc_decode();
    let mut capability_tick = time::interval(video_policy::SAMPLE);
    capability_tick.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    let mut video_readers: BTreeMap<(String, bool), (String, JoinHandle<()>)> = BTreeMap::new();
    capture_tick.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    playback_tick.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            Some(result) = futures_util::future::OptionFuture::from(screen_start.as_mut()) => {
                screen_start = None;
                match result {
                    Ok(video) => { screen_share = Some(video); }
                    Err(error) => {
                        send_media_error(&status, &room, can_speak, can_stream, screen_share.as_ref(), camera.as_ref(), error.user_message());
                        continue;
                    }
                }
                let _ = status.send(VoiceStatus::Connected {
                    room: room.name(), can_speak, can_stream,
                    screen_sharing: screen_share.is_some(), camera_enabled: camera.is_some(),
                });
            }
            Some(result) = futures_util::future::OptionFuture::from(camera_start.as_mut()) => {
                camera_start = None;
                match result {
                    Ok(video) => { camera = Some(video); }
                    Err(error) => {
                        send_media_error(&status, &room, can_speak, can_stream, screen_share.as_ref(), camera.as_ref(), error.user_message());
                        continue;
                    }
                }
                let _ = status.send(VoiceStatus::Connected {
                    room: room.name(), can_speak, can_stream,
                    screen_sharing: screen_share.is_some(), camera_enabled: camera.is_some(),
                });
            }
            report = performance_rx.recv() => {
                if let Some(report) = report {
                    let now = Instant::now();
                    if camera.is_none() && screen_share.is_none() {
                        budget = VideoBudget::default();
                        upload = UploadPressure::default();
                        encode_headroom = EncodeHeadroom::default();
                    } else {
                        encode_headroom.observe(report.encode_load, report.cpu, now);
                    }
                    if report.observed {
                        // Capture threads read the level per frame: adapting never
                        // republishes, so viewers see no interruption.
                        budget.observe(report.cpu);
                        upload.observe(report.bandwidth);
                    } else {
                        budget.bad = 0;
                        budget.healthy = 0;
                    }
                    for video in [camera.as_mut(), screen_share.as_mut()].into_iter().flatten() {
                        video.capture_level.store(u32::from(budget.level), Ordering::Release);
                        if report.software_variants.is_empty() || video.adjustment.is_some() { continue; }
                        let Ok(mut tracks) = video.variants.lock() else { continue };
                        let (software, kept): (Vec<_>, Vec<_>) = std::mem::take(&mut *tracks).into_iter().partition(|track| report.software_variants.contains(&track.name()));
                        *tracks = kept;
                        drop(tracks);
                        if software.is_empty() { continue; }
                        // A "hardware" variant that encodes in software costs CPU and
                        // brings nothing over the VP8 base: retire it and back off.
                        if let Ok(mut backoff) = variant_backoff.lock() {
                            for track in &software {
                                if let Some(codec) = track.name().rsplit(':').next() { backoff.fail(&variant_key(video, codec), now); }
                            }
                        }
                        let participant = room.local_participant();
                        let variants = video.variants.clone();
                        let send_budget = video.send_budget.clone();
                        video.adjustment = Some(tokio::spawn(async move {
                            for variant in software {
                                if unpublish_budgeted_video(&participant, &variant, &send_budget).await.is_err()
                                    && let Ok(mut tracks) = variants.lock()
                                { tracks.push(variant); }
                            }
                            None
                        }));
                    }
                    let receiver_degraded = receiver_health.lock().is_ok_and(|states| states.values().any(|state| matches!(state.health, "decode" | "download")));
                    video_degraded.store(budget.level > 0 || upload.constrained || receiver_degraded, Ordering::Release);
                }
            }
            _ = capability_tick.tick() => {
                native_video_probe::retry_hevc_decode();
                if let Ok(mut limits) = send_budget.try_lock() {
                    limits.level = budget.level;
                    limits.apply(None);
                }
                let now = Instant::now();
                let identities: BTreeSet<String> = room.remote_participants().keys().map(ToString::to_string).collect();
                let participant_count = identities.len();
                video_peers.retain(|identity, _| identities.contains(identity));
                let h264_ready = room_allows_h264(encrypted, identities.iter().map(|id| video_peers.get(id)));
                allow_h264.store(h264_ready, Ordering::Release);
                first_seen.retain(|identity, _| identities.contains(identity));
                for identity in &identities { first_seen.entry(identity.clone()).or_insert(now); }
                // Hold premium changes during hint grace, while restoring VP8 for unknown viewers.
                let pending = first_seen.iter().any(|(identity, seen)| !video_peers.contains_key(identity) && now.duration_since(*seen) < video_policy::HINT_GRACE);
                for video in [camera.as_mut(), screen_share.as_mut()].into_iter().flatten() {
                    video.capture_level.store(u32::from(budget.level), Ordering::Release);
                    if video.adjustment.as_ref().is_some_and(JoinHandle::is_finished)
                        && let Some(task) = video.adjustment.take()
                        && let Ok(Some((track, codec))) = task.await
                    {
                        if video.track.rtc_track().id() != track.rtc_track().id() {
                            video.h264_withdrawn = false;
                        }
                        video.track = track;
                        video.compatible_codec = codec;
                    }
                    if !h264_ready && video.compatible_codec == VideoCodec::H264 && video.track.is_enabled() {
                        video.track.disable();
                        video.h264_withdrawn = true;
                    } else if h264_ready && video.h264_withdrawn {
                        if !video.track.is_muted() { video.track.enable(); }
                        video.h264_withdrawn = false;
                    }
                    let needs_compatible_track = video.compatible_codec == VideoCodec::VP9
                        || (!h264_ready && video.compatible_codec == VideoCodec::H264);
                    if video.adjustment.is_some() || (pending && !needs_compatible_track) { continue; }
                    let demand_key = format!("{}/{}", local_identity, if video.source == TrackSource::Screenshare { "screen_share" } else { "camera" });
                    let mut blocked: BTreeSet<String> = ["av1", "h265", "h264", "vp9"].into_iter().filter(|codec| {
                        variant_backoff.lock().map_or(true, |backoff| backoff.blocked(&variant_key(video, codec), now))
                    }).map(str::to_owned).collect();
                    if !h264_ready { blocked.insert("h264".to_owned()); }
                    let context = VariantContext { level: budget.level, upload_constrained: upload.constrained, cpu_spare: encode_headroom.has_spare(now), cpu_recovered: budget.healthy >= video_policy::RECOVER_SAMPLES, blocked };
                    let desired = preferred_native_variant(&video_peers, participant_count, &demand_key, &context);
                    let compatible = if pending { VideoCodec::VP8 } else { preferred_native_compatible(&video_peers, participant_count, &demand_key, video.compatible_codec, &context) };
                    // A late legacy viewer needs the compatible publication restored first.
                    if video.compatible_codec != compatible || !room.local_participant().track_publications().contains_key(&video.track.sid()) {
                        video.adjustment = Some(change_video_codec(&room, video, compatible, true, variant_backoff.clone(), allow_h264.clone()));
                        continue;
                    }
                    let current = video.variants.lock().ok().and_then(|tracks| tracks.first().map(LocalVideoTrack::name));
                    if current.as_ref().is_some_and(|name| desired.is_none_or(|codec| !name.ends_with(&format!(":{}", codec.as_str())))) {
                        if let Some(name) = &current
                            && let Some(codec) = name.rsplit(':').next()
                            && pressure_retires_native_variant(&video_peers, participant_count, &demand_key, codec, &context)
                            && let Ok(mut backoff) = variant_backoff.lock()
                        { backoff.fail(&variant_key(video, codec), now); }
                        let retired = video.variants.lock().map(|mut tracks| std::mem::take(&mut *tracks)).unwrap_or_default();
                        let participant = room.local_participant();
                        let variants = video.variants.clone();
                        let send_budget = video.send_budget.clone();
                        video.adjustment = Some(tokio::spawn(async move {
                            for track in retired {
                                if unpublish_budgeted_video(&participant, &track, &send_budget).await.is_err()
                                    && let Ok(mut tracks) = variants.lock()
                                { tracks.push(track); }
                            }
                            None
                        }));
                        continue;
                    }
                    if current.is_none() && let Some(codec) = desired {
                        video.adjustment = Some(change_video_codec(&room, video, codec, false, variant_backoff.clone(), allow_h264.clone()));
                    }
                }
                if performance_job.as_ref().is_none_or(JoinHandle::is_finished) {
                    let tracks: Vec<_> = [camera.as_ref(), screen_share.as_ref()].into_iter().flatten().flat_map(|video| {
                        let mut tracks = vec![video.track.clone()];
                        if let Ok(variants) = video.variants.lock() { tracks.extend(variants.iter().cloned()); }
                        tracks
                    }).collect();
                    let receivers: Vec<_> = room.remote_participants().values().flat_map(|participant| {
                        participant.track_publications().into_values().filter(|publication| publication.kind() == TrackKind::Video)
                            .map(|publication| (format!("{}/{}", participant.identity(), if publication.source() == TrackSource::Screenshare { "screen_share" } else { "camera" }), publication)).collect::<Vec<_>>()
                    }).collect();
                    if let Ok(mut states) = receiver_health.lock() {
                        states.retain(|key, _| receivers.iter().any(|(active_key, _)| active_key == key));
                    }
                    let receiver_state = receiver_health.clone();
                    let publisher_state = publisher_samples.clone();
                    let publisher_history = publisher_tracks.clone();
                    let report_tx = performance_tx.clone();
                    performance_job = Some(tokio::spawn(async move {
                        use livekit::webrtc::stats::{RtcStats, QualityLimitationReason};
                        let (mut cpu, mut bandwidth, mut observed) = (false, false, false);
                        let (mut combined_load, mut incomplete) = (0.0, false);
                        let mut software_variants = Vec::new();
                        if let Ok(mut history) = publisher_history.lock() {
                            history.retain(|sid, _| tracks.iter().any(|track| track.sid().to_string() == *sid));
                        }
                        for track in tracks {
                            let sid = track.sid().to_string();
                            let variant = track.name().rsplit(':').next().is_some_and(premium_codec);
                            let expected_layers = track.sender().map(|sender| sender.parameters().encodings.into_iter().filter(|encoding| encoding.active).map(|encoding| encoding.rid).collect::<BTreeSet<_>>());
                            let mut measured_layers = BTreeSet::new();
                            if let Ok(Ok(stats)) = time::timeout(Duration::from_secs(2), track.get_stats()).await {
                                for stat in stats {
                                    if let RtcStats::OutboundRtp(stat) = stat {
                                        let key = format!("{}/{}", track.sid(), stat.rtc.id);
                                        let now = std::time::Instant::now();
                                        let previous = publisher_state.lock().ok().and_then(|mut states| states.insert(key, (stat.outbound.frames_encoded, stat.outbound.total_encode_time, now)));
                                        let Some((previous_frames, previous_time, previous_sample)) = previous else { continue; };
                                        let frames = stat.outbound.frames_encoded.saturating_sub(previous_frames);
                                        if frames == 0 { continue; }
                                        observed = true;
                                        let verified_hardware = native_video_probe::hardware_encoder(stat.outbound.power_efficient_encoder, &stat.outbound.encoder_implementation);
                                        let (published, software_samples) = publisher_history.lock().map_or((now, 0), |mut history| {
                                            let entry = history.entry(sid.clone()).or_insert((now, 0));
                                            entry.1 = if verified_hardware { 0 } else { entry.1.saturating_add(1) };
                                            *entry
                                        });
                                        let software_limit = if track.name().ends_with(":h265") { 1 } else { video_policy::SOFTWARE_SAMPLES };
                                        if variant && software_samples >= software_limit { software_variants.push(track.name()); }
                                        // Ramp-up reports "bandwidth" while the estimate grows; it is not pressure.
                                        let warmed = now.duration_since(published) >= video_policy::UPLOAD_WARMUP;
                                        let elapsed = stat.outbound.total_encode_time - previous_time;
                                        if !elapsed.is_finite() || elapsed <= 0.0 { incomplete = true; continue; }
                                        measured_layers.insert(stat.outbound.rid.clone());
                                        // Hardware encode durations overlap across independent engines;
                                        // only software work consumes this shared CPU budget.
                                        if !verified_hardware { combined_load += elapsed / now.duration_since(previous_sample).as_secs_f64().max(0.001); }
                                        cpu |= elapsed / f64::from(frames) > 0.8 / stat.outbound.frames_per_second.max(15.0);
                                        cpu |= stat.outbound.quality_limitation_reason == QualityLimitationReason::Cpu;
                                        bandwidth |= warmed && stat.outbound.quality_limitation_reason == QualityLimitationReason::Bandwidth;
                                    }
                                }
                            }
                            incomplete |= expected_layers.as_ref().is_none_or(|layers| {
                                if layers.len() == 1 { measured_layers.is_empty() } else { !layers.is_subset(&measured_layers) }
                            });
                        }
                        for (key, publication) in receivers {
                            let Some(RemoteTrack::Video(track)) = publication.track() else { continue; };
                            let mut sampled = false;
                            if let Ok(Ok(stats)) = time::timeout(Duration::from_secs(2), track.get_stats()).await {
                                for stat in stats {
                                    if let RtcStats::InboundRtp(stat) = stat {
                                        sampled = true;
                                        if let Ok(mut states) = receiver_state.lock() {
                                            let state = states.entry(key.clone()).or_default();
                                            let sid = publication.sid().to_string();
                                            if state.sid != sid {
                                                *state = ReceiverHealth { sid, health: state.health, premium_backoff: std::mem::take(&mut state.premium_backoff), ..ReceiverHealth::default() };
                                            }
                                            let codec = publication.mime_type().to_ascii_lowercase();
                                            let codec = codec.strip_prefix("video/").unwrap_or("");
                                            let watching_premium = premium_codec(codec);
                                            if codec == "h265" && stat.inbound.frames_decoded > state.frames {
                                                native_video_probe::observe_hevc_decoder(stat.inbound.power_efficient_decoder, &stat.inbound.decoder_implementation);
                                            }
                                            let software_hevc = codec == "h265" && stat.inbound.frames_decoded > state.frames
                                                && !native_video_probe::hardware_hevc_decode();
                                            if software_hevc {
                                                state.health = "decode";
                                                state.layer_trial_until = None;
                                                if !state.premium_blocked(codec, Instant::now()) {
                                                    state.fail_premium(codec, Instant::now());
                                                }
                                                publication.set_subscribed(false);
                                                // Do not re-record the same final frame as a fresh failure after unsubscribe.
                                                state.frames = stat.inbound.frames_decoded;
                                                continue;
                                            }
                                            let was_bad = matches!(state.health, "decode" | "download");
                                            match state.observe(&stat) {
                                                Some("healthy") => {
                                                    // One bad spell early in a long call must not ratchet to permanent fallback.
                                                    if watching_premium && state.healthy >= video_policy::PREMIUM_TRUST_SAMPLES {
                                                        state.premium_backoff.entries.remove(codec);
                                                    }
                                                    state.layer_trial_until = None;
                                                    publication.set_video_quality(livekit::track::VideoQuality::High);
                                                }
                                                Some(_) => {
                                                    let now = Instant::now();
                                                    let software_hevc = codec == "h265" && !native_video_probe::hardware_hevc_decode();
                                                    // Give spatial layers one adaptation interval before requesting a different encode.
                                                    if watching_premium && !was_bad && publication.simulcasted() && !software_hevc {
                                                        state.layer_trial_until = Some(now + video_policy::SAMPLE * u32::from(video_policy::MIN_CHANGE_SAMPLES));
                                                    }
                                                    if watching_premium && (software_hevc || state.needs_fallback(now)) && !state.premium_blocked(codec, now) {
                                                        state.layer_trial_until = None;
                                                        state.fail_premium(codec, now);
                                                    }
                                                    publication.set_video_quality(livekit::track::VideoQuality::Low);
                                                }
                                                None => {},
                                            }
                                        }
                                    }
                                }
                            }
                            if !sampled
                                && let Ok(mut states) = receiver_state.lock()
                                && let Some(state) = states.get_mut(&key)
                            {
                                state.unknown();
                            }
                        }
                        cpu |= combined_load > 0.8;
                        let _ = report_tx.send(PerformanceReport {
                            cpu,
                            bandwidth,
                            observed: observed && (!incomplete || cpu || bandwidth),
                            encode_load: (observed && !incomplete).then_some(combined_load),
                            software_variants,
                        }).await;
                    }));
                }

                // Hints go out when they change, on membership events, and otherwise
                // every ANNOUNCE_EVERY instead of on every two-second sample.
                let payload = native_video_capabilities(&receiver_health, encrypted, h264_ready);
                if force_announce || payload != last_announced || last_announce_at.is_none_or(|at| now.duration_since(at) >= video_policy::ANNOUNCE_EVERY) {
                    force_announce = false;
                    last_announced.clone_from(&payload);
                    last_announce_at = Some(now);
                    let local = room.local_participant();
                    let allow_h264 = allow_h264.clone();
                    tokio::spawn(async move {
                        if encrypted && h264_ready && !allow_h264.load(Ordering::Acquire) {
                            return;
                        }
                        let _ = local.publish_data(DataPacket {
                            payload, topic: Some("kaede.video.v1".to_owned()), reliable: true,
                            ..DataPacket::default()
                        }).await;
                    });
                }
            }
            _ = video_tick.tick() => {
                if capture.as_ref().is_some_and(NativeCapture::has_failed) || playback.has_failed() {
                    // Reuse the fenced restart so unplugged custom devices can
                    // fall back to the system default with current mute state.
                    restart_requested = true;
                    let _ = status.send(VoiceStatus::Reconnecting);
                    let _ = grant_stale.send(true);
                    break;
                }
                if screen_share.as_ref().is_some_and(|share| share.stop.load(Ordering::Acquire)) {
                    let message = screen_share.as_ref().and_then(|share| share.capture_failure.lock().ok()?.clone())
                        .unwrap_or_else(|| "Screen sharing ended.".to_owned());
                    let _ = stop_published_video(&room, screen_share.take()).await;
                    send_media_error(&status, &room, can_speak, can_stream, None, camera.as_ref(), message);
                }
                if camera.as_ref().is_some_and(|camera| camera.stop.load(Ordering::Acquire)) {
                    let message = camera.as_ref().and_then(|camera| camera.capture_failure.lock().ok()?.clone())
                        .unwrap_or_else(|| "Camera capture ended.".to_owned());
                    let _ = stop_published_video(&room, camera.take()).await;
                    send_media_error(&status, &room, can_speak, can_stream, screen_share.as_ref(), None, message);
                }
                reconcile_video_subscriptions(&room, &mut subscriptions, &receiver_health, video_visible, &video_peers);
            }
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
                    Some(VoiceCommand::SetVolume { identity, stream, volume }) => {
                        playback_mixer.set_volume(&identity, stream, volume);
                    }
                    Some(VoiceCommand::SetVideoVisible(visible)) => {
                        if video_visible == visible { continue; }
                        video_visible = visible;
                        reconcile_video_subscriptions(&room, &mut subscriptions, &receiver_health, video_visible, &video_peers);
                    }
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
                        let result = if enabled && camera.is_none() && camera_start.is_none() {
                            let room = &room;
                            let send_budget = send_budget.clone();
                            camera_start = Some(Box::pin(async move {
                                publish_camera(room, device_id.as_deref(), video_quality_mode, send_budget).await
                            }));
                            continue;
                        } else if !enabled {
                            camera_start = None;
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
                        let result = if enabled && screen_share.is_none() && screen_start.is_none() {
                            let room = &room;
                            let send_budget = send_budget.clone();
                            screen_start = Some(Box::pin(async move {
                                publish_screen_share(room, source_id.as_deref(), settings, send_budget).await
                            }));
                            continue;
                        } else if !enabled {
                            screen_start = None;
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
                    Some(RoomEvent::TrackSubscribed { track: RemoteTrack::Video(track), publication, participant }) => {
                        let screen_share = publication.source() == TrackSource::Screenshare;
                        let video_frames = video_frames.clone();
                        let participant = participant.identity().to_string();
                        let key = (participant.clone(), screen_share);
                        if let Some((_, reader)) = video_readers.remove(&key) { reader.abort(); }
                        let sid = publication.sid().to_string();
                        let reader = tokio::spawn(async move {
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
                                let _ = video_frames.try_send(RemoteVideoFrame { participant: participant.clone(), screen_share, width, height, rgba, removed: false });
                            }
                        });
                        video_readers.insert(key, (sid, reader));
                    }
                    Some(RoomEvent::TrackUnsubscribed { track: RemoteTrack::Video(_), publication, participant }) => {
                        let key = (participant.identity().to_string(), publication.source() == TrackSource::Screenshare);
                        if video_readers.get(&key).is_some_and(|(sid, _)| *sid == publication.sid().to_string()) {
                            if let Some((_, reader)) = video_readers.remove(&key) { reader.abort(); }
                            let _ = video_frames.try_send(RemoteVideoFrame {
                                participant: key.0, screen_share: key.1,
                                width: 0, height: 0, rgba: Vec::new(), removed: true,
                            });
                        }
                    }
                    Some(RoomEvent::DataReceived { payload, topic, kind, participant }) => {
                        let Some(participant) = participant else { continue };
                        let identity = participant.identity().to_string();
                        if topic.as_deref() == Some("kaede.video.v1") {
                            if room.remote_participants().get(&participant.identity()).is_some_and(|current| current.sid() == participant.sid())
                                && let Some(compatible) = update_peer_video_hint(&mut video_peers, identity, &payload)
                                && encrypted {
                                if !compatible { allow_h264.store(false, Ordering::Release); }
                                capability_tick.reset_immediately();
                                video_tick.reset_immediately();
                            }
                            continue;
                        }
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
                    Some(RoomEvent::ParticipantConnected(participant)) => {
                        // A new viewer needs our hints before its publishers choose codecs.
                        let identity = participant.identity().to_string();
                        video_peers.remove(&identity);
                        first_seen.insert(identity, Instant::now());
                        if encrypted {
                            allow_h264.store(false, Ordering::Release);
                            capability_tick.reset_immediately();
                            video_tick.reset_immediately();
                        }
                        force_announce = true;
                    }
                    Some(RoomEvent::ParticipantDisconnected(participant)) => {
                        let identity = participant.identity().to_string();
                        playback_mixer.remove_participant(&identity);
                        video_peers.remove(&identity);
                        first_seen.remove(&identity);
                        if encrypted {
                            capability_tick.reset_immediately();
                            video_tick.reset_immediately();
                        }
                        for screen_share in [false, true] {
                            if let Some((_, reader)) = video_readers.remove(&(identity.clone(), screen_share)) {
                                reader.abort();
                                let _ = video_frames.try_send(RemoteVideoFrame { participant: identity.clone(), screen_share, width: 0, height: 0, rgba: Vec::new(), removed: true });
                            }
                        }
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
                        video_degraded.store(false, Ordering::Release);
                        video_peers.clear();
                        allow_h264.store(!encrypted, Ordering::Release);
                        // Everyone re-announces after reconnecting: grace, not VP8-only.
                        first_seen.clear();
                        force_announce = true;
                        if let Ok(mut states) = receiver_health.lock() { states.clear(); }
                        priority_push_to_talk_active = false;
                        playback_mixer.clear_priority_active();
                        priority_speakers.send_modify(BTreeSet::clear);
                        if let Some(capture) = capture.as_ref() {
                            capture.gate.set_priority_push_to_talk(false);
                        }
                        let _ = status.send(VoiceStatus::Reconnecting);
                    }
                    Some(RoomEvent::Reconnected) => {
                        if encrypted {
                            video_peers.clear();
                            allow_h264.store(false, Ordering::Release);
                            force_announce = true;
                        }
                        subscriptions.clear();
                        capability_tick.reset_immediately();
                        reconcile_video_subscriptions(&room, &mut subscriptions, &receiver_health, video_visible, &video_peers);
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
    drop(screen_start);
    drop(camera_start);
    video_degraded.store(false, Ordering::Release);
    if let Some(job) = performance_job {
        job.abort();
    }
    for (_, reader) in video_readers.into_values() {
        reader.abort();
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

/// Shared adaptation policy. Mirrors `VIDEO_POLICY` in
/// frontend/src/lib/voice/adaptive-video.ts and `VideoPolicy` in the mobile
/// `adaptive_video.dart`; change all three together.
mod video_policy {
    use std::time::Duration;

    pub const SAMPLE: Duration = Duration::from_secs(2);
    /// Consecutive bad samples before degrading (6s).
    pub const DEGRADE_SAMPLES: u8 = 3;
    /// Consecutive healthy samples before recovering one step (30s).
    pub const RECOVER_SAMPLES: u8 = 15;
    /// Samples between two adaptation steps (20s).
    pub const MIN_CHANGE_SAMPLES: u8 = 10;
    /// Bandwidth limitation is expected while the congestion controller ramps up.
    pub const UPLOAD_WARMUP: Duration = Duration::from_secs(10);
    /// A viewer without codec hints is pending this long before it counts as VP8-only.
    pub const HINT_GRACE: Duration = Duration::from_secs(8);
    pub const ANNOUNCE_EVERY: Duration = Duration::from_secs(4);
    /// Retry delays after a failed codec attempt; afterwards the session stops retrying.
    pub const BACKOFF: [Duration; 3] = [
        Duration::from_mins(1),
        Duration::from_mins(5),
        Duration::from_mins(15),
    ];
    /// Dual-cast only when at least one in this many viewers would watch a premium codec.
    pub const DUAL_CAST_SHARE: usize = 3;
    /// AV1 software samples before retirement; HEVC is retired on its first sample.
    pub const SOFTWARE_SAMPLES: u8 = 3;
    /// Healthy premium samples (5 min) after which earlier playback failures are forgotten.
    pub const PREMIUM_TRUST_SAMPLES: u8 = 150;
}

/// Escalating retry delays for an attempt that failed (variant encode or premium playback).
#[derive(Default)]
struct VideoBackoff {
    entries: BTreeMap<String, (usize, Option<Instant>)>,
}

impl VideoBackoff {
    fn blocked(&self, key: &str, now: Instant) -> bool {
        self.entries
            .get(key)
            .is_some_and(|(_, until)| until.is_none_or(|until| until > now))
    }

    fn fail(&mut self, key: &str, now: Instant) {
        let entry = self.entries.entry(key.to_owned()).or_insert((0, None));
        entry.1 = video_policy::BACKOFF.get(entry.0).map(|delay| now + *delay);
        entry.0 = entry.0.saturating_add(1);
    }
}

/// Encode (CPU) pressure only; upload pressure is tracked by [`UploadPressure`].
#[derive(Default)]
struct VideoBudget {
    level: u8,
    bad: u8,
    healthy: u8,
    cooldown: u8,
}

impl VideoBudget {
    fn observe(&mut self, cpu: bool) -> bool {
        self.cooldown = self.cooldown.saturating_sub(1);
        if cpu {
            self.bad = self.bad.saturating_add(1);
            self.healthy = 0;
        } else {
            self.healthy = self.healthy.saturating_add(1);
            self.bad = 0;
        }
        let next = if self.bad >= video_policy::DEGRADE_SAMPLES {
            (self.level + 1).min(3)
        } else if self.healthy >= video_policy::RECOVER_SAMPLES {
            self.level.saturating_sub(1)
        } else {
            self.level
        };
        if next == self.level || self.cooldown > 0 {
            return false;
        }
        self.level = next;
        self.cooldown = video_policy::MIN_CHANGE_SAMPLES;
        self.bad = 0;
        self.healthy = 0;
        true
    }
}

/// Sustained bandwidth limitation sheds dual-cast; bitrate itself is left to
/// the congestion controller instead of being capped a second time.
#[derive(Default)]
struct UploadPressure {
    bad: u8,
    healthy: u8,
    constrained: bool,
}

impl UploadPressure {
    fn observe(&mut self, bandwidth: bool) {
        if bandwidth {
            self.bad = self.bad.saturating_add(1);
            self.healthy = 0;
        } else {
            self.healthy = self.healthy.saturating_add(1);
            self.bad = 0;
        }
        if self.bad >= video_policy::DEGRADE_SAMPLES {
            self.constrained = true;
        } else if self.healthy >= video_policy::RECOVER_SAMPLES {
            self.constrained = false;
        }
    }
}

// Preference order is also the tie-breaker: AV1 wins equal audiences.
const PREMIUM_CODECS: [VideoCodec; 2] = [VideoCodec::AV1, VideoCodec::H265];
const LEGACY_VIDEO_CODECS: [&str; 3] = ["vp8", "h264", "av1"];
const EXTENDED_VIDEO_CODECS: [&str; 2] = ["h265", "vp9"];

fn premium_codec(codec: &str) -> bool {
    PREMIUM_CODECS.iter().any(|entry| entry.as_str() == codec)
}

struct VariantContext {
    level: u8,
    upload_constrained: bool,
    cpu_spare: bool,
    cpu_recovered: bool,
    blocked: BTreeSet<String>,
}

/// One frame-rate and resolution step per budget level, shared by every
/// capture path. Integer ratios: 1/1.25 (= 4/5) per level for pixels, 0.85
/// (17/20) per level for motion frame rate, 0.65 (13/20) for detail shares.
fn adapted_dimension(value: u32, level: u32, minimum: u32) -> u32 {
    let level = level.min(3);
    let scaled = u64::from(value) * 4_u64.pow(level) / 5_u64.pow(level);
    u32::try_from(scaled)
        .unwrap_or(value)
        .max(minimum)
        .min(value.max(minimum))
}

fn adapted_frame_rate(frame_rate: u32, level: u32, detail: bool) -> u32 {
    let level = level.min(3);
    let (numerator, floor) = if detail { (13_u64, 5) } else { (17_u64, 12) };
    let scaled = u64::from(frame_rate) * numerator.pow(level) / 20_u64.pow(level);
    u32::try_from(scaled)
        .unwrap_or(frame_rate)
        .max(floor.min(frame_rate))
}

// Publishing a replacement resets stats counters; brief gaps can reuse recent CPU proof.
#[derive(Default)]
struct EncodeHeadroom {
    last_spare: Option<Instant>,
}

impl EncodeHeadroom {
    fn observe(&mut self, load: Option<f64>, overloaded: bool, now: Instant) {
        if overloaded {
            self.last_spare = None;
        } else if let Some(load) = load {
            self.last_spare = (0.0..0.5).contains(&load).then_some(now);
        }
        // Unknown samples neither create proof nor extend its lifetime.
    }

    fn has_spare(&self, now: Instant) -> bool {
        self.last_spare
            .is_some_and(|at| now.saturating_duration_since(at) < video_policy::UPLOAD_WARMUP)
    }
}

#[derive(Default)]
struct PerformanceReport {
    cpu: bool,
    bandwidth: bool,
    observed: bool,
    encode_load: Option<f64>,
    /// Variant track names whose "hardware" encoder runs in software.
    software_variants: Vec<String>,
}

/// Text shares (<= 15 fps profiles) keep pixels until the lowest budget and
/// surrender frames instead; motion shares and cameras trade pixels first.
fn adapted_screen_settings(settings: ScreenShareSettings, level: u32) -> ScreenShareSettings {
    if settings.frame_rate <= 15 {
        let halve = u32::from(level >= 3);
        return ScreenShareSettings {
            width: (settings.width >> halve).max(640),
            height: (settings.height >> halve).max(360),
            ..settings
        };
    }
    ScreenShareSettings {
        width: adapted_dimension(settings.width, level, 640),
        height: adapted_dimension(settings.height, level, 360),
        ..settings
    }
}

fn variant_key(video: &PublishedVideo, codec: &str) -> String {
    let resolution = video.track.rtc_source().video_resolution();
    format!(
        "{}/{codec}/{}x{}",
        if video.source == TrackSource::Screenshare {
            "screen_share"
        } else {
            "camera"
        },
        resolution.width,
        resolution.height
    )
}

fn preferred_native_variant(
    peers: &BTreeMap<String, VideoPeer>,
    participant_count: usize,
    key: &str,
    context: &VariantContext,
) -> Option<VideoCodec> {
    let mut selected = None;
    let mut largest_audience = 0;
    for codec in PREMIUM_CODECS {
        let name = codec.as_str();
        if context.blocked.contains(name) {
            continue;
        }
        let audience = peers
            .values()
            .filter(|peer| {
                peer.supports(name) && peer.demand.get(key).is_none_or(|demand| demand == name)
            })
            .count();
        let others = participant_count.saturating_sub(audience);
        let allowed = if others == 0 {
            context.level < 3
        } else {
            context.level == 0
                && !context.upload_constrained
                && audience * video_policy::DUAL_CAST_SHARE >= participant_count
        };
        if allowed && audience > largest_audience {
            selected = Some(codec);
            largest_audience = audience;
        }
    }
    selected
}

// A codec is not a failed attempt when viewers leave or prefer another codec.
fn pressure_retires_native_variant(
    peers: &BTreeMap<String, VideoPeer>,
    participant_count: usize,
    key: &str,
    codec: &str,
    context: &VariantContext,
) -> bool {
    if context.level == 0 && !context.upload_constrained {
        return false;
    }
    let unpressured = VariantContext {
        level: 0,
        upload_constrained: false,
        cpu_spare: context.cpu_spare,
        cpu_recovered: context.cpu_recovered,
        blocked: context.blocked.clone(),
    };
    preferred_native_variant(peers, participant_count, key, &unpressured)
        .is_some_and(|preferred| preferred.as_str() == codec)
}

fn compatible_with_every_viewer(
    peers: &BTreeMap<String, VideoPeer>,
    participant_count: usize,
    key: &str,
    codec: &str,
) -> bool {
    participant_count > 0
        && peers.len() == participant_count
        && peers.values().all(|peer| {
            peer.supports(codec)
                && peer
                    .demand
                    .get(key)
                    .is_none_or(|demand| demand == codec || premium_codec(demand))
        })
}

fn preferred_native_compatible(
    peers: &BTreeMap<String, VideoPeer>,
    participant_count: usize,
    key: &str,
    current: VideoCodec,
    context: &VariantContext,
) -> VideoCodec {
    if context.level == 0
        && context.cpu_spare
        && (current == VideoCodec::VP9 || (context.upload_constrained && context.cpu_recovered))
        && !context.blocked.contains("vp9")
        && compatible_with_every_viewer(peers, participant_count, key, "vp9")
    {
        VideoCodec::VP9
    } else if !context.blocked.contains("h264")
        && compatible_with_every_viewer(peers, participant_count, key, "h264")
    {
        VideoCodec::H264
    } else {
        VideoCodec::VP8
    }
}

fn adapted_bitrate(bitrate: u64, level: u8) -> u64 {
    // 0.72^level (shared policy) for the four adaptation levels; u128 avoids overflow.
    let numerator = [10_000_u128, 7200, 5184, 3732][usize::from(level.min(3))];
    u64::try_from(u128::from(bitrate) * numerator / 10_000)
        .unwrap_or(bitrate)
        .max(300_000)
}

#[derive(Default)]
struct VideoSendBudget {
    level: u8,
    sources: BTreeMap<String, VideoSourceBudget>,
}

struct VideoSourceBudget {
    bitrate: u64,
    tracks: BTreeMap<String, BudgetedVideoTrack>,
}

struct BudgetedVideoTrack {
    track: LocalVideoTrack,
    // Preserve the ladder before caps so retirement can restore its original proportions.
    encodings: Vec<RtpEncodingParameters>,
}

fn source_video_bitrate(profile: u64, level: u8, sources: usize, variants: usize) -> u64 {
    adapted_bitrate(profile, level)
        .min(profile)
        .min(8_000_000 / u64::try_from(sources.max(1)).unwrap_or(u64::MAX))
        / u64::try_from(variants.max(1)).unwrap_or(u64::MAX)
}

fn compatible_video_scale(width: u32, height: u32) -> f64 {
    (f64::from(width.max(height)) / 1280.0)
        .max(f64::from(width.min(height)) / 720.0)
        .max(1.0)
}

fn limit_video_encodings(
    encodings: &mut [RtpEncodingParameters],
    original: &[RtpEncodingParameters],
    bitrate: u64,
    downscale: f64,
) -> bool {
    let total: u128 = original
        .iter()
        .map(|encoding| u128::from(encoding.max_bitrate.unwrap_or(bitrate).max(1)))
        .sum();
    let mut changed = false;
    for encoding in encodings {
        // WebRTC may omit RID for a single RTP stream; multiple streams must match by RID.
        let base = if original.len() == 1 {
            original.first()
        } else {
            original.iter().find(|base| base.rid == encoding.rid)
        };
        let Some(base) = base else { continue };
        let weight = u128::from(base.max_bitrate.unwrap_or(bitrate).max(1));
        let cap = u64::try_from(u128::from(bitrate) * weight / total.max(1))
            .unwrap_or(bitrate)
            .max(1);
        let scale = if downscale > 1.0 {
            Some(base.scale_resolution_down_by.unwrap_or(1.0).max(downscale))
        } else {
            base.scale_resolution_down_by
        };
        changed |= encoding.max_bitrate != Some(cap) || encoding.scale_resolution_down_by != scale;
        encoding.max_bitrate = Some(cap);
        encoding.scale_resolution_down_by = scale;
    }
    changed
}

impl VideoSendBudget {
    fn limits(&self, source: &VideoSourceBudget, track: &BudgetedVideoTrack) -> (u64, f64) {
        let dual = source.tracks.len() > 1;
        let compatible = track
            .track
            .name()
            .rsplit(':')
            .next()
            .is_none_or(|codec| !premium_codec(codec));
        let size = track.track.rtc_source().video_resolution();
        let scale = if dual && compatible {
            compatible_video_scale(size.width, size.height)
        } else {
            1.0
        };
        (
            source_video_bitrate(
                source.bitrate,
                self.level,
                self.sources.len(),
                source.tracks.len(),
            ),
            scale,
        )
    }

    fn apply(&self, pending: Option<&str>) -> bool {
        let mut applied = true;
        for source in self.sources.values() {
            for (id, track) in &source.tracks {
                // Only the new reservation may lack a sender; reconnecting publications
                // must recover their sender before another allocation can be admitted.
                let Some(sender) = track.track.sender() else {
                    applied &= pending == Some(id.as_str());
                    continue;
                };
                let mut parameters = sender.parameters();
                if parameters.encodings.len() != track.encodings.len()
                    || (track.encodings.len() > 1
                        && parameters.encodings.iter().any(|encoding| {
                            !track.encodings.iter().any(|base| base.rid == encoding.rid)
                        }))
                {
                    applied = false;
                    continue;
                }
                let (bitrate, scale) = self.limits(source, track);
                if limit_video_encodings(
                    &mut parameters.encodings,
                    &track.encodings,
                    bitrate,
                    scale,
                ) && let Err(error) = sender.set_parameters(parameters)
                {
                    tracing::warn!(%error, "video sender upload limit could not be applied");
                    applied = false;
                }
            }
        }
        applied
    }

    fn remove(&mut self, track: &LocalVideoTrack) {
        let id = track.rtc_track().id();
        self.sources.retain(|_, source| {
            source.tracks.remove(&id);
            !source.tracks.is_empty()
        });
        self.apply(None);
    }
}

async fn publish_budgeted_video(
    participant: &LocalParticipant,
    track: &LocalVideoTrack,
    options: TrackPublishOptions,
    send_budget: &tokio::sync::Mutex<VideoSendBudget>,
    source_id: &str,
    profile_bitrate: u64,
) -> Result<(), VoiceError> {
    // Serialize reservations with publish/unpublish: an in-flight sender cannot retain a
    // larger allocation while another source or codec starts using its share.
    let mut budget = send_budget.lock().await;
    let size = track.rtc_source().video_resolution();
    let original = compute_video_encodings(size.width, size.height, &options);
    let id = track.rtc_track().id();
    let source = budget
        .sources
        .entry(source_id.to_owned())
        .or_insert_with(|| VideoSourceBudget {
            bitrate: profile_bitrate,
            tracks: BTreeMap::new(),
        });
    source.tracks.insert(
        id.clone(),
        BudgetedVideoTrack {
            track: track.clone(),
            encodings: original.clone(),
        },
    );
    if !budget.apply(Some(&id)) {
        budget.remove(track);
        return Err(VoiceError::VideoSendBudget);
    }
    let source = &budget.sources[source_id];
    let (bitrate, scale) = budget.limits(source, &source.tracks[&id]);
    let mut encodings = original.clone();
    limit_video_encodings(&mut encodings, &original, bitrate, scale);
    match participant
        .publish_track_with_video_send_encodings(
            LocalTrack::Video(track.clone()),
            options,
            Some(encodings),
        )
        .await
    {
        Ok(_) => Ok(()),
        Err(error) => {
            budget.remove(track);
            Err(error.into())
        }
    }
}

async fn unpublish_budgeted_video(
    participant: &LocalParticipant,
    track: &LocalVideoTrack,
    send_budget: &tokio::sync::Mutex<VideoSendBudget>,
) -> Result<(), VoiceError> {
    let mut budget = send_budget.lock().await;
    participant.unpublish_track(&track.sid()).await?;
    budget.remove(track);
    Ok(())
}

// The worker returns a replacement compatible track only after it is published.
// Premium tracks are kept separately, so each source has at most two publications.
#[allow(clippy::too_many_lines)]
fn change_video_codec(
    room: &Room,
    video: &PublishedVideo,
    codec: VideoCodec,
    compatible: bool,
    backoff: Arc<Mutex<VideoBackoff>>,
    allow_h264: Arc<AtomicBool>,
) -> JoinHandle<Option<(LocalVideoTrack, VideoCodec)>> {
    let participant = room.local_participant();
    let stop = video.stop.clone();
    let original = video.track.clone();
    let original_codec = video.compatible_codec;
    let restore_options = participant
        .track_publications()
        .get(&original.sid())
        .map(livekit::prelude::LocalTrackPublication::publish_options);
    let source = original.rtc_source();
    let dimensions = source.video_resolution();
    let mut encoding = video.encoding.clone();
    let level = u8::try_from(video.capture_level.load(Ordering::Acquire).min(3)).unwrap_or(3);
    encoding.max_bitrate = adapted_bitrate(encoding.max_bitrate, level);
    encoding.max_framerate = (encoding.max_framerate * 0.85_f64.powi(i32::from(level))).max(5.0);
    let key = variant_key(video, codec.as_str());
    let variants = video.variants.clone();
    let track_source = video.source;
    let send_budget = video.send_budget.clone();
    let budget_source = video.budget_source.clone();
    let profile_bitrate = video.encoding.max_bitrate;
    let encrypted = room.e2ee_manager().encryption_type() != EncryptionType::None;
    tokio::spawn(async move {
        // A viewer can arrive while probing or publishing a track.
        let allowed =
            |codec: VideoCodec| codec != VideoCodec::H264 || allow_h264.load(Ordering::Acquire);
        let fail = || {
            if let Ok(mut backoff) = backoff.lock() {
                backoff.fail(&key, Instant::now());
            }
        };
        let hardware = codec != VideoCodec::VP9 && codec != VideoCodec::VP8;
        let modes: &[Option<&str>] = if matches!(codec, VideoCodec::AV1 | VideoCodec::VP9) {
            &[Some("L3T3_KEY"), Some("L1T3"), Some("L1T1")]
        } else if codec == VideoCodec::H265 {
            &[Some("L1T3"), Some("L1T1")]
        } else {
            &[None]
        };
        let mut mode = None;
        let mut supported = codec == VideoCodec::VP8;
        if !supported {
            for candidate in modes {
                if !allowed(codec) || (!compatible && !allowed(original_codec)) {
                    return None;
                }
                if native_video_probe::codec_supported(
                    codec.as_str(),
                    dimensions.width,
                    dimensions.height,
                    encoding.clone(),
                    *candidate,
                    hardware,
                    &stop,
                )
                .await
                {
                    mode = *candidate;
                    supported = true;
                    break;
                }
                if stop.load(Ordering::Acquire) {
                    return None;
                }
            }
        }
        if !supported {
            fail();
            return None;
        }
        if !allowed(codec) || (!compatible && !allowed(original_codec)) {
            return None;
        }
        // H.264 only replaces VP8 when it brings proven acceleration.
        if codec == VideoCodec::H264
            && native_video_probe::hardware_codec(
                "vp8",
                dimensions.width,
                dimensions.height,
                encoding.clone(),
                &stop,
            )
            .await
        {
            fail();
            return None;
        }
        if stop.load(Ordering::Acquire) || !allowed(codec) {
            return None;
        }
        let source_name = if track_source == TrackSource::Screenshare {
            "screen_share"
        } else {
            "camera"
        };
        let name = format!("kaede.video.v1:{source_name}:{}", codec.as_str());
        let track = LocalVideoTrack::create_video_track(&name, source);
        let options = TrackPublishOptions {
            source: track_source,
            video_codec: codec,
            video_encoder: if hardware {
                livekit::webrtc::rtp_sender::VideoEncoderBackend::Hardware
            } else {
                livekit::webrtc::rtp_sender::VideoEncoderBackend::Auto
            },
            simulcast: compatible && codec != VideoCodec::VP9,
            scalability_mode: mode.map(str::to_owned),
            ..video_publish_options(encrypted, encoding.clone())
        };
        if compatible {
            if participant
                .track_publications()
                .contains_key(&original.sid())
                && unpublish_budgeted_video(&participant, &original, &send_budget)
                    .await
                    .is_err()
            {
                return None;
            }
        } else {
            let retired = variants
                .lock()
                .map(|mut tracks| std::mem::take(&mut *tracks))
                .unwrap_or_default();
            for retired in retired {
                if unpublish_budgeted_video(&participant, &retired, &send_budget)
                    .await
                    .is_err()
                {
                    if let Ok(mut variants) = variants.lock() {
                        variants.push(retired);
                    }
                    return None;
                }
            }
        }
        let published = allowed(codec)
            && publish_budgeted_video(
                &participant,
                &track,
                options,
                &send_budget,
                &budget_source,
                profile_bitrate,
            )
            .await
            .is_ok();
        if published && allowed(codec) {
            if compatible {
                Some((track, codec))
            } else {
                if let Ok(mut variants) = variants.lock() {
                    variants.push(track);
                }
                None
            }
        } else {
            if published
                && unpublish_budgeted_video(&participant, &track, &send_budget)
                    .await
                    .is_err()
            {
                // The incompatible H.264 track still exists; let the next tick retry its replacement.
                return Some((track, codec));
            }
            if allowed(codec) {
                fail();
            }
            if compatible && allowed(original_codec) {
                // Restore the previous compatible publication after a failed replacement.
                let restore = restore_options.unwrap_or_else(|| TrackPublishOptions {
                    source: track_source,
                    video_codec: original_codec,
                    ..video_publish_options(encrypted, encoding)
                });
                let restored = publish_budgeted_video(
                    &participant,
                    &original,
                    restore,
                    &send_budget,
                    &budget_source,
                    profile_bitrate,
                )
                .await
                .is_ok();
                if restored && !allowed(original_codec) {
                    let _ = unpublish_budgeted_video(&participant, &original, &send_budget).await;
                }
                Some((original, original_codec))
            } else {
                None
            }
        }
    })
}

#[derive(Deserialize)]
struct VideoPeer {
    v: u8,
    codecs: Vec<String>,
    #[serde(default, rename = "extendedCodecs")]
    extended_codecs: Vec<String>,
    #[serde(default, rename = "encryptedH264")]
    encrypted_h264: serde_json::Value,
    #[serde(skip, default = "Instant::now")]
    received_at: Instant,
    #[serde(default)]
    demand: BTreeMap<String, String>,
    #[serde(default)]
    health: BTreeMap<String, String>,
}

impl VideoPeer {
    fn supports_encrypted_h264(&self) -> bool {
        self.encrypted_h264.as_u64() == Some(2)
    }

    fn current_encrypted_h264(&self, now: Instant) -> bool {
        self.supports_encrypted_h264()
            && now.saturating_duration_since(self.received_at) <= Duration::from_secs(25)
    }

    fn supports(&self, codec: &str) -> bool {
        self.codecs.iter().any(|entry| entry == codec)
            || (EXTENDED_VIDEO_CODECS.contains(&codec)
                && self.extended_codecs.iter().any(|entry| entry == codec))
    }

    fn valid(&self) -> bool {
        self.v == 1
            && (!self.codecs.is_empty() || self.supports_encrypted_h264())
            && self.codecs.len() <= 3
            && self
                .codecs
                .iter()
                .all(|codec| LEGACY_VIDEO_CODECS.contains(&codec.as_str()))
            && self.extended_codecs.len() <= 8
            && self.extended_codecs.iter().all(|codec| codec.len() <= 16)
            && self.demand.len() <= 64
            && self.demand.iter().all(|(key, codec)| {
                key.len() <= 256 && LEGACY_VIDEO_CODECS.contains(&codec.as_str())
            })
            && self
                .health
                .values()
                .all(|value| matches!(value.as_str(), "healthy" | "download" | "decode"))
    }
}

// Only a current valid announcement authorizes the H.264 framing change.
fn update_peer_video_hint(
    peers: &mut BTreeMap<String, VideoPeer>,
    identity: String,
    payload: &[u8],
) -> Option<bool> {
    let previous = peers
        .get(&identity)
        .is_some_and(|peer| peer.current_encrypted_h264(Instant::now()));
    let compatible = if payload.len() <= 8192
        && let Ok(peer) = serde_json::from_slice::<VideoPeer>(payload)
        && peer.valid()
    {
        let compatible = peer.supports_encrypted_h264();
        peers.insert(identity, peer);
        compatible
    } else {
        if let Some(peer) = peers.get_mut(&identity) {
            peer.encrypted_h264 = serde_json::Value::Null;
        }
        false
    };
    (previous != compatible).then_some(compatible)
}

// The marker describes framing support even when H.264 is withheld from the codec list.
fn room_allows_h264<'a>(
    encrypted: bool,
    current_viewers: impl Iterator<Item = Option<&'a VideoPeer>>,
) -> bool {
    let mut viewers = current_viewers.peekable();
    let now = Instant::now();
    !encrypted
        || (viewers.peek().is_some()
            && viewers.all(|peer| peer.is_some_and(|peer| peer.current_encrypted_h264(now))))
}

#[derive(Default)]
struct ReceiverHealth {
    sid: String,
    frames: u32,
    frames_received: u64,
    initialized: bool,
    decode_time: f64,
    lost: i64,
    received: u64,
    bad: u8,
    healthy: u8,
    health: &'static str,
    /// Per-codec playback failures survive switching publications.
    premium_backoff: VideoBackoff,
    layer_trial_until: Option<Instant>,
}

impl ReceiverHealth {
    fn fail_premium(&mut self, codec: &str, now: Instant) {
        self.premium_backoff.fail(codec, now);
    }

    fn premium_blocked(&self, codec: &str, now: Instant) -> bool {
        self.premium_backoff.blocked(codec, now)
    }

    fn needs_fallback(&self, now: Instant) -> bool {
        matches!(self.health, "decode" | "download")
            && self.layer_trial_until.is_none_or(|until| now >= until)
    }

    fn unknown(&mut self) {
        self.bad = 0;
        self.healthy = 0;
        self.initialized = false;
    }

    fn observe(&mut self, stat: &livekit::webrtc::stats::InboundRtpStats) -> Option<&'static str> {
        let frames = stat.inbound.frames_decoded.saturating_sub(self.frames);
        let complete = stat
            .inbound
            .frames_received
            .saturating_sub(self.frames_received);
        let decode_time = stat.inbound.total_decode_time - self.decode_time;
        let lost = stat.received.packets_lost.saturating_sub(self.lost);
        let received = stat.received.packets_received.saturating_sub(self.received);
        let download = lost > 0 && u128::from(lost.unsigned_abs()) * 19 > u128::from(received);
        let stalled = frames == 0 && complete > 0;
        let known_decode = frames > 0 && decode_time.is_finite() && decode_time > 0.0;
        let decode = stalled
            || (known_decode
                && decode_time / f64::from(frames)
                    > 0.8 / stat.inbound.frames_per_second.max(15.0));
        let known = self.initialized && (download || stalled || known_decode);
        self.frames = stat.inbound.frames_decoded;
        self.frames_received = stat.inbound.frames_received;
        self.decode_time = stat.inbound.total_decode_time;
        self.lost = stat.received.packets_lost;
        self.received = stat.received.packets_received;
        self.initialized = true;
        if !known {
            self.bad = 0;
            self.healthy = 0;
            return None;
        }
        self.bad = if decode || download {
            self.bad.saturating_add(1)
        } else {
            0
        };
        self.healthy = if decode || download {
            0
        } else {
            self.healthy.saturating_add(1)
        };
        if self.bad >= 3 {
            self.health = if download { "download" } else { "decode" };
        } else if self.healthy >= video_policy::RECOVER_SAMPLES {
            self.health = "healthy";
        } else {
            return None;
        }
        Some(self.health)
    }
}

fn native_video_capabilities(
    receiver_health: &Mutex<BTreeMap<String, ReceiverHealth>>,
    encrypted: bool,
    allow_h264: bool,
) -> Vec<u8> {
    let capabilities = livekit::rtc_engine::lk_runtime::LkRuntime::instance()
        .pc_factory()
        .get_rtp_receiver_capabilities(livekit::webrtc::MediaType::Video);
    let codecs: Vec<_> = LEGACY_VIDEO_CODECS
        .into_iter()
        .filter(|codec| *codec != "h264" || allow_h264)
        .filter(|codec| {
            capabilities.codecs.iter().any(|entry| {
                entry
                    .mime_type
                    .eq_ignore_ascii_case(&format!("video/{codec}"))
            })
        })
        .collect();
    let extended_codecs: Vec<_> = EXTENDED_VIDEO_CODECS
        .into_iter()
        .filter(|codec| {
            (codec != &"h265" || native_video_probe::hardware_hevc_decode())
                && capabilities.codecs.iter().any(|entry| {
                    entry
                        .mime_type
                        .eq_ignore_ascii_case(&format!("video/{codec}"))
                })
        })
        .collect();
    let mut health = BTreeMap::new();
    let mut demand = BTreeMap::new();
    let now = Instant::now();
    if let Ok(states) = receiver_health.lock() {
        for (key, state) in states.iter() {
            if !state.health.is_empty() {
                health.insert(key.clone(), state.health);
            }
            if state.needs_fallback(now)
                || PREMIUM_CODECS
                    .iter()
                    .any(|codec| state.premium_blocked(codec.as_str(), now))
            {
                demand.insert(key.clone(), "vp8");
            }
        }
    }
    encode_video_hints(&codecs, &extended_codecs, &demand, &health, encrypted)
}

fn encode_video_hints(
    codecs: &[&str],
    extended: &[&str],
    demand: &BTreeMap<String, &str>,
    health: &BTreeMap<String, &str>,
    encrypted: bool,
) -> Vec<u8> {
    let mut hints = serde_json::json!({"v": 1, "encryptedH264": 2, "codecs": codecs, "extendedCodecs": extended, "demand": demand, "health": health});
    let encoded = hints.to_string().into_bytes();
    if encrypted
        && (encoded.len() > 8192 || demand.len() > 64 || demand.keys().any(|key| key.len() > 256))
    {
        hints["demand"] = serde_json::json!({});
        hints["health"] = serde_json::json!({});
        return hints.to_string().into_bytes();
    }
    encoded
}

// Select one publication per logical source. Audio is explicitly retained when
// automatic subscriptions are disabled, including independent share audio.
fn reconcile_video_subscriptions(
    room: &Room,
    subscribed: &mut BTreeSet<String>,
    receiver_health: &Mutex<BTreeMap<String, ReceiverHealth>>,
    video_visible: bool,
    peers: &BTreeMap<String, VideoPeer>,
) {
    let encrypted = room.e2ee_manager().encryption_type() != EncryptionType::None;
    let capabilities = livekit::rtc_engine::lk_runtime::LkRuntime::instance()
        .pc_factory()
        .get_rtp_receiver_capabilities(livekit::webrtc::MediaType::Video);
    let mut next = BTreeSet::new();
    let participants = room.remote_participants();
    let allow_h264 = room_allows_h264(
        encrypted,
        participants.keys().map(|id| peers.get(&id.to_string())),
    );
    for participant in participants.values() {
        let publications = participant.track_publications();
        let mut selected = BTreeMap::new();
        for publication in publications.values() {
            if publication.kind() == TrackKind::Audio {
                next.insert(publication.sid().to_string());
                continue;
            }
            if !video_visible {
                continue;
            }
            let mut mime = publication.mime_type().to_ascii_lowercase();
            let name = publication.name();
            if !allow_h264 {
                let named_codec = name
                    .strip_prefix("kaede.video.v1:")
                    .and_then(|name| name.split_once(':'))
                    .filter(|(source, codec)| {
                        matches!(*source, "camera" | "screen_share")
                            && (LEGACY_VIDEO_CODECS.contains(codec)
                                || EXTENDED_VIDEO_CODECS.contains(codec))
                    })
                    .map(|(_, codec)| codec);
                if mime == "video/h264" || named_codec == Some("h264") {
                    continue;
                }
                if mime.is_empty() {
                    let Some(codec) = named_codec else { continue };
                    mime = format!("video/{codec}");
                }
            }
            if mime == "video/h265" && !native_video_probe::hardware_hevc_decode() {
                continue;
            }
            if !mime.is_empty()
                && !capabilities
                    .codecs
                    .iter()
                    .any(|codec| codec.mime_type.eq_ignore_ascii_case(&mime))
            {
                continue;
            }
            let source = format!("{:?}", publication.source());
            // Software AV1 decoding is allowed; MIME support never authorizes
            // hardware AV1 publishing. Prefer AV1 only over an equivalent source.
            let key = format!(
                "{}/{}",
                participant.identity(),
                if publication.source() == TrackSource::Screenshare {
                    "screen_share"
                } else {
                    "camera"
                }
            );
            let decode_overload = receiver_health
                .lock()
                .ok()
                .and_then(|states| {
                    states.get(&key).map(|state| {
                        state.needs_fallback(Instant::now())
                            || state.premium_blocked(
                                mime.strip_prefix("video/").unwrap_or(""),
                                Instant::now(),
                            )
                    })
                })
                .unwrap_or(false);
            let rank = (
                if premium_codec(mime.strip_prefix("video/").unwrap_or("")) {
                    if decode_overload { 0 } else { 4 }
                } else if mime == "video/vp9" {
                    3
                } else if mime == "video/h264" && name.starts_with("kaede.video.v1:") {
                    2
                } else {
                    1
                },
                publication.sid().to_string(),
            );
            let entry = selected.entry(source).or_insert_with(|| rank.clone());
            if rank > *entry {
                *entry = rank;
            }
        }
        next.extend(selected.into_values().map(|(_, sid)| sid));
        for publication in publications.values() {
            let sid = publication.sid().to_string();
            if next.contains(&sid) != subscribed.contains(&sid) {
                publication.set_subscribed(next.contains(&sid));
            }
        }
    }
    *subscribed = next;
}

// Dropping a pending chooser/camera future must also cancel its capture thread.
type PendingVideo<'a> = std::pin::Pin<
    Box<dyn std::future::Future<Output = Result<PublishedVideo, VoiceError>> + Send + 'a>,
>;

struct CaptureStartupGuard {
    send_budget: Arc<tokio::sync::Mutex<VideoSendBudget>>,
    stop: Option<Arc<AtomicBool>>,
    participant: LocalParticipant,
    track: LocalVideoTrack,
}

impl Drop for CaptureStartupGuard {
    fn drop(&mut self) {
        if let Some(stop) = self.stop.take() {
            stop.store(true, Ordering::Release);
            let participant = self.participant.clone();
            let track = self.track.clone();
            let send_budget = self.send_budget.clone();
            tokio::spawn(async move {
                if unpublish_budgeted_video(&participant, &track, &send_budget)
                    .await
                    .is_err()
                {
                    // Capture was stopped above, including cancellation during publication.
                    send_budget.lock().await.remove(&track);
                }
            });
        }
    }
}

struct PublishedVideo {
    send_budget: Arc<tokio::sync::Mutex<VideoSendBudget>>,
    budget_source: String,
    audio: Option<screen_audio::PublishedAudio>,
    audio_capture_thread: Option<thread::JoinHandle<()>>,
    capture_failure: screen_audio::CaptureFailure,
    track: LocalVideoTrack,
    source: TrackSource,
    encoding: VideoEncoding,
    adjustment: Option<JoinHandle<Option<(LocalVideoTrack, VideoCodec)>>>,
    capture_level: Arc<AtomicU32>,
    variants: Arc<Mutex<Vec<LocalVideoTrack>>>,
    compatible_codec: VideoCodec,
    h264_withdrawn: bool,
    stop: Arc<AtomicBool>,
    capture_thread: Option<thread::JoinHandle<()>>,
}

// The pinned native SDK reports codec MIME support and backend availability,
// not hardware support for a codec at the requested dimensions/framerate.
// Unknown premium acceleration must therefore use the interoperable single encode.
// Every room starts with VP8 while the adaptive controller gathers codec and framing support.
fn video_publish_options(_encrypted: bool, encoding: VideoEncoding) -> TrackPublishOptions {
    TrackPublishOptions {
        video_encoding: Some(encoding),
        video_codec: VideoCodec::VP8,
        backup_codec: None,
        ..Default::default()
    }
}

#[allow(clippy::too_many_lines)] // Keep capture startup and rollback in one transaction.
async fn publish_screen_share(
    room: &Room,
    source_id: Option<&str>,
    settings: ScreenShareSettings,
    send_budget: Arc<tokio::sync::Mutex<VideoSendBudget>>,
) -> Result<PublishedVideo, VoiceError> {
    let settings = ScreenShareSettings {
        width: settings.width.clamp(640, 3840),
        height: settings.height.clamp(360, 2160),
        frame_rate: settings.frame_rate.clamp(5, 60),
        max_bitrate: settings.max_bitrate.clamp(300_000, 12_000_000),
        ..settings
    };
    let source = NativeVideoSource::new(
        VideoResolution {
            width: settings.width,
            height: settings.height,
        },
        true,
    );
    let track = LocalVideoTrack::create_video_track(
        "kaede.video.v1:screen_share:vp8",
        RtcVideoSource::Native(source.clone()),
    );
    let stop = Arc::new(AtomicBool::new(false));
    let budget_source = track.rtc_track().id();
    let mut startup_guard = CaptureStartupGuard {
        send_budget: send_budget.clone(),
        stop: Some(stop.clone()),
        participant: room.local_participant(),
        track: track.clone(),
    };
    let failure = Arc::new(Mutex::new(None));
    let thread_failure = failure.clone();
    let (audio_sender, audio_receiver) = mpsc::channel(10);
    let thread_audio = audio_sender.clone();
    let capture_level = Arc::new(AtomicU32::new(0));
    let thread_level = capture_level.clone();
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
                    thread_level,
                    source_id.as_deref(),
                    settings,
                    ready_tx,
                    thread_audio,
                    thread_failure,
                );
            }
        })
        .map_err(VoiceError::CaptureThread)?;
    match time::timeout(Duration::from_mins(2), ready_rx).await {
        Ok(Ok(Ok(()))) => {}
        Ok(Ok(Err(error))) => {
            stop.store(true, Ordering::Release);
            let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
            return Err(if settings.share_audio {
                VoiceError::ScreenAudio(error)
            } else {
                VoiceError::ScreenWorker(error)
            });
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
    #[cfg(not(target_os = "macos"))]
    let audio_capture_thread = if settings.share_audio {
        match screen_audio::start_capture(
            source_id,
            settings,
            audio_sender,
            stop.clone(),
            failure.clone(),
        )
        .await
        {
            Ok(worker) => Some(worker),
            Err(error) => {
                stop.store(true, Ordering::Release);
                let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
                return Err(error);
            }
        }
    } else {
        None
    };
    #[cfg(target_os = "macos")]
    let audio_capture_thread: Option<thread::JoinHandle<()>> = None;
    if let Err(error) = publish_budgeted_video(
        &room.local_participant(),
        &track,
        TrackPublishOptions {
            source: TrackSource::Screenshare,
            ..video_publish_options(
                room.e2ee_manager().encryption_type() != EncryptionType::None,
                VideoEncoding {
                    max_bitrate: settings.max_bitrate,
                    max_framerate: f64::from(settings.frame_rate),
                },
            )
        },
        &send_budget,
        &budget_source,
        settings.max_bitrate,
    )
    .await
    {
        stop.store(true, Ordering::Release);
        let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
        if let Some(worker) = audio_capture_thread {
            let _ = tokio::task::spawn_blocking(move || worker.join()).await;
        }
        return Err(error);
    }
    let audio = if settings.share_audio {
        match screen_audio::publish(room, audio_receiver, stop.clone(), failure.clone()).await {
            Ok(audio) => Some(audio),
            Err(error) => {
                stop.store(true, Ordering::Release);
                let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
                if let Some(worker) = audio_capture_thread {
                    let _ = tokio::task::spawn_blocking(move || worker.join()).await;
                }
                let _ =
                    unpublish_budgeted_video(&room.local_participant(), &track, &send_budget).await;
                return Err(error);
            }
        }
    } else {
        None
    };
    let video = PublishedVideo {
        send_budget,
        budget_source,
        audio,
        audio_capture_thread,
        capture_failure: failure,
        source: TrackSource::Screenshare,
        encoding: VideoEncoding {
            max_bitrate: settings.max_bitrate,
            max_framerate: f64::from(settings.frame_rate),
        },
        adjustment: None,
        capture_level,
        variants: Arc::new(Mutex::new(Vec::new())),
        compatible_codec: VideoCodec::VP8,
        h264_withdrawn: false,
        track,
        stop,
        capture_thread: Some(capture_thread),
    };
    startup_guard.stop = None;
    Ok(video)
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CameraDevice {
    pub id: String,
    pub label: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub aliases: Vec<String>,
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
        Vec::new()
    }
    #[cfg(not(target_os = "macos"))]
    {
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
        result.sort_by_key(|device| device.label.to_lowercase());
        result
    }
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
    Ok(camera_sources()?
        .into_iter()
        .map(|camera| camera.device)
        .collect())
}

fn camera_sources() -> Result<Vec<camera_catalog::CameraSource>, VoiceError> {
    let native = nokhwa::query(nokhwa::utils::ApiBackend::Auto)
        .map(|cameras| {
            cameras
                .into_iter()
                .map(|camera| {
                    (
                        CameraDevice {
                            id: camera.index().as_string(),
                            label: camera.human_name(),
                            aliases: Vec::new(),
                        },
                        {
                            #[cfg(target_os = "windows")]
                            {
                                camera.misc()
                            }
                            #[cfg(not(target_os = "windows"))]
                            {
                                String::new()
                            }
                        },
                    )
                })
                .collect()
        })
        .map_err(|error| error.to_string());
    #[cfg(target_os = "windows")]
    let directshow = kaede_capture::directshow::devices().map(|devices| {
        devices
            .into_iter()
            .map(|device| {
                (
                    CameraDevice {
                        id: format!("dshow:{}", device.id),
                        label: device.label,
                        aliases: Vec::new(),
                    },
                    if device.device_path.is_empty() {
                        device.id
                    } else {
                        device.device_path
                    },
                )
            })
            .collect()
    });
    #[cfg(not(target_os = "windows"))]
    let directshow = Ok(Vec::new());
    if let Err(error) = &native {
        tracing::warn!(%error, "native camera discovery failed");
    }
    if let Err(error) = &directshow {
        tracing::warn!(%error, "DirectShow camera discovery failed");
    }
    camera_catalog::combine(native, directshow).map_err(VoiceError::CameraWorker)
}

async fn publish_camera(
    room: &Room,
    device_id: Option<&str>,
    video_quality_mode: u8,
    send_budget: Arc<tokio::sync::Mutex<VideoSendBudget>>,
) -> Result<PublishedVideo, VoiceError> {
    let settings = camera_settings(video_quality_mode);
    let encoding = VideoEncoding {
        max_bitrate: settings.max_bitrate,
        max_framerate: f64::from(settings.frame_rate),
    };
    let camera_id = device_id.map(str::to_owned);
    let source = NativeVideoSource::new(
        VideoResolution {
            width: settings.width,
            height: settings.height,
        },
        false,
    );
    let track = LocalVideoTrack::create_video_track(
        "kaede.video.v1:camera:vp8",
        RtcVideoSource::Native(source.clone()),
    );
    let stop = Arc::new(AtomicBool::new(false));
    let budget_source = track.rtc_track().id();
    let mut startup_guard = CaptureStartupGuard {
        send_budget: send_budget.clone(),
        stop: Some(stop.clone()),
        participant: room.local_participant(),
        track: track.clone(),
    };
    let capture_level = Arc::new(AtomicU32::new(0));
    let thread_level = capture_level.clone();
    let thread_stop = stop.clone();
    let failure = Arc::new(Mutex::new(None));
    let thread_failure = failure.clone();
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    let capture_thread = thread::Builder::new()
        .name("kaede-camera-capture".to_owned())
        .spawn(move || {
            run_camera_capture(
                source,
                thread_stop,
                thread_level,
                camera_id,
                settings,
                ready_tx,
                thread_failure,
            );
        })
        .map_err(VoiceError::CaptureThread)?;
    match ready_rx.await {
        Ok(Ok(())) => {}
        Ok(Err(error)) => {
            stop.store(true, Ordering::Release);
            let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
            return Err(error);
        }
        Err(error) => {
            stop.store(true, Ordering::Release);
            let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
            return Err(VoiceError::CameraWorker(error.to_string()));
        }
    }
    if let Err(error) = publish_budgeted_video(
        &room.local_participant(),
        &track,
        TrackPublishOptions {
            source: TrackSource::Camera,
            ..video_publish_options(
                room.e2ee_manager().encryption_type() != EncryptionType::None,
                encoding.clone(),
            )
        },
        &send_budget,
        &budget_source,
        settings.max_bitrate,
    )
    .await
    {
        stop.store(true, Ordering::Release);
        let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
        return Err(error);
    }
    let video = PublishedVideo {
        send_budget,
        budget_source,
        audio: None,
        audio_capture_thread: None,
        capture_failure: failure,
        source: TrackSource::Camera,
        encoding,
        adjustment: None,
        capture_level,
        variants: Arc::new(Mutex::new(Vec::new())),
        compatible_codec: VideoCodec::VP8,
        h264_withdrawn: false,
        track,
        stop,
        capture_thread: Some(capture_thread),
    };
    startup_guard.stop = None;
    Ok(video)
}

fn select_camera<'a>(
    cameras: &'a [CameraDevice],
    preferred: Option<&str>,
) -> Option<&'a CameraDevice> {
    cameras
        .iter()
        .find(|camera| {
            Some(camera.id.as_str()) == preferred
                || camera
                    .aliases
                    .iter()
                    .any(|id| Some(id.as_str()) == preferred)
        })
        .or_else(|| cameras.first())
}

fn start_camera_with_fallback<T>(
    settings: CameraSettings,
    mut start: impl FnMut(RequestedFormat<'_>) -> Result<T, nokhwa::NokhwaError>,
) -> Result<T, nokhwa::NokhwaError> {
    let preferred =
        RequestedFormat::new::<RgbFormat>(RequestedFormatType::Closest(CameraFormat::new_from(
            settings.width,
            settings.height,
            FrameFormat::MJPEG,
            settings.frame_rate,
        )));
    start(preferred).or_else(|error| {
        tracing::warn!(%error, "preferred camera format failed to start; retrying a supported format");
        start(RequestedFormat::new::<RgbFormat>(RequestedFormatType::None))
    })
}

fn open_camera(selected: &CameraDevice, settings: CameraSettings) -> Result<Camera, VoiceError> {
    let index = selected.id.parse::<u32>().map_or_else(
        |_| CameraIndex::String(selected.id.clone()),
        CameraIndex::Index,
    );
    tracing::debug!(camera_id = %selected.id, camera_name = %selected.label, "starting camera");
    Ok(start_camera_with_fallback(settings, |format| {
        let mut camera = Camera::new(index.clone(), format)?;
        // Include stream startup in the retry, dropping a failed camera before
        // reopening it. A listed format is not necessarily usable by the driver.
        camera.open_stream()?;
        Ok(camera)
    })?)
}

#[allow(clippy::needless_pass_by_value, clippy::too_many_lines)]
fn run_camera_capture(
    source: NativeVideoSource,
    stop: Arc<AtomicBool>,
    capture_level: Arc<AtomicU32>,
    device_id: Option<String>,
    settings: CameraSettings,
    ready: tokio::sync::oneshot::Sender<Result<(), VoiceError>>,
    failure: screen_audio::CaptureFailure,
) {
    let cameras = match camera_sources() {
        Ok(cameras) => cameras,
        Err(error) => {
            let _ = ready.send(Err(error));
            return;
        }
    };
    let devices = cameras
        .iter()
        .map(|camera| camera.device.clone())
        .collect::<Vec<_>>();
    let Some(selected) = select_camera(&devices, device_id.as_deref()) else {
        let _ = ready.send(Err(VoiceError::NoCamera));
        return;
    };
    let selected = cameras
        .iter()
        .find(|camera| camera.device.id == selected.id);
    let Some(selected) = selected else {
        return;
    };
    let native = if selected.native {
        open_camera(&selected.device, settings)
    } else {
        Err(VoiceError::NoCamera)
    };
    #[cfg(target_os = "windows")]
    if native.is_err()
        && let Some(id) = selected.directshow.as_deref()
    {
        if let Err(error) = &native {
            tracing::debug!(%error, "using DirectShow camera capture");
        }
        let ready = Mutex::new(Some(ready));
        kaede_capture::directshow::run(
            id,
            kaede_capture::system_audio::CaptureOptions {
                window: 0,
                process: 0,
                width: settings.width,
                height: settings.height,
                fps: settings.frame_rate,
                audio: false,
            },
            &|| stop.load(Ordering::Acquire),
            &|frame| publish_camera_frame(&source, &capture_level, settings, frame),
            &|status| {
                if let Ok(mut pending) = ready.lock()
                    && let Some(sender) = pending.take()
                {
                    let _ = sender.send(status.map_err(VoiceError::CameraWorker));
                } else if let Err(message) = status {
                    screen_audio::fail(&stop, &failure, message);
                }
            },
        );
        return;
    }
    #[cfg(not(target_os = "windows"))]
    let _ = (failure, &selected.directshow);
    let mut camera = match native {
        Ok(camera) => camera,
        Err(error) => {
            let _ = ready.send(Err(error));
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
                publish_camera_frame(
                    &source,
                    &capture_level,
                    settings,
                    PackedFrame {
                        width,
                        height,
                        stride: stride_width.saturating_mul(3),
                        format: PackedPixelFormat::Rgb,
                        data: &rgb,
                    },
                );
            }
            Err(error) => {
                tracing::warn!(%error, "camera frame capture failed");
                thread::sleep(Duration::from_millis(100));
            }
        }
    }
}

fn publish_camera_frame(
    source: &NativeVideoSource,
    capture_level: &AtomicU32,
    settings: CameraSettings,
    frame: PackedFrame<'_>,
) {
    let Some(converted) = frame.to_i420() else {
        return;
    };
    let mut buffer = I420Buffer::new(converted.width, converted.height);
    let (y, u, v) = buffer.data_mut();
    y.copy_from_slice(&converted.y);
    u.copy_from_slice(&converted.u);
    v.copy_from_slice(&converted.v);
    let level = capture_level.load(Ordering::Acquire);
    let (width, height) = bounded_dimensions(
        converted.width,
        converted.height,
        adapted_dimension(settings.width, level, 320),
        adapted_dimension(settings.height, level, 180),
    );
    let buffer = if width != converted.width || height != converted.height {
        let (Ok(width), Ok(height)) = (i32::try_from(width), i32::try_from(height)) else {
            return;
        };
        buffer.scale(width, height)
    } else {
        buffer
    };
    source.capture_frame(&VideoFrame::new(VideoRotation::VideoRotation0, buffer));
}

async fn stop_published_video(
    room: &Room,
    publication: Option<PublishedVideo>,
) -> Result<(), VoiceError> {
    let Some(mut publication) = publication else {
        return Ok(());
    };
    publication.stop.store(true, Ordering::Release);
    if let Some(audio) = publication.audio.take() {
        audio.publisher.abort();
        let _ = audio.publisher.await;
        let _ = room
            .local_participant()
            .unpublish_track(&audio.track.sid())
            .await;
    }
    if let Some(worker) = publication.audio_capture_thread.take() {
        let _ = tokio::task::spawn_blocking(move || worker.join()).await;
    }
    if let Some(adjustment) = publication.adjustment.take()
        && let Ok(Some((track, codec))) = adjustment.await
    {
        publication.track = track;
        publication.compatible_codec = codec;
    }
    let variants = publication
        .variants
        .lock()
        .map(|mut tracks| std::mem::take(&mut *tracks))
        .unwrap_or_default();
    for variant in variants {
        let _ = unpublish_budgeted_video(
            &room.local_participant(),
            &variant,
            &publication.send_budget,
        )
        .await;
    }
    publication.stop.store(true, Ordering::Release);
    if let Some(capture_thread) = publication.capture_thread.take() {
        let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
    }
    let result = unpublish_budgeted_video(
        &room.local_participant(),
        &publication.track,
        &publication.send_budget,
    )
    .await;
    let mut budget = publication.send_budget.lock().await;
    budget.sources.remove(&publication.budget_source);
    budget.apply(None);
    result
}

#[allow(clippy::needless_pass_by_value, clippy::too_many_arguments)]
fn run_screen_capture(
    source: NativeVideoSource,
    stop: Arc<AtomicBool>,
    capture_level: Arc<AtomicU32>,
    requested: Option<&str>,
    settings: ScreenShareSettings,
    ready: tokio::sync::oneshot::Sender<Result<(), String>>,
    audio: mpsc::Sender<Vec<i16>>,
    failure: screen_audio::CaptureFailure,
) {
    #[cfg(target_os = "macos")]
    if settings.share_audio {
        run_macos_share(source, stop, capture_level, settings, ready, audio, failure);
        return;
    }
    let _ = audio;
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
    let frame_level = capture_level.clone();
    let frame_stop = stop.clone();
    capturer.start_capture(selected, move |result| match result {
        Ok(frame) => {
            let level = frame_level.load(Ordering::Acquire).min(3);
            let adapted = adapted_screen_settings(settings, level);
            let Some(buffer) = prepare_screen_frame(&frame, adapted) else {
                return;
            };
            source.capture_frame(&VideoFrame::new(VideoRotation::VideoRotation0, buffer));
            if let Some(ready) = ready.take() {
                let _ = ready.send(Ok(()));
            }
        }
        Err(error) => {
            if error == CaptureError::Permanent {
                let message = "Screen capture permission was denied or the selected source closed."
                    .to_owned();
                if let Some(ready) = ready.take() {
                    let _ = ready.send(Err(message.clone()));
                }
                screen_audio::fail(&frame_stop, &failure, message);
            }
            tracing::warn!(?error, "screen capture frame failed");
        }
    });

    while !stop.load(Ordering::Acquire) {
        capturer.capture_frame();
        thread::sleep(Duration::from_millis(
            1000_u64
                / u64::from(
                    adapted_frame_rate(
                        settings.frame_rate,
                        capture_level.load(Ordering::Acquire),
                        settings.frame_rate <= 15,
                    )
                    .max(1),
                ),
        ));
    }
}

#[cfg(target_os = "macos")]
#[allow(clippy::too_many_arguments, clippy::needless_pass_by_value)]
fn run_macos_share(
    source: NativeVideoSource,
    stop: Arc<AtomicBool>,
    level: Arc<AtomicU32>,
    settings: ScreenShareSettings,
    ready: tokio::sync::oneshot::Sender<Result<(), String>>,
    audio: mpsc::Sender<Vec<i16>>,
    failure: screen_audio::CaptureFailure,
) {
    let ready = Mutex::new(Some(ready));
    kaede_capture::system_audio::run(
        kaede_capture::system_audio::CaptureOptions {
            window: 0,
            process: 0,
            width: settings.width,
            height: settings.height,
            fps: settings.frame_rate,
            audio: settings.share_audio,
        },
        &|| stop.load(Ordering::Acquire),
        &|samples| {
            let _ = audio.try_send(samples.to_vec());
        },
        &|frame| {
            let adapted = adapted_screen_settings(settings, level.load(Ordering::Acquire));
            if let Some(buffer) = prepare_packed_screen_frame(frame, adapted) {
                source.capture_frame(&VideoFrame::new(VideoRotation::VideoRotation0, buffer));
                if let Ok(mut ready) = ready.lock()
                    && let Some(ready) = ready.take()
                {
                    let _ = ready.send(Ok(()));
                }
            }
        },
        &|result| {
            if let Err(message) = result {
                screen_audio::fail(&stop, &failure, message.clone());
                if let Ok(mut ready) = ready.lock()
                    && let Some(ready) = ready.take()
                {
                    let _ = ready.send(Err(message));
                }
            }
        },
    );
}

fn prepare_screen_frame(frame: &DesktopFrame, settings: ScreenShareSettings) -> Option<I420Buffer> {
    let width = u32::try_from(frame.width()).ok()?;
    let height = u32::try_from(frame.height()).ok()?;
    prepare_packed_screen_frame(
        PackedFrame {
            width,
            height,
            stride: usize::try_from(frame.stride()).ok()?,
            format: PackedPixelFormat::Bgra,
            data: frame.data(),
        },
        settings,
    )
}

fn prepare_packed_screen_frame(
    frame: PackedFrame<'_>,
    settings: ScreenShareSettings,
) -> Option<I420Buffer> {
    let (width, height) = (frame.width, frame.height);
    let converted = frame.to_i420();
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
    #[error("video sender could not apply the shared upload limit")]
    VideoSendBudget,
    #[error("camera capture failed: {0}")]
    Camera(#[from] nokhwa::NokhwaError),
    #[error("No cameras available")]
    NoCamera,
    #[error("camera capture worker failed: {0}")]
    CameraWorker(String),
    #[error("screen capture worker failed: {0}")]
    ScreenWorker(String),
    #[error("screen audio: {0}")]
    ScreenAudio(String),
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
            Self::VideoSendBudget => "Video could not start. Try sharing again.".to_owned(),
            Self::LiveKit(_) =>
                "The voice service could not complete the connection. Check your connection and try joining voice again."
                    .to_owned(),
            Self::NoCamera => "No cameras available. Connect a camera and try again.".to_owned(),
            Self::Camera(_) =>
                "The camera could not start. Check camera permission, close other apps using it, or choose another camera."
                    .to_owned(),
            Self::CameraWorker(_) =>
                "The camera could not start. Try again or choose another camera."
                    .to_owned(),
            Self::ScreenWorker(_) =>
                "Screen sharing did not start. Approve the system chooser and screen-recording permission, then try again."
                    .to_owned(),
            Self::ScreenAudio(message) =>
                format!("{message} You can also share with audio disabled."),
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
        DataPacketKind, DisconnectReason, ExpectedVoicePolicy, VideoCodec, VideoEncoding,
        VoiceError, VoiceGrant, bounded_dimensions, camera_settings,
        decode_priority_speaker_signal, disconnect_message, effective_microphone_bitrate,
        local_priority_speaker_allowed, local_priority_speaker_metadata_allowed,
        local_priority_speaker_metadata_transition, local_voice_grant_rotation, media_room_options,
        participant_can_priority_speak, priority_speaker_transition, video_publish_options,
    };

    #[test]
    fn receiver_detects_decode_stall_and_resets_evidence_across_gaps() {
        let mut health = super::ReceiverHealth::default();
        let mut stat = livekit::webrtc::stats::InboundRtpStats::default();
        assert_eq!(health.observe(&stat), None);
        for _ in 0..2 {
            stat.inbound.frames_received += 30;
            stat.received.packets_received += 100;
            assert_eq!(health.observe(&stat), None);
        }
        // No complete frames or decoded frames means unknown, never recovery.
        assert_eq!(health.observe(&stat), None);
        assert_eq!(health.bad, 0);
        for _ in 0..3 {
            stat.inbound.frames_received += 30;
            stat.received.packets_received += 100;
            health.observe(&stat);
        }
        assert_eq!(health.health, "decode");
        stat.inbound.frames_received += 30;
        stat.received.packets_lost += 50;
        assert_eq!(health.observe(&stat), Some("download"));
        health.unknown();
        assert_eq!((health.bad, health.healthy), (0, 0));
        assert_eq!(health.health, "download");
    }

    #[test]
    fn receiver_loss_threshold_handles_large_and_negative_counters() {
        for (lost, received, expected) in [
            (1, 19, None), // Exactly five percent is not overload.
            (1, 18, Some("download")),
            (-1, 0, None), // WebRTC may correct cumulative loss downwards.
            (i64::MAX, u64::MAX, Some("download")),
        ] {
            let mut health = super::ReceiverHealth {
                initialized: true,
                bad: 2,
                ..super::ReceiverHealth::default()
            };
            let mut stat = livekit::webrtc::stats::InboundRtpStats::default();
            stat.inbound.frames_decoded = 1;
            stat.inbound.total_decode_time = 0.001;
            stat.received.packets_lost = lost;
            stat.received.packets_received = received;
            assert_eq!(health.observe(&stat), expected);
        }
    }

    fn video_peer(codecs: &[&str], extended: &[&str]) -> super::VideoPeer {
        super::VideoPeer {
            v: 1,
            codecs: codecs.iter().map(|codec| (*codec).to_owned()).collect(),
            extended_codecs: extended.iter().map(|codec| (*codec).to_owned()).collect(),
            encrypted_h264: serde_json::Value::Null,
            received_at: std::time::Instant::now(),
            demand: std::collections::BTreeMap::new(),
            health: std::collections::BTreeMap::new(),
        }
    }

    fn variant_context(level: u8, upload_constrained: bool) -> super::VariantContext {
        super::VariantContext {
            level,
            upload_constrained,
            cpu_spare: true,
            cpu_recovered: true,
            blocked: std::collections::BTreeSet::new(),
        }
    }

    #[test]
    fn premium_choice_uses_audience_and_av1_wins_ties() {
        let key = "publisher/camera";
        let mut peers = std::collections::BTreeMap::new();
        peers.insert("a".to_owned(), video_peer(&["vp8", "av1"], &["h265"]));
        let context = variant_context(0, false);
        assert_eq!(
            super::preferred_native_variant(&peers, 1, key, &context),
            Some(VideoCodec::AV1)
        );
        peers.insert("b".to_owned(), video_peer(&["vp8"], &["h265"]));
        assert_eq!(
            super::preferred_native_variant(&peers, 2, key, &context),
            Some(VideoCodec::H265)
        );
        // A per-source compatible-track demand excludes the viewer from both premium audiences.
        peers
            .get_mut("b")
            .unwrap_or_else(|| panic!("viewer fixture missing"))
            .demand
            .insert(key.to_owned(), "vp8".to_owned());
        assert_eq!(
            super::preferred_native_variant(&peers, 2, key, &context),
            Some(VideoCodec::AV1)
        );
        let mut blocked = variant_context(0, false);
        blocked.blocked.insert("av1".to_owned());
        assert_eq!(
            super::preferred_native_variant(&peers, 2, key, &blocked),
            Some(VideoCodec::H265)
        );
    }

    #[test]
    fn compatible_changes_require_the_entire_audience_and_vp9_pressure_only_on_entry() {
        let key = "publisher/camera";
        let mut peers = std::collections::BTreeMap::new();
        peers.insert(
            "viewer".to_owned(),
            video_peer(&["vp8", "h264"], &["vp9", "h265"]),
        );
        assert_eq!(
            super::preferred_native_compatible(
                &peers,
                1,
                key,
                VideoCodec::VP8,
                &variant_context(0, false)
            ),
            VideoCodec::H264
        );
        assert_eq!(
            super::preferred_native_compatible(
                &peers,
                2,
                key,
                VideoCodec::VP8,
                &variant_context(0, false)
            ),
            VideoCodec::VP8
        );
        assert_eq!(
            super::preferred_native_compatible(
                &peers,
                1,
                key,
                VideoCodec::VP8,
                &variant_context(0, true)
            ),
            VideoCodec::VP9
        );
        // Once selected, bandwidth relief and the recovery counter reset must not flap VP9.
        let mut relieved = variant_context(0, false);
        relieved.cpu_recovered = false;
        assert_eq!(
            super::preferred_native_compatible(&peers, 1, key, VideoCodec::VP9, &relieved),
            VideoCodec::VP9
        );
        relieved.upload_constrained = true;
        assert_eq!(
            super::preferred_native_compatible(&peers, 1, key, VideoCodec::VP8, &relieved),
            VideoCodec::H264
        );
        // A late viewer without hints needs the VP8 publication restored immediately.
        assert_eq!(
            super::preferred_native_compatible(&peers, 2, key, VideoCodec::VP9, &relieved),
            VideoCodec::VP8
        );
        let mut unmeasured = variant_context(0, true);
        unmeasured.cpu_spare = false;
        assert_eq!(
            super::preferred_native_compatible(&peers, 1, key, VideoCodec::VP9, &unmeasured),
            VideoCodec::H264
        );
        assert_eq!(
            super::preferred_native_compatible(&peers, 1, key, VideoCodec::VP8, &unmeasured),
            VideoCodec::H264
        );
        assert_eq!(
            super::preferred_native_compatible(
                &peers,
                1,
                key,
                VideoCodec::VP8,
                &variant_context(1, true)
            ),
            VideoCodec::H264
        );
        peers
            .get_mut("viewer")
            .unwrap_or_else(|| panic!("viewer fixture missing"))
            .demand
            .insert(key.to_owned(), "vp8".to_owned());
        assert_eq!(
            super::preferred_native_compatible(
                &peers,
                1,
                key,
                VideoCodec::VP8,
                &variant_context(0, true)
            ),
            VideoCodec::VP8
        );
    }

    #[test]
    fn dual_cast_requires_audience_headroom_and_no_pressure() {
        let key = "publisher/camera";
        let mut peers = std::collections::BTreeMap::new();
        peers.insert("a".to_owned(), video_peer(&["av1", "vp8"], &[]));
        for name in ["b", "c", "d"] {
            peers.insert(name.to_owned(), video_peer(&["vp8"], &[]));
        }
        assert_eq!(
            super::preferred_native_variant(&peers, 4, key, &variant_context(0, false)),
            None
        );
        peers.insert("b".to_owned(), video_peer(&["av1", "vp8"], &[]));
        assert_eq!(
            super::preferred_native_variant(&peers, 4, key, &variant_context(0, false)),
            Some(VideoCodec::AV1)
        );
        assert_eq!(
            super::preferred_native_variant(&peers, 4, key, &variant_context(1, false)),
            None
        );
        assert_eq!(
            super::preferred_native_variant(&peers, 4, key, &variant_context(0, true)),
            None
        );
        assert!(super::pressure_retires_native_variant(
            &peers,
            4,
            key,
            "av1",
            &variant_context(0, true)
        ));
        peers.remove("b");
        assert!(!super::pressure_retires_native_variant(
            &peers,
            4,
            key,
            "av1",
            &variant_context(0, true)
        ));
        assert!(!super::pressure_retires_native_variant(
            &std::collections::BTreeMap::new(),
            0,
            key,
            "av1",
            &variant_context(3, true)
        ));
        peers.insert("b".to_owned(), video_peer(&["av1", "vp8"], &[]));
        // An audience-driven swap under pressure does not penalize the previous codec.
        peers.insert("c".to_owned(), video_peer(&["vp8"], &["h265"]));
        peers.insert("d".to_owned(), video_peer(&["vp8"], &["h265"]));
        peers.insert("e".to_owned(), video_peer(&["vp8"], &["h265"]));
        assert!(!super::pressure_retires_native_variant(
            &peers,
            5,
            key,
            "av1",
            &variant_context(1, true)
        ));
        peers.remove("e");
        // One encode can continue under moderate pressure when everyone watches it.
        peers.remove("c");
        peers.remove("d");
        assert_eq!(
            super::preferred_native_variant(&peers, 2, key, &variant_context(2, true)),
            Some(VideoCodec::AV1)
        );
        assert_eq!(
            super::preferred_native_variant(&peers, 2, key, &variant_context(3, true)),
            None
        );
    }

    #[test]
    fn sender_caps_share_one_source_budget_and_restore_the_ladder() {
        use livekit::webrtc::rtp_parameters::RtpEncodingParameters;
        let original: Vec<_> = [
            ("q", 250_000, 4.0),
            ("h", 750_000, 2.0),
            ("f", 3_000_000, 1.0),
        ]
        .into_iter()
        .map(|(rid, bitrate, scale)| RtpEncodingParameters {
            rid: rid.to_owned(),
            max_bitrate: Some(bitrate),
            scale_resolution_down_by: Some(scale),
            ..RtpEncodingParameters::default()
        })
        .collect();
        let mut encodings = original.clone();
        encodings[0].active = false; // Dynacast still owns subscription-driven activity.
        assert!((super::compatible_video_scale(3840, 2160) - 3.0).abs() < f64::EPSILON);
        assert!((super::compatible_video_scale(2160, 3840) - 3.0).abs() < f64::EPSILON);
        assert!((super::compatible_video_scale(640, 360) - 1.0).abs() < f64::EPSILON);
        let dual = super::source_video_bitrate(12_000_000, 0, 2, 2);
        assert_eq!(dual, 2_000_000);
        assert!(super::limit_video_encodings(
            &mut encodings,
            &original,
            dual,
            3.0
        ));
        assert_eq!(
            encodings
                .iter()
                .map(|encoding| encoding.max_bitrate.unwrap_or(0))
                .sum::<u64>(),
            dual
        );
        assert_eq!(encodings[0].max_bitrate, Some(125_000));
        assert_eq!(encodings[2].max_bitrate, Some(1_500_000));
        assert_eq!(encodings[0].scale_resolution_down_by, Some(4.0));
        assert_eq!(encodings[1].scale_resolution_down_by, Some(3.0));
        assert_eq!(encodings[2].scale_resolution_down_by, Some(3.0));
        assert!(!encodings[0].active);
        assert!(!super::limit_video_encodings(
            &mut encodings,
            &original,
            dual,
            3.0
        ));
        let single = super::source_video_bitrate(12_000_000, 0, 1, 1);
        assert_eq!(single, 8_000_000);
        assert!(super::limit_video_encodings(
            &mut encodings,
            &original,
            single,
            1.0
        ));
        assert_eq!(encodings[2].scale_resolution_down_by, Some(1.0));
        assert_eq!(
            encodings
                .iter()
                .map(|encoding| encoding.max_bitrate.unwrap_or(0))
                .sum::<u64>(),
            single
        );
        assert_eq!(super::source_video_bitrate(1_700_000, 0, 2, 2), 850_000);
        assert_eq!(super::source_video_bitrate(200_000, 3, 1, 1), 200_000);
        assert_eq!(super::source_video_bitrate(200_000, 3, 1, 2), 100_000);
        let mut svc = vec![RtpEncodingParameters {
            max_bitrate: Some(12_000_000),
            scalability_mode: Some("L3T3_KEY".to_owned()),
            ..RtpEncodingParameters::default()
        }];
        let mut original_svc = svc.clone();
        original_svc[0].rid = "q".to_owned();
        super::limit_video_encodings(&mut svc, &original_svc, dual, 1.0);
        assert_eq!(svc[0].max_bitrate, Some(dual));
        assert_eq!(svc[0].scalability_mode.as_deref(), Some("L3T3_KEY"));
    }

    #[test]
    fn video_hints_extend_legacy_codecs_without_a_flag_day() {
        let legacy: super::VideoPeer =
            serde_json::from_str(r#"{"v":1,"codecs":["vp8","h264","av1"]}"#)
                .unwrap_or_else(|error| panic!("invalid fixture: {error}"));
        assert!(legacy.valid());
        assert!(!legacy.supports("h265"));
        assert!(!legacy.supports_encrypted_h264());
        let extended: super::VideoPeer = serde_json::from_str(r#"{"v":1,"codecs":["vp8","h264","av1"],"extendedCodecs":["h265","vp9","future"],"futureField":true}"#).unwrap_or_else(|error| panic!("invalid fixture: {error}"));
        assert!(extended.valid());
        assert!(extended.supports("h265"));
        let wrong_field = video_peer(&["vp8", "h265"], &[]);
        assert!(!wrong_field.valid());
        let too_many = video_peer(&["vp8"], &["h265"; 9]);
        assert!(!too_many.valid());
    }

    #[test]
    fn encrypted_h264_requires_a_marker_from_every_current_viewer() -> Result<(), serde_json::Error>
    {
        use std::collections::BTreeMap;
        assert!(!super::room_allows_h264(true, std::iter::empty()));
        let mut peer = video_peer(&["vp8", "h264"], &[]);
        peer.encrypted_h264 = serde_json::json!("2");
        assert!(super::room_allows_h264(false, [Some(&peer)].into_iter()));
        assert!(!super::room_allows_h264(true, [Some(&peer)].into_iter()));
        // A new viewer can announce the marker before either side advertises H.264.
        let hint = super::encode_video_hints(&[], &[], &BTreeMap::new(), &BTreeMap::new(), true);
        let updated: super::VideoPeer = serde_json::from_slice(&hint)?;
        assert!(
            updated
                .current_encrypted_h264(updated.received_at + std::time::Duration::from_secs(24))
        );
        assert!(
            !updated
                .current_encrypted_h264(updated.received_at + std::time::Duration::from_secs(26))
        );
        assert!(updated.valid() && updated.supports_encrypted_h264() && !updated.supports("h264"));
        assert!(!super::room_allows_h264(
            true,
            [Some(&updated), Some(&peer)].into_iter()
        ));
        assert!(super::room_allows_h264(true, [Some(&updated)].into_iter()));
        assert!(!super::room_allows_h264(
            true,
            [Some(&updated), None].into_iter()
        ));
        let mut peers = BTreeMap::new();
        let oversized = vec![b' '; 8193];
        for invalid in [b"malformed".as_slice(), oversized.as_slice()] {
            assert_eq!(
                super::update_peer_video_hint(&mut peers, "viewer".into(), &hint),
                Some(true)
            );
            assert_eq!(
                super::update_peer_video_hint(&mut peers, "viewer".into(), invalid),
                Some(false)
            );
            assert!(!super::room_allows_h264(
                true,
                [peers.get("viewer")].into_iter()
            ));
        }
        Ok(())
    }

    #[test]
    fn crowded_encrypted_rooms_still_announce_the_compatible_codecs()
    -> Result<(), serde_json::Error> {
        use std::collections::BTreeMap;
        let demand = BTreeMap::from([("x".repeat(257), "vp8")]);
        let health = BTreeMap::from([("x".repeat(8192), "download")]);
        let crowded = (0..65)
            .map(|i| (format!("viewer{i}/camera"), "vp8"))
            .collect();
        for (demand, health) in [
            (demand, BTreeMap::new()),
            (crowded, BTreeMap::new()),
            (BTreeMap::new(), health),
        ] {
            let hint = super::encode_video_hints(&["vp8"], &["vp9"], &demand, &health, true);
            assert!(hint.len() <= 8192);
            let peer: super::VideoPeer = serde_json::from_slice(&hint)?;
            assert!(peer.valid() && peer.supports_encrypted_h264());
            assert!(peer.supports("vp8") && peer.supports("vp9") && !peer.supports("h264"));
            assert!(peer.demand.is_empty());
        }
        Ok(())
    }

    #[test]
    fn spatial_layer_trial_delays_premium_fallback_but_not_codec_backoff() {
        let start = std::time::Instant::now();
        let mut health = super::ReceiverHealth {
            health: "decode",
            layer_trial_until: Some(start + std::time::Duration::from_secs(20)),
            ..super::ReceiverHealth::default()
        };
        assert!(!health.needs_fallback(start + std::time::Duration::from_secs(19)));
        assert!(health.needs_fallback(start + std::time::Duration::from_secs(20)));
        health.fail_premium("h265", start);
        assert!(health.premium_blocked("h265", start));
        assert!(!health.premium_blocked("av1", start));
    }

    #[test]
    fn failed_attempts_back_off_then_stop() {
        let mut backoff = super::VideoBackoff::default();
        let start = std::time::Instant::now();
        assert!(!backoff.blocked("camera/av1/1280x720", start));
        backoff.fail("camera/av1/1280x720", start);
        assert!(backoff.blocked(
            "camera/av1/1280x720",
            start + std::time::Duration::from_secs(59)
        ));
        assert!(!backoff.blocked(
            "camera/av1/1280x720",
            start + std::time::Duration::from_mins(1)
        ));
        for _ in 0..3 {
            backoff.fail("camera/av1/1280x720", start);
        }
        assert!(backoff.blocked(
            "camera/av1/1280x720",
            start + std::time::Duration::from_hours(24)
        ));
        let mut health = super::ReceiverHealth::default();
        health.fail_premium("av1", start);
        assert!(health.premium_blocked("av1", start + std::time::Duration::from_secs(30)));
        assert!(!health.premium_blocked("h265", start));
        assert!(!health.premium_blocked("av1", start + std::time::Duration::from_secs(61)));
    }

    #[test]
    fn capture_adaptation_matches_the_shared_steps() {
        assert_eq!(super::adapted_dimension(1920, 0, 640), 1920);
        assert_eq!(super::adapted_dimension(1920, 1, 640), 1536);
        assert_eq!(super::adapted_dimension(1920, 3, 640), 983);
        assert_eq!(super::adapted_dimension(640, 3, 640), 640);
        assert_eq!(super::adapted_frame_rate(30, 1, false), 25);
        assert_eq!(super::adapted_frame_rate(30, 3, false), 18);
        assert_eq!(super::adapted_frame_rate(15, 2, true), 6);
        assert_eq!(super::adapted_frame_rate(15, 3, true), 5);
        assert_eq!(super::adapted_frame_rate(10, 3, false), 10);
    }

    #[test]
    fn bitrate_adaptation_is_bounded_and_does_not_overflow() {
        for (level, expected) in [(0, 2_500_000), (1, 1_800_000), (2, 1_296_000), (3, 933_000)] {
            assert_eq!(super::adapted_bitrate(2_500_000, level), expected);
        }
        assert_eq!(super::adapted_bitrate(0, 3), 300_000);
        assert_eq!(super::adapted_bitrate(u64::MAX, 0), u64::MAX);
        assert_eq!(
            super::adapted_bitrate(u64::MAX, 255),
            super::adapted_bitrate(u64::MAX, 3)
        );
    }

    #[test]
    fn encode_headroom_survives_brief_stats_gaps_without_renewing_unknown_proof() {
        let start = std::time::Instant::now();
        let mut headroom = super::EncodeHeadroom::default();
        headroom.observe(None, false, start);
        assert!(!headroom.has_spare(start));
        headroom.observe(Some(0.2), false, start);
        for seconds in [2, 8, 9] {
            let now = start + std::time::Duration::from_secs(seconds);
            headroom.observe(None, false, now);
            assert!(headroom.has_spare(now));
        }
        let expired = start + super::video_policy::UPLOAD_WARMUP;
        assert!(!headroom.has_spare(expired));
        headroom.observe(None, false, expired);
        assert!(!headroom.has_spare(expired));
        for (load, overloaded) in [(Some(0.5), false), (None, true), (Some(f64::NAN), false)] {
            headroom.observe(Some(0.2), false, expired);
            assert!(headroom.has_spare(expired));
            headroom.observe(load, overloaded, expired);
            assert!(!headroom.has_spare(expired));
        }
        headroom.observe(Some(0.2), false, expired);
        assert!(headroom.has_spare(expired + std::time::Duration::from_secs(9)));
    }

    #[test]
    fn combined_video_budget_requires_sustained_evidence_and_slow_recovery() {
        let mut budget = super::VideoBudget::default();
        assert!(!budget.observe(true));
        assert!(!budget.observe(false));
        assert!(!budget.observe(true));
        assert!(!budget.observe(true));
        assert!(budget.observe(true));
        assert_eq!(budget.level, 1);
        for _ in 0..14 {
            assert!(!budget.observe(false));
        }
        assert!(budget.observe(false));
        assert_eq!(budget.level, 0);
        // Upload pressure never raises the encode level; it is tracked apart.
        let mut upload = super::UploadPressure::default();
        for _ in 0..3 {
            upload.observe(true);
        }
        assert!(upload.constrained);
        for _ in 0..15 {
            upload.observe(false);
        }
        assert!(!upload.constrained);
    }

    #[test]
    fn video_codec_defaults_are_conservative_and_independent_of_encryption() {
        let encoding = VideoEncoding {
            max_bitrate: 2_500_000,
            max_framerate: 30.0,
        };
        for encrypted in [false, true] {
            let options = video_publish_options(encrypted, encoding.clone());
            assert_eq!(options.video_codec, VideoCodec::VP8);
            assert!(options.backup_codec.is_none());
            assert!(options.scalability_mode.is_none());
            assert_eq!(
                options.video_encoding.map(|encoding| encoding.max_bitrate),
                Some(2_500_000)
            );
        }
    }

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
        assert!(
            VoiceError::CameraWorker("thread exited".into())
                .user_message()
                .contains("could not start")
        );
        assert!(
            VoiceError::Camera(nokhwa::NokhwaError::OpenStreamError("busy".into()))
                .user_message()
                .contains("close other apps")
        );
        assert!(
            VoiceError::NoCamera
                .user_message()
                .contains("No cameras available")
        );
        let audio_error =
            VoiceError::ScreenAudio("The selected audio app has closed.".to_owned()).user_message();
        assert!(audio_error.contains("app has closed") && audio_error.contains("audio disabled"));
        let message = VoiceError::Audio(kaede_audio::AudioError::DeviceNotFound).user_message();
        assert!(message.contains("another audio device") && message.contains("try again"));
        assert!(
            VoiceError::VoiceActivityDenied
                .user_message()
                .contains("push to talk")
        );
    }

    #[test]
    fn disconnect_reasons_do_not_leak_debug_enum_names() {
        let message = disconnect_message(DisconnectReason::DuplicateIdentity);
        assert!(message.contains("another device") && message.contains("disconnected"));
        assert!(!message.contains("DuplicateIdentity"));
        assert!(disconnect_message(DisconnectReason::ConnectionTimeout).contains("join again"));
    }

    #[test]
    fn native_voice_policy_is_bidirectional_before_media_setup()
    -> Result<(), Box<dyn std::error::Error>> {
        let plain = media_room_options(&grant(false), &expected(false), None)?;
        assert!(plain.encryption.is_none());
        let encrypted = media_room_options(&grant(true), &expected(true), Some(vec![7; 32]))?;
        let encryption = encrypted.encryption.ok_or("encryption not configured")?;
        assert_eq!(encryption.encryption_type, super::EncryptionType::Gcm);
        assert_eq!(encryption.key_provider.get_shared_key(0), Some(vec![7; 32]));
        assert!(matches!(
            media_room_options(&grant(true), &expected(true), None),
            Err(VoiceError::EncryptionKeyMissing)
        ));
        for length in [0, 31, 33] {
            assert!(matches!(
                media_room_options(&grant(true), &expected(true), Some(vec![7; length])),
                Err(VoiceError::EncryptionPolicyMismatch)
            ));
        }
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
        Ok(())
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

        for field in [
            "bitrate",
            "user_limit",
            "rtc_region",
            "video_quality_mode",
            "e2ee",
        ] {
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
        denied.can_priority_speak = true;
        denied.can_speak = false;
        assert!(!local_priority_speaker_allowed(
            true,
            &denied,
            kaede_audio::InputMode::PushToTalk
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
    fn camera_selection_preserves_available_preferences_and_falls_back() {
        let cameras = [
            super::CameraDevice {
                id: "4".into(),
                label: "USB camera".into(),
                aliases: Vec::new(),
            },
            super::CameraDevice {
                id: "7".into(),
                label: "Virtual camera".into(),
                aliases: vec!["dshow:virtual".into()],
            },
        ];
        assert_eq!(super::select_camera(&cameras, None), cameras.first());
        assert_eq!(
            super::select_camera(&cameras, Some("removed")),
            cameras.first()
        );
        assert_eq!(super::select_camera(&cameras, Some("7")), cameras.get(1));
        assert_eq!(
            super::select_camera(&cameras, Some("dshow:virtual")),
            cameras.get(1)
        );
        assert_eq!(super::select_camera(&[], Some("7")), None);
    }

    #[test]
    fn camera_startup_retries_construction_and_stream_failures() {
        use nokhwa::{NokhwaError, utils::RequestedFormatType};
        for first_error in [
            NokhwaError::InitializeError {
                backend: nokhwa::utils::ApiBackend::Auto,
                error: "unsupported format".into(),
            },
            NokhwaError::OpenStreamError("driver rejected preferred mode".into()),
        ] {
            let mut attempts = 0;
            let result = super::start_camera_with_fallback(camera_settings(1), |format| {
                attempts += 1;
                if attempts == 1 {
                    assert!(matches!(
                        format.requested_format_type(),
                        RequestedFormatType::Closest(_)
                    ));
                    Err(first_error.clone())
                } else {
                    assert_eq!(format.requested_format_type(), RequestedFormatType::None);
                    Ok(())
                }
            });
            assert!(result.is_ok());
            assert_eq!(attempts, 2);
        }
        let mut attempts = 0;
        assert!(
            super::start_camera_with_fallback(camera_settings(1), |_| {
                attempts += 1;
                Ok(())
            })
            .is_ok()
        );
        assert_eq!(attempts, 1);
        let error = super::start_camera_with_fallback::<()>(camera_settings(1), |_| {
            Err(nokhwa::NokhwaError::OpenStreamError(
                "permission denied".into(),
            ))
        })
        .err();
        assert!(
            matches!(error, Some(nokhwa::NokhwaError::OpenStreamError(reason)) if reason == "permission denied")
        );
    }

    #[test]
    fn media_policy_caps_microphone_and_selects_camera_defaults() {
        assert_eq!(effective_microphone_bitrate(128_000, 32_000), 32_000);
        assert_eq!(effective_microphone_bitrate(24_000, 96_000), 24_000);
        assert_eq!(effective_microphone_bitrate(4_000, 8_000), 8_000);

        let automatic = camera_settings(1);
        let full = camera_settings(2);
        assert!(automatic.width > 0 && automatic.height > 0);
        assert!(full.width >= automatic.width && full.height >= automatic.height);
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
