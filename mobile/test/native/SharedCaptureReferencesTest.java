package com.cloudwebrtc.webrtc;

public final class SharedCaptureReferencesTest {
    public static void main(String[] args) {
        SharedCaptureReferences capture = new SharedCaptureReferences("camera");
        capture.retain("av1");
        assert !capture.releaseLast("camera") : "camera disposal stopped AV1 capture";
        assert !capture.releaseLast("camera") : "stream disposal double-released owner";
        assert !capture.contains("camera") : "disposed capture ID could be cloned";
        capture.retain("fallback");
        assert !capture.releaseLast("av1") : "codec switch stopped replacement";
        assert capture.releaseLast("fallback") : "last track did not release source";
        assert !capture.releaseLast("fallback") : "source disposed twice";
        SharedCaptureReferences reverse = new SharedCaptureReferences("screen");
        reverse.retain("av1");
        assert !reverse.releaseLast("av1");
        assert reverse.releaseLast("screen");
    }
}
