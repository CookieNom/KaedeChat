import { afterEach, expect, it } from 'vitest';
import { KEY_PROVIDER_DEFAULTS } from '@livekit-e2ee/constants';
import { createKeyMaterialFromString } from '@livekit-e2ee/utils';
import { FrameCryptor, setLocalEncryptionEnabled } from '@livekit-e2ee/worker/FrameCryptor';
import { ParticipantKeyHandler } from '@livekit-e2ee/worker/ParticipantKeyHandler';

afterEach(() => setLocalEncryptionEnabled(undefined));

it.each([true, false])(
  'camera/share variants preserve encrypted=%s through key rotation and late joins',
  async (encrypted) => {
    const identity = 'variant-publisher';
    setLocalEncryptionEnabled(encrypted);
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
      value.setEncrypted(encrypted);
      return value;
    };
    const variants = ['camera', 'screen_share'].flatMap((source) =>
      ['av1', 'h265', 'vp9', 'vp8', 'h264'].map((codec) => ({
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
            : variant.codec === 'h265'
              ? new Uint8Array([
                  0,
                  0,
                  0,
                  1,
                  0x40,
                  1,
                  ...new Array(300).fill(0x55),
                  0,
                  0,
                  0,
                  1,
                  0x26,
                  1,
                  0x80,
                  11,
                  22,
                  33,
                  44
                ])
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
        expect(decoder.isEnabled()).toBe(encrypted);
      }
    }
  }
);

it.each([3, 4])(
  'authenticates HEVC key/delta frames with %s-byte Annex B start codes',
  async (startCodeSize) => {
    const identity = 'hevc-publisher';
    setLocalEncryptionEnabled(true);
    const keys = new ParticipantKeyHandler(identity, KEY_PROVIDER_DEFAULTS);
    await keys.setKey(await createKeyMaterialFromString('hevc-key'), 0);
    const make = () => {
      const value = new FrameCryptor({
        participantIdentity: identity,
        keys,
        keyProviderOptions: KEY_PROVIDER_DEFAULTS,
        sifTrailer: new Uint8Array()
      });
      value.setVideoCodec('h265');
      value.setEncrypted(true);
      return value;
    };
    const encoder = make();
    for (const [index, nal] of [0x26, 0x02].entries()) {
      const prefix = startCodeSize === 3 ? [0, 0, 1] : [0, 0, 0, 1];
      const leading = [0x40, 1, ...new Array(300).fill(0x55)];
      const slice = [nal, 1, 0x80, 11, 22, 33, 44];
      const input = new Uint8Array([...prefix, ...leading, ...prefix, ...slice]);
      const expected = new Uint8Array([0, 0, 0, 1, ...leading, 0, 0, 0, 1, ...slice]);
      const frame = {
        data: input.buffer,
        type: index ? 'delta' : 'key',
        timestamp: index,
        getMetadata: () => ({ synchronizationSource: 123 })
      };
      let encrypted;
      await encoder.encodeFunction(frame, {
        enqueue: (value) => {
          encrypted = value;
        }
      });
      expect(encrypted).toBeDefined();
      // Clear parameter sets beyond byte 255 are authenticated, as are the slice and tag.
      for (const offset of [270, expected.length - 1, encrypted.data.byteLength - 16]) {
        const data = new Uint8Array(encrypted.data.slice(0));
        data[offset] ^= 0x40;
        let leaked;
        await make().decodeFunction(
          { ...frame, data: data.buffer },
          {
            enqueue: (value) => {
              leaked = value;
            }
          }
        );
        expect(leaked).toBeUndefined();
      }
      let decrypted;
      await make().decodeFunction(encrypted, {
        enqueue: (value) => {
          decrypted = value;
        }
      });
      expect(new Uint8Array(decrypted.data)).toEqual(expected);
    }
  }
);
