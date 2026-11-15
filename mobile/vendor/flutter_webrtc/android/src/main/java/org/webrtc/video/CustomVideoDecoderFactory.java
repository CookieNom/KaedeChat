package org.webrtc.video;

import androidx.annotation.Nullable;

import org.webrtc.EglBase;
import org.webrtc.HardwareVideoDecoderFactory;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.os.Build;
import org.webrtc.SoftwareVideoDecoderFactory;
import org.webrtc.VideoCodecInfo;
import org.webrtc.VideoDecoder;
import org.webrtc.VideoDecoderFactory;
import org.webrtc.WrappedVideoDecoderFactory;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class CustomVideoDecoderFactory implements VideoDecoderFactory {
    private SoftwareVideoDecoderFactory softwareVideoDecoderFactory = new SoftwareVideoDecoderFactory();
    private WrappedVideoDecoderFactory wrappedVideoDecoderFactory;
    private HardwareVideoDecoderFactory hardwareFactory;
    private boolean forceSWCodec  = false;

    private List<String> forceSWCodecs = new ArrayList<>();

    public  CustomVideoDecoderFactory(EglBase.Context sharedContext) {
        this.wrappedVideoDecoderFactory = new WrappedVideoDecoderFactory(sharedContext);
        this.hardwareFactory = new HardwareVideoDecoderFactory(sharedContext);
    }

    public void setForceSWCodec(boolean forceSWCodec) {
        this.forceSWCodec = forceSWCodec;
    }

    public void setForceSWCodecList(List<String> forceSWCodecs) {
        this.forceSWCodecs = forceSWCodecs;
    }

    public boolean hasHardwareDecoder(String codec) {
        if (Build.VERSION.SDK_INT < 29 || forceSWCodec) return false;
        for (String forced : forceSWCodecs) if (forced.equalsIgnoreCase(codec)) return false;
        for (VideoCodecInfo info : hardwareFactory.getSupportedCodecs()) {
            if (!info.name.equalsIgnoreCase(codec)) continue;
            VideoDecoder decoder = hardwareFactory.createDecoder(info);
            if (decoder == null) continue;
            try {
                String name = decoder.getImplementationName();
                for (MediaCodecInfo candidate : new MediaCodecList(MediaCodecList.REGULAR_CODECS).getCodecInfos()) {
                    if (candidate.getName().equals(name) && !candidate.isEncoder() && candidate.isHardwareAccelerated()) return true;
                }
            } finally { decoder.release(); }
        }
        return false;
    }

    @Nullable
    @Override
    public VideoDecoder createDecoder(VideoCodecInfo videoCodecInfo) {
        if (videoCodecInfo.name.equalsIgnoreCase("h265")) {
            return hasHardwareDecoder("h265") ? hardwareFactory.createDecoder(videoCodecInfo) : null;
        }
        if(forceSWCodec) {
            return softwareVideoDecoderFactory.createDecoder(videoCodecInfo);
        }
        if(!forceSWCodecs.isEmpty()) {
            if(forceSWCodecs.contains(videoCodecInfo.name)) {
                return softwareVideoDecoderFactory.createDecoder(videoCodecInfo);
            }
        }
        return wrappedVideoDecoderFactory.createDecoder(videoCodecInfo);
    }

    @Override
    public VideoCodecInfo[] getSupportedCodecs() {
        VideoCodecInfo[] codecs;
        if(forceSWCodec && forceSWCodecs.isEmpty()) {
            codecs = softwareVideoDecoderFactory.getSupportedCodecs();
        } else {
            codecs = wrappedVideoDecoderFactory.getSupportedCodecs();
        }

        return withHigherH264Level(codecs);
    }

    private VideoCodecInfo[] withHigherH264Level(VideoCodecInfo[] codecs) {
        VideoCodecInfo[] adjustedCodecs = new VideoCodecInfo[codecs.length];
        for (int i = 0; i < codecs.length; i++) {
            VideoCodecInfo codec = codecs[i];
            if (!"H264".equalsIgnoreCase(codec.name)) {
                adjustedCodecs[i] = codec;
                continue;
            }

            String profileLevelId = codec.params.get("profile-level-id");
            if (profileLevelId == null || profileLevelId.length() != 6) {
                adjustedCodecs[i] = codec;
                continue;
            }

            String profile = profileLevelId.substring(0, 4);
            String level = profileLevelId.substring(4);
            if (Integer.parseInt(level, 16) >= 0x29) {
                adjustedCodecs[i] = codec;
                continue;
            }

            Map<String, String> params = new HashMap<>(codec.params);
            params.put("profile-level-id", profile + "29");
            adjustedCodecs[i] = new VideoCodecInfo(codec.name, params, codec.scalabilityModes);
        }

        return adjustedCodecs;
    }
}
