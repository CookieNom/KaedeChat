//! Settings-specific hardware probe using synthetic frames on local peers.
use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

use livekit::webrtc::{
    MediaType,
    media_stream_track::MediaStreamTrack,
    peer_connection::{AnswerOptions, OfferOptions, PeerConnection},
    peer_connection_factory::RtcConfiguration,
    rtp_parameters::{Priority, RtpEncodingParameters},
    rtp_sender::VideoEncoderBackend,
    rtp_transceiver::{RtpTransceiverDirection, RtpTransceiverInit},
    stats::RtcStats,
    video_frame::{I420Buffer, VideoFrame, VideoRotation},
    video_source::{RtcVideoSource, VideoResolution, native::NativeVideoSource},
    video_stream::native::NativeVideoStream,
};
use livekit::{options::VideoEncoding, prelude::LocalVideoTrack};

struct LocalPeers(PeerConnection, PeerConnection);
impl Drop for LocalPeers {
    fn drop(&mut self) {
        self.0.on_ice_candidate(None);
        self.1.on_ice_candidate(None);
        self.1.on_track(None);
        self.0.close();
        self.1.close();
    }
}

pub fn hardware_encoder(power_efficient: bool, implementation: &str) -> bool {
    let implementation = implementation.to_ascii_lowercase();
    power_efficient
        && [
            "externalencoder",
            "externaldecoder",
            "videotoolbox",
            "mediacodec",
            "nvenc",
            "nvdec",
            "nvidia",
            "vaapi",
            "va-api",
            "quicksync",
            "qsv",
            "amf",
            "d3d11",
            "mediafoundation",
        ]
        .iter()
        .any(|name| implementation.contains(name))
        && !["libaom", "libvpx", "openh264", "software", "fallback"]
            .iter()
            .any(|name| implementation.contains(name))
}

pub async fn hardware_codec(
    codec: &str,
    width: u32,
    height: u32,
    encoding: VideoEncoding,
    stop: &AtomicBool,
) -> bool {
    codec_supported(
        codec,
        width,
        height,
        encoding,
        matches!(codec, "av1" | "vp9" | "h265").then_some("L1T1"),
        true,
        stop,
    )
    .await
}

#[derive(Default)]
struct HevcDecoderProof {
    proven: bool,
    probing: bool,
    generation: u64,
    healthy: u8,
    healthy_at: Option<Instant>,
    backoff: crate::VideoBackoff,
}

impl HevcDecoderProof {
    fn begin(&mut self, now: Instant) -> Option<u64> {
        if self.proven || self.probing || self.backoff.blocked("h265", now) {
            return None;
        }
        self.probing = true;
        Some(self.generation)
    }

    fn complete(&mut self, generation: u64, proven: bool, now: Instant) {
        self.probing = false;
        // A live failure/new call supersedes a probe which was already running.
        if generation != self.generation {
            return;
        }
        self.proven = proven;
        if !proven {
            self.backoff.fail("h265", now);
        }
    }

    fn observe(&mut self, hardware: bool, now: Instant) {
        if hardware {
            if self.proven
                && self
                    .healthy_at
                    .is_none_or(|at| now.duration_since(at) >= crate::video_policy::SAMPLE)
            {
                self.healthy_at = Some(now);
                self.healthy = self.healthy.saturating_add(1);
                if self.healthy >= crate::video_policy::PREMIUM_TRUST_SAMPLES {
                    self.backoff.entries.remove("h265");
                }
            }
            return;
        }
        self.healthy = 0;
        self.healthy_at = None;
        if self.proven || !self.backoff.blocked("h265", now) {
            self.backoff.fail("h265", now);
            self.generation = self.generation.wrapping_add(1);
        }
        self.proven = false;
    }
}

fn hevc_decoder_proof() -> &'static Mutex<HevcDecoderProof> {
    static PROOF: OnceLock<Mutex<HevcDecoderProof>> = OnceLock::new();
    PROOF.get_or_init(|| Mutex::new(HevcDecoderProof::default()))
}

pub fn hardware_hevc_decode() -> bool {
    hevc_decoder_proof().lock().is_ok_and(|proof| proof.proven)
}

pub fn observe_hevc_decoder(power_efficient: bool, implementation: &str) {
    if let Ok(mut proof) = hevc_decoder_proof().lock() {
        proof.observe(
            hardware_encoder(power_efficient, implementation),
            Instant::now(),
        );
    }
}

/// A prerecorded frame proves the real decoder without requiring an encoder.
pub fn warm_hevc_decode() {
    if let Ok(mut proof) = hevc_decoder_proof().lock() {
        proof.proven = false;
        proof.healthy = 0;
        proof.healthy_at = None;
        proof.backoff = crate::VideoBackoff::default();
        proof.generation = proof.generation.wrapping_add(1);
    }
    retry_hevc_decode();
}

