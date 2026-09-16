#include "capture.h"
#include <pulse/pulseaudio.h>
#include <xcb/xcb.h>
#include <vector>
#include <algorithm>
#include <array>
#include <chrono>
#include <deque>
#include <fstream>
#include <map>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unistd.h>

using Clock = std::chrono::steady_clock;
using namespace std::chrono_literals;

namespace {
struct Process {
    uint32_t parent = 0;
    uint64_t started = 0;
};
Process process_info(uint32_t pid) {
    std::ifstream file("/proc/" + std::to_string(pid) + "/stat");
    std::string line;
    std::getline(file, line);
    const auto end = line.rfind(')');
    if (end == std::string::npos) return {};
    std::istringstream fields(line.substr(end + 2));
    std::string field;
    Process result;
    for (int index = 3; index <= 22 && fields >> field; ++index) {
        if (index == 4) result.parent = std::stoul(field);
        if (index == 22) result.started = std::stoull(field);
    }
    return result;
}
bool in_tree(uint32_t pid, uint32_t root) {
    for (int depth = 0; pid > 1 && depth < 128; ++depth) {
        if (pid == root) return true;
        pid = process_info(pid).parent;
    }
    return false;
}
std::string executable(uint32_t pid) {
    std::array<char, 4096> path{};
    const auto length = readlink(("/proc/" + std::to_string(pid) + "/exe").c_str(), path.data(), path.size());
    return length > 0 ? std::string(path.data(), static_cast<size_t>(length)) : std::string();
}
uint32_t application_root(uint32_t pid) {
    const auto image = executable(pid);
    if (image.empty()) return pid;
    for (int depth = 0; depth < 128; ++depth) {
        const auto parent = process_info(pid).parent;
        if (parent <= 1 || executable(parent) != image) break;
        pid = parent;
    }
    return pid;
}
uint32_t window_process(uint64_t window) {
    xcb_connection_t *connection = xcb_connect(nullptr, nullptr);
    if (!connection || xcb_connection_has_error(connection)) {
        if (connection) xcb_disconnect(connection);
        throw std::runtime_error("Could not identify the selected app. Choose its audio in the app audio chooser.");
    }
    const char name[] = "_NET_WM_PID";
    auto *atom = xcb_intern_atom_reply(connection, xcb_intern_atom(connection, 1, sizeof(name) - 1, name), nullptr);
    uint32_t pid = 0;
    if (atom && atom->atom != XCB_ATOM_NONE) {
        auto *property = xcb_get_property_reply(connection,
            xcb_get_property(connection, 0, static_cast<xcb_window_t>(window), atom->atom, XCB_ATOM_CARDINAL, 0, 1), nullptr);
        if (property && property->type == XCB_ATOM_CARDINAL && property->format == 32 &&
            xcb_get_property_value_length(property) == 4)
            pid = *static_cast<uint32_t *>(xcb_get_property_value(property));
        free(property);
    }
    free(atom);
    xcb_disconnect(connection);
    if (!pid) throw std::runtime_error("This window does not identify its audio app. Share without audio or choose another window.");
    return pid;
}

struct Pulse {
    pa_mainloop *loop = pa_mainloop_new();
    pa_context *context = nullptr;
    Pulse() {
        if (!loop) throw std::runtime_error("Could not create the audio connection.");
        context = pa_context_new(pa_mainloop_get_api(loop), "Kaede screen audio");
        if (!context || pa_context_connect(context, nullptr, PA_CONTEXT_NOAUTOSPAWN, nullptr) < 0) {
            if (context) pa_context_unref(context);
            pa_mainloop_free(loop);
            throw std::runtime_error("Screen audio needs a running PipeWire-Pulse or PulseAudio server.");
        }
    }
    ~Pulse() {
        pa_context_disconnect(context);
        pa_context_unref(context);
        pa_mainloop_free(loop);
    }
    void iterate() {
        int result = 0;
        if (pa_mainloop_iterate(loop, 0, &result) < 0 ||
            !PA_CONTEXT_IS_GOOD(pa_context_get_state(context)))
            throw std::runtime_error("The audio server disconnected. Start sharing again after audio is available.");
    }
    void connect() {
        auto deadline = Clock::now() + 5s;
        while (pa_context_get_state(context) != PA_CONTEXT_READY) {
            iterate();
            if (Clock::now() > deadline) throw std::runtime_error("The audio server did not respond.");
            std::this_thread::sleep_for(2ms);
        }
    }
    void complete(pa_operation *operation) {
        if (!operation) throw std::runtime_error("Could not list application audio.");
        const auto deadline = Clock::now() + 5s;
        try {
            while (pa_operation_get_state(operation) == PA_OPERATION_RUNNING) {
                iterate();
                if (Clock::now() > deadline) throw std::runtime_error("The audio server did not respond.");
                std::this_thread::sleep_for(2ms);
            }
        } catch (...) {
            pa_operation_cancel(operation);
            pa_operation_unref(operation);
            throw;
        }
        pa_operation_unref(operation);
    }
};
struct Input {
    uint32_t index, sink, pid;
    std::string name;
};
std::vector<Input> inputs(Pulse &pulse) {
    std::vector<Input> result;
    pulse.complete(pa_context_get_sink_input_info_list(pulse.context,
        [](pa_context *, const pa_sink_input_info *info, int, void *data) {
            if (!info) return;
            const char *pid = pa_proplist_gets(info->proplist, PA_PROP_APPLICATION_PROCESS_ID);
            const char *name = pa_proplist_gets(info->proplist, PA_PROP_APPLICATION_NAME);
            static_cast<std::vector<Input> *>(data)->push_back({info->index, info->sink,
                pid ? static_cast<uint32_t>(strtoul(pid, nullptr, 10)) : 0,
                name ? name : (info->name ? info->name : "Application")});
        }, &result));
    return result;
}
std::map<uint32_t, std::string> monitors(Pulse &pulse) {
    std::map<uint32_t, std::string> result;
    pulse.complete(pa_context_get_sink_info_list(pulse.context,
        [](pa_context *, const pa_sink_info *info, int, void *data) {
            if (info && info->monitor_source_name)
                (*static_cast<std::map<uint32_t, std::string> *>(data))[info->index] = info->monitor_source_name;
        }, &result));
    return result;
}
struct Monitor {
    pa_stream *stream = nullptr;
    uint32_t sink;
    std::deque<int16_t> samples;
    Monitor(Pulse &pulse, const Input &input, const std::string &source) : sink(input.sink) {
        const pa_sample_spec format{PA_SAMPLE_S16LE, 48000, 2};
        stream = pa_stream_new(pulse.context, "Kaede shared app audio", &format, nullptr);
        if (!stream) throw std::runtime_error("Could not open application audio.");
        pa_stream_set_read_callback(stream, [](pa_stream *stream, size_t, void *data) {
            auto &self = *static_cast<Monitor *>(data);
            const void *buffer = nullptr;
            size_t bytes = 0;
            if (pa_stream_peek(stream, &buffer, &bytes) < 0 || bytes == 0) return;
            const auto *pcm = static_cast<const int16_t *>(buffer);
            // Bound latency to 100 ms even when the publisher stalls.
            const size_t count = std::min(bytes / 2, size_t(9600));
            for (size_t i = bytes / 2 - count; i < bytes / 2; ++i)
                self.samples.push_back(pcm ? pcm[i] : 0);
            while (self.samples.size() > 9600) self.samples.pop_front();
            pa_stream_drop(stream);
        }, this);
        pa_buffer_attr buffer{19200, UINT32_MAX, UINT32_MAX, UINT32_MAX, 1920};
        // This is an individual playback stream monitor, never a whole-sink
        // fallback. Failure must not disclose other apps' audio.
        if (pa_stream_set_monitor_stream(stream, input.index) < 0 ||
            pa_stream_connect_record(stream, source.c_str(), &buffer, PA_STREAM_ADJUST_LATENCY) < 0) {
            pa_stream_unref(stream);
            stream = nullptr;
            throw std::runtime_error("The audio server could not isolate the selected application's output.");
        }
    }
    ~Monitor() {
        if (stream) {
            pa_stream_set_read_callback(stream, nullptr, nullptr);
            pa_stream_disconnect(stream);
            pa_stream_unref(stream);
        }
    }
};
}

