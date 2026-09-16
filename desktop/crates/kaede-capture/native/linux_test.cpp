// Integration test against an isolated PulseAudio server. See desktop/test-screen-audio.sh.
#include "capture.h"
#include <pulse/simple.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <string>

struct Observation {
    std::chrono::steady_clock::time_point deadline = std::chrono::steady_clock::now() + std::chrono::seconds(4);
    int left = 0, right = 0;
    bool ready = false, failed = false;
};
int main(int argc, char **argv) {
    if (argc < 3) return 2;
    if (std::string(argv[1]) == "play") {
        const int channel = std::atoi(argv[2]);
        pa_sample_spec format{PA_SAMPLE_S16LE, 48000, 2};
        auto *stream = pa_simple_new(nullptr, channel ? "Other app" : "Selected app", PA_STREAM_PLAYBACK,
            nullptr, "Isolation test", &format, nullptr, nullptr, nullptr);
        if (!stream) return 3;
        std::array<int16_t, 960> data{};
        for (size_t i = channel; i < data.size(); i += 2) data[i] = channel ? 9000 : 6000;
        while (pa_simple_write(stream, data.data(), sizeof(data), nullptr) >= 0) {}
        pa_simple_free(stream);
        return 0;
    }
    const uint32_t pid = static_cast<uint32_t>(std::strtoul(argv[2], nullptr, 10));
    Observation seen;
    CaptureOptions options{0, pid, 1280, 720, 30, true};
    CaptureCallbacks cb{
        &seen,
        [](void *data) { auto &s = *static_cast<Observation *>(data); return s.failed || std::chrono::steady_clock::now() >= s.deadline; },
        [](void *data, const int16_t *samples, size_t count) {
            auto &s = *static_cast<Observation *>(data);
            for (size_t i = 0; i + 1 < count; i += 2) {
                s.left = std::max(s.left, std::abs(int(samples[i])));
                s.right = std::max(s.right, std::abs(int(samples[i + 1])));
            }
        },
        [](void *, const uint8_t *, uint32_t, uint32_t, size_t) {},
        [](void *data, const char *error) {
            auto &s = *static_cast<Observation *>(data);
            if (error) { s.failed = true; std::cerr << error << '\n'; } else s.ready = true;
        }
    };
    kaede_capture_run(&options, &cb);
    std::cout << "pid=" << pid << " ready=" << seen.ready << " failed=" << seen.failed
              << " left=" << seen.left << " right=" << seen.right << '\n';
    if (std::string(argv[1]) == "missing") return seen.failed && !seen.left && !seen.right ? 0 : 1;
    return seen.ready && !seen.failed && seen.left > 1000 && (pid ? seen.right < 50 : seen.right > 1000) ? 0 : 1;
}
