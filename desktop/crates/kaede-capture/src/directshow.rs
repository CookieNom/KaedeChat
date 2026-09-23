//! Windows virtual cameras exposed through `DirectShow` instead of Media Foundation.
#![allow(unsafe_code)] // The native bridge owns COM and joins all callbacks before returning.

use crate::{
    PackedFrame,
    system_audio::{self, Callbacks, CaptureOptions},
};
use std::{
    ffi::{CStr, CString, c_char, c_void},
    panic::{AssertUnwindSafe, catch_unwind},
};

#[derive(Debug)]
pub struct Device {
    /// Moniker display name, stable across enumeration order changes.
    pub id: String,
    pub label: String,
    /// Matches Media Foundation's symbolic link for physical cameras.
    pub device_path: String,
}

unsafe extern "C" {
    fn kaede_camera_devices(
        context: *mut c_void,
        append: extern "C" fn(*mut c_void, *const c_char, *const c_char, *const c_char),
    ) -> i32;
    fn kaede_camera_run(
        id: *const c_char,
        options: *const CaptureOptions,
        callbacks: *const Callbacks,
    );
}

/// Enumerates registered cameras without opening their capture filters.
///
/// # Errors
/// Returns a Windows error if the camera category cannot be queried.
pub fn devices() -> Result<Vec<Device>, String> {
    extern "C" fn append(
        context: *mut c_void,
        id: *const c_char,
        label: *const c_char,
        path: *const c_char,
    ) {
        if id.is_null() || label.is_null() || path.is_null() {
            return;
        }
        // SAFETY: native enumeration borrows this vector and NUL-terminated strings synchronously.
        let _ = catch_unwind(AssertUnwindSafe(|| unsafe {
            let entries = &mut *context.cast::<Vec<Device>>();
            entries.push(Device {
                id: CStr::from_ptr(id).to_string_lossy().into_owned(),
                label: CStr::from_ptr(label).to_string_lossy().into_owned(),
                device_path: CStr::from_ptr(path).to_string_lossy().into_owned(),
            });
        }));
    }
    let mut entries: Vec<Device> = Vec::new();
    // SAFETY: entries and the callback remain valid for this synchronous scan.
    let result = unsafe { kaede_camera_devices((&raw mut entries).cast(), append) };
    if result < 0 {
        return Err(format!("Windows camera discovery failed ({result:#010x})."));
    }
    Ok(entries)
}

/// Runs capture on the calling thread until stopped or a fatal error occurs.
/// The first successful status is sent only after a valid frame arrives.
pub fn run(
    id: &str,
    options: CaptureOptions,
    stopped: &(dyn Fn() -> bool + Sync),
    video: &(dyn Fn(PackedFrame<'_>) + Sync),
    status: &(dyn Fn(Result<(), String>) + Sync),
) {
    let Ok(id) = CString::new(id) else {
        status(Err("The selected camera identifier is invalid.".to_owned()));
        return;
    };
    system_audio::with_callbacks(
        options,
        stopped,
        &|_| {},
        video,
        status,
        |options, callbacks| {
            // SAFETY: native capture drains its worker callbacks before this scope ends.
            unsafe { kaede_camera_run(id.as_ptr(), options, callbacks) };
        },
    );
}

#[cfg(test)]
mod tests {
    unsafe extern "C" {
        fn kaede_camera_bgra(
            source: *const u8,
            length: usize,
            width: u32,
            height: i32,
            output: *mut u8,
            capacity: usize,
        ) -> bool;
    }

    #[test]
    fn camera_rows_are_unpadded_oriented_and_bounds_checked() {
        // One pixel per row, padded to four bytes; input is BGR, bottom row first.
        let source = [255, 0, 0, 99, 0, 0, 255, 99];
        let mut output = [0_u8; 8];
        // SAFETY: pointers remain live and each supplied size matches its allocation.
        unsafe {
            assert!(kaede_camera_bgra(
                source.as_ptr(),
                source.len(),
                1,
                2,
                output.as_mut_ptr(),
                output.len()
            ));
            assert_eq!(output, [0, 0, 255, 255, 255, 0, 0, 255]);
            assert!(kaede_camera_bgra(
                source.as_ptr(),
                source.len(),
                1,
                -2,
                output.as_mut_ptr(),
                output.len()
            ));
            assert_eq!(output, [255, 0, 0, 255, 0, 0, 255, 255]);
            assert!(!kaede_camera_bgra(
                source.as_ptr(),
                source.len() - 1,
                1,
                2,
                output.as_mut_ptr(),
                output.len()
            ));
            assert!(!kaede_camera_bgra(
                source.as_ptr(),
                source.len(),
                1,
                2,
                output.as_mut_ptr(),
                output.len() - 1
            ));
            assert!(!kaede_camera_bgra(
                source.as_ptr(),
                source.len(),
                1,
                i32::MIN,
                output.as_mut_ptr(),
                output.len()
            ));
        }
    }

    #[test]
    fn enumerating_without_opening_cameras_returns_unique_monikers() -> Result<(), String> {
        let devices = super::devices()?;
        let mut seen = std::collections::BTreeSet::new();
        for device in devices {
            assert!(!device.id.is_empty());
            assert!(!device.label.is_empty());
            assert!(seen.insert(device.id.to_lowercase()));
        }
        Ok(())
    }
}