extern "C" void kaede_capture_run(const CaptureOptions *options, const CaptureCallbacks *cb) {
    try {
        const uint32_t root = options->process == UINT32_MAX ? window_process(options->window) : options->process;
        const auto identity = root ? process_info(root).started : 0;
        if (root && !identity) throw std::runtime_error("The selected audio app has closed. Choose it again.");
        Pulse pulse;
        pulse.connect();
        std::map<uint32_t, std::unique_ptr<Monitor>> active;
        auto refresh = Clock::now();
        auto tick = Clock::now();
        bool ready = false;
        while (!cb->stopped(cb->context)) {
            if (Clock::now() >= refresh) {
                if (root && process_info(root).started != identity)
                    throw std::runtime_error("The shared audio app closed. Screen sharing has stopped.");
                const auto outputs = inputs(pulse);
                const auto sinks = monitors(pulse);
                std::map<uint32_t, bool> keep;
                for (const auto &input : outputs) {
                    // Exclude Kaede's own playback to prevent callers hearing themselves.
                    if (in_tree(input.pid, getpid()) || (root && !in_tree(input.pid, root))) continue;
                    const auto sink = sinks.find(input.sink);
                    if (sink == sinks.end()) continue;
                    keep[input.index] = true;
                    if (active.count(input.index) && active[input.index]->sink != input.sink)
                        active.erase(input.index);
                    if (!active.count(input.index))
                        active.emplace(input.index, std::make_unique<Monitor>(pulse, input, sink->second));
                }
                for (auto it = active.begin(); it != active.end();)
                    if (!keep.count(it->first)) it = active.erase(it); else ++it;
                refresh = Clock::now() + 250ms;
            }
            pulse.iterate();
            bool streams_ready = true;
            for (const auto &entry : active) {
                const auto state = pa_stream_get_state(entry.second->stream);
                if (state == PA_STREAM_FAILED)
                    throw std::runtime_error("Application audio capture failed. Share without audio or try again.");
                streams_ready &= state == PA_STREAM_READY;
            }
            if (!ready && streams_ready) { cb->status(cb->context, nullptr); ready = true; }
            if (Clock::now() >= tick) {
                std::array<int16_t, 960> mixed{};
                std::array<int32_t, 960> sum{};
                for (auto &entry : active) {
                    auto &samples = entry.second->samples;
                    for (size_t i = 0; i < sum.size() && !samples.empty(); ++i) {
                        sum[i] += samples.front(); samples.pop_front();
                    }
                }
                for (size_t i = 0; i < sum.size(); ++i)
                    mixed[i] = static_cast<int16_t>(std::clamp(sum[i], -32768, 32767));
                cb->audio(cb->context, mixed.data(), mixed.size());
                tick += 10ms;
                if (Clock::now() - tick > 100ms) tick = Clock::now();
            }
            std::this_thread::sleep_for(2ms);
        }
    } catch (const std::exception &error) { cb->status(cb->context, error.what()); }
}

extern "C" void kaede_audio_apps(void *context, void (*append)(void *, uint32_t, const char *)) {
    try {
        Pulse pulse;
        pulse.connect();
        for (const auto &input : inputs(pulse))
            if (input.pid && !in_tree(input.pid, getpid())) append(context, application_root(input.pid), input.name.c_str());
    } catch (...) { /* The chooser still offers sharing without audio. */ }
}
