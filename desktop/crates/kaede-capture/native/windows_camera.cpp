#include "capture.h"
#define NOMINMAX
#include <windows.h>
#include <dshow.h>
#include <wrl.h>
#include <algorithm>
#include <chrono>
#include <climits>
#include <cstdlib>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

// Converts padded Windows BGR24 DIB rows to the bridge's top-down BGRA layout.
// Kept callable by the Windows CI tests without requiring a camera driver.
extern "C" bool kaede_camera_bgra(const uint8_t *source, size_t length, uint32_t width,
    int32_t signed_height, uint8_t *output, size_t capacity) {
    if (!source || !output || !width || width > 8192 || !signed_height ||
        signed_height < -8192 || signed_height > 8192) return false;
    uint32_t height = static_cast<uint32_t>(std::abs(signed_height));
    size_t stride = (static_cast<size_t>(width) * 3 + 3) & ~size_t(3);
    if (length < stride * height || capacity < static_cast<size_t>(width) * height * 4) return false;
    for (uint32_t y = 0; y < height; ++y) {
        size_t source_y = signed_height > 0 ? height - 1 - y : y;
        const uint8_t *row = source + source_y * stride;
        uint8_t *out = output + static_cast<size_t>(y) * width * 4;
        for (uint32_t x = 0; x < width; ++x) {
            std::memcpy(out + x * 4, row + x * 3, 3);
            out[x * 4 + 3] = 255;
        }
    }
    return true;
}

using Microsoft::WRL::ComPtr;
namespace {
// Qedit.h was removed from modern SDKs; these are the documented Sample Grabber
// COM interfaces, in their original vtable order. qedit.dll ships with Windows 10.
MIDL_INTERFACE("0579154A-2B53-4994-B0D0-E773148EFF85") SampleCallback : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE SampleCB(double, IMediaSample *) = 0;
    virtual HRESULT STDMETHODCALLTYPE BufferCB(double, BYTE *, long) = 0;
};
MIDL_INTERFACE("6B652FFF-11FE-4FCE-92AD-0266B5D7C78F") SampleGrabber : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE SetOneShot(BOOL) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetMediaType(const AM_MEDIA_TYPE *) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetConnectedMediaType(AM_MEDIA_TYPE *) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetBufferSamples(BOOL) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetCurrentBuffer(long *, long *) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetCurrentSample(IMediaSample **) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetCallback(SampleCallback *, long) = 0;
};
constexpr GUID sample_grabber_clsid{0xc1f400a0, 0x3f08, 0x11d3, {0x9f, 0x0b, 0x00, 0x60, 0x08, 0x03, 0x9e, 0x37}};
constexpr size_t max_frame_bytes = 256 * 1024 * 1024;

