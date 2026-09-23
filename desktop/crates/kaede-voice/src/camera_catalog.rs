use super::CameraDevice;
use std::collections::BTreeMap;

pub(super) struct CameraSource {
    pub device: CameraDevice,
    pub native: bool,
    pub directshow: Option<String>,
}

// Windows can expose the same device instance through different capture
// interface classes. Compare the device instance, preserving any pin suffix.
fn identity(path: &str) -> String {
    let path = path.trim().to_lowercase();
    let path = path.strip_prefix("@device:pnp:").unwrap_or(&path);
    let path = path
        .strip_prefix(r"\\?\")
        .or_else(|| path.strip_prefix(r"\\.\"))
        .unwrap_or(path);
    let mut identity = path.to_owned();
    for category in [
        "#{65e8773d-8f56-11d0-a3b9-00a0c9223196}", // KSCATEGORY_CAPTURE
        "#{e5323777-f976-4f5b-9b55-b94699c46e44}", // KSCATEGORY_VIDEO_CAMERA
        "#{6994ad05-93ef-11d0-a3cc-00a0c9223196}", // KSCATEGORY_VIDEO
    ] {
        if let Some((instance, suffix)) = path.split_once(category) {
            identity = format!(
                "{instance}{}",
                suffix.strip_suffix(r"\global").unwrap_or(suffix)
            );
            break;
        }
    }
    identity
}

type Scan = Result<Vec<(CameraDevice, String)>, String>;

pub(super) fn combine(
    media_foundation: Scan,
    directshow: Scan,
) -> Result<Vec<CameraSource>, String> {
    let mut cameras: Vec<CameraSource> = Vec::new();
    let mut identities: BTreeMap<String, usize> = BTreeMap::new();
    let mut ids: BTreeMap<String, usize> = BTreeMap::new();
    let mut errors = Vec::new();
    for (native, scan) in [(true, media_foundation), (false, directshow)] {
        let devices = match scan {
            Ok(devices) => devices,
            Err(error) => {
                errors.push(error);
                continue;
            }
        };
        for (device, path) in devices {
            let key = identity(&path);
            let directshow = (!native).then(|| {
                device
                    .id
                    .strip_prefix("dshow:")
                    .unwrap_or(&device.id)
                    .to_owned()
            });
            let duplicate = ids.get(&device.id).copied().or_else(|| {
                (!key.is_empty())
                    .then(|| identities.get(&key).copied())
                    .flatten()
            });
            if let Some(index) = duplicate {
                ids.insert(device.id.clone(), index);
                if cameras[index].device.id != device.id
                    && !cameras[index].device.aliases.contains(&device.id)
                {
                    cameras[index].device.aliases.push(device.id.clone());
                }
                if directshow.is_some() {
                    cameras[index].directshow = directshow;
                }
                continue;
            }
            let index = cameras.len();
            ids.insert(device.id.clone(), index);
            if !key.is_empty() {
                identities.insert(key, index);
            }
            cameras.push(CameraSource {
                device,
                native,
                directshow,
            });
        }
    }
    if cameras.is_empty() && !errors.is_empty() {
        return Err(errors.join("; "));
    }
    cameras.sort_by(|a, b| {
        a.device
            .label
            .to_lowercase()
            .cmp(&b.device.label.to_lowercase())
            .then(a.device.id.cmp(&b.device.id))
    });
    Ok(cameras)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn entry(id: &str, label: &str, path: &str) -> (CameraDevice, String) {
        (
            CameraDevice {
                id: id.into(),
                label: label.into(),
                aliases: Vec::new(),
            },
            path.into(),
        )
    }

    #[test]
    fn combines_backends_by_device_identity_and_preserves_capture_fallback() -> Result<(), String> {
        let cameras = combine(
            Ok(vec![entry(
                "0",
                "Webcam",
                r"\\?\USB#VID_1234#ONE#{e5323777-f976-4f5b-9b55-b94699c46e44}\global",
            )]),
            Ok(vec![
                entry(
                    "dshow:physical",
                    "Webcam (driver)",
                    r"@device:pnp:\\?\usb#vid_1234#one#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global",
                ),
                entry("dshow:obs", "OBS Virtual Camera", ""),
                entry("dshow:obs", "OBS Virtual Camera", ""),
                entry("dshow:nvidia", "Camera (NVIDIA Broadcast)", ""),
            ]),
        )?;
        assert_eq!(cameras.len(), 3);
        let physical = cameras
            .iter()
            .find(|camera| camera.device.id == "0")
            .ok_or("Missing physical camera")?;
        assert!(physical.native);
        assert_eq!(physical.device.aliases, vec!["dshow:physical"]);
        assert_eq!(physical.directshow.as_deref(), Some("physical"));
        assert!(
            cameras
                .iter()
                .any(|camera| !camera.native && camera.directshow.as_deref() == Some("obs"))
        );
        Ok(())
    }

    #[test]
    fn identical_names_do_not_hide_distinct_devices_and_a_failed_backend_does_not_hide_working_cameras()
    -> Result<(), String> {
        let cameras = combine(
            Ok(vec![entry("0", "Webcam", "device-one")]),
            Ok(vec![
                entry("dshow:two", "Webcam", "device-two"),
                entry("dshow:three", "Webcam", ""),
            ]),
        )?;
        assert_eq!(cameras.len(), 3);
        assert_eq!(
            combine(
                Err("MF unavailable".into()),
                Ok(vec![entry("dshow:obs", "OBS", "")])
            )?
            .len(),
            1
        );
        assert_eq!(
            combine(
                Ok(vec![entry("0", "Webcam", "")]),
                Err("DirectShow unavailable".into())
            )?
            .len(),
            1
        );
        assert!(combine(Err("MF failed".into()), Ok(vec![])).is_err());
        assert!(combine(Ok(vec![]), Ok(vec![]))?.is_empty());
        Ok(())
    }
}
