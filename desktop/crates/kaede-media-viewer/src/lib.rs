//! Sandboxed native window for scanned image and video attachments.

use std::{env, path::Path, process::Command};

#[cfg(feature = "embedded-webview")]
use base64::{Engine as _, engine::general_purpose::STANDARD};
#[cfg(feature = "embedded-webview")]
use tao::{
    event::{Event, WindowEvent},
    event_loop::{ControlFlow, EventLoopBuilder},
    window::WindowBuilder,
};
use thiserror::Error;
#[cfg(feature = "embedded-webview")]
use wry::WebViewBuilder;

pub const HELPER_FLAG: &str = "--kaede-media-viewer";

#[derive(Debug, Error)]
pub enum ViewerError {
    #[error("the media path is invalid")]
    InvalidPath,
    #[error("the attachment type cannot be shown in the media viewer")]
    UnsupportedType,
    #[error("could not start the media viewer: {0}")]
    Start(String),
}

/// Start a separate, in-app media window. A helper process keeps the platform
/// web view event loop isolated from Slint and receives no credentials or URL.
///
/// # Errors
///
/// Returns an error when the path is not a local file, the MIME type is not a
/// supported image or video type, or the helper process cannot be started.
pub fn spawn(path: &Path, content_type: &str) -> Result<(), ViewerError> {
    let path = path.canonicalize().map_err(|_| ViewerError::InvalidPath)?;
    if !path.is_file() {
        return Err(ViewerError::InvalidPath);
    }
    media_kind(content_type)?;
    let executable = env::current_exe().map_err(|error| ViewerError::Start(error.to_string()))?;
    Command::new(executable)
        .arg(HELPER_FLAG)
        .arg(path)
        .arg(content_type)
        .spawn()
        .map_err(|error| ViewerError::Start(error.to_string()))?;
    Ok(())
}

fn media_kind(content_type: &str) -> Result<&'static str, ViewerError> {
    match content_type {
        "image/avif" | "image/gif" | "image/jpeg" | "image/png" | "image/webp" => Ok("image"),
        "video/mp4" | "video/ogg" | "video/quicktime" | "video/webm" => Ok("video"),
        _ => Err(ViewerError::UnsupportedType),
    }
}

/// Own the short-lived native media event loop. Call this before initializing
/// Slint when the helper flag is present.
///
/// # Errors
///
/// Returns an error when the media is invalid or too large, or when the native
/// window or embedded web view cannot be created.
#[cfg(feature = "embedded-webview")]
pub fn run_helper(path: &str, content_type: &str) -> Result<(), ViewerError> {
    let path = Path::new(path)
        .canonicalize()
        .map_err(|_| ViewerError::InvalidPath)?;
    if !path.is_file() {
        return Err(ViewerError::InvalidPath);
    }
    let kind = media_kind(content_type)?;
    let bytes = std::fs::read(path).map_err(|error| ViewerError::Start(error.to_string()))?;
    if bytes.is_empty() || bytes.len() > 16 * 1024 * 1024 {
        return Err(ViewerError::InvalidPath);
    }
    let source = serde_json_string(&format!(
        "data:{content_type};base64,{}",
        STANDARD.encode(bytes)
    ));
    let content_type = serde_json_string(content_type);
    let media = if kind == "video" {
        format!(
            r#"<video controls autoplay playsinline preload="metadata"><source src={source} type={content_type}></video>"#
        )
    } else {
        format!(r#"<img src={source} alt="Attachment">"#)
    };
    let html = format!(
        r#"<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; media-src data:; style-src 'unsafe-inline'"><style>html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#0b0c0b}}body{{display:grid;place-items:center}}img,video{{display:block;max-width:100%;max-height:100%;width:auto;height:auto}}</style></head><body>{media}</body></html>"#
    );
    let event_loop = EventLoopBuilder::<()>::with_user_event().build();
    let window = WindowBuilder::new()
        .with_title("Kaede media viewer")
        .with_inner_size(tao::dpi::LogicalSize::new(1100.0, 760.0))
        .build(&event_loop)
        .map_err(|error| ViewerError::Start(error.to_string()))?;
    let builder = WebViewBuilder::new()
        .with_html(html)
        .with_devtools(false)
        .with_navigation_handler(|_| false);
    #[cfg(any(target_os = "windows", target_os = "macos"))]
    let _webview = builder
        .build(&window)
        .map_err(|error| ViewerError::Start(error.to_string()))?;
    #[cfg(target_os = "linux")]
    let _webview = {
        use tao::platform::unix::WindowExtUnix;
        use wry::WebViewBuilderExtUnix;
        let container = window
            .default_vbox()
            .ok_or_else(|| ViewerError::Start("WebView container is unavailable".to_owned()))?;
        builder
            .build_gtk(container)
            .map_err(|error| ViewerError::Start(error.to_string()))?
    };
    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        if matches!(
            event,
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            }
        ) {
            *control_flow = ControlFlow::Exit;
        }
    });
}

#[cfg(not(feature = "embedded-webview"))]
/// Report that this binary was compiled without native media-viewer support.
///
/// # Errors
///
/// Always returns [`ViewerError::Start`] because no viewer is compiled in.
pub fn run_helper(_path: &str, _content_type: &str) -> Result<(), ViewerError> {
    Err(ViewerError::Start(
        "this build does not include the embedded media viewer".to_owned(),
    ))
}

#[cfg(any(feature = "embedded-webview", test))]
fn serde_json_string(value: &str) -> String {
    let mut output = String::with_capacity(value.len() + 2);
    output.push('"');
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '<' => output.push_str("\\u003c"),
            '>' => output.push_str("\\u003e"),
            '&' => output.push_str("\\u0026"),
            character => output.push(character),
        }
    }
    output.push('"');
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_non_media_content_types() {
        assert!(matches!(
            media_kind("text/html"),
            Err(ViewerError::UnsupportedType)
        ));
    }

    #[test]
    fn escapes_html_sensitive_file_url_characters() {
        assert_eq!(
            serde_json_string("file:///tmp/<x>"),
            "\"file:///tmp/\\u003cx\\u003e\""
        );
    }
}
