Mobile app icons use the shared desktop artwork at `desktop/tauri/src-tauri/icons/icon.png` (relative to the repository root).

Android launcher sizes are 48, 72, 96, 144, and 192 pixels in the corresponding `mobile/android/app/src/main/res/mipmap-*` directories. iOS sizes are defined by `mobile/ios/Runner/Assets.xcassets/AppIcon.appiconset/Contents.json`. The Google Play icon is `mobile/android/google-play-icon.png` at 512 pixels.

When updating the artwork, resize with Lanczos filtering and flatten the iOS and Google Play icons onto the logo's dark background (`#171614`) so they remain opaque. Keep source artwork lossless and do not commit signing material.

Android 8+ uses `res/mipmap-anydpi-v26/ic_launcher.xml` with a full-bleed dark background and a vector bubble foreground. This avoids the white legacy-icon wrapper and keeps the bubble large across launcher masks. Keep the foreground inside Android's central 66dp safe circle on the 108dp canvas; do not add a second rounded-square background to it. Older Android versions retain the PNG fallback.