void check(HRESULT result, const char *message) {
    if (FAILED(result)) {
        char code[16];
        std::snprintf(code, sizeof(code), " (0x%08lX)", static_cast<unsigned long>(result));
        throw std::runtime_error(std::string(message) + code);
    }
}
struct Apartment {
    HRESULT result = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    ~Apartment() { if (SUCCEEDED(result)) CoUninitialize(); }
    void check_ready() const {
        if (result != RPC_E_CHANGED_MODE) check(result, "Could not initialize camera discovery.");
    }
};
void free_type(AM_MEDIA_TYPE &type) {
    CoTaskMemFree(type.pbFormat);
    if (type.pUnk) type.pUnk->Release();
    type = {};
}
struct MediaType {
    AM_MEDIA_TYPE value{};
    ~MediaType() { free_type(value); }
};
struct TypeDeleter {
    void operator()(AM_MEDIA_TYPE *type) const {
        if (type) { free_type(*type); CoTaskMemFree(type); }
    }
};
using OwnedType = std::unique_ptr<AM_MEDIA_TYPE, TypeDeleter>;
std::string utf8(const wchar_t *text) {
    if (!text || !*text) return {};
    int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, -1, nullptr, 0, nullptr, nullptr);
    if (size <= 0) return {};
    std::string result(static_cast<size_t>(size), '\0');
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, -1, result.data(), size, nullptr, nullptr)) return {};
    result.pop_back();
    return result;
}
std::string property(IPropertyBag *bag, const wchar_t *name) {
    VARIANT value;
    VariantInit(&value);
    HRESULT result = bag->Read(name, &value, nullptr);
    std::string text;
    if (SUCCEEDED(result) && value.vt == VT_BSTR) text = utf8(value.bstrVal);
    VariantClear(&value);
    return text;
}
struct Device {
    ComPtr<IMoniker> moniker;
    std::string id, label, path;
};
std::vector<Device> devices() {
    ComPtr<ICreateDevEnum> system;
    check(CoCreateInstance(CLSID_SystemDeviceEnum, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&system)),
        "Could not list Windows cameras.");
    ComPtr<IEnumMoniker> list;
    HRESULT result = system->CreateClassEnumerator(CLSID_VideoInputDeviceCategory, &list, 0);
    check(result, "Could not list Windows cameras.");
    if (result == S_FALSE || !list) return {};
    std::vector<Device> found;
    ComPtr<IMoniker> moniker;
    while ((result = list->Next(1, &moniker, nullptr)) == S_OK) {
        ComPtr<IPropertyBag> bag;
        LPOLESTR display = nullptr;
        if (SUCCEEDED(moniker->GetDisplayName(nullptr, nullptr, &display))) {
            Device device{moniker, utf8(display), {}, {}};
            CoTaskMemFree(display);
            if (SUCCEEDED(moniker->BindToStorage(nullptr, nullptr, IID_PPV_ARGS(&bag)))) {
                device.label = property(bag.Get(), L"FriendlyName");
                device.path = property(bag.Get(), L"DevicePath");
            }
            if (device.label.empty()) device.label = "Camera";
            if (!device.id.empty()) found.push_back(std::move(device));
        }
        moniker.Reset();
    }
    check(result, "Windows camera discovery stopped unexpectedly.");
    return found;
}
VIDEOINFOHEADER *video_info(AM_MEDIA_TYPE &type) {
    if (type.formattype != FORMAT_VideoInfo || !type.pbFormat || type.cbFormat < sizeof(VIDEOINFOHEADER)) return nullptr;
    return reinterpret_cast<VIDEOINFOHEADER *>(type.pbFormat);
}
// Prefer a supported mode near the requested quality. Keep a driver's default
// if it cannot expose stream configuration or rejects a proposed frame rate.
void configure(ICaptureGraphBuilder2 *builder, IBaseFilter *camera, const CaptureOptions &options) {
    ComPtr<IAMStreamConfig> config;
    if (FAILED(builder->FindInterface(&PIN_CATEGORY_CAPTURE, &MEDIATYPE_Video, camera, IID_PPV_ARGS(&config)))) return;
    int count = 0, size = 0;
    if (FAILED(config->GetNumberOfCapabilities(&count, &size)) || count <= 0 || count > 4096 || size < sizeof(VIDEO_STREAM_CONFIG_CAPS) || size > 65536) return;
    std::vector<BYTE> caps(static_cast<size_t>(size));
    OwnedType best;
    long long best_score = std::numeric_limits<long long>::max();
    for (int i = 0; i < count; ++i) {
        AM_MEDIA_TYPE *raw = nullptr;
        HRESULT result = config->GetStreamCaps(i, &raw, caps.data());
        OwnedType type(raw);
        if (FAILED(result) || !type) continue;
        auto *info = video_info(*type);
        if (!info || info->bmiHeader.biWidth <= 0 || info->bmiHeader.biHeight == LONG_MIN) continue;
        // These formats have standard DirectShow conversion to RGB24.
        if (type->subtype != MEDIASUBTYPE_RGB24 && type->subtype != MEDIASUBTYPE_RGB32 &&
            type->subtype != MEDIASUBTYPE_YUY2 && type->subtype != MEDIASUBTYPE_MJPG) continue;
        long long score = std::abs(static_cast<long long>(info->bmiHeader.biWidth) - options.width)
            + std::abs(std::abs(static_cast<long long>(info->bmiHeader.biHeight)) - options.height);
        if (score < best_score) { best_score = score; best = std::move(type); }
    }
    if (best) {
        auto *info = video_info(*best);
        REFERENCE_TIME original = info->AvgTimePerFrame;
        info->AvgTimePerFrame = 10000000 / std::clamp(options.fps, 1u, 60u);
        if (FAILED(config->SetFormat(best.get()))) {
            info->AvgTimePerFrame = original;
            config->SetFormat(best.get());
        }
    }
}

