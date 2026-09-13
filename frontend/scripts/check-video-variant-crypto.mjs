import { afterEach, expect, it } from 'vitest';
import { KEY_PROVIDER_DEFAULTS } from '@livekit-e2ee/constants';
import { createKeyMaterialFromString } from '@livekit-e2ee/utils';
import { FrameCryptor, encryptionEnabledMap } from '@livekit-e2ee/worker/FrameCryptor';
import { ParticipantKeyHandler } from '@livekit-e2ee/worker/ParticipantKeyHandler';

afterEach(() => encryptionEnabledMap.clear());

it.each([true, false])(
  'camera/share variants preserve encrypted=%s through key rotation and late joins',
  async (encrypted) => {
    const identity = 'variant-publisher';
    encryptionEnabledMap.set(identity, encrypted);
    const keys = new ParticipantKeyHandler(identity, KEY_PROVIDER_DEFAULTS);
    const receiverKeys = new ParticipantKeyHandler(identity, KEY_PROVIDER_DEFAULTS);
    const cryptor = (handler, codec) => {
      const value = new FrameCryptor({
        participantIdentity: identity,
        keys: handler,
        keyProviderOptions: KEY_PROVIDER_DEFAULTS,
        sifTrailer: new Uint8Array()
      });
      value.setVideoCodec(codec);
      return value;
    };
    const variants = ['camera', 'screen_share'].flatMap((source) =>
      ['av1', 'vp8', 'h264'].map((codec) => ({
        source,
        codec,
        encoder: cryptor(keys, codec),
        decoder: cryptor(receiverKeys, codec)
      }))
    );
    for (const epoch of [0, 1]) {
      const material = await createKeyMaterialFromString(`variant-key-${epoch}`);
      await keys.setKey(material, epoch);
      await receiverKeys.setKey(material, epoch);
      for (const [index, variant] of variants.entries()) {
        const data =
          variant.codec === 'av1'
            ? new Uint8Array([0x0a, 2, 0, 0xaa, 0x32, 5, 0, 11, 22, 33, 44])
            : variant.codec === 'h264'
              ? new Uint8Array([0, 0, 0, 1, 0x65, 0x88, 0x84, 0x21, 11, 22, 33, 44])
              : new Uint8Array([0, 0, 0, 0x9d, 1, 0x2a, 4, 0, 4, 0, 11, 22, 33, 44]);
        const frame = {
          data: data.slice().buffer,
          timestamp: epoch * 100 + index,
          type: 'key',
          getMetadata: () => ({ synchronizationSource: index + 1 })
        };
        if (encrypted) {
          const noKey = cryptor(
            new ParticipantKeyHandler(identity, KEY_PROVIDER_DEFAULTS),
            variant.codec
          );
          let leaked;
          await noKey.encodeFunction(
            { ...frame, data: data.slice().buffer },
            {
              enqueue: (value) => {
                leaked = value;
              }
            }
          );
          expect(leaked).toBeUndefined();
        }
        let encoded;
        await variant.encoder.encodeFunction(frame, {
          enqueue: (value) => {
            encoded = value;
          }
        });
        expect(encoded).toBeDefined();
        if (encrypted) expect(new Uint8Array(encoded.data)).not.toEqual(data);
        else expect(new Uint8Array(encoded.data)).toEqual(data);
        // A late subscriber after rotation uses the same current room key.
        const decoder = epoch ? cryptor(receiverKeys, variant.codec) : variant.decoder;
        let decoded;
        await decoder.decodeFunction(encoded, {
          enqueue: (value) => {
            decoded = value;
          }
        });
        expect(new Uint8Array(decoded.data)).toEqual(data);
        expect(encryptionEnabledMap.get(identity)).toBe(encrypted);
      }
    }
  }
);
