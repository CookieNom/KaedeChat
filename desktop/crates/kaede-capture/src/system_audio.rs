//! Small, scoped boundary around the OS screen/audio capture APIs.
#![allow(unsafe_code)] // OS buffers are borrowed only for the duration of each callback.

use crate::{PackedFrame, PackedPixelFormat};
use serde::Serialize;
use std::{
    ffi::{CStr, c_char, c_void},
    panic::{AssertUnwindSafe, catch_unwind},
    slice,
};

#[repr(C)]
#[derive(Clone, Copy)]
pub struct CaptureOptions {
    pub window: u64,
    /// Zero captures all output; MAX resolves the selected window's process.
    pub process: u32,
    pub width: u32,
    pub height: u32,
    pub fps: u32,
    pub audio: bool,
}

#[repr(C)]
pub(crate) struct Callbacks {
    context: *mut c_void,
    stopped: extern "C" fn(*mut c_void) -> bool,
    audio: extern "C" fn(*mut c_void, *const i16, usize),
    video: extern "C" fn(*mut c_void, *const u8, u32, u32, usize),
    status: extern "C" fn(*mut c_void, *const c_char),
}

unsafe extern "C" {
    fn kaede_capture_run(options: *const CaptureOptions, callbacks: *const Callbacks);
    fn kaede_audio_apps(
        context: *mut c_void,
        callback: extern "C" fn(*mut c_void, u32, *const c_char),
    );
}

/// The native backend joins/drains its callbacks before returning. Callbacks
/// may run on OS capture queues, so every closure must be thread safe.
pub fn run(
    options: CaptureOptions,
    stopped: &(dyn Fn() -> bool + Sync),
    audio: &(dyn Fn(&[i16]) + Sync),
    video: &(dyn Fn(PackedFrame<'_>) + Sync),
    status: &(dyn Fn(Result<(), String>) + Sync),
) {
    with_callbacks(
        options,
        stopped,
        audio,
        video,
        status,
        |options, callbacks| {
            // SAFETY: with_callbacks keeps all arguments alive until this call returns.
            unsafe { kaede_capture_run(options, callbacks) };
        },
    );
}

pub(crate) fn with_callbacks(
    options: CaptureOptions,
    stopped: &(dyn Fn() -> bool + Sync),
    audio: &(dyn Fn(&[i16]) + Sync),
    video: &(dyn Fn(PackedFrame<'_>) + Sync),
    status: &(dyn Fn(Result<(), String>) + Sync),
    native: impl FnOnce(*const CaptureOptions, *const Callbacks),
) {
    struct Context<'a> {
        stopped: &'a (dyn Fn() -> bool + Sync),
        audio: &'a (dyn Fn(&[i16]) + Sync),
        video: &'a (dyn Fn(PackedFrame<'_>) + Sync),
        status: &'a (dyn Fn(Result<(), String>) + Sync),
    }
    extern "C" fn is_stopped(ctx: *mut c_void) -> bool {
        // SAFETY: ctx points to Context until the blocking native call returns.
        let ctx = unsafe { &*ctx.cast::<Context<'_>>() };
        catch_unwind(AssertUnwindSafe(|| (ctx.stopped)())).unwrap_or(true)
    }
    extern "C" fn on_audio(ctx: *mut c_void, data: *const i16, count: usize) {
        if data.is_null() || count == 0 || count > 96_000 {
            return;
        }
        // SAFETY: backend supplies count initialized i16 samples during this call.
        let ctx = unsafe { &*ctx.cast::<Context<'_>>() };
        let samples = unsafe { slice::from_raw_parts(data, count) };
        let _ = catch_unwind(AssertUnwindSafe(|| (ctx.audio)(samples)));
    }
    extern "C" fn on_video(
        ctx: *mut c_void,
        data: *const u8,
        width: u32,
        height: u32,
        stride: usize,
    ) {
        let Some(length) = stride.checked_mul(height as usize) else {
            return;
        };
        if data.is_null() || length == 0 || length > 256 * 1024 * 1024 {
            return;
        }
        // SAFETY: the pixel buffer is locked by the backend for this callback.
        let ctx = unsafe { &*ctx.cast::<Context<'_>>() };
        let data = unsafe { slice::from_raw_parts(data, length) };
        let _ = catch_unwind(AssertUnwindSafe(|| {
            (ctx.video)(PackedFrame {
                width,
                height,
                stride,
                data,
                format: PackedPixelFormat::Bgra,
            });
        }));
    }
    extern "C" fn on_status(ctx: *mut c_void, message: *const c_char) {
        // SAFETY: native messages are NUL terminated and valid during this call.
        let ctx = unsafe { &*ctx.cast::<Context<'_>>() };
        let result = if message.is_null() {
            Ok(())
        } else {
            Err(unsafe { CStr::from_ptr(message) }
                .to_string_lossy()
                .into_owned())
        };
        let _ = catch_unwind(AssertUnwindSafe(|| (ctx.status)(result)));
    }
    let mut context = Context {
        stopped,
        audio,
        video,
        status,
    };
    let callbacks = Callbacks {
        context: (&raw mut context).cast(),
        stopped: is_stopped,
        audio: on_audio,
        video: on_video,
        status: on_status,
    };
    native(&raw const options, &raw const callbacks);
}

#[derive(Debug, Serialize)]
pub struct AudioApplication {
    pub process_id: u32,
    pub label: String,
}

/// Linux applications currently publishing output audio. No microphone inputs.
#[must_use]
pub fn applications() -> Vec<AudioApplication> {
    extern "C" fn append(context: *mut c_void, process_id: u32, label: *const c_char) {
        if label.is_null() || process_id == 0 {
            return;
        }
        // SAFETY: this synchronous enumeration owns the vector; names are C strings.
        let entries = unsafe { &mut *context.cast::<Vec<AudioApplication>>() };
        let label = unsafe { CStr::from_ptr(label) }
            .to_string_lossy()
            .into_owned();
        if !entries.iter().any(|entry| entry.process_id == process_id) {
            entries.push(AudioApplication { process_id, label });
        }
    }
    let mut entries: Vec<AudioApplication> = Vec::new();
    unsafe { kaede_audio_apps((&raw mut entries).cast(), append) };
    entries.sort_by_key(|entry| entry.label.to_lowercase());
    entries
}
