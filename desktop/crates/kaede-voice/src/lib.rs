//! `LiveKit` transport backed exclusively by Kaede's `CPAL` audio graph.

use std::{
    borrow::Cow,
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
use kaede_protocol::EntityRef;
use livekit::{
    E2eeOptions,
    e2ee::{
        EncryptionType,
        key_provider::{KeyProvider, KeyProviderOptions},
    },
    options::TrackPublishOptions,
    prelude::{
        DisconnectReason, LocalAudioTrack, LocalTrack, LocalVideoTrack, RemoteTrack, Room,
        RoomEvent, RoomOptions, TrackSource,
    },
    webrtc::{
        audio_frame::AudioFrame,
        audio_source::{AudioSourceOptions, RtcAudioSource, native::NativeAudioSource},
        audio_stream::native::NativeAudioStream,
        desktop_capturer::{DesktopCaptureSourceType, DesktopCapturer, DesktopCapturerOptions},
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
use serde::{Deserialize, Serialize};
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
    pub can_use_vad: bool,
    #[serde(default)]
    pub move_session_id: Option<String>,
    #[serde(default)]
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
    SetCamera {
        enabled: bool,
        device_id: Option<String>,
    },
    SetScreenShare {
        enabled: bool,
        source_id: Option<String>,
    },
    Leave,
}

pub struct VoiceHandle {
    pub commands: mpsc::Sender<VoiceCommand>,
    pub status: watch::Receiver<VoiceStatus>,
    pub video_frames: Option<mpsc::Receiver<RemoteVideoFrame>>,
    pub input_level: Option<Arc<CaptureGate>>,
    /// Opaque broker correlation for a federated guild voice session.
    /// Replacement grants must carry the same value as the active handle.
    pub move_session_id: Option<String>,
    task: JoinHandle<()>,
}

impl VoiceHandle {
    pub async fn leave(self) {
        let _ = self.commands.send(VoiceCommand::Leave).await;
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
    capture_settings: CaptureSettings,
    output_device: Option<String>,
    media_key: Option<Vec<u8>>,
    sender_device_id: Option<&str>,
) -> Result<VoiceHandle, VoiceError> {
    let grant: VoiceGrant = api
        .post(
            &format!("channels/{channel}/voice/token"),
            &serde_json::json!({"sender_device_id": sender_device_id}),
        )
        .await?;
    Box::pin(join(grant, capture_settings, output_device, media_key)).await
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
    capture_settings: CaptureSettings,
    output_device: Option<String>,
    media_key: Option<Vec<u8>>,
    sender_device_id: Option<&str>,
) -> Result<VoiceHandle, VoiceError> {
    let grant: VoiceGrant = api
        .post(
            &format!("calls/{call}/voice/token"),
            &serde_json::json!({"sender_device_id": sender_device_id}),
        )
        .await?;
    Box::pin(join(grant, capture_settings, output_device, media_key)).await
}

async fn join(
    grant: VoiceGrant,
    capture_settings: CaptureSettings,
    output_device: Option<String>,
    media_key: Option<Vec<u8>>,
) -> Result<VoiceHandle, VoiceError> {
    let room_options = media_room_options(&grant, media_key)?;
    let move_session_id = grant.move_session_id.clone();
    if grant.can_speak
        && capture_settings.mode == kaede_audio::InputMode::VoiceActivity
        && !grant.can_use_vad
    {
        return Err(VoiceError::VoiceActivityDenied);
    }
    let (status_tx, status_rx) = watch::channel(VoiceStatus::Connecting);
    let (command_tx, command_rx) = mpsc::channel(32);
    // Video frames are intentionally lossy, but participant removal must not be.
    // A modest buffer gives several simultaneous tracks a fair chance to publish
    // without retaining a large amount of decoded RGBA data.
    let (video_tx, video_rx) = mpsc::channel(16);
    let mut processor_chain = ProcessorChain::default();
    processor_chain.push(Box::new(SpeechProcessor::from_settings(&capture_settings)));
    // Do not open the operating-system microphone when the server grant is
    // listen-only. This avoids an unnecessary privacy prompt and ensures that
    // a missing SPEAK grant cannot accidentally feed a local capture graph.
    let capture = grant
        .can_speak
        .then(|| NativeCapture::open(&capture_settings))
        .transpose()?;
    let playback = NativePlayback::open(output_device.as_deref())?;
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
        video_tx,
        processor_chain,
    ));
    Ok(VoiceHandle {
        commands: command_tx,
        status: status_rx,
        video_frames: Some(video_rx),
        input_level,
        move_session_id,
        task,
    })
}

fn media_room_options(
    grant: &VoiceGrant,
    media_key: Option<Vec<u8>>,
) -> Result<RoomOptions, VoiceError> {
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
                || grant.media_session_id.as_deref().is_none_or(|value| {
                    value.len() != 43
                        || !value
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
                })
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

#[allow(clippy::too_many_arguments, clippy::too_many_lines)]
async fn run_room(
    room: Room,
    mut events: tokio::sync::mpsc::UnboundedReceiver<RoomEvent>,
    mut commands: mpsc::Receiver<VoiceCommand>,
    status: watch::Sender<VoiceStatus>,
    capture: Option<NativeCapture>,
    playback: NativePlayback,
    source: Option<NativeAudioSource>,
    can_speak: bool,
    can_stream: bool,
    video_frames: mpsc::Sender<RemoteVideoFrame>,
    mut processor_chain: ProcessorChain,
) {
    let playback_sink = playback.sink();
    let playback_mixer = playback.mixer();
    let render_reference = playback.render_reference();
    let mut explicitly_muted = false;
    let mut deafened = false;
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
                    }
                    Some(VoiceCommand::SetDeafened(next_deafened)) => {
                        deafened = next_deafened;
                        playback.set_deafened(deafened);
                        // Match the familiar voice-client behavior: deafen also
                        // suppresses local publication while it is active.
                        if let Some(capture) = capture.as_ref() {
                            capture.gate.set_muted(explicitly_muted || deafened);
                        }
                    }
                    Some(VoiceCommand::SetPushToTalk(active)) => {
                        if let Some(capture) = capture.as_ref() { capture.gate.set_push_to_talk(active); }
                    }
                    Some(VoiceCommand::SetCamera { enabled, device_id }) => {
                        if enabled && !can_stream {
                            send_media_error(&status, &room, can_speak, can_stream, screen_share.as_ref(), camera.as_ref(),
                                "You do not have permission to use your camera in this channel.".to_owned());
                            continue;
                        }
                        let result = if enabled && camera.is_none() {
                            publish_camera(&room, device_id.as_deref()).await.map(|published| {
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
                    Some(VoiceCommand::SetScreenShare { enabled, source_id }) => {
                        if enabled && !can_stream {
                            send_media_error(&status, &room, can_speak, can_stream, screen_share.as_ref(), camera.as_ref(),
                                "You do not have permission to share your screen in this channel.".to_owned());
                            continue;
                        }
                        let result = if enabled && screen_share.is_none() {
                            publish_screen_share(&room, source_id.as_deref()).await.map(|published| {
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
                    Some(VoiceCommand::Leave) | None => break,
                }
            }
            event = events.recv() => {
                match event {
                    Some(RoomEvent::TrackSubscribed { track: RemoteTrack::Audio(track), participant, .. }) => {
                        let mixer = playback_mixer.clone();
                        let participant = participant.identity().to_string();
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
                                mixer.push(&participant, &samples, frame.sample_rate, channels);
                            }
                            mixer.remove(&participant);
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
                    Some(RoomEvent::Reconnecting) => { let _ = status.send(VoiceStatus::Reconnecting); }
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
    if let Err(error) = stop_published_video(&room, screen_share.take()).await {
        tracing::warn!(%error, "screen-share cleanup failed");
    }
    if let Err(error) = stop_published_video(&room, camera.take()).await {
        tracing::warn!(%error, "camera cleanup failed");
    }
    if let Err(error) = room.close().await {
        tracing::warn!(%error, "LiveKit room close failed");
    }
    let _ = status.send(VoiceStatus::Disconnected);
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
) -> Result<PublishedVideo, VoiceError> {
    let source = NativeVideoSource::new(
        VideoResolution {
            width: 1280,
            height: 720,
        },
        true,
    );
    let track =
        LocalVideoTrack::create_video_track("screen", RtcVideoSource::Native(source.clone()));
    room.local_participant()
        .publish_track(
            LocalTrack::Video(track.clone()),
            TrackPublishOptions {
                source: TrackSource::Screenshare,
                ..TrackPublishOptions::default()
            },
        )
        .await?;

    let stop = Arc::new(AtomicBool::new(false));
    let thread_stop = stop.clone();
    let capture_thread = thread::Builder::new()
        .name("kaede-screen-capture".to_owned())
        .spawn({
            let source_id = source_id.map(str::to_owned);
            move || run_screen_capture(source, thread_stop, source_id.as_deref())
        })
        .map_err(VoiceError::CaptureThread)?;
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
}

/// Enumerate displays and individual windows without starting capture. Some
/// macOS and Wayland sessions intentionally expose no list and instead show a
/// secure operating-system picker when sharing begins.
#[must_use]
pub fn screen_sources() -> Vec<ScreenSource> {
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
            });
        }
    }
    result.sort_by(|left, right| left.label.to_lowercase().cmp(&right.label.to_lowercase()));
    result
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
) -> Result<PublishedVideo, VoiceError> {
    let camera_id = device_id.map(str::to_owned);
    let source = NativeVideoSource::new(
        VideoResolution {
            width: 1280,
            height: 720,
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
        .spawn(move || run_camera_capture(source, thread_stop, camera_id, ready_tx))
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

fn open_camera(device_id: Option<String>) -> Result<Camera, nokhwa::NokhwaError> {
    let index = match device_id {
        Some(value) => value
            .parse::<u32>()
            .map_or_else(|_| CameraIndex::String(value), CameraIndex::Index),
        None => CameraIndex::Index(0),
    };
    let format = RequestedFormat::new::<RgbFormat>(RequestedFormatType::Closest(
        CameraFormat::new_from(1280, 720, FrameFormat::MJPEG, 30),
    ));
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
    ready: tokio::sync::oneshot::Sender<Result<(), String>>,
) {
    let mut camera = match open_camera(device_id) {
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
fn run_screen_capture(source: NativeVideoSource, stop: Arc<AtomicBool>, requested: Option<&str>) {
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
    let mut options = DesktopCapturerOptions::new(kind);
    options.set_include_cursor(true);
    let Some(mut capturer) = DesktopCapturer::new(options) else {
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
        requested_id
            .and_then(|id| sources.iter().find(|source| source.id() == id).cloned())
            .or_else(|| sources.into_iter().next())
    };

    capturer.start_capture(selected, move |result| match result {
        Ok(frame) => {
            let Ok(width) = u32::try_from(frame.width()) else {
                return;
            };
            let Ok(height) = u32::try_from(frame.height()) else {
                return;
            };
            let Some(converted) = (PackedFrame {
                width,
                height,
                stride: frame.stride() as usize,
                format: PackedPixelFormat::Bgra,
                data: frame.data(),
            })
            .to_i420() else {
                tracing::warn!(width, height, "screen capture produced an invalid frame");
                return;
            };
            let mut buffer = I420Buffer::new(converted.width, converted.height);
            let (y, u, v) = buffer.data_mut();
            y.copy_from_slice(&converted.y);
            u.copy_from_slice(&converted.u);
            v.copy_from_slice(&converted.v);
            source.capture_frame(&VideoFrame::new(VideoRotation::VideoRotation0, buffer));
        }
        Err(error) => tracing::warn!(?error, "screen capture frame failed"),
    });

    while !stop.load(Ordering::Acquire) {
        capturer.capture_frame();
        thread::sleep(Duration::from_millis(67));
    }
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
    #[error("voice activity is disabled in this channel; select push to talk")]
    VoiceActivityDenied,
    #[error("the encrypted voice grant did not match the supplied media key")]
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
            Self::VoiceActivityDenied =>
                "Voice activity is not allowed in this channel. Switch your input mode to push to talk and try again."
                    .to_owned(),
            Self::EncryptionPolicyMismatch =>
                "The encrypted call changed before Kaede could join. Refresh the conversation and try again."
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
            "This account joined voice from another client, so this connection was closed."
        }
        DisconnectReason::ServerShutdown => {
            "The voice server restarted. Wait a moment and join voice again."
        }
        DisconnectReason::ParticipantRemoved => "A moderator disconnected you from voice.",
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
    use super::{DisconnectReason, VoiceError, disconnect_message};

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
            "This account joined voice from another client, so this connection was closed."
        );
        assert!(disconnect_message(DisconnectReason::ConnectionTimeout).contains("join again"));
    }
}
