package com.cloudwebrtc.webrtc;

import java.util.HashSet;
import java.util.Set;

/** Native track and stream disposal can both release a track: count identities. */
final class SharedCaptureReferences {
    private final Set<String> tracks = new HashSet<>();

    SharedCaptureReferences(String owner) { tracks.add(owner); }
    boolean contains(String track) { return tracks.contains(track); }
    void retain(String track) { tracks.add(track); }
    boolean releaseLast(String track) { return tracks.remove(track) && tracks.isEmpty(); }
}
