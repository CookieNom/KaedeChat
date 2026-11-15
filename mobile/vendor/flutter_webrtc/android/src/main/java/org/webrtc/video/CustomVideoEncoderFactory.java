package org.webrtc.video;

import androidx.annotation.Nullable;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.os.Build;
import org.webrtc.HardwareVideoEncoderFactory;
import org.webrtc.EncodedImage;
import org.webrtc.JavaI420Buffer;
import org.webrtc.VideoFrame;
import org.webrtc.VideoCodecStatus;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import com.cloudwebrtc.webrtc.SimulcastVideoEncoderFactoryWrapper;

import org.webrtc.EglBase;
import org.webrtc.SoftwareVideoEncoderFactory;
import org.webrtc.VideoCodecInfo;
import org.webrtc.VideoEncoder;
import org.webrtc.VideoEncoderFactory;

import java.util.ArrayList;
import java.util.List;

public class CustomVideoEncoderFactory implements VideoEncoderFactory {
    private SoftwareVideoEncoderFactory softwareVideoEncoderFactory = new SoftwareVideoEncoderFactory();
    private SimulcastVideoEncoderFactoryWrapper simulcastVideoEncoderFactoryWrapper;

    private HardwareVideoEncoderFactory hardwareFactory;

    private boolean forceSWCodec  = false;

    private List<String> forceSWCodecs = new ArrayList<>();

    public CustomVideoEncoderFactory(EglBase.Context sharedContext,
                                     boolean enableIntelVp8Encoder,
                                     boolean enableH264HighProfile) {
        this.hardwareFactory = new HardwareVideoEncoderFactory(sharedContext, enableIntelVp8Encoder, enableH264HighProfile);
        this.simulcastVideoEncoderFactoryWrapper = new SimulcastVideoEncoderFactoryWrapper(sharedContext, enableIntelVp8Encoder, enableH264HighProfile);
    }

    public void setForceSWCodec(boolean forceSWCodec) {
        this.forceSWCodec = forceSWCodec;
    }

    public void setForceSWCodecList(List<String> forceSWCodecs) {
        this.forceSWCodecs = forceSWCodecs;
    }

