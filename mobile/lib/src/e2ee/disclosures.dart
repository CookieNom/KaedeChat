import 'package:shared_preferences/shared_preferences.dart';

enum EncryptedRoomKind { conversation, messages, media }

const _roomWarningVersion = 'v1';
const _roomWarningPrefix = 'kaede.e2ee-room-warning.$_roomWarningVersion';

String encryptedRoomWarningKey(String accountRef, String channelRef) =>
    '$_roomWarningPrefix.${Uri.encodeComponent(accountRef)}.${Uri.encodeComponent(channelRef)}';

Future<bool> hasAcknowledgedEncryptedRoom(
  String accountRef,
  String channelRef,
) async {
  try {
    final preferences = await SharedPreferences.getInstance();
    return preferences.getBool(
          encryptedRoomWarningKey(accountRef, channelRef),
        ) ==
        true;
  } on Object {
    // A storage failure must never silently suppress a safety disclosure.
    return false;
  }
}

Future<void> acknowledgeEncryptedRoom(
  String accountRef,
  String channelRef,
) async {
  try {
    final preferences = await SharedPreferences.getInstance();
    await preferences.setBool(
      encryptedRoomWarningKey(accountRef, channelRef),
      true,
    );
  } on Object {
    // The room remains usable, but this warning will be shown again next time.
  }
}

String encryptedRoomJoinWarning(EncryptedRoomKind kind) {
  final protectedContent = switch (kind) {
    EncryptedRoomKind.media =>
      'This voice channel encrypts microphone, camera, screen video, and screen audio at participant devices.',
    EncryptedRoomKind.messages =>
      'New messages and files in this channel are encrypted at participant devices. Supported calls in this channel are encrypted too.',
    EncryptedRoomKind.conversation =>
      'New messages and files in this conversation are encrypted at participant devices. Supported calls are encrypted too.',
  };
  final unavailableFeatures = kind == EncryptedRoomKind.media
      ? 'Server recording and transcription are unavailable, and an unsupported client cannot join.'
      : 'Server message search, automatic previews, bots, webhooks, server file previews and scanning, call recording, and transcription are unavailable. Notifications are generic, and unsupported clients cannot participate.';
  final historyAndRecovery = kind == EncryptedRoomKind.media
      ? ''
      : ' Existing plaintext history is not retroactively protected. Losing the synchronized encrypted account vault, all trusted local state, and the recovery backup permanently loses encrypted history.';

  return 'You are opening an end-to-end encrypted room for the first time on this account. '
      '$protectedContent $unavailableFeatures$historyAndRecovery '
      'The instance can still see participants, timing, sizes, track types, and traffic metadata, and another participant can save or record content on their device. '
      'Participant identities are unverified until everyone compares the safety number through a separate trusted channel. Tap the lock in the room header to view it, and compare it again after membership or identity changes. '
      'Removed members keep content they already received or recorded.';
}
