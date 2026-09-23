//! Observe voice shortcuts without reserving (and swallowing) keys with `RegisterHotKey`.
use tauri::{AppHandle, Manager};
use tauri_plugin_global_shortcut::{Code, Modifiers, Shortcut};
use windows::Win32::UI::Input::KeyboardAndMouse::{self as keys, GetAsyncKeyState, VIRTUAL_KEY};

#[allow(unsafe_code)]
fn down(key: VIRTUAL_KEY) -> bool {
    // Only the high bit is reliable; the low bit is shared with other applications.
    unsafe { GetAsyncKeyState(i32::from(key.0)) < 0 }
}

fn pressed(shortcut: &Shortcut, down: impl Fn(VIRTUAL_KEY) -> bool) -> bool {
    key_to_vk(&shortcut.key).is_some_and(&down)
        && shortcut.mods.contains(Modifiers::SHIFT) == down(keys::VK_SHIFT)
        && shortcut.mods.contains(Modifiers::CONTROL) == down(keys::VK_CONTROL)
        && shortcut.mods.contains(Modifiers::ALT) == down(keys::VK_MENU)
        && shortcut.mods.intersects(Modifiers::SUPER | Modifiers::META)
            == (down(keys::VK_LWIN) || down(keys::VK_RWIN))
}

pub(super) fn start(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        // ponytail: polling can miss taps shorter than 8 ms; use a passive keyboard hook
        // if those taps need to trigger voice actions. No keystrokes are reserved or suppressed.
        let mut timer = tokio::time::interval(std::time::Duration::from_millis(8));
        timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            timer.tick().await;
            let state = app.state::<super::NativeState>();
            let changes = {
                let keys = state.hotkey.lock();
                [
                    &keys.push_to_talk,
                    &keys.priority_push_to_talk,
                    &keys.toggle_mute,
                    &keys.toggle_deafen,
                ]
                .into_iter()
                .flatten()
                .filter_map(|key| {
                    let down = pressed(&key.shortcut, down);
                    (down != keys.pressed.contains(&key.shortcut.id()))
                        .then_some((key.shortcut, down))
                })
                .collect::<Vec<_>>()
            };
            for (shortcut, down) in changes {
                super::handle_voice_shortcut(&app, &shortcut, down);
            }
        }
    });
}