    // Query the encoder selected by the same primary factory used for sending,
    // not an arbitrary MediaCodec which WebRTC may never choose.
    public String hardwareEncoderForSettings(String codec, int width, int height, int fps, int bitrate,
                                             String scalabilityMode) {
        // Java's encoder settings do not carry scalability modes. Prove only the
        // single-layer configuration that this bridge can actually initialize.
        if (!"L1T1".equals(scalabilityMode) || Build.VERSION.SDK_INT < 29 || forceSWCodec ||
            width <= 0 || height <= 0 || width > 4096 || height > 4096 ||
            fps <= 0 || fps > 120 || bitrate <= 0 || bitrate > 100000000) return null;
        final String mime;
        switch (codec.toLowerCase(java.util.Locale.ROOT)) {
            case "av1": mime = "video/av01"; break;
            case "vp8": mime = "video/x-vnd.on2.vp8"; break;
            case "vp9": mime = "video/x-vnd.on2.vp9"; break;
            case "h264": mime = "video/avc"; break;
            case "h265": mime = "video/hevc"; break;
            default: return null;
        }
        for (String forced : forceSWCodecs) if (forced.equalsIgnoreCase(codec)) return null;
        for (VideoCodecInfo info : hardwareFactory.getSupportedCodecs()) {
            if (!info.name.equalsIgnoreCase(codec)) continue;
            VideoEncoder encoder = hardwareFactory.createEncoder(info);
            if (encoder == null) continue;
            try {
                String selected = encoder.getImplementationName();
                boolean capable = false;
                for (MediaCodecInfo candidate : new MediaCodecList(MediaCodecList.ALL_CODECS).getCodecInfos()) {
                    if (!candidate.getName().equals(selected) || !candidate.isEncoder() || !candidate.isHardwareAccelerated()) continue;
                    MediaCodecInfo.VideoCapabilities video = candidate.getCapabilitiesForType(mime).getVideoCapabilities();
                    capable = video != null && video.areSizeAndRateSupported(width, height, fps) && video.getBitrateRange().contains(bitrate);
                    break;
                }
                if (!capable) continue;
                final int frames = Math.max(1, Math.min(15, fps / 2));
                CountDownLatch output = new CountDownLatch(frames);
                AtomicBoolean failed = new AtomicBoolean(false);
                VideoEncoder.Settings settings = new VideoEncoder.Settings(
                    Math.max(1, Runtime.getRuntime().availableProcessors()), width, height,
                    Math.max(1, bitrate / 1000), fps, 1, false);
                if (encoder.initEncode(settings, (image, details) -> {
                    if (image.encodedWidth != width || image.encodedHeight != height || image.buffer.remaining() == 0) failed.set(true);
                    output.countDown();
                }) != VideoCodecStatus.OK) continue;
                if (encoder.setRateAllocation(new VideoEncoder.BitrateAllocation(new int[][] {{bitrate}}), fps) != VideoCodecStatus.OK) continue;
                long started = System.nanoTime();
                for (int i = 0; i < frames; i++) {
                    // Burst submission measures queue depth and drops frames on otherwise capable hardware.
                    long waitNanos = started + i * 1000000000L / fps - System.nanoTime();
                    if (waitNanos > 0) TimeUnit.NANOSECONDS.sleep(waitNanos);
                    JavaI420Buffer buffer = JavaI420Buffer.allocate(width, height);
                    // An initialized synthetic image avoids reading uninitialized pixel memory.
                    java.nio.ByteBuffer y = buffer.getDataY(), u = buffer.getDataU(), v = buffer.getDataV();
                    while (y.hasRemaining()) y.put((byte)64);
                    while (u.hasRemaining()) u.put((byte)128);
                    while (v.hasRemaining()) v.put((byte)128);
                    VideoFrame frame = new VideoFrame(buffer, 0, i * 1000000000L / fps);
                    try {
                        EncodedImage.FrameType type = i == 0 ? EncodedImage.FrameType.VideoFrameKey : EncodedImage.FrameType.VideoFrameDelta;
                        if (encoder.encode(frame, new VideoEncoder.EncodeInfo(new EncodedImage.FrameType[] {type})) != VideoCodecStatus.OK) {
                            failed.set(true);
                            break;
                        }
                    } finally { frame.release(); }
                }
                boolean complete = output.await(1500, TimeUnit.MILLISECONDS);
                if (complete && !failed.get() &&
                    System.nanoTime() - started <= (long)(1000000000.0 * frames / fps * 1.25)) return selected;
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return null;
            } catch (RuntimeException unavailable) {
                // A listed encoder can still reject initialization or run out of sessions.
            } finally { encoder.release(); }
        }
        return null;
    }

    @Nullable
    @Override
    public VideoEncoder createEncoder(VideoCodecInfo videoCodecInfo) {
        if (videoCodecInfo.name.equalsIgnoreCase("h265")) {
            if (forceSWCodec) return null;
            for (String codec : forceSWCodecs) if (codec.equalsIgnoreCase("h265")) return null;
            return hardwareFactory.createEncoder(videoCodecInfo);
        }
        if(forceSWCodec) {
            return softwareVideoEncoderFactory.createEncoder(videoCodecInfo);
        }

        if(!forceSWCodecs.isEmpty()) {
            if(forceSWCodecs.contains(videoCodecInfo.name)) {
                return softwareVideoEncoderFactory.createEncoder(videoCodecInfo);
            }
        }

        return simulcastVideoEncoderFactoryWrapper.createEncoder(videoCodecInfo);
    }

    @Override
    public VideoCodecInfo[] getSupportedCodecs() {
        if(forceSWCodec && forceSWCodecs.isEmpty()) {
            return softwareVideoEncoderFactory.getSupportedCodecs();
        }
        return simulcastVideoEncoderFactoryWrapper.getSupportedCodecs();
    }
}
