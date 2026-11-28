// Exercise the vendored SDK's publication entry point and signaling boundary.
// ignore_for_file: implementation_imports, invalid_use_of_internal_member

import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart' as rtc;
import 'package:livekit_client/livekit_client.dart';
import 'package:livekit_client/src/core/engine.dart';
import 'package:livekit_client/src/core/transport.dart';
import 'package:livekit_client/src/proto/livekit_models.pb.dart' as models;
import 'package:livekit_client/src/proto/livekit_rtc.pb.dart' as signal;
import 'package:mocktail/mocktail.dart';

class _Room extends Mock implements Room {}

class _Engine extends Mock implements Engine {}

class _Transport extends Mock implements Transport {}

class _PeerConnection extends Mock implements rtc.RTCPeerConnection {}

class _MediaStreamTrack extends Mock implements rtc.MediaStreamTrack {}

class _Encryption extends Mock implements E2EEManager {}

class _Keys extends Fake implements BaseKeyProvider {}

class _Microphone extends Mock implements LocalAudioTrack {}

class _Transceiver extends Fake implements rtc.RTCRtpTransceiver {}

class _Sender extends Fake implements rtc.RTCRtpSender {}

class _Cryptor extends Fake implements rtc.FrameCryptor {}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() {
    registerFallbackValue(signal.AddTrackRequest());
    registerFallbackValue(rtc.RTCRtpTransceiverInit());
  });

  for (final rejectPublication in [false, true]) {
    test(
        rejectPublication
            ? 'a rejected microphone publication discards its encrypted sender'
            : 'an unavailable microphone cryptor prevents negotiation',
        () async {
      final room = _Room();
      final engine = _Engine();
      final encryption = _Encryption();
      final events = EventsEmitter<RoomEvent>();
      addTearDown(events.dispose);
      when(() => room.events).thenReturn(events);
      when(() => room.engine).thenReturn(engine);
      when(() => room.e2eeManager).thenReturn(encryption);
      when(() => room.roomOptions).thenReturn(
          RoomOptions(encryption: E2EEOptions(keyProvider: _Keys())));
      final local = await LocalParticipant.createFromInfo(
          room: room, info: models.ParticipantInfo(identity: 'alice'));
      addTearDown(local.dispose);

      final mic = _Microphone();
      final sender = _Sender();
      const options = AudioPublishOptions();
      when(() => mic.isActive).thenReturn(false);
      when(() => mic.start()).thenAnswer((_) async => true);
      when(() => mic.stop()).thenAnswer((_) async => true);
      when(() => mic.getCid()).thenReturn('mic');
      when(() => mic.kind).thenReturn(TrackType.AUDIO);
      when(() => mic.source).thenReturn(TrackSource.microphone);
      when(() => mic.muted).thenReturn(false);
      when(() => mic.sender).thenReturn(sender);
      final publisher = _Transport();
      final peerConnection = _PeerConnection();
      final mediaTrack = _MediaStreamTrack();
      when(() => engine.publisher).thenReturn(publisher);
      when(() => publisher.pc).thenReturn(peerConnection);
      when(() => mic.mediaStreamTrack).thenReturn(mediaTrack);
      when(() => mediaTrack.kind).thenReturn('audio');
      when(() => peerConnection.addTransceiver(
            track: mediaTrack,
            kind: rtc.RTCRtpMediaType.RTCRtpMediaTypeAudio,
            init: any(named: 'init'),
          )).thenAnswer((_) async => _Transceiver());

      // Exercise legacy sequential signaling and concurrent modern signaling.
      final addTrack = Completer<models.TrackInfo>();
      when(() => engine.enabledPublishCodecs).thenReturn(
          rejectPublication ? [models.Codec(mime: 'audio/opus')] : []);
      when(() => engine.addTrack(any())).thenAnswer((_) => addTrack.future);
      if (!rejectPublication) addTrack.complete(models.TrackInfo());
      final cryptor = Completer<rtc.FrameCryptor>();
      when(() => encryption.addRtpSender(
          sender: sender,
          identity: 'alice',
          sid: 'mic')).thenAnswer((_) => cryptor.future);
      when(() => engine.negotiate()).thenAnswer((_) async {});
      when(() => encryption.discardUnpublishedSender(
          sender: sender,
          identity: 'alice',
          sid: 'mic')).thenAnswer((_) async {});

      final failure = StateError(
          rejectPublication ? 'addTrack rejected' : 'cryptor unavailable');
      final checked = expectLater(
          local.publishAudioTrack(mic, publishOptions: options),
          throwsA(same(failure)));
      await Future<void>.delayed(Duration.zero);
      verify(() => encryption.addRtpSender(
          sender: sender, identity: 'alice', sid: 'mic')).called(1);
      verifyNever(() => engine.negotiate());
      if (rejectPublication) {
        cryptor.complete(_Cryptor());
        await Future<void>.delayed(Duration.zero);
        verify(() => engine.negotiate()).called(1);
        addTrack.completeError(failure);
      } else {
        cryptor.completeError(failure);
      }
      await checked;
      if (rejectPublication) {
        verify(() => encryption.discardUnpublishedSender(
            sender: sender, identity: 'alice', sid: 'mic')).called(1);
      } else {
        verifyNever(() => engine.negotiate());
      }
      verify(() => mic.stop()).called(1);
      expect(local.audioTrackPublications, isEmpty);
    });
  }
}