// Key mapping follows global-hotkey (MIT/Apache-2.0), preserving existing bindings.
#[allow(clippy::too_many_lines)]
pub(super) fn key_to_vk(key: &Code) -> Option<VIRTUAL_KEY> {
    Some(match key {
        Code::KeyA => keys::VK_A,
        Code::KeyB => keys::VK_B,
        Code::KeyC => keys::VK_C,
        Code::KeyD => keys::VK_D,
        Code::KeyE => keys::VK_E,
        Code::KeyF => keys::VK_F,
        Code::KeyG => keys::VK_G,
        Code::KeyH => keys::VK_H,
        Code::KeyI => keys::VK_I,
        Code::KeyJ => keys::VK_J,
        Code::KeyK => keys::VK_K,
        Code::KeyL => keys::VK_L,
        Code::KeyM => keys::VK_M,
        Code::KeyN => keys::VK_N,
        Code::KeyO => keys::VK_O,
        Code::KeyP => keys::VK_P,
        Code::KeyQ => keys::VK_Q,
        Code::KeyR => keys::VK_R,
        Code::KeyS => keys::VK_S,
        Code::KeyT => keys::VK_T,
        Code::KeyU => keys::VK_U,
        Code::KeyV => keys::VK_V,
        Code::KeyW => keys::VK_W,
        Code::KeyX => keys::VK_X,
        Code::KeyY => keys::VK_Y,
        Code::KeyZ => keys::VK_Z,
        Code::Digit0 => keys::VK_0,
        Code::Digit1 => keys::VK_1,
        Code::Digit2 => keys::VK_2,
        Code::Digit3 => keys::VK_3,
        Code::Digit4 => keys::VK_4,
        Code::Digit5 => keys::VK_5,
        Code::Digit6 => keys::VK_6,
        Code::Digit7 => keys::VK_7,
        Code::Digit8 => keys::VK_8,
        Code::Digit9 => keys::VK_9,
        Code::Equal => keys::VK_OEM_PLUS,
        Code::Comma => keys::VK_OEM_COMMA,
        Code::Minus => keys::VK_OEM_MINUS,
        Code::Period => keys::VK_OEM_PERIOD,
        Code::Semicolon => keys::VK_OEM_1,
        Code::Slash => keys::VK_OEM_2,
        Code::Backquote => keys::VK_OEM_3,
        Code::BracketLeft => keys::VK_OEM_4,
        Code::Backslash => keys::VK_OEM_5,
        Code::BracketRight => keys::VK_OEM_6,
        Code::Quote => keys::VK_OEM_7,
        Code::Backspace => keys::VK_BACK,
        Code::Tab => keys::VK_TAB,
        Code::Space => keys::VK_SPACE,
        Code::Enter | Code::NumpadEnter => keys::VK_RETURN,
        Code::CapsLock => keys::VK_CAPITAL,
        Code::Escape => keys::VK_ESCAPE,
        Code::PageUp => keys::VK_PRIOR,
        Code::PageDown => keys::VK_NEXT,
        Code::End => keys::VK_END,
        Code::Home => keys::VK_HOME,
        Code::ArrowLeft => keys::VK_LEFT,
        Code::ArrowUp => keys::VK_UP,
        Code::ArrowRight => keys::VK_RIGHT,
        Code::ArrowDown => keys::VK_DOWN,
        Code::PrintScreen => keys::VK_SNAPSHOT,
        Code::Insert => keys::VK_INSERT,
        Code::Delete => keys::VK_DELETE,
        Code::F1 => keys::VK_F1,
        Code::F2 => keys::VK_F2,
        Code::F3 => keys::VK_F3,
        Code::F4 => keys::VK_F4,
        Code::F5 => keys::VK_F5,
        Code::F6 => keys::VK_F6,
        Code::F7 => keys::VK_F7,
        Code::F8 => keys::VK_F8,
        Code::F9 => keys::VK_F9,
        Code::F10 => keys::VK_F10,
        Code::F11 => keys::VK_F11,
        Code::F12 => keys::VK_F12,
        Code::F13 => keys::VK_F13,
        Code::F14 => keys::VK_F14,
        Code::F15 => keys::VK_F15,
        Code::F16 => keys::VK_F16,
        Code::F17 => keys::VK_F17,
        Code::F18 => keys::VK_F18,
        Code::F19 => keys::VK_F19,
        Code::F20 => keys::VK_F20,
        Code::F21 => keys::VK_F21,
        Code::F22 => keys::VK_F22,
        Code::F23 => keys::VK_F23,
        Code::F24 => keys::VK_F24,
        Code::NumLock => keys::VK_NUMLOCK,
        Code::Numpad0 => keys::VK_NUMPAD0,
        Code::Numpad1 => keys::VK_NUMPAD1,
        Code::Numpad2 => keys::VK_NUMPAD2,
        Code::Numpad3 => keys::VK_NUMPAD3,
        Code::Numpad4 => keys::VK_NUMPAD4,
        Code::Numpad5 => keys::VK_NUMPAD5,
        Code::Numpad6 => keys::VK_NUMPAD6,
        Code::Numpad7 => keys::VK_NUMPAD7,
        Code::Numpad8 => keys::VK_NUMPAD8,
        Code::Numpad9 => keys::VK_NUMPAD9,
        Code::NumpadAdd => keys::VK_ADD,
        Code::NumpadDecimal => keys::VK_DECIMAL,
        Code::NumpadDivide => keys::VK_DIVIDE,
        Code::NumpadMultiply => keys::VK_MULTIPLY,
        Code::NumpadSubtract => keys::VK_SUBTRACT,
        Code::ScrollLock => keys::VK_SCROLL,
        Code::AudioVolumeDown => keys::VK_VOLUME_DOWN,
        Code::AudioVolumeUp => keys::VK_VOLUME_UP,
        Code::AudioVolumeMute => keys::VK_VOLUME_MUTE,
        Code::MediaPlay => keys::VK_PLAY,
        Code::MediaPause | Code::Pause => keys::VK_PAUSE,
        Code::MediaPlayPause => keys::VK_MEDIA_PLAY_PAUSE,
        Code::MediaStop => keys::VK_MEDIA_STOP,
        Code::MediaTrackNext => keys::VK_MEDIA_NEXT_TRACK,
        Code::MediaTrackPrevious => keys::VK_MEDIA_PREV_TRACK,
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn letter_shortcuts_follow_press_release_and_exact_modifiers() {
        let letter = Shortcut::new(None, Code::KeyV);
        let chord = Shortcut::new(Some(Modifiers::CONTROL), Code::KeyV);
        assert!(pressed(&letter, |key| key == keys::VK_V));
        assert!(!pressed(&letter, |_| false));
        assert!(!pressed(&letter, |key| [keys::VK_V, keys::VK_CONTROL].contains(&key)));
        assert!(!pressed(&chord, |key| key == keys::VK_V));
        assert!(pressed(&chord, |key| [keys::VK_V, keys::VK_CONTROL].contains(&key)));
        assert!(!pressed(&chord, |key| key == keys::VK_CONTROL));
        assert_eq!(key_to_vk(&Code::NumpadEqual), None);
    }
}