class Frames final : public Microsoft::WRL::RuntimeClass<Microsoft::WRL::RuntimeClassFlags<Microsoft::WRL::ClassicCom>, SampleCallback> {
public:
    std::mutex mutex;
    std::condition_variable changed;
    std::vector<uint8_t> latest;
    bool failed = false;
    size_t expected = 0;
    LONG width = 0, height = 0;

    HRESULT STDMETHODCALLTYPE SampleCB(double, IMediaSample *sample) override {
        try {
            if (!sample || sample->IsPreroll() == S_OK) return S_OK;
            AM_MEDIA_TYPE *raw = nullptr;
            HRESULT changed_type = sample->GetMediaType(&raw);
            OwnedType type(raw);
            bool valid = true;
            if (changed_type == S_OK && type) {
                auto *info = video_info(*type);
                valid = info && type->subtype == MEDIASUBTYPE_RGB24 &&
                    info->bmiHeader.biWidth == width && info->bmiHeader.biHeight == height;
            }
            BYTE *data = nullptr;
            long length = sample->GetActualDataLength();
            valid = valid && SUCCEEDED(sample->GetPointer(&data)) && data && length >= 0 && length <= sample->GetSize() &&
                static_cast<size_t>(length) >= expected && static_cast<size_t>(length) <= max_frame_bytes;
            {
                std::lock_guard<std::mutex> lock(mutex);
                if (valid) latest.assign(data, data + expected);
                else failed = true;
            }
            changed.notify_one();
            return valid ? S_OK : E_FAIL;
        } catch (...) {
            { std::lock_guard<std::mutex> lock(mutex); failed = true; }
            changed.notify_one();
            return E_OUTOFMEMORY;
        }
    }
    HRESULT STDMETHODCALLTYPE BufferCB(double, BYTE *, long) override { return E_NOTIMPL; }
};
struct StopGraph {
    ComPtr<IMediaControl> control;
    ~StopGraph() { if (control) control->Stop(); }
};