pub fn retry_hevc_decode() {
    let generation = hevc_decoder_proof()
        .lock()
        .ok()
        .and_then(|mut proof| proof.begin(Instant::now()));
    let Some(generation) = generation else {
        return;
    };
    tokio::spawn(async move {
        let proven =
            tokio::task::spawn_blocking(livekit::webrtc::native::probe_hardware_hevc_decoder)
                .await
                .unwrap_or(false);
        if let Ok(mut proof) = hevc_decoder_proof().lock() {
            proof.complete(generation, proven, Instant::now());
        }
    });
}

#[allow(clippy::too_many_arguments)]
pub async fn codec_supported(
    codec: &str,
    width: u32,
    height: u32,
    encoding: VideoEncoding,
    mode: Option<&str>,
    hardware: bool,
    stop: &AtomicBool,
) -> bool {
    // Capture may stop or reset between scheduling and running the probe.
    // Reject unknown/out-of-policy settings before touching native resources.
    if stop.load(Ordering::Acquire)
        || !matches!(codec, "av1" | "vp8" | "h264" | "h265" | "vp9")
        || (codec == "h265" && !hardware)
        || mode.is_some_and(|mode| !matches!(mode, "L1T1" | "L1T3" | "L3T3_KEY"))
        || !(1..=3840).contains(&width)
        || !(1..=2160).contains(&height)
        || !encoding.max_framerate.is_finite()
        || encoding.max_framerate <= 0.0
        || encoding.max_framerate > 60.0
        || encoding.max_bitrate == 0
        || encoding.max_bitrate > 12_000_000
    {
        return false;
    }
    // Encoder capability depends on the machine and settings, not the call:
    // reuse verdicts so restarts and variant retries skip the 4s probe.
    // Negative verdicts expire because drivers and power state change.
    let key = (
        codec.to_owned(),
        width,
        height,
        encoding.max_framerate.to_bits(),
        encoding.max_bitrate,
        mode.map(str::to_owned),
        hardware,
    );
    if let Some(cached) = cached_verdict(&key) {
        return cached;
    }
    let verdict = tokio::time::timeout(
        Duration::from_secs(4),
        probe(codec, width, height, encoding, mode, hardware, stop),
    )
    .await
    .ok()
    .flatten();
    // Timeouts and cancelled probes are unknown, not negative.
    if let Some(verdict) = verdict
        && !stop.load(Ordering::Acquire)
        && let Ok(mut cache) = probe_cache().lock()
    {
        cache.insert(key, (verdict, Instant::now()));
    }
    verdict.unwrap_or(false)
}

type ProbeKey = (String, u32, u32, u64, u64, Option<String>, bool);
const NEGATIVE_PROBE_TTL: Duration = Duration::from_mins(10);

fn probe_cache() -> &'static Mutex<BTreeMap<ProbeKey, (bool, Instant)>> {
    static CACHE: OnceLock<Mutex<BTreeMap<ProbeKey, (bool, Instant)>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(BTreeMap::new()))
}

fn cached_verdict(key: &ProbeKey) -> Option<bool> {
    let cache = probe_cache().lock().ok()?;
    let (verdict, at) = cache.get(key)?;
    (*verdict || at.elapsed() < NEGATIVE_PROBE_TTL).then_some(*verdict)
}

