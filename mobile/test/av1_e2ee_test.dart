// Tests exercise the SDK event hooks used by native publishing.
// ignore_for_file: invalid_use_of_internal_member

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
// The native factory casts these interfaces to the plugin's native classes.
// ignore: implementation_imports
import 'package:flutter_webrtc/src/native/rtc_rtp_receiver_impl.dart';
// ignore: implementation_imports
import 'package:flutter_webrtc/src/native/rtc_rtp_sender_impl.dart';
import 'package:livekit_client/livekit_client.dart';
import 'package:mocktail/mocktail.dart';

class _Sender extends Mock implements RTCRtpSenderNative {}

class _Receiver extends Mock implements RTCRtpReceiverNative {}

class _KeyProvider extends Mock implements rtc.KeyProvider {}

class _LocalParticipant extends Mock implements LocalParticipant {}

class _RemoteParticipant extends Mock implements RemoteParticipant {}

class _LocalVideo extends Mock implements LocalVideoTrack {}

class _RemoteVideo extends Mock implements RemoteVideoTrack {}

class _LocalPublication extends Mock
    implements LocalTrackPublication<LocalVideoTrack> {}

class _RemotePublication extends Mock
    implements RemoteTrackPublication<RemoteVideoTrack> {}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('AV1 sender/receiver and VP8 backup encrypt, rotate, and clean up',
      () async {
    final messenger =
        TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
    const channel = MethodChannel('FlutterWebRTC.Method');
    final calls = <MethodCall>[];
    final eventChannels = <MethodChannel>[];
    var nextId = 0;
    messenger.setMockMethodCallHandler(channel, (call) async {
      calls.add(call);
      if (call.method == 'frameCryptorFactoryCreateFrameCryptor') {
        final id = 'cryptor-${nextId++}';
        final events = MethodChannel('FlutterWebRTC/frameCryptorEvent$id');
        eventChannels.add(events);
        messenger.setMockMethodCallHandler(events, (_) async => null);
        return {'frameCryptorId': id};
      }
      if (call.method == 'createDataPacketCryptor') {
        return {'dataCryptorId': 'data'};
      }
      if (call.method == 'initialize') {
        return null;
      }
      return {'result': true};
    });
    addTearDown(() {
      messenger.setMockMethodCallHandler(channel, null);
      for (final events in eventChannels) {
        messenger.setMockMethodCallHandler(events, null);
      }
    });

    final nativeKey = _KeyProvider();
    when(() => nativeKey.id).thenReturn('key');
    final manager = E2EEManager(BaseKeyProvider(
        nativeKey,
        rtc.KeyProviderOptions(
            sharedKey: true,
            ratchetSalt: Uint8List(0),
            ratchetWindowSize: 16)));
    final room = Room();
    await manager.setup(room);
    final local = _LocalParticipant();
    when(() => local.identity).thenReturn('alice');
    final sender = _Sender();
    when(() => sender.senderId).thenReturn('primary');
    when(() => sender.peerConnectionId).thenReturn('pc');
    final video = _LocalVideo();
    when(() => video.codec).thenReturn('av1');
    when(() => video.sender).thenReturn(sender);
    final publication = _LocalPublication();
    when(() => publication.track).thenReturn(video);
    when(() => publication.sid).thenReturn('video');
    when(() => publication.encryptionType).thenReturn(EncryptionType.kGcm);

    // The initial sender is protected before negotiation; publishing binds its SID.
    await manager.addRtpSender(sender: sender, identity: 'alice', sid: 'cid');
    room.events.emit(
        LocalTrackPublishedEvent(participant: local, publication: publication));
    await Future<void>.delayed(Duration.zero);
    await manager.setKeyIndex(3, participantIdentity: 'alice');
    final backup = _Sender();
    when(() => backup.senderId).thenReturn('backup');
    when(() => backup.peerConnectionId).thenReturn('pc');
    await manager.addRtpSender(sender: backup, identity: 'alice', sid: 'video');
    expect((calls.last.arguments as Map)['keyIndex'],
        3); // Late backup uses the rotated key.

    final remote = _RemoteParticipant();
    when(() => remote.identity).thenReturn('bob');
    final receiver = _Receiver();
    when(() => receiver.receiverId).thenReturn('receiver');
    when(() => receiver.peerConnectionId).thenReturn('pc');
    final remoteVideo = _RemoteVideo();
    when(() => remoteVideo.receiver).thenReturn(receiver);
    final remotePublication = _RemotePublication();
    when(() => remotePublication.sid).thenReturn('remote-video');
    when(() => remotePublication.mimeType).thenReturn('video/AV1');
    when(() => remotePublication.encryptionType)
        .thenReturn(EncryptionType.kGcm);
    room.events.emit(TrackSubscribedEvent(
        participant: remote,
        publication: remotePublication,
        track: remoteVideo));
    await Future<void>.delayed(Duration.zero);

    final created = calls
        .where((call) => call.method == 'frameCryptorFactoryCreateFrameCryptor')
        .toList();
    expect(created, hasLength(3)); // Publishing reused the primary cryptor.
    expect(created.map((call) => (call.arguments as Map)['type']),
        ['sender', 'sender', 'receiver']);
    calls.clear();
    await manager.setKeyIndex(3, participantIdentity: 'alice');
    expect(calls.where((call) => call.method == 'frameCryptorSetKeyIndex'),
        hasLength(2));
    expect(calls.every((call) => (call.arguments as Map)['keyIndex'] == 3),
        isTrue);
    calls.clear();
    room.events.emit(LocalTrackUnpublishedEvent(
        participant: local, publication: publication));
    room.events.emit(TrackUnsubscribedEvent(
        participant: remote,
        publication: remotePublication,
        track: remoteVideo));
    await Future<void>.delayed(Duration.zero);
    expect(calls.where((call) => call.method == 'frameCryptorDispose'),
        hasLength(3));
    calls.clear();
    await manager.setKeyIndex(4, participantIdentity: 'alice');
    expect(calls, isEmpty);
    // Native cryptor setup failures detach the media before any later negotiation.
    messenger.setMockMethodCallHandler(channel, (call) async {
      throw PlatformException(code: 'cryptor-unavailable');
    });
    when(() => backup.replaceTrack(null)).thenAnswer((_) async {});
    await expectLater(
      manager.addRtpSender(sender: backup, identity: 'alice', sid: 'failed'),
      throwsA(isA<String>()),
    );
    verify(() => backup.replaceTrack(null)).called(1);
    await room.dispose();
  });
}
