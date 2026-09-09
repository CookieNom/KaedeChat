Mobile app icons use the shared desktop artwork at `desktop/tauri/src-tauri/icons/icon.png` (relative to the repository root).

Android launcher sizes are 48, 72, 96, 144, and 192 pixels in the corresponding `mobile/android/app/src/main/res/mipmap-*` directories. iOS sizes are defined by `mobile/ios/Runner/Assets.xcassets/AppIcon.appiconset/Contents.json`. The Google Play icon is `mobile/android/google-play-icon.png` at 512 pixels.

When updating the artwork, resize with Lanczos filtering and flatten the iOS and Google Play icons onto the logo's dark background (`#171614`) so they remain opaque. Keep source artwork lossless and do not commit signing material.
