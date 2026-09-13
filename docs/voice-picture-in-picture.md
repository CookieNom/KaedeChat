# Call video visibility

Mobile automatically enters native picture in picture when leaving the app during a video call. The **Automatic picture in picture** switch in user settings is on by default and saved on the device. System support and system PiP permissions still apply. Android uses the activity PiP API (automatic entry on Android 12+, leave-hint entry on Android 8–11). iOS uses the video-call PiP API on iOS 15+ and a native sample-buffer renderer for the selected call track.

Desktop voice channels offer **Picture in picture** (a small, draggable, resizable, always-on-top window) and **Pop out video** (a normal separate window). Closing the window or choosing **Return video to channel** restores the inline view. Both views use the existing native call and encryption context. A single window consumes the native frame queue, so opening video does not open another call or duplicate audio.

The browser has no PiP controls. Its incoming video subscriptions pause when the document is hidden. Desktop pauses video subscriptions when its video window, or the main window if video is inline, is hidden or minimized. Mobile pauses subscriptions in the background unless native PiP reports itself visible. Audio, participant signaling, and outgoing media are preserved. Restoring a visible presentation resubscribes to incoming video.

## Device verification

The automated checks cover browser subscription changes and the mobile preference/native-state bridge. Native window behavior and OS home gestures also need device testing:

1. Join a call with another participant publishing camera and screen share. Test an encrypted call too.
2. On mobile, swipe home: verify video appears in PiP and audio continues. Return to the app and repeat after turning automatic PiP off in settings.
3. Close mobile PiP while staying in another app. Verify video inbound bytes stop increasing after in-flight packets drain, while audio continues. Restore Kaede and verify video resumes.
4. On desktop, open each window mode, drag and resize it, minimize the main client, and verify video remains live. Close the video window and verify it returns to the channel.
5. Minimize the desktop video window as well; verify incoming video stops. Restore it and verify playback returns. Repeat with the main window closed to the tray.
6. Leave the call, reconnect, change codec variants, and have a participant stop video while PiP is active; check that no stale video window remains.
7. In the browser, hide and restore the tab. Verify video pauses/resumes, audio continues, and no PiP controls appear.

Native API references: [Android PiP](https://developer.android.com/develop/ui/views/picture-in-picture), [Apple video-call PiP content source](https://developer.apple.com/documentation/avkit/avpictureinpicturecontroller/contentsource-swift.class/activevideocallsourceview).
