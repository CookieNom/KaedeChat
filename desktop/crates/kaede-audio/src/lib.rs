#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_precision_loss,
    clippy::cast_sign_loss
)]
//! Native device capture/playback and the desktop DSP boundary.
//!
//! CPAL owns the devices. `LiveKit` receives and produces PCM through bounded
//! queues, so it never opens a second microphone or speaker behind our back.
//!
//! The explicitly bounded casts in this module convert clamped PCM samples and
//! frame indices between the scalar types required by native audio APIs.

use std::{
    collections::{HashMap, HashSet, VecDeque},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU32, Ordering},
        mpsc::{Receiver, SyncSender, TrySendError, sync_channel},
    },
    thread::JoinHandle,
    time::Duration,
};

use aec3::{nodes::audio::AudioFormat, pipelines::linear};
use cpal::{
    Device, SampleFormat, Stream, StreamConfig,
    traits::{DeviceTrait, HostTrait, StreamTrait},
};
use crossbeam_queue::ArrayQueue;
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const VOICE_SAMPLE_RATE: u32 = 48_000;
pub const VOICE_CHANNELS: u16 = 1;
const QUEUE_SECONDS: usize = 2;
const VAD_HANGOVER_FRAMES: u32 = 28;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InputMode {
    #[default]
    VoiceActivity,
    PushToTalk,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct AudioDevice {
    pub id: String,
    pub label: String,
    pub is_default: bool,
    pub channels: u16,
    pub sample_rate: u32,
}

#[derive(Clone, Debug)]
pub struct CaptureSettings {
    pub device_id: Option<String>,
    pub mode: InputMode,
    pub vad_threshold: f32,
    pub noise_suppression: NoiseSuppression,
    pub echo_cancellation: bool,
    pub automatic_gain_control: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NoiseSuppression {
    Off,
    #[default]
    Standard,
    VoiceIsolation,
}

impl Default for CaptureSettings {
    fn default() -> Self {
        Self {
            device_id: None,
            mode: InputMode::VoiceActivity,
            vad_threshold: 0.015,
            noise_suppression: NoiseSuppression::Standard,
            echo_cancellation: true,
            automatic_gain_control: true,
        }
    }
}

#[derive(Default)]
pub struct CaptureGate {
    muted: AtomicBool,
    push_to_talk: AtomicBool,
    priority_push_to_talk: AtomicBool,
    use_push_to_talk: AtomicBool,
    vad_threshold_bits: AtomicU32,
    level_bits: AtomicU32,
    vad_hangover: AtomicU32,
}

impl CaptureGate {
    #[must_use]
    pub fn new(settings: &CaptureSettings) -> Self {
        let gate = Self::default();
        gate.set_mode(settings.mode);
        gate.set_vad_threshold(settings.vad_threshold);
        gate
    }

    pub fn set_muted(&self, muted: bool) {
        self.muted.store(muted, Ordering::Release);
    }
    pub fn set_push_to_talk(&self, active: bool) {
        self.push_to_talk.store(active, Ordering::Release);
    }
    pub fn set_priority_push_to_talk(&self, active: bool) {
        self.priority_push_to_talk.store(active, Ordering::Release);
    }
    pub fn set_mode(&self, mode: InputMode) {
        self.use_push_to_talk
            .store(mode == InputMode::PushToTalk, Ordering::Release);
    }
    pub fn set_vad_threshold(&self, threshold: f32) {
        self.vad_threshold_bits
            .store(threshold.clamp(0.0, 1.0).to_bits(), Ordering::Release);
    }
    #[must_use]
    pub fn level(&self) -> f32 {
        f32::from_bits(self.level_bits.load(Ordering::Acquire))
    }

    fn permits(&self, level: f32) -> bool {
        self.level_bits.store(level.to_bits(), Ordering::Release);
        if self.muted.load(Ordering::Acquire) {
            return false;
        }
        if self.use_push_to_talk.load(Ordering::Acquire) {
            self.push_to_talk.load(Ordering::Acquire)
                || self.priority_push_to_talk.load(Ordering::Acquire)
        } else {
            let threshold = f32::from_bits(self.vad_threshold_bits.load(Ordering::Acquire));
            if level >= threshold {
                self.vad_hangover
                    .store(VAD_HANGOVER_FRAMES, Ordering::Release);
                true
            } else {
                self.vad_hangover
                    .fetch_update(Ordering::AcqRel, Ordering::Acquire, |remaining| {
                        remaining.checked_sub(1)
                    })
                    .is_ok()
            }
        }
    }
}

/// A frame processor runs off the realtime callback. Implementations for AEC,
/// noise suppression and AGC can be inserted without changing capture or UI.
pub trait AudioProcessor: Send {
    /// Supply the remote mix that is about to reach the selected speaker. AEC
    /// implementations use this as their render reference; processors that do
    /// not need it can keep the default no-op implementation.
    fn observe_render(&mut self, _interleaved_mono: &[f32], _sample_rate: u32) {}

    fn process(&mut self, interleaved_mono: &mut [f32], sample_rate: u32);
}

#[derive(Default)]
pub struct ProcessorChain {
    processors: Vec<Box<dyn AudioProcessor>>,
}

impl ProcessorChain {
    pub fn push(&mut self, processor: Box<dyn AudioProcessor>) {
        self.processors.push(processor);
    }
    pub fn process(&mut self, samples: &mut [f32], sample_rate: u32) {
        for processor in &mut self.processors {
            processor.process(samples, sample_rate);
        }
    }

    pub fn observe_render(&mut self, samples: &[f32], sample_rate: u32) {
        for processor in &mut self.processors {
            processor.observe_render(samples, sample_rate);
        }
    }
}

/// Low-latency desktop speech processing. This implementation intentionally
/// lives behind [`AudioProcessor`], so a pinned neural denoiser can replace the
/// isolation stage without changing CPAL or `LiveKit` ownership.
struct SpeechProcessorCore {
    noise_suppression: NoiseSuppression,
    echo_cancellation: bool,
    automatic_gain_control: bool,
    render: Vec<f32>,
    dc_previous_input: f32,
    dc_previous_output: f32,
    gain: f32,
    standard: Option<linear::LinearPipeline>,
    isolation: Option<Box<nnnoiseless::DenoiseState<'static>>>,
    isolation_warmed: bool,
}

impl SpeechProcessorCore {
    #[must_use]
    pub fn from_settings(settings: &CaptureSettings) -> Self {
        let format = AudioFormat::ten_ms(VOICE_SAMPLE_RATE, VOICE_CHANNELS);
        let standard = (settings.echo_cancellation
            || settings.noise_suppression == NoiseSuppression::Standard)
            .then(|| {
                linear::builder(format, format)
                    .initial_delay_ms(80)
                    .build()
                    .ok()
            })
            .flatten();
        Self {
            noise_suppression: settings.noise_suppression,
            echo_cancellation: settings.echo_cancellation,
            automatic_gain_control: settings.automatic_gain_control,
            render: Vec::new(),
            dc_previous_input: 0.0,
            dc_previous_output: 0.0,
            gain: 1.0,
            standard,
            isolation: (settings.noise_suppression == NoiseSuppression::VoiceIsolation)
                .then(nnnoiseless::DenoiseState::new),
            isolation_warmed: false,
        }
    }
}

impl SpeechProcessorCore {
    fn observe_render(&mut self, samples: &[f32], _sample_rate: u32) {
        self.render.clear();
        self.render.extend_from_slice(samples);
        if samples.len() == nnnoiseless::DenoiseState::FRAME_SIZE
            && let Some(pipeline) = &mut self.standard
            && let Err(error) = pipeline.handle_render_frame(samples)
        {
            tracing::debug!(%error, "native echo render frame was rejected");
        }
    }

    fn process(&mut self, samples: &mut [f32], sample_rate: u32) {
        if sample_rate == VOICE_SAMPLE_RATE
            && samples.len() == nnnoiseless::DenoiseState::FRAME_SIZE
            && let Some(pipeline) = &mut self.standard
        {
            let capture = samples.to_vec();
            if let Ok(true) = pipeline.process_capture_frame(&capture, samples) {
                self.apply_voice_isolation(samples);
                return;
            }
        }

        // DC blocking is cheap and stable enough to run on every 10 ms frame.
        for sample in samples.iter_mut() {
            let output = *sample - self.dc_previous_input + 0.995 * self.dc_previous_output;
            self.dc_previous_input = *sample;
            self.dc_previous_output = output;
            *sample = output;
        }

        if self.echo_cancellation && self.render.len() == samples.len() {
            let capture_energy = samples.iter().map(|value| value * value).sum::<f32>();
            let render_energy = self.render.iter().map(|value| value * value).sum::<f32>();
            let correlation = samples
                .iter()
                .zip(&self.render)
                .map(|(capture, render)| capture * render)
                .sum::<f32>();
            let normalized = correlation.abs()
                / (capture_energy.sqrt() * render_energy.sqrt()).max(f32::EPSILON);
            if render_energy > 0.000_01 && normalized > 0.45 {
                let attenuation = (1.0 - normalized * 0.7).clamp(0.2, 1.0);
                for sample in samples.iter_mut() {
                    *sample *= attenuation;
                }
            }
        }

        let floor = match self.noise_suppression {
            NoiseSuppression::Off => 0.0,
            NoiseSuppression::Standard => 0.004,
            NoiseSuppression::VoiceIsolation => 0.009,
        };
        if floor > 0.0 {
            for sample in samples.iter_mut() {
                let magnitude = sample.abs();
                if magnitude < floor {
                    *sample *= (magnitude / floor).powi(2);
                }
            }
        }

        if self.automatic_gain_control {
            let rms = (samples.iter().map(|value| value * value).sum::<f32>()
                / samples.len().max(1) as f32)
                .sqrt();
            let target = if rms > 0.001 {
                (0.12 / rms).clamp(0.5, 4.0)
            } else {
                1.0
            };
            self.gain = self.gain * 0.92 + target * 0.08;
            for sample in samples.iter_mut() {
                *sample = (*sample * self.gain).clamp(-0.98, 0.98);
            }
        }
        self.apply_voice_isolation(samples);
    }
}

impl SpeechProcessorCore {
    fn apply_voice_isolation(&mut self, samples: &mut [f32]) {
        let Some(denoiser) = &mut self.isolation else {
            return;
        };
        if samples.len() != nnnoiseless::DenoiseState::FRAME_SIZE {
            return;
        }
        let input = samples
            .iter()
            .map(|sample| sample.clamp(-1.0, 1.0) * 32_767.0)
            .collect::<Vec<_>>();
        let mut output = vec![0.0; nnnoiseless::DenoiseState::FRAME_SIZE];
        denoiser.process_frame(&mut output, &input);
        if self.isolation_warmed {
            for (sample, denoised) in samples.iter_mut().zip(output) {
                *sample = (denoised / 32_767.0).clamp(-1.0, 1.0);
            }
        } else {
            // RNNoise's first synthesized frame contains a documented fade-in.
            // Keep the already processed frame once, while still warming state.
            self.isolation_warmed = true;
        }
    }
}

enum ProcessorCommand {
    Render(Vec<f32>, u32),
    Capture(Vec<f32>, u32, SyncSender<Vec<f32>>),
}

/// Send-safe handle for the native DSP worker.
///
/// AEC3 owns a graph containing thread-affine nodes, so the graph is created,
/// used, and destroyed on one dedicated thread. The async room loop only
/// exchanges bounded 10 ms frames with that worker; no unsafe `Send` shim is
/// used and the CPAL callbacks remain limited to lock-free queue operations.
pub struct SpeechProcessor {
    commands: Option<SyncSender<ProcessorCommand>>,
    worker: Option<JoinHandle<()>>,
}

impl SpeechProcessor {
    #[must_use]
    pub fn from_settings(settings: &CaptureSettings) -> Self {
        let settings = settings.clone();
        let (commands, receiver) = sync_channel(4);
        let worker = std::thread::Builder::new()
            .name("kaede-speech-dsp".to_owned())
            .spawn(move || run_speech_processor(&receiver, &settings))
            .ok();
        Self {
            commands: worker.as_ref().map(|_| commands),
            worker,
        }
    }
}

fn run_speech_processor(receiver: &Receiver<ProcessorCommand>, settings: &CaptureSettings) {
    let mut processor = SpeechProcessorCore::from_settings(settings);
    while let Ok(command) = receiver.recv() {
        match command {
            ProcessorCommand::Render(samples, sample_rate) => {
                processor.observe_render(&samples, sample_rate);
            }
            ProcessorCommand::Capture(mut samples, sample_rate, reply) => {
                processor.process(&mut samples, sample_rate);
                let _ = reply.try_send(samples);
            }
        }
    }
}

impl AudioProcessor for SpeechProcessor {
    fn observe_render(&mut self, samples: &[f32], sample_rate: u32) {
        let Some(commands) = &self.commands else {
            return;
        };
        match commands.try_send(ProcessorCommand::Render(samples.to_vec(), sample_rate)) {
            Ok(()) | Err(TrySendError::Full(_)) => {}
            Err(TrySendError::Disconnected(_)) => self.commands = None,
        }
    }

    fn process(&mut self, samples: &mut [f32], sample_rate: u32) {
        let Some(commands) = &self.commands else {
            samples.fill(0.0);
            return;
        };
        let (reply, processed) = sync_channel(1);
        match commands.try_send(ProcessorCommand::Capture(
            samples.to_vec(),
            sample_rate,
            reply,
        )) {
            Ok(()) => match processed.recv_timeout(Duration::from_millis(8)) {
                Ok(output) if output.len() == samples.len() => samples.copy_from_slice(&output),
                Ok(_) | Err(_) => samples.fill(0.0),
            },
            Err(TrySendError::Full(_)) => samples.fill(0.0),
            Err(TrySendError::Disconnected(_)) => {
                self.commands = None;
                samples.fill(0.0);
            }
        }
    }
}

impl Drop for SpeechProcessor {
    fn drop(&mut self) {
        self.commands.take();
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

pub struct NativeCapture {
    _stream: Stream,
    queue: Arc<ArrayQueue<f32>>,
    pub gate: Arc<CaptureGate>,
    source_rate: u32,
}

impl NativeCapture {
    /// Opens the selected native input and starts its bounded capture stream.
    ///
    /// # Errors
    ///
    /// Returns [`AudioError`] when the device is absent, its format is not
    /// supported, or the platform audio backend cannot create the stream.
    pub fn open(settings: &CaptureSettings) -> Result<Self, AudioError> {
        let host = cpal::default_host();
        let device = select_input(&host, settings.device_id.as_deref())?;
        let supported = device.default_input_config()?;
        let format = supported.sample_format();
        let config = supported.config();
        let source_rate = config.sample_rate;
        let channels = config.channels as usize;
        let queue = Arc::new(ArrayQueue::new(source_rate as usize * QUEUE_SECONDS));
        let gate = Arc::new(CaptureGate::new(settings));
        let error_callback = |error| tracing::error!(%error, "native input stream failed");
        let stream = match format {
            SampleFormat::F32 => {
                build_input::<f32>(&device, config, channels, queue.clone(), error_callback)?
            }
            SampleFormat::I16 => {
                build_input::<i16>(&device, config, channels, queue.clone(), error_callback)?
            }
            SampleFormat::U16 => {
                build_input::<u16>(&device, config, channels, queue.clone(), error_callback)?
            }
            other => return Err(AudioError::UnsupportedFormat(other)),
        };
        stream.play()?;
        Ok(Self {
            _stream: stream,
            queue,
            gate,
            source_rate,
        })
    }

    #[must_use]
    pub fn source_rate(&self) -> u32 {
        self.source_rate
    }

    /// Drain approximately `duration` of mono PCM, resampled to 48 kHz.
    pub fn drain_voice_frame(
        &self,
        duration: Duration,
        processors: &mut ProcessorChain,
    ) -> Vec<f32> {
        let wanted_source = (f64::from(self.source_rate) * duration.as_secs_f64()).round() as usize;
        let mut source = Vec::with_capacity(wanted_source);
        for _ in 0..wanted_source {
            source.push(self.queue.pop().unwrap_or(0.0));
        }
        let mut output = resample_linear(&source, self.source_rate, VOICE_SAMPLE_RATE);
        processors.process(&mut output, VOICE_SAMPLE_RATE);
        let peak = output
            .iter()
            .fold(0.0_f32, |current, sample| current.max(sample.abs()));
        if !self.gate.permits(peak) {
            output.fill(0.0);
        }
        output
    }
}

pub struct NativePlayback {
    _stream: Stream,
    queue: Arc<ArrayQueue<f32>>,
    deafened: Arc<AtomicBool>,
    sink_rate: u32,
    sink_channels: u16,
    render_reference: RenderReference,
}

#[derive(Clone)]
pub struct PlaybackSink {
    queue: Arc<ArrayQueue<f32>>,
    deafened: Arc<AtomicBool>,
    sink_rate: u32,
    sink_channels: u16,
    render_reference: RenderReference,
}

/// Per-participant bounded queues feeding a single deterministic speaker mix.
///
/// Remote tracks must never write directly into one FIFO: that serializes
/// participants instead of mixing them. This mixer also produces the exact
/// far-end signal supplied to echo cancellation.
#[derive(Default)]
struct VoiceMixState {
    tracks: HashMap<VoiceMixKey, VoiceMixQueue>,
    priority_capable: HashSet<String>,
    priority_active: HashSet<String>,
}

#[derive(Eq, Hash, PartialEq)]
struct VoiceMixKey {
    participant: String,
    track: String,
}

struct VoiceMixQueue {
    priority_eligible: bool,
    samples: VecDeque<f32>,
}

#[derive(Clone, Default)]
pub struct VoiceMixer {
    state: Arc<Mutex<VoiceMixState>>,
}

impl VoiceMixer {
    pub fn push(&self, participant: &str, samples: &[f32], source_rate: u32, source_channels: u16) {
        self.push_track(
            participant,
            participant,
            true,
            samples,
            source_rate,
            source_channels,
        );
    }

    pub fn push_track(
        &self,
        participant: &str,
        track: &str,
        priority_eligible: bool,
        samples: &[f32],
        source_rate: u32,
        source_channels: u16,
    ) {
        let mono = downmix(samples, usize::from(source_channels));
        let resampled = resample_linear(&mono, source_rate, VOICE_SAMPLE_RATE);
        let mut state = self.state.lock();
        let queue = register_voice_mix_track(&mut state, participant, track, priority_eligible);
        queue.priority_eligible = priority_eligible;
        queue.samples.extend(resampled);
        let maximum = VOICE_SAMPLE_RATE as usize * QUEUE_SECONDS;
        if queue.samples.len() > maximum {
            queue.samples.drain(..queue.samples.len() - maximum);
        }
    }

    pub fn register_track(&self, participant: &str, track: &str, priority_eligible: bool) {
        register_voice_mix_track(
            &mut self.state.lock(),
            participant,
            track,
            priority_eligible,
        );
    }

    pub fn set_priority_capability(&self, participant: &str, capable: bool) {
        let mut state = self.state.lock();
        if capable {
            state.priority_capable.insert(participant.to_owned());
        } else {
            state.priority_capable.remove(participant);
            state.priority_active.remove(participant);
        }
    }

    #[must_use]
    pub fn set_priority_active(&self, participant: &str, active: bool) -> bool {
        let mut state = self.state.lock();
        if active && !state.priority_capable.contains(participant) {
            return false;
        }
        if active {
            state.priority_active.insert(participant.to_owned());
        } else {
            state.priority_active.remove(participant);
        }
        true
    }

    pub fn clear_priority_active(&self) {
        self.state.lock().priority_active.clear();
    }

    pub fn remove(&self, participant: &str) {
        self.remove_participant(participant);
    }

    #[must_use]
    pub fn remove_track(&self, participant: &str, track: &str) -> bool {
        let mut state = self.state.lock();
        state.tracks.remove(&VoiceMixKey {
            participant: participant.to_owned(),
            track: track.to_owned(),
        });
        state
            .tracks
            .iter()
            .any(|(key, queue)| key.participant == participant && queue.priority_eligible)
    }

    pub fn remove_participant(&self, participant: &str) {
        let mut state = self.state.lock();
        state
            .tracks
            .retain(|key, _queue| key.participant != participant);
        state.priority_capable.remove(participant);
        state.priority_active.remove(participant);
    }

    #[must_use]
    pub fn drain(&self, samples: usize) -> Vec<f32> {
        let mut state = self.state.lock();
        let VoiceMixState {
            tracks,
            priority_capable,
            priority_active,
        } = &mut *state;
        let mut mixed = vec![0.0; samples];
        for sample in &mut mixed {
            let active = tracks
                .values()
                .filter(|queue| !queue.samples.is_empty())
                .count();
            let normalization = if active > 1 {
                1.0 / (active as f32).sqrt()
            } else {
                1.0
            };
            let priority_audio = tracks.iter().any(|(key, queue)| {
                queue.priority_eligible
                    && !queue.samples.is_empty()
                    && priority_active.contains(&key.participant)
                    && priority_capable.contains(&key.participant)
            });
            for (key, queue) in tracks.iter_mut() {
                let attenuation = if priority_audio
                    && !(queue.priority_eligible
                        && priority_active.contains(&key.participant)
                        && priority_capable.contains(&key.participant))
                {
                    0.2
                } else {
                    1.0
                };
                *sample += queue.samples.pop_front().unwrap_or(0.0) * normalization * attenuation;
            }
            *sample = sample.clamp(-1.0, 1.0);
        }
        mixed
    }
}

fn register_voice_mix_track<'a>(
    state: &'a mut VoiceMixState,
    participant: &str,
    track: &str,
    priority_eligible: bool,
) -> &'a mut VoiceMixQueue {
    state
        .tracks
        .entry(VoiceMixKey {
            participant: participant.to_owned(),
            track: track.to_owned(),
        })
        .or_insert_with(|| VoiceMixQueue {
            priority_eligible,
            samples: VecDeque::new(),
        })
}

/// Bounded copy of the mono speaker mix used only by capture-side DSP. It
/// cannot block either the `LiveKit` receive task or the realtime CPAL callback.
#[derive(Clone)]
pub struct RenderReference {
    queue: Arc<ArrayQueue<f32>>,
}

impl NativePlayback {
    /// Opens the selected native output and starts its bounded playback stream.
    ///
    /// # Errors
    ///
    /// Returns [`AudioError`] when the device is absent, its format is not
    /// supported, or the platform audio backend cannot create the stream.
    pub fn open(device_id: Option<&str>) -> Result<Self, AudioError> {
        let host = cpal::default_host();
        let device = select_output(&host, device_id)?;
        let supported = device.default_output_config()?;
        let format = supported.sample_format();
        let config = supported.config();
        let sink_rate = config.sample_rate;
        let sink_channels = config.channels;
        let queue = Arc::new(ArrayQueue::new(
            sink_rate as usize * sink_channels as usize * QUEUE_SECONDS,
        ));
        let deafened = Arc::new(AtomicBool::new(false));
        let render_reference = RenderReference {
            queue: Arc::new(ArrayQueue::new(VOICE_SAMPLE_RATE as usize * QUEUE_SECONDS)),
        };
        let error_callback = |error| tracing::error!(%error, "native output stream failed");
        let stream = match format {
            SampleFormat::F32 => {
                build_output::<f32>(&device, config, queue.clone(), error_callback)?
            }
            SampleFormat::I16 => {
                build_output::<i16>(&device, config, queue.clone(), error_callback)?
            }
            SampleFormat::U16 => {
                build_output::<u16>(&device, config, queue.clone(), error_callback)?
            }
            other => return Err(AudioError::UnsupportedFormat(other)),
        };
        stream.play()?;
        Ok(Self {
            _stream: stream,
            queue,
            deafened,
            sink_rate,
            sink_channels,
            render_reference,
        })
    }

    /// Stop or resume all remote playback. Enabling deafen also empties queued
    /// PCM so audio captured before the action cannot leak out afterwards.
    pub fn set_deafened(&self, deafened: bool) {
        self.deafened.store(deafened, Ordering::Release);
        if deafened {
            while self.queue.pop().is_some() {}
        }
    }

    pub fn push_voice_frame(&self, samples: &[f32], source_rate: u32, source_channels: u16) {
        self.sink()
            .push_voice_frame(samples, source_rate, source_channels);
    }

    #[must_use]
    pub fn sink(&self) -> PlaybackSink {
        PlaybackSink {
            queue: self.queue.clone(),
            deafened: self.deafened.clone(),
            sink_rate: self.sink_rate,
            sink_channels: self.sink_channels,
            render_reference: self.render_reference.clone(),
        }
    }

    #[must_use]
    pub fn render_reference(&self) -> RenderReference {
        self.render_reference.clone()
    }

    #[must_use]
    pub fn mixer(&self) -> VoiceMixer {
        VoiceMixer::default()
    }
}

impl PlaybackSink {
    pub fn push_voice_frame(&self, samples: &[f32], source_rate: u32, source_channels: u16) {
        if self.deafened.load(Ordering::Acquire) {
            return;
        }
        let mono = downmix(samples, source_channels as usize);
        let resampled = resample_linear(&mono, source_rate, self.sink_rate);
        let render = resample_linear(&mono, source_rate, VOICE_SAMPLE_RATE);
        self.render_reference.push(&render);
        for sample in resampled {
            for _ in 0..self.sink_channels {
                let _ = self.queue.push(sample.clamp(-1.0, 1.0));
            }
        }
    }
}

impl RenderReference {
    fn push(&self, samples: &[f32]) {
        for sample in samples {
            if self.queue.push(*sample).is_err() {
                let _ = self.queue.pop();
                let _ = self.queue.push(*sample);
            }
        }
    }

    #[must_use]
    pub fn drain(&self, samples: usize) -> Vec<f32> {
        (0..samples)
            .map(|_| self.queue.pop().unwrap_or(0.0))
            .collect()
    }
}

/// Enumerates usable native input devices.
///
/// # Errors
///
/// Returns [`AudioError`] when the platform backend cannot enumerate devices
/// or inspect their default capture format.
pub fn input_devices() -> Result<Vec<AudioDevice>, AudioError> {
    enumerate(true)
}

/// Enumerates usable native output devices.
///
/// # Errors
///
/// Returns [`AudioError`] when the platform backend cannot enumerate devices
/// or inspect their default playback format.
pub fn output_devices() -> Result<Vec<AudioDevice>, AudioError> {
    enumerate(false)
}

fn enumerate(input: bool) -> Result<Vec<AudioDevice>, AudioError> {
    let host = cpal::default_host();
    let default_id = if input {
        host.default_input_device()
    } else {
        host.default_output_device()
    }
    .and_then(|device| device.id().ok())
    .map(|id| id.to_string());
    let devices = if input {
        host.input_devices()?
    } else {
        host.output_devices()?
    };
    let mut result = Vec::new();
    for device in devices {
        let id = device.id()?.to_string();
        let config = if input {
            device.default_input_config()
        } else {
            device.default_output_config()
        }?;
        result.push(AudioDevice {
            label: device
                .description()
                .map_or_else(|_| id.clone(), |value| value.to_string()),
            is_default: default_id.as_deref() == Some(&id),
            id,
            channels: config.channels(),
            sample_rate: config.sample_rate(),
        });
    }
    result.sort_by(|left, right| {
        right
            .is_default
            .cmp(&left.is_default)
            .then_with(|| left.label.cmp(&right.label))
    });
    Ok(result)
}

fn select_input(host: &cpal::Host, id: Option<&str>) -> Result<Device, AudioError> {
    select_device(host.input_devices()?, host.default_input_device(), id)
}

fn select_output(host: &cpal::Host, id: Option<&str>) -> Result<Device, AudioError> {
    select_device(host.output_devices()?, host.default_output_device(), id)
}

fn select_device(
    devices: impl Iterator<Item = Device>,
    default: Option<Device>,
    requested: Option<&str>,
) -> Result<Device, AudioError> {
    if let Some(requested) = requested {
        for device in devices {
            if device.id().is_ok_and(|id| id.to_string() == requested) {
                return Ok(device);
            }
        }
        return Err(AudioError::DeviceNotFound);
    }
    default.ok_or(AudioError::DeviceNotFound)
}

trait PcmSample: cpal::SizedSample + Send + 'static {
    fn to_float(value: Self) -> f32;
    fn from_float(value: f32) -> Self;
}

impl PcmSample for f32 {
    fn to_float(value: Self) -> f32 {
        value
    }
    fn from_float(value: f32) -> Self {
        value
    }
}
impl PcmSample for i16 {
    fn to_float(value: Self) -> f32 {
        f32::from(value) / f32::from(i16::MAX)
    }
    fn from_float(value: f32) -> Self {
        (value.clamp(-1.0, 1.0) * f32::from(i16::MAX)).round() as i16
    }
}
impl PcmSample for u16 {
    fn to_float(value: Self) -> f32 {
        (f32::from(value) / f32::from(u16::MAX)) * 2.0 - 1.0
    }
    fn from_float(value: f32) -> Self {
        (((value.clamp(-1.0, 1.0) + 1.0) * 0.5) * f32::from(u16::MAX)).round() as u16
    }
}

fn build_input<T: PcmSample>(
    device: &Device,
    config: StreamConfig,
    channels: usize,
    queue: Arc<ArrayQueue<f32>>,
    error: impl FnMut(cpal::Error) + Send + 'static,
) -> Result<Stream, AudioError> {
    Ok(device.build_input_stream(
        config,
        move |data: &[T], _| {
            for frame in data.chunks(channels) {
                let sample =
                    frame.iter().copied().map(T::to_float).sum::<f32>() / channels.max(1) as f32;
                if queue.push(sample).is_err() {
                    let _ = queue.pop();
                    let _ = queue.push(sample);
                }
            }
        },
        error,
        None,
    )?)
}

fn build_output<T: PcmSample>(
    device: &Device,
    config: StreamConfig,
    queue: Arc<ArrayQueue<f32>>,
    error: impl FnMut(cpal::Error) + Send + 'static,
) -> Result<Stream, AudioError> {
    Ok(device.build_output_stream(
        config,
        move |data: &mut [T], _| {
            for sample in data {
                *sample = T::from_float(queue.pop().unwrap_or(0.0));
            }
        },
        error,
        None,
    )?)
}

fn downmix(samples: &[f32], channels: usize) -> Vec<f32> {
    if channels <= 1 {
        return samples.to_vec();
    }
    samples
        .chunks(channels)
        .map(|frame| frame.iter().sum::<f32>() / channels as f32)
        .collect()
}

fn resample_linear(input: &[f32], source_rate: u32, target_rate: u32) -> Vec<f32> {
    if input.is_empty() || source_rate == target_rate {
        return input.to_vec();
    }
    let output_len =
        ((input.len() as u64 * u64::from(target_rate)) / u64::from(source_rate)) as usize;
    let mut output = Vec::with_capacity(output_len);
    for index in 0..output_len {
        let position = index as f64 * f64::from(source_rate) / f64::from(target_rate);
        let left = position.floor() as usize;
        let right = (left + 1).min(input.len() - 1);
        let fraction = (position - left as f64) as f32;
        output.push(input[left] + (input[right] - input[left]) * fraction);
    }
    output
}

#[derive(Debug, Error)]
pub enum AudioError {
    #[error("audio device is unavailable")]
    DeviceNotFound,
    #[error("audio backend failed: {0}")]
    Backend(#[from] cpal::Error),
    #[error("audio sample format {0:?} is not supported")]
    UnsupportedFormat(SampleFormat),
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    struct RenderRecorder(Arc<Mutex<Vec<f32>>>);

    impl AudioProcessor for RenderRecorder {
        fn observe_render(&mut self, samples: &[f32], _sample_rate: u32) {
            self.0
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner)
                .extend(samples);
        }

        fn process(&mut self, _interleaved_mono: &mut [f32], _sample_rate: u32) {}
    }

    #[test]
    fn resampler_preserves_duration() {
        let source = vec![0.25; 441];
        let result = resample_linear(&source, 44_100, 48_000);
        assert_eq!(result.len(), 480);
        assert!(
            result
                .iter()
                .all(|sample| (*sample - 0.25).abs() < f32::EPSILON)
        );
    }

    #[test]
    fn push_to_talk_gate_is_explicit() {
        let gate = CaptureGate::new(&CaptureSettings {
            mode: InputMode::PushToTalk,
            ..CaptureSettings::default()
        });
        assert!(!gate.permits(0.5));
        gate.set_push_to_talk(true);
        assert!(gate.permits(0.5));
        gate.set_muted(true);
        assert!(!gate.permits(0.5));
    }

    #[test]
    fn normal_and_priority_push_to_talk_keys_have_independent_state() {
        let push_to_talk = CaptureGate::new(&CaptureSettings {
            mode: InputMode::PushToTalk,
            ..CaptureSettings::default()
        });
        push_to_talk.set_priority_push_to_talk(true);
        assert!(push_to_talk.permits(0.1));
        push_to_talk.set_push_to_talk(true);
        push_to_talk.set_priority_push_to_talk(false);
        assert!(push_to_talk.permits(0.1));
        push_to_talk.set_muted(true);
        assert!(!push_to_talk.permits(1.0));
    }

    #[test]
    fn render_reference_is_bounded_and_zero_pads() {
        let reference = RenderReference {
            queue: Arc::new(ArrayQueue::new(3)),
        };
        reference.push(&[1.0, 2.0, 3.0, 4.0]);
        assert_eq!(reference.drain(4), vec![2.0, 3.0, 4.0, 0.0]);
    }

    #[test]
    fn processor_chain_receives_render_reference() {
        let observed = Arc::new(Mutex::new(Vec::new()));
        let mut chain = ProcessorChain::default();
        chain.push(Box::new(RenderRecorder(observed.clone())));
        chain.observe_render(&[0.25, -0.25], VOICE_SAMPLE_RATE);
        let recorded = observed
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone();
        assert_eq!(recorded, vec![0.25, -0.25]);
    }

    #[test]
    fn mixer_combines_participants_without_serializing_tracks() {
        let mixer = VoiceMixer::default();
        mixer.push("one", &[0.5, 0.5], VOICE_SAMPLE_RATE, 1);
        mixer.push("two", &[0.5, -0.5], VOICE_SAMPLE_RATE, 1);
        let output = mixer.drain(2);
        let scale = 1.0 / 2.0_f32.sqrt();
        assert!((output[0] - scale).abs() < 0.000_01);
        assert!(output[1].abs() < 0.000_01);
    }

    #[test]
    fn removing_participant_discards_buffered_audio() {
        let mixer = VoiceMixer::default();
        mixer.push("gone", &[0.8; 480], VOICE_SAMPLE_RATE, 1);
        mixer.remove("gone");
        assert_eq!(mixer.drain(2), vec![0.0, 0.0]);
    }

    #[test]
    fn authorized_priority_audio_ducks_other_participants() {
        let mixer = VoiceMixer::default();
        mixer.set_priority_capability("priority", true);
        assert!(mixer.set_priority_active("priority", true));
        mixer.push("priority", &[0.5], VOICE_SAMPLE_RATE, 1);
        mixer.push("normal", &[0.5], VOICE_SAMPLE_RATE, 1);

        let scale = 1.0 / 2.0_f32.sqrt();
        assert!((mixer.drain(1)[0] - (0.5 + 0.1) * scale).abs() < 0.000_01);
    }

    #[test]
    fn priority_ducking_applies_only_to_the_microphone_track() {
        let mixer = VoiceMixer::default();
        mixer.set_priority_capability("priority", true);
        assert!(mixer.set_priority_active("priority", true));
        mixer.push_track("priority", "microphone", true, &[0.5], VOICE_SAMPLE_RATE, 1);
        mixer.push_track(
            "priority",
            "screen-audio",
            false,
            &[0.5],
            VOICE_SAMPLE_RATE,
            1,
        );
        mixer.push_track("normal", "microphone", true, &[0.5], VOICE_SAMPLE_RATE, 1);

        let scale = 1.0 / 3.0_f32.sqrt();
        assert!((mixer.drain(1)[0] - (0.5 + 0.1 + 0.1) * scale).abs() < 0.000_01);
    }

    #[test]
    fn participant_removal_discards_all_of_their_audio_tracks() {
        let mixer = VoiceMixer::default();
        mixer.push_track("gone", "microphone", true, &[0.5], VOICE_SAMPLE_RATE, 1);
        mixer.push_track("gone", "screen-audio", false, &[0.5], VOICE_SAMPLE_RATE, 1);
        mixer.remove_participant("gone");
        assert_eq!(mixer.drain(1), vec![0.0]);
    }

    #[test]
    fn replacing_a_microphone_does_not_clear_priority_until_the_last_track_ends() {
        let mixer = VoiceMixer::default();
        mixer.register_track("priority", "old-microphone", true);
        mixer.register_track("priority", "new-microphone", true);
        assert!(mixer.remove_track("priority", "old-microphone"));
        assert!(!mixer.remove_track("priority", "new-microphone"));
    }

    #[test]
    fn priority_ducking_requires_capability_and_queued_priority_audio() {
        let mixer = VoiceMixer::default();
        assert!(!mixer.set_priority_active("unauthorized", true));
        mixer.push("unauthorized", &[0.5], VOICE_SAMPLE_RATE, 1);
        mixer.push("normal", &[0.5], VOICE_SAMPLE_RATE, 1);
        assert!((mixer.drain(1)[0] - 1.0 / 2.0_f32.sqrt()).abs() < 0.000_01);

        mixer.set_priority_capability("priority", true);
        assert!(mixer.set_priority_active("priority", true));
        mixer.push("normal", &[0.5], VOICE_SAMPLE_RATE, 1);
        assert_eq!(mixer.drain(1), vec![0.5]);
    }

    #[test]
    fn removing_or_resetting_priority_speaker_clears_ducking() {
        let mixer = VoiceMixer::default();
        mixer.set_priority_capability("priority", true);
        assert!(mixer.set_priority_active("priority", true));
        mixer.remove("priority");
        mixer.push("normal", &[0.5], VOICE_SAMPLE_RATE, 1);
        assert_eq!(mixer.drain(1), vec![0.5]);

        mixer.set_priority_capability("priority", true);
        assert!(mixer.set_priority_active("priority", true));
        mixer.clear_priority_active();
        mixer.push("normal", &[0.5], VOICE_SAMPLE_RATE, 1);
        assert_eq!(mixer.drain(1), vec![0.5]);
    }
}