#[allow(clippy::too_many_lines, clippy::too_many_arguments)] // One local peer setup/probe; LocalPeers owns cleanup on every exit.
async fn probe(
    codec: &str,
    width: u32,
    height: u32,
    encoding: VideoEncoding,
    mode: Option<&str>,
    hardware: bool,
    stop: &AtomicBool,
) -> Option<bool> {
    let runtime = livekit::rtc_engine::lk_runtime::LkRuntime::instance();
    let factory = runtime.pc_factory();
    let codecs: Vec<_> = factory
        .get_rtp_sender_capabilities(MediaType::Video)
        .codecs
        .into_iter()
        .filter(|capability| {
            capability
                .mime_type
                .eq_ignore_ascii_case(&format!("video/{codec}"))
        })
        .collect();
    if codecs.is_empty() {
        return Some(false);
    }
    // No STUN/TURN or signaling service. Only synthetic local media is used.
    let peers = LocalPeers(
        factory
            .create_peer_connection(RtcConfiguration::default())
            .ok()?,
        factory
            .create_peer_connection(RtcConfiguration::default())
            .ok()?,
    );
    let decoded_stream = Arc::new(Mutex::new(None::<NativeVideoStream>));
    let stream_slot = decoded_stream.clone();
    peers.1.on_track(Some(Box::new(move |event| {
        if let MediaStreamTrack::Video(track) = event.track
            && let Ok(mut slot) = stream_slot.lock()
        {
            *slot = Some(NativeVideoStream::new(track));
        }
    })));
    let (candidate_tx, mut candidate_rx) = tokio::sync::mpsc::unbounded_channel();
    let first_tx = candidate_tx.clone();
    peers.0.on_ice_candidate(Some(Box::new(move |candidate| {
        let _ = first_tx.send((true, candidate));
    })));
    peers.1.on_ice_candidate(Some(Box::new(move |candidate| {
        let _ = candidate_tx.send((false, candidate));
    })));
    let source = NativeVideoSource::new(VideoResolution { width, height }, false);
    let track = LocalVideoTrack::create_video_track(
        "capability-probe",
        RtcVideoSource::Native(source.clone()),
    );
    let transceiver = peers
        .0
        .add_transceiver(
            MediaStreamTrack::Video(track.rtc_track()),
            RtpTransceiverInit {
                direction: RtpTransceiverDirection::SendOnly,
                stream_ids: vec![],
                send_encodings: vec![RtpEncodingParameters {
                    active: true,
                    max_bitrate: Some(encoding.max_bitrate),
                    max_framerate: Some(encoding.max_framerate),
                    priority: Priority::Low,
                    rid: String::new(),
                    scale_resolution_down_by: None,
                    scalability_mode: mode.map(str::to_owned),
                    has_ssrc: false,
                    ssrc: 0,
                }],
            },
        )
        .ok()?;
    transceiver.set_codec_preferences(codecs).ok()?;
    transceiver.sender().set_video_encoder_backend(if hardware {
        VideoEncoderBackend::Hardware
    } else {
        VideoEncoderBackend::Auto
    });
    let offer = peers.0.create_offer(OfferOptions::default()).await.ok()?;
    peers.0.set_local_description(offer.clone()).await.ok()?;
    peers.1.set_remote_description(offer).await.ok()?;
    let answer = peers.1.create_answer(AnswerOptions::default()).await.ok()?;
    peers.1.set_local_description(answer.clone()).await.ok()?;
    peers.0.set_remote_description(answer).await.ok()?;
    let mut tick = tokio::time::interval(Duration::from_secs_f64(
        1.0 / encoding.max_framerate.max(1.0),
    ));
    let mut samples = 0u32;
    loop {
        if stop.load(Ordering::Acquire) {
            return Some(false);
        }
        tokio::select! {
            Some((first, candidate)) = candidate_rx.recv() => {
                let peer = if first { &peers.1 } else { &peers.0 };
                let _ = peer.add_ice_candidate(candidate).await;
            }
            _ = tick.tick() => {
                let mut buffer = I420Buffer::new(width, height);
                let (y, u, v) = buffer.data_mut();
                y.fill(u8::try_from(samples % 200 + 20).ok()?); u.fill(128); v.fill(128);
                source.capture_frame(&VideoFrame::new(VideoRotation::VideoRotation0, buffer));
                samples += 1;
                if f64::from(samples) >= (encoding.max_framerate * 2.0).floor() {
                    let stats = transceiver.sender().get_stats().await.ok()?;
                    let exact_mode = mode.is_none_or(|mode| transceiver.sender().parameters().encodings
                        .first().and_then(|encoding| encoding.scalability_mode.as_deref()) == Some(mode));
                    let encoded = stats.into_iter().any(|stat| match stat {
                        RtcStats::OutboundRtp(stat) => (!hardware || hardware_encoder(stat.outbound.power_efficient_encoder, &stat.outbound.encoder_implementation))
                            // Missing mode evidence permits only a single layer. The SDK's legacy field name is misspelled.
                            && mode.is_none_or(|mode| stat.outbound.scalibility_mode == mode || (mode == "L1T1" && stat.outbound.scalibility_mode.is_empty()))
                            && stat.outbound.frame_width >= width && stat.outbound.frame_height >= height
                            && f64::from(stat.outbound.frames_encoded) >= (encoding.max_framerate * 0.8).floor()
                            && stat.outbound.frames_per_second >= encoding.max_framerate * 0.8,
                        _ => false,
                    });
                    if codec == "h265" {
                        for stat in peers.1.get_stats().await.ok()? {
                            if let RtcStats::InboundRtp(stat) = stat
                                && stat.inbound.frames_decoded > 0
                            {
                                observe_hevc_decoder(stat.inbound.power_efficient_decoder, &stat.inbound.decoder_implementation);
                            }
                        }
                    }
                    return Some(encoded && exact_mode);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use futures_util::StreamExt;
    use livekit::webrtc::video_stream::native::NativeVideoStream;

    #[test]
    fn hardware_evidence_rejects_unknown_and_software_fallbacks() {
        assert!(hardware_encoder(true, "NVENC"));
        assert!(hardware_encoder(true, "NVIDIA AV1 Encoder"));
        assert!(!hardware_encoder(true, "NVENC software fallback"));
        assert!(!hardware_encoder(false, "VideoToolbox"));
        assert!(!hardware_encoder(true, ""));
    }

    #[test]
    fn hevc_recovery_is_bounded_and_late_probe_cannot_override_live_failure() {
        let mut proof = HevcDecoderProof::default();
        let now = Instant::now();
        let generation = proof.begin(now).unwrap();
        assert!(proof.begin(now).is_none());
        proof.observe(false, now);
        proof.complete(generation, true, now);
        assert!(!proof.proven);
        assert!(proof.begin(now + Duration::from_secs(59)).is_none());
        let retry = now + Duration::from_mins(1);
        let generation = proof.begin(retry).unwrap();
        proof.complete(generation, true, retry);
        assert!(proof.proven);
        proof.observe(false, retry);
        assert!(proof.begin(retry + Duration::from_mins(4)).is_none());
        let retry = retry + Duration::from_mins(5);
        let generation = proof.begin(retry).unwrap();
        proof.complete(generation, true, retry);
        for sample in 0..crate::video_policy::PREMIUM_TRUST_SAMPLES {
            proof.observe(
                true,
                retry + crate::video_policy::SAMPLE * u32::from(sample),
            );
        }
        let trusted = retry
            + crate::video_policy::SAMPLE * u32::from(crate::video_policy::PREMIUM_TRUST_SAMPLES);
        proof.observe(false, trusted);
        assert!(proof.begin(trusted + Duration::from_mins(1)).is_some());
    }

    #[tokio::test]
    async fn invalid_or_stopped_probe_never_initializes_native_resources() {
        let stop = AtomicBool::new(false);
        for (codec, mode, hardware) in [
            ("unknown", None, true),
            ("h265", None, false),
            ("av1", Some("L9T9"), true),
        ] {
            assert!(
                !codec_supported(
                    codec,
                    640,
                    360,
                    VideoEncoding {
                        max_bitrate: 600_000,
                        max_framerate: 15.0
                    },
                    mode,
                    hardware,
                    &stop
                )
                .await
            );
        }
        for (width, height, fps, bitrate) in [
            (0, 720, 30.0, 1_000_000),
            (1280, 0, 30.0, 1_000_000),
            (u32::MAX, 720, 30.0, 1_000_000),
            (1280, 720, f64::NAN, 1_000_000),
            (1280, 720, f64::INFINITY, 1_000_000),
            (1280, 720, 0.0, 1_000_000),
            (1280, 720, -1.0, 1_000_000),
            (1280, 720, 30.0, 0),
        ] {
            assert!(
                !hardware_codec(
                    "av1",
                    width,
                    height,
                    VideoEncoding {
                        max_bitrate: bitrate,
                        max_framerate: fps
                    },
                    &stop
                )
                .await
            );
        }
        stop.store(true, Ordering::Release);
        assert!(
            !hardware_codec(
                "av1",
                1280,
                720,
                VideoEncoding {
                    max_bitrate: 1_000_000,
                    max_framerate: 30.0
                },
                &stop
            )
            .await
        );
    }

    #[tokio::test]
    async fn distinct_native_tracks_share_one_capture_source() {
        let source = NativeVideoSource::new(
            VideoResolution {
                width: 64,
                height: 64,
            },
            false,
        );
        let first = LocalVideoTrack::create_video_track(
            "kaede.video.v1:camera:vp8",
            RtcVideoSource::Native(source.clone()),
        );
        let second = LocalVideoTrack::create_video_track(
            "kaede.video.v1:camera:av1",
            RtcVideoSource::Native(source.clone()),
        );
        assert_ne!(first.rtc_track().id(), second.rtc_track().id());
        let mut first_frames = NativeVideoStream::new(first.rtc_track());
        let mut second_frames = NativeVideoStream::new(second.rtc_track());
        source.capture_frame(&VideoFrame::new(
            VideoRotation::VideoRotation0,
            I420Buffer::new(64, 64),
        ));
        for stream in [&mut first_frames, &mut second_frames] {
            let Ok(Some(frame)) = tokio::time::timeout(Duration::from_secs(2), stream.next()).await
            else {
                panic!("both tracks must receive the shared source frame within two seconds");
            };
            assert_eq!((frame.buffer.width(), frame.buffer.height()), (64, 64));
        }
    }
}
