package org.webrtc.video;

import androidx.annotation.Nullable;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.os.Build;
import org.webrtc.HardwareVideoEncoderFactory;

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
    public String hardwareEncoderForSettings(String codec, int width, int height, int fps, int bitrate) {
        if (Build.VERSION.SDK_INT < 29 || forceSWCodec || width <= 0 || height <= 0 || fps <= 0 || bitrate <= 0) return null;
        for (String forced : forceSWCodecs) if (forced.equalsIgnoreCase(codec)) return null;
        for (VideoCodecInfo info : hardwareFactory.getSupportedCodecs()) {
            if (!info.name.equalsIgnoreCase(codec)) continue;
            VideoEncoder encoder = hardwareFactory.createEncoder(info);
            if (encoder == null) continue;
            String selected = encoder.getImplementationName();
            // HardwareVideoEncoder construction does not allocate a MediaCodec;
            // release it on the creating thread before inspecting capabilities.
            encoder.release();
            for (MediaCodecInfo candidate : new MediaCodecList(MediaCodecList.ALL_CODECS).getCodecInfos()) {
                if (!candidate.getName().equals(selected) || !candidate.isEncoder() || !candidate.isHardwareAccelerated()) continue;
                String mime = codec.equalsIgnoreCase("av1") ? "video/av01" : codec.equalsIgnoreCase("vp8") ? "video/x-vnd.on2.vp8" : "video/avc";
                try {
                    MediaCodecInfo.VideoCapabilities video = candidate.getCapabilitiesForType(mime).getVideoCapabilities();
                    return video != null && video.areSizeAndRateSupported(width, height, fps) && video.getBitrateRange().contains(bitrate) ? selected : null;
                } catch (IllegalArgumentException ignored) { return null; }
            }
        }
        return null;
    }

    @Nullable
    @Override
    public VideoEncoder createEncoder(VideoCodecInfo videoCodecInfo) {
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
