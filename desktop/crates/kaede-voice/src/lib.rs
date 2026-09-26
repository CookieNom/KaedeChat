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
        key_provider::{KeyDerivationAlgorithm, KeyProvider, KeyProviderOptions},
    },
    options::{AudioEncoding, TrackPublishOptions, VideoCodec, VideoEncoding},
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
    let mut video_peers: BTreeMap<String, VideoPeer> = BTreeMap::new();
    let publisher_samples = Arc::new(Mutex::new(
        BTreeMap::<String, (u32, f64, std::time::Instant)>::new(),
    ));
    let receiver_health = Arc::new(Mutex::new(BTreeMap::<String, ReceiverHealth>::new()));
    let (performance_tx, mut performance_rx) = mpsc::channel(1);
    let mut performance_job: Option<JoinHandle<()>> = None;
    let mut capability_tick = time::interval(Duration::from_secs(10));
    capability_tick.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    let mut video_readers: BTreeMap<(String, bool), (String, JoinHandle<()>)> = BTreeMap::new();
    capture_tick.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    playback_tick.set_missed_tick_behavior(time::MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            result = async { screen_start.as_mut().expect("pending capture").await }, if screen_start.is_some() => {
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
            result = async { camera_start.as_mut().expect("pending capture").await }, if camera_start.is_some() => {
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
                if let Some((cpu, bandwidth, observed)) = report {
                    if camera.is_none() && screen_share.is_none() { budget = VideoBudget::default(); }
                    if observed && budget.observe(cpu, bandwidth) {
                        for video in [camera.as_mut(), screen_share.as_mut()].into_iter().flatten() {
                            if video.adjustment.as_ref().is_some_and(|task| !task.is_finished()) { continue; }
                            video.adjustment = Some(adjust_video_budget(&room, video, budget.level, cpu));
                        }
                    }
                    if !observed { budget.bad = 0; budget.healthy = 0; }
                    let receiver_degraded = receiver_health.lock().is_ok_and(|states| states.values().any(|state| matches!(state.health, "decode" | "download")));
                    video_degraded.store(budget.level > 0 || receiver_degraded, Ordering::Release);
                }
            }
            _ = capability_tick.tick() => {
                for video in [camera.as_mut(), screen_share.as_mut()].into_iter().flatten() {
                    let demand_key = format!("{}/{}", local_identity, if video.source == TrackSource::Screenshare { "screen_share" } else { "camera" });
                    let desired = preferred_native_variant(&video_peers, room.remote_participants().len(), &demand_key, budget.level);
                    video.variant_retry = video.variant_retry.saturating_sub(1);
                    let h264_allowed = h264_compatible(&video_peers, room.remote_participants().len(), &demand_key);
                    let has_h264 = video.variants.lock().is_ok_and(|tracks| tracks.iter().any(|track| track.name().ends_with(":h264")));
                    if (desired != video.requested_variant || (has_h264 && !h264_allowed)) && video.adjustment.as_ref().is_none_or(JoinHandle::is_finished) {
                        let retired = video.variants.lock().map(|mut tracks| std::mem::take(&mut *tracks)).unwrap_or_default();
                        if !retired.is_empty() {
                            let participant = room.local_participant();
                            video.adjustment = Some(tokio::spawn(async move { for variant in retired { let _ = participant.unpublish_track(&variant.sid()).await; } }));
                        }
                        video.variant_retry = 0;
                        video.requested_variant = desired;
                    }
                    let has_variant = video.variants.lock().is_ok_and(|tracks| !tracks.is_empty());
                    if let Some(codec) = desired
                        && (!has_variant || (codec == VideoCodec::AV1 && has_h264))
                        && video.variant_retry == 0
                        && video.adjustment.as_ref().is_none_or(JoinHandle::is_finished)
                    {
                        video.adjustment = Some(enable_video_variant(&room, video, codec, h264_compatible(&video_peers, room.remote_participants().len(), &demand_key)));
                        video.variant_retry = 6;
                    }
                    if video.capture_level.load(Ordering::Acquire) != u32::from(budget.level)
                        && video.adjustment.as_ref().is_none_or(JoinHandle::is_finished) {
                        video.adjustment = Some(adjust_video_budget(&room, video, budget.level, false));
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
                    let report_tx = performance_tx.clone();
                    performance_job = Some(tokio::spawn(async move {
                        use livekit::webrtc::stats::{RtcStats, QualityLimitationReason};
                        let (mut cpu, mut bandwidth, mut observed) = (false, false, false);
                        let (mut combined_load, mut incomplete) = (0.0, false);
                        for track in tracks {
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
                                        cpu |= (track.name().ends_with(":av1") || track.name().ends_with(":h264")) && !verified_hardware;
                                        let elapsed = stat.outbound.total_encode_time - previous_time;
                                        if !elapsed.is_finite() || elapsed <= 0.0 { incomplete = true; continue; }
                                        // Hardware encode durations overlap across independent engines;
                                        // only software work consumes this shared CPU budget.
                                        if !verified_hardware { combined_load += elapsed / now.duration_since(previous_sample).as_secs_f64().max(0.001); }
                                        cpu |= elapsed / f64::from(frames) > 0.8 / stat.outbound.frames_per_second.max(15.0);
                                        cpu |= stat.outbound.quality_limitation_reason == QualityLimitationReason::Cpu;
                                        bandwidth |= stat.outbound.quality_limitation_reason == QualityLimitationReason::Bandwidth;
                                    }
                                }
                            }
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
                                                *state = ReceiverHealth { sid, health: state.health, ..ReceiverHealth::default() };
                                            }
                                            match state.observe(&stat) {
                                                Some("healthy") => publication.set_video_quality(livekit::track::VideoQuality::High),
                                                Some(_) => publication.set_video_quality(livekit::track::VideoQuality::Low),
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
                        let _ = report_tx.send((cpu, bandwidth, observed && (!incomplete || cpu || bandwidth))).await;
                    }));
                }

                let local = room.local_participant();
                let payload = native_video_capabilities(&receiver_health);
                tokio::spawn(async move {
                    let _ = local.publish_data(DataPacket {
                        payload, topic: Some("kaede.video.v1".to_owned()), reliable: true,
                        ..DataPacket::default()
                    }).await;
                });
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
                reconcile_video_subscriptions(&room, &mut subscriptions, &receiver_health, video_visible);
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
                        reconcile_video_subscriptions(&room, &mut subscriptions, &receiver_health, video_visible);
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
                            camera_start = Some(Box::pin(async move {
                                publish_camera(room, device_id.as_deref(), video_quality_mode).await
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
                            screen_start = Some(Box::pin(async move {
                                publish_screen_share(room, source_id.as_deref(), settings).await
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
                            if payload.len() <= 8192
                                && let Ok(peer) = serde_json::from_slice::<VideoPeer>(&payload)
                                && peer.v == 1 && !peer.codecs.is_empty() && peer.codecs.len() <= 3
                                && peer.codecs.iter().all(|codec| matches!(codec.as_str(), "av1" | "vp8" | "h264"))
                                && peer.demand.len() <= 64
                            {
                                video_peers.insert(identity, peer);
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
                    Some(RoomEvent::ParticipantDisconnected(participant)) => {
                        let identity = participant.identity().to_string();
                        playback_mixer.remove_participant(&identity);
                        video_peers.remove(&identity);
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
                        subscriptions.clear();
                        reconcile_video_subscriptions(&room, &mut subscriptions, &receiver_health, video_visible);
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

#[derive(Default)]
struct VideoBudget {
    level: u8,
    bad: u8,
    healthy: u8,
    cooldown: u8,
}

impl VideoBudget {
    // Ten-second samples: thirty seconds of degradation, sixty seconds of
    // recovery, with thirty seconds between transitions across both sources.
    fn observe(&mut self, cpu: bool, bandwidth: bool) -> bool {
        self.cooldown = self.cooldown.saturating_sub(1);
        if cpu || bandwidth {
            self.bad = self.bad.saturating_add(1);
            self.healthy = 0;
        } else {
            self.healthy = self.healthy.saturating_add(1);
            self.bad = 0;
        }
        let next = if self.bad >= 3 {
            (self.level + 1).min(3)
        } else if self.healthy >= 6 {
            self.level.saturating_sub(1)
        } else {
            self.level
        };
        if next == self.level || self.cooldown > 0 {
            return false;
        }
        self.level = next;
        self.cooldown = 3;
        self.bad = 0;
        self.healthy = 0;
        true
    }
}

fn preferred_native_variant(
    peers: &BTreeMap<String, VideoPeer>,
    participant_count: usize,
    key: &str,
    budget: u8,
) -> Option<VideoCodec> {
    if budget == 0
        && peers.values().any(|peer| {
            peer.codecs.iter().any(|codec| codec == "av1")
                && peer.demand.get(key).is_none_or(|codec| codec == "av1")
        })
    {
        return Some(VideoCodec::AV1);
    }
    if h264_compatible(peers, participant_count, key) {
        return Some(VideoCodec::H264);
    }
    None
}

fn h264_compatible(
    peers: &BTreeMap<String, VideoPeer>,
    participant_count: usize,
    key: &str,
) -> bool {
    participant_count > 0
        && peers.len() == participant_count
        && peers.values().all(|peer| {
            peer.codecs.iter().any(|codec| codec == "h264")
                && peer.demand.get(key).is_none_or(|codec| codec != "vp8")
        })
}

fn adapted_bitrate(bitrate: u64, level: u8) -> u64 {
    // Exact 0.65^level for the four adaptation levels; u128 avoids overflow.
    let numerator = [8000_u128, 5200, 3380, 2197][usize::from(level.min(3))];
    u64::try_from(u128::from(bitrate) * numerator / 8000)
        .unwrap_or(bitrate)
        .max(300_000)
}

fn enable_video_variant(
    room: &Room,
    video: &PublishedVideo,
    mut codec: VideoCodec,
    allow_h264: bool,
) -> JoinHandle<()> {
    let participant = room.local_participant();
    let stop = video.stop.clone();
    let source = video.track.rtc_source();
    let dimensions = source.video_resolution();
    let mut encoding = video.encoding.clone();
    let level = u8::try_from(video.capture_level.load(Ordering::Acquire).min(3)).unwrap_or(3);
    let factor = 0.65_f64.powi(i32::from(level));
    encoding.max_bitrate = adapted_bitrate(encoding.max_bitrate, level);
    encoding.max_framerate = (encoding.max_framerate * factor.sqrt()).max(5.0);
    let variants = video.variants.clone();
    let track_source = video.source;
    let encrypted = room.e2ee_manager().encryption_type() != EncryptionType::None;
    tokio::spawn(async move {
        let mut codec_name = if codec == VideoCodec::H264 {
            "h264"
        } else {
            "av1"
        };
        if !native_video_probe::hardware_codec(
            codec_name,
            dimensions.width,
            dimensions.height,
            encoding.clone(),
            &stop,
        )
        .await
        {
            if stop.load(Ordering::Acquire)
                || codec != VideoCodec::AV1
                || !allow_h264
                || !native_video_probe::hardware_codec(
                    "h264",
                    dimensions.width,
                    dimensions.height,
                    encoding.clone(),
                    &stop,
                )
                .await
            {
                return;
            }
            codec = VideoCodec::H264;
            codec_name = "h264";
        }
        if variants.lock().is_ok_and(|tracks| {
            tracks
                .iter()
                .any(|track| track.name().ends_with(&format!(":{codec_name}")))
        }) {
            return;
        }
        // H264 is useful here only when its actual acceleration beats VP8.
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
            return;
        }
        if stop.load(Ordering::Acquire) {
            return;
        }
        let source_name = if track_source == TrackSource::Screenshare {
            "screen_share"
        } else {
            "camera"
        };
        let name = format!("kaede.video.v1:{source_name}:{codec_name}");
        let track = LocalVideoTrack::create_video_track(&name, source);
        let options = TrackPublishOptions {
            source: track_source,
            video_codec: codec,
            video_encoder: livekit::webrtc::rtp_sender::VideoEncoderBackend::Hardware,
            simulcast: false,
            scalability_mode: (codec == VideoCodec::AV1).then(|| "L1T1".to_owned()),
            ..video_publish_options(encrypted, encoding)
        };
        let retired = variants
            .lock()
            .map(|mut tracks| std::mem::take(&mut *tracks))
            .unwrap_or_default();
        for retired in retired {
            let _ = participant.unpublish_track(&retired.sid()).await;
        }
        if participant
            .publish_track(LocalTrack::Video(track.clone()), options)
            .await
            .is_ok()
            && let Ok(mut variants) = variants.lock()
        {
            variants.push(track);
        }
    })
}

fn adjust_video_budget(
    room: &Room,
    video: &PublishedVideo,
    level: u8,
    cpu: bool,
) -> JoinHandle<()> {
    video
        .capture_level
        .store(u32::from(level), Ordering::Release);
    let participant = room.local_participant();
    let track = video.track.clone();
    let source = video.source;
    let mut encoding = video.encoding.clone();
    let factor = 0.65_f64.powi(i32::from(level));
    encoding.max_bitrate = adapted_bitrate(encoding.max_bitrate, level);
    // Low-motion screen content keeps pixels legible by surrendering frames
    // first. Motion-oriented shares and cameras retain at least 15 fps.
    let floor = if source == TrackSource::Screenshare && encoding.max_framerate <= 15.0 {
        5.0
    } else {
        15.0
    };
    encoding.max_framerate = (encoding.max_framerate * factor.sqrt())
        .max(floor)
        .min(video.encoding.max_framerate);
    let encrypted = room.e2ee_manager().encryption_type() != EncryptionType::None;
    let options = TrackPublishOptions {
        source,
        simulcast: !(cpu && level > 0),
        ..video_publish_options(encrypted, encoding)
    };
    let variants = video.variants.clone();
    let original = TrackPublishOptions {
        source,
        ..video_publish_options(encrypted, video.encoding.clone())
    };
    tokio::spawn(async move {
        // Shared sustained pressure retires the second encoder before reducing
        // the universally supported track. Audio remains untouched.
        let retired = variants
            .lock()
            .map(|mut tracks| std::mem::take(&mut *tracks))
            .unwrap_or_default();
        for variant in retired {
            let _ = participant.unpublish_track(&variant.sid()).await;
        }
        // The public SDK has no sender handle. Re-publish the SAME source;
        // room E2EE/key provider and capture ownership survive unchanged.
        if participant.unpublish_track(&track.sid()).await.is_err() {
            return;
        }
        if let Err(error) = participant
            .publish_track(LocalTrack::Video(track.clone()), options)
            .await
        {
            tracing::warn!(%error, "video adaptation failed; restoring fallback publication");
            if let Err(error) = participant
                .publish_track(LocalTrack::Video(track), original)
                .await
            {
                tracing::warn!(%error, "video fallback restoration failed");
            }
        }
    })
}

#[derive(Deserialize)]
struct VideoPeer {
    v: u8,
    codecs: Vec<String>,
    #[serde(default)]
    demand: BTreeMap<String, String>,
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
}

impl ReceiverHealth {
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
        } else if self.healthy >= 6 {
            self.health = "healthy";
        } else {
            return None;
        }
        Some(self.health)
    }
}

fn native_video_capabilities(receiver_health: &Mutex<BTreeMap<String, ReceiverHealth>>) -> Vec<u8> {
    let capabilities = livekit::rtc_engine::lk_runtime::LkRuntime::instance()
        .pc_factory()
        .get_rtp_receiver_capabilities(livekit::webrtc::MediaType::Video);
    let codecs: Vec<_> = ["av1", "vp8", "h264"]
        .into_iter()
        .filter(|codec| {
            capabilities.codecs.iter().any(|entry| {
                entry
                    .mime_type
                    .eq_ignore_ascii_case(&format!("video/{codec}"))
            })
        })
        .collect();
    let mut health = BTreeMap::new();
    let mut demand = BTreeMap::new();
    if let Ok(states) = receiver_health.lock() {
        for (key, state) in states.iter() {
            if !state.health.is_empty() {
                health.insert(key.clone(), state.health);
            }
            if matches!(state.health, "decode" | "download") {
                demand.insert(key.clone(), "vp8");
            }
        }
    }
    serde_json::json!({"v": 1, "codecs": codecs, "demand": demand, "health": health})
        .to_string()
        .into_bytes()
}

// Select one publication per logical source. Audio is explicitly retained when
// automatic subscriptions are disabled, including independent share audio.
fn reconcile_video_subscriptions(
    room: &Room,
    subscribed: &mut BTreeSet<String>,
    receiver_health: &Mutex<BTreeMap<String, ReceiverHealth>>,
    video_visible: bool,
) {
    let capabilities = livekit::rtc_engine::lk_runtime::LkRuntime::instance()
        .pc_factory()
        .get_rtp_receiver_capabilities(livekit::webrtc::MediaType::Video);
    let mut next = BTreeSet::new();
    for participant in room.remote_participants().values() {
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
            let mime = publication.mime_type().to_ascii_lowercase();
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
                    states
                        .get(&key)
                        .map(|state| matches!(state.health, "decode" | "download"))
                })
                .unwrap_or(false);
            let rank = (
                if mime == "video/av1" {
                    if decode_overload { 0 } else { 3 }
                } else if mime == "video/h264" && publication.name().starts_with("kaede.video.v1:")
                {
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
            tokio::spawn(async move {
                let _ = participant.unpublish_track(&track.sid()).await;
            });
        }
    }
}

struct PublishedVideo {
    audio: Option<screen_audio::PublishedAudio>,
    audio_capture_thread: Option<thread::JoinHandle<()>>,
    capture_failure: screen_audio::CaptureFailure,
    track: LocalVideoTrack,
    source: TrackSource,
    encoding: VideoEncoding,
    adjustment: Option<JoinHandle<()>>,
    capture_level: Arc<AtomicU32>,
    variants: Arc<Mutex<Vec<LocalVideoTrack>>>,
    requested_variant: Option<VideoCodec>,
    variant_retry: u8,
    stop: Arc<AtomicBool>,
    capture_thread: Option<thread::JoinHandle<()>>,
}

// The pinned native SDK reports codec MIME support and backend availability,
// not hardware support for a codec at the requested dimensions/framerate.
// Unknown AV1 acceleration must therefore use the interoperable single encode.
// Encryption belongs to the Room and never participates in codec selection.
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
    let mut startup_guard = CaptureStartupGuard {
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
    if let Err(error) = room
        .local_participant()
        .publish_track(
            LocalTrack::Video(track.clone()),
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
        )
        .await
    {
        stop.store(true, Ordering::Release);
        let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
        if let Some(worker) = audio_capture_thread {
            let _ = tokio::task::spawn_blocking(move || worker.join()).await;
        }
        return Err(error.into());
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
                let _ = room.local_participant().unpublish_track(&track.sid()).await;
                return Err(error);
            }
        }
    } else {
        None
    };
    let video = PublishedVideo {
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
        requested_variant: None,
        variant_retry: 0,
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
    let track = LocalVideoTrack::create_video_track(
        "kaede.video.v1:camera:vp8",
        RtcVideoSource::Native(source.clone()),
    );
    let stop = Arc::new(AtomicBool::new(false));
    let mut startup_guard = CaptureStartupGuard {
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
    let capture_thread = match thread::Builder::new()
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
        }) {
        Ok(thread) => thread,
        Err(error) => return Err(VoiceError::CaptureThread(error)),
    };
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
    if let Err(error) = room
        .local_participant()
        .publish_track(
            LocalTrack::Video(track.clone()),
            TrackPublishOptions {
                source: TrackSource::Camera,
                ..video_publish_options(
                    room.e2ee_manager().encryption_type() != EncryptionType::None,
                    VideoEncoding {
                        max_bitrate: settings.max_bitrate,
                        max_framerate: f64::from(settings.frame_rate),
                    },
                )
            },
        )
        .await
    {
        stop.store(true, Ordering::Release);
        let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
        return Err(error.into());
    }
    let video = PublishedVideo {
        audio: None,
        audio_capture_thread: None,
        capture_failure: failure,
        source: TrackSource::Camera,
        encoding: VideoEncoding {
            max_bitrate: settings.max_bitrate,
            max_framerate: f64::from(settings.frame_rate),
        },
        adjustment: None,
        capture_level,
        variants: Arc::new(Mutex::new(Vec::new())),
        requested_variant: None,
        variant_retry: 0,
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
    let shift = capture_level.load(Ordering::Acquire).min(3);
    let (width, height) = bounded_dimensions(
        converted.width,
        converted.height,
        (settings.width >> shift).max(320),
        (settings.height >> shift).max(180),
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
    if let Some(adjustment) = publication.adjustment.take() {
        let _ = adjustment.await;
    }
    let variants = publication
        .variants
        .lock()
        .map(|mut tracks| std::mem::take(&mut *tracks))
        .unwrap_or_default();
    for variant in variants {
        let _ = room
            .local_participant()
            .unpublish_track(&variant.sid())
            .await;
    }
    publication.stop.store(true, Ordering::Release);
    if let Some(capture_thread) = publication.capture_thread.take() {
        let _ = tokio::task::spawn_blocking(move || capture_thread.join()).await;
    }
    room.local_participant()
        .unpublish_track(&publication.track.sid())
        .await?;
    Ok(())
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
            // Text shares retain resolution until the lowest budget; motion
            // shares trade pixels before dropping below smooth playback.
            let shift = if settings.frame_rate <= 15 {
                u32::from(level == 3)
            } else {
                level
            };
            let adapted = ScreenShareSettings {
                width: (settings.width >> shift).max(640),
                height: (settings.height >> shift).max(360),
                ..settings
            };
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
                    (settings.frame_rate >> capture_level.load(Ordering::Acquire).min(2)).max(5),
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
            let shift = level.load(Ordering::Acquire).min(3);
            let adapted = ScreenShareSettings {
                width: (settings.width >> shift).max(640),
                height: (settings.height >> shift).max(360),
                ..settings
            };
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

    #[test]
    fn native_h264_requires_every_viewer_and_respects_explicit_demand() {
        let key = "publisher/camera";
        let mut peers = std::collections::BTreeMap::new();
        peers.insert(
            "viewer".to_owned(),
            super::VideoPeer {
                v: 1,
                codecs: vec!["vp8".to_owned(), "h264".to_owned()],
                demand: std::collections::BTreeMap::default(),
            },
        );
        assert_eq!(
            super::preferred_native_variant(&peers, 1, key, 0),
            Some(VideoCodec::H264)
        );
        assert_eq!(super::preferred_native_variant(&peers, 2, key, 0), None);
        if let Some(viewer) = peers.get_mut("viewer") {
            viewer.demand.insert(key.to_owned(), "vp8".to_owned());
        } else {
            panic!("viewer fixture is missing");
        }
        assert_eq!(super::preferred_native_variant(&peers, 1, key, 0), None);
        if let Some(viewer) = peers.get_mut("viewer") {
            viewer.demand.clear();
            viewer.codecs.push("av1".to_owned());
        } else {
            panic!("viewer fixture is missing");
        }
        assert_eq!(
            super::preferred_native_variant(&peers, 1, key, 0),
            Some(VideoCodec::AV1)
        );
        assert_eq!(
            super::preferred_native_variant(&peers, 1, key, 1),
            Some(VideoCodec::H264)
        );
    }

    #[test]
    fn bitrate_adaptation_is_bounded_and_does_not_overflow() {
        for (level, expected) in [(0, 2_500_000), (1, 1_625_000), (2, 1_056_250), (3, 686_562)] {
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
    fn combined_video_budget_requires_sustained_evidence_and_slow_recovery() {
        let mut budget = super::VideoBudget::default();
        assert!(!budget.observe(true, false));
        assert!(!budget.observe(false, false));
        assert!(!budget.observe(false, true));
        assert!(!budget.observe(false, true));
        assert!(budget.observe(false, true));
        assert_eq!(budget.level, 1);
        for _ in 0..5 {
            assert!(!budget.observe(false, false));
        }
        assert!(budget.observe(false, false));
        assert_eq!(budget.level, 0);
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
