//! Settings-specific hardware probe using synthetic frames on local peers.
use std::{
    sync::atomic::{AtomicBool, Ordering},
    time::Duration,
};

use livekit::webrtc::{
    MediaType,
    media_stream_track::MediaStreamTrack,
    peer_connection::PeerConnection,
    peer_connection_factory::RtcConfiguration,
    rtp_parameters::{Priority, RtpEncodingParameters},
    rtp_sender::VideoEncoderBackend,
    rtp_transceiver::{RtpTransceiverDirection, RtpTransceiverInit},
    stats::RtcStats,
    video_frame::{I420Buffer, VideoFrame, VideoRotation},
    video_source::{RtcVideoSource, VideoResolution, native::NativeVideoSource},
};
use livekit::{options::VideoEncoding, prelude::LocalVideoTrack};

struct LocalPeers(PeerConnection, PeerConnection);
impl Drop for LocalPeers {
    fn drop(&mut self) {
        self.0.on_ice_candidate(None);
        self.1.on_ice_candidate(None);
        self.0.close();
        self.1.close();
    }
}

pub fn hardware_encoder(power_efficient: bool, implementation: &str) -> bool {
    let implementation = implementation.to_ascii_lowercase();
    power_efficient
        && [
            "externalencoder",
            "videotoolbox",
            "mediacodec",
            "nvenc",
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
    // Capture may stop or reset between scheduling and running the probe.
    // Reject unknown/out-of-policy settings before touching native resources.
    if stop.load(Ordering::Acquire)
        || !matches!(codec, "av1" | "vp8" | "h264")
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
    tokio::time::timeout(
        Duration::from_secs(4),
        probe(codec, width, height, encoding, stop),
    )
    .await
    .ok()
    .flatten()
    .unwrap_or(false)
}

async fn probe(
    codec: &str,
    width: u32,
    height: u32,
    encoding: VideoEncoding,
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
                    scalability_mode: (codec == "av1").then(|| "L1T1".to_owned()),
                    has_ssrc: false,
                    ssrc: 0,
                }],
            },
        )
        .ok()?;
    transceiver.set_codec_preferences(codecs).ok()?;
    transceiver
        .sender()
        .set_video_encoder_backend(VideoEncoderBackend::Hardware);
    let offer = peers.0.create_offer(Default::default()).await.ok()?;
    peers.0.set_local_description(offer.clone()).await.ok()?;
    peers.1.set_remote_description(offer).await.ok()?;
    let answer = peers.1.create_answer(Default::default()).await.ok()?;
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
                y.fill((samples % 200 + 20) as u8); u.fill(128); v.fill(128);
                source.capture_frame(&VideoFrame::new(VideoRotation::VideoRotation0, buffer));
                samples += 1;
                if samples >= (encoding.max_framerate * 2.0) as u32 {
                    let stats = transceiver.sender().get_stats().await.ok()?;
                    return Some(stats.into_iter().any(|stat| match stat {
                        RtcStats::OutboundRtp(stat) => hardware_encoder(stat.outbound.power_efficient_encoder, &stat.outbound.encoder_implementation)
                            && stat.outbound.frame_width >= width && stat.outbound.frame_height >= height
                            && stat.outbound.frames_encoded >= (encoding.max_framerate * 0.8) as u32
                            && stat.outbound.frames_per_second >= encoding.max_framerate * 0.8,
                        _ => false,
                    }));
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

    #[tokio::test]
    async fn invalid_or_stopped_probe_never_initializes_native_resources() {
        let stop = AtomicBool::new(false);
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
            let frame = tokio::time::timeout(Duration::from_secs(2), stream.next())
                .await
                .unwrap()
                .unwrap();
            assert_eq!((frame.buffer.width(), frame.buffer.height()), (64, 64));
        }
    }
}