void capture(const char *id, const CaptureOptions &options, const CaptureCallbacks &cb) {
    Apartment apartment;
    apartment.check_ready();
    auto available = devices();
    auto selected = std::find_if(available.begin(), available.end(), [id](const Device &device) { return device.id == id; });
    if (selected == available.end()) throw std::runtime_error("The selected camera is no longer connected.");
    ComPtr<IBaseFilter> camera;
    check(selected->moniker->BindToObject(nullptr, nullptr, IID_PPV_ARGS(&camera)),
        "Could not open the camera. Check camera access in Windows Settings and whether another app is using it.");
    ComPtr<IGraphBuilder> graph;
    ComPtr<ICaptureGraphBuilder2> builder;
    check(CoCreateInstance(CLSID_FilterGraph, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&graph)), "Could not create the camera capture graph.");
    check(CoCreateInstance(CLSID_CaptureGraphBuilder2, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&builder)), "Could not create the camera capture graph.");
    check(builder->SetFiltergraph(graph.Get()), "Could not prepare camera capture.");
    check(graph->AddFilter(camera.Get(), L"Camera"), "Could not prepare the selected camera.");
    configure(builder.Get(), camera.Get(), options);
    ComPtr<IBaseFilter> grabber_filter, renderer;
    ComPtr<SampleGrabber> grabber;
    check(CoCreateInstance(sample_grabber_clsid, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&grabber_filter)), "Windows camera frame capture is unavailable.");
    check(grabber_filter.As(&grabber), "Windows camera frame capture is unavailable.");
    AM_MEDIA_TYPE requested{};
    requested.majortype = MEDIATYPE_Video;
    requested.subtype = MEDIASUBTYPE_RGB24;
    requested.formattype = FORMAT_VideoInfo;
    check(grabber->SetMediaType(&requested), "Could not request a camera video format.");
    check(graph->AddFilter(grabber_filter.Get(), L"Camera frames"), "Could not prepare camera frames.");
    check(CoCreateInstance(CLSID_NullRenderer, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&renderer)), "Could not prepare camera capture.");
    check(graph->AddFilter(renderer.Get(), L"Camera sink"), "Could not prepare camera capture.");
    check(builder->RenderStream(&PIN_CATEGORY_CAPTURE, &MEDIATYPE_Video, camera.Get(), grabber_filter.Get(), renderer.Get()),
        "The camera could not provide a supported video format.");
    MediaType connected;
    check(grabber->GetConnectedMediaType(&connected.value), "Could not read the camera video format.");
    auto *info = video_info(connected.value);
    if (!info || connected.value.subtype != MEDIASUBTYPE_RGB24 || info->bmiHeader.biBitCount != 24 ||
        info->bmiHeader.biCompression != BI_RGB || info->bmiHeader.biWidth <= 0 ||
        info->bmiHeader.biWidth > 8192 || info->bmiHeader.biHeight == 0 ||
        info->bmiHeader.biHeight < -8192 || info->bmiHeader.biHeight > 8192)
        throw std::runtime_error("The camera returned an unsupported frame layout.");
    uint32_t width = static_cast<uint32_t>(info->bmiHeader.biWidth);
    uint32_t height = static_cast<uint32_t>(std::abs(info->bmiHeader.biHeight));
    size_t stride = (static_cast<size_t>(width) * 3 + 3) & ~size_t(3);
    size_t bytes = stride * height;
    if (bytes > max_frame_bytes) throw std::runtime_error("The camera frame is too large.");
    auto frames = Microsoft::WRL::Make<Frames>();
    if (!frames) throw std::bad_alloc();
    frames->expected = bytes;
    frames->width = info->bmiHeader.biWidth;
    frames->height = info->bmiHeader.biHeight;
    check(grabber->SetBufferSamples(FALSE), "Could not prepare camera frames.");
    check(grabber->SetOneShot(FALSE), "Could not prepare continuous camera capture.");
    check(grabber->SetCallback(frames.Get(), 0), "Could not receive camera frames.");
    ComPtr<IMediaControl> control;
    check(graph.As(&control), "Could not control camera capture.");
    StopGraph stopped{control}; // Stop and drain driver callbacks before releasing their state.
    if (cb.stopped(cb.context)) return;
    check(control->Run(), "Could not start the camera. Close other apps using it and try again.");
    bool ready = false;
    auto last_frame = std::chrono::steady_clock::now();
    auto next_frame = last_frame;
    auto interval = std::chrono::microseconds(1000000 / std::clamp(options.fps, 1u, 60u));
    std::vector<uint8_t> bgra(static_cast<size_t>(width) * height * 4);
    std::vector<uint8_t> frame;
    while (!cb.stopped(cb.context)) {
        frame.clear();
        {
            std::unique_lock<std::mutex> lock(frames->mutex);
            frames->changed.wait_for(lock, std::chrono::milliseconds(50), [&] { return frames->failed || !frames->latest.empty(); });
            if (frames->failed) throw std::runtime_error("The camera changed format or returned an invalid frame. Restart the camera.");
            frame.swap(frames->latest);
        }
        auto now = std::chrono::steady_clock::now();
        if (frame.empty()) {
            if (now - last_frame > std::chrono::seconds(10)) throw std::runtime_error("The camera stopped sending video. Check that the virtual camera is running.");
            continue;
        }
        last_frame = now;
        if (now < next_frame) continue;
        next_frame = std::max(next_frame + interval, now);
        if (!kaede_camera_bgra(frame.data(), frame.size(), width, info->bmiHeader.biHeight, bgra.data(), bgra.size()))
            throw std::runtime_error("The camera returned an incomplete video frame.");
        if (!ready) { cb.status(cb.context, nullptr); ready = true; }
        cb.video(cb.context, bgra.data(), width, height, static_cast<size_t>(width) * 4);
    }
}
}

extern "C" int32_t kaede_camera_devices(void *context, void (*append)(void *, const char *, const char *, const char *)) {
    try {
        Apartment apartment;
        apartment.check_ready();
        for (const auto &device : devices()) append(context, device.id.c_str(), device.label.c_str(), device.path.c_str());
        return S_OK;
    } catch (...) { return E_FAIL; }
}
extern "C" void kaede_camera_run(const char *id, const CaptureOptions *options, const CaptureCallbacks *callbacks) {
    try { capture(id, *options, *callbacks); }
    catch (const std::exception &error) { callbacks->status(callbacks->context, error.what()); }
    catch (...) { callbacks->status(callbacks->context, "Windows camera capture stopped unexpectedly."); }
}
