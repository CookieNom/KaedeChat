use super::{
    Arc, AtomicBool, AudioEncoding, AudioFrame, AudioSourceOptions, Cow, Duration, JoinHandle,
    LocalAudioTrack, LocalTrack, Mutex, NativeAudioSource, Ordering, Room, RtcAudioSource,
    TrackPublishOptions, TrackSource, VoiceError, mpsc, time,
};
#[cfg(not(target_os = "macos"))]
use super::{ScreenShareSettings, thread};
#[cfg(not(target_os = "macos"))]
use kaede_capture::system_audio::{self, CaptureOptions};

pub(super) type CaptureFailure = Arc<Mutex<Option<String>>>;

pub(super) fn fail(stop: &AtomicBool, failure: &CaptureFailure, message: String) {
    if let Ok(mut current) = failure.lock() {
        *current = Some(message);
    }
    stop.store(true, Ordering::Release);
}

#[cfg(not(target_os = "macos"))]
pub(super) async fn start_capture(
    source_id: Option<&str>,
    settings: ScreenShareSettings,
    sender: mpsc::Sender<Vec<i16>>,
    stop: Arc<AtomicBool>,
    failure: CaptureFailure,
) -> Result<thread::JoinHandle<()>, VoiceError> {
    let window = source_id
        .and_then(|id| id.strip_prefix("window:"))
        .and_then(|id| id.parse::<u64>().ok());
    let process = if window.is_some() {
        u32::MAX
    } else {
        settings.audio_process.unwrap_or(0)
    };
    #[cfg(target_os = "linux")]
    if super::is_wayland_session() && settings.audio_process.is_none() {
        return Err(VoiceError::ScreenAudio(
            "Choose computer audio or an app in the audio chooser before sharing.".to_owned(),
        ));
    }
    let options = CaptureOptions {
        window: window.unwrap_or(0),
        process,
        width: settings.width,
        height: settings.height,
        fps: settings.frame_rate,
        audio: true,
    };
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    let thread_stop = stop.clone();
    let worker = thread::Builder::new()
        .name("kaede-screen-audio".to_owned())
        .spawn(move || {
            let ready = Mutex::new(Some(ready_tx));
            system_audio::run(
                options,
                &|| thread_stop.load(Ordering::Acquire),
                &|samples| {
                    let _ = sender.try_send(samples.to_vec());
                },
                &|_| {},
                &|result| {
                    if let Err(message) = &result {
                        fail(&thread_stop, &failure, message.clone());
                    }
                    if let Ok(mut ready) = ready.lock()
                        && let Some(ready) = ready.take()
                    {
                        let _ = ready.send(result);
                    }
                },
            );
        })
        .map_err(VoiceError::CaptureThread)?;
    let result = match time::timeout(Duration::from_secs(15), ready_rx).await {
        Ok(Ok(result)) => result,
        _ => Err("Screen audio did not start. Share without audio or try again.".to_owned()),
    };
    if let Err(message) = result {
        stop.store(true, Ordering::Release);
        let _ = tokio::task::spawn_blocking(move || worker.join()).await;
        return Err(VoiceError::ScreenAudio(message));
    }
    Ok(worker)
}

pub(super) struct PublishedAudio {
    pub track: LocalAudioTrack,
    pub publisher: JoinHandle<()>,
}

pub(super) async fn publish(
    room: &Room,
    mut receiver: mpsc::Receiver<Vec<i16>>,
    stop: Arc<AtomicBool>,
    failure: CaptureFailure,
) -> Result<PublishedAudio, VoiceError> {
    // Music/game audio bypasses microphone VAD, denoising, AGC and mic mute.
    let source = NativeAudioSource::new(
        AudioSourceOptions {
            echo_cancellation: false,
            noise_suppression: false,
            auto_gain_control: false,
        },
        48_000,
        2,
        100,
    );
    let track =
        LocalAudioTrack::create_audio_track("screen_audio", RtcAudioSource::Native(source.clone()));
    room.local_participant()
        .publish_track(
            LocalTrack::Audio(track.clone()),
            TrackPublishOptions {
                source: TrackSource::ScreenshareAudio,
                dtx: false,
                audio_encoding: Some(AudioEncoding {
                    max_bitrate: 128_000,
                }),
                ..Default::default()
            },
        )
        .await?;
    let publisher = tokio::spawn(async move {
        let mut pending = Vec::with_capacity(960);
        while let Some(samples) = receiver.recv().await {
            if stop.load(Ordering::Acquire) {
                break;
            }
            for sample in samples {
                pending.push(sample);
                if pending.len() != 960 {
                    continue;
                }
                let frame = AudioFrame {
                    data: Cow::Borrowed(&pending),
                    sample_rate: 48_000,
                    num_channels: 2,
                    samples_per_channel: 480,
                };
                if !matches!(
                    time::timeout(Duration::from_millis(200), source.capture_frame(&frame)).await,
                    Ok(Ok(()))
                ) {
                    fail(
                        &stop,
                        &failure,
                        "Screen audio stopped responding. Start sharing again.".to_owned(),
                    );
                    return;
                }
                pending.clear();
            }
        }
    });
    Ok(PublishedAudio { track, publisher })
}
