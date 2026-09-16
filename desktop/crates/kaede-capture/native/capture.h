#pragma once
#include <cstddef>
#include <cstdint>

// Callbacks are synchronous and valid only during kaede_capture_run. Audio is
// interleaved signed PCM, 48 kHz stereo; video is packed BGRA. Implementations
// must drain all OS callbacks before returning, including on cancellation.
struct CaptureOptions {
    uint64_t window;
    uint32_t process; // 0 = all output; UINT32_MAX = resolve the window owner
    uint32_t width;
    uint32_t height;
    uint32_t fps;
    bool audio;
};
struct CaptureCallbacks {
    void *context;
    bool (*stopped)(void *);
    void (*audio)(void *, const int16_t *, size_t);
    void (*video)(void *, const uint8_t *, uint32_t, uint32_t, size_t);
    void (*status)(void *, const char *); // nullptr = ready; otherwise fatal error
};
extern "C" void kaede_capture_run(const CaptureOptions *, const CaptureCallbacks *);
extern "C" void kaede_audio_apps(void *, void (*)(void *, uint32_t, const char *));
