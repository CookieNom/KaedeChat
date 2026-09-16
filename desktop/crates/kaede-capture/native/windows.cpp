#include "capture.h"
#define NOMINMAX
#include <windows.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <mmdeviceapi.h>
#include <wrl.h>
#include <algorithm>
#include <memory>
#include <stdexcept>
#include <vector>

using Microsoft::WRL::ComPtr;
using Microsoft::WRL::RuntimeClass;
using Microsoft::WRL::RuntimeClassFlags;
using Microsoft::WRL::ClassicCom;
using Microsoft::WRL::FtmBase;

namespace {
void check(HRESULT result, const char *message) {
    if (FAILED(result)) throw std::runtime_error(message);
}
struct Handle {
    HANDLE value = nullptr;
    ~Handle() { if (value) CloseHandle(value); }
};
struct ComApartment {
    HRESULT result = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    ~ComApartment() { if (SUCCEEDED(result)) CoUninitialize(); }
};
class Activation final : public RuntimeClass<RuntimeClassFlags<ClassicCom>,
    IActivateAudioInterfaceCompletionHandler, FtmBase> {
public:
    Handle ready{CreateEventW(nullptr, TRUE, FALSE, nullptr)};
    ComPtr<IAudioClient> client;
    HRESULT result = E_PENDING;
    AUDIOCLIENT_ACTIVATION_PARAMS parameters{};
    PROPVARIANT variant{};

    HRESULT STDMETHODCALLTYPE ActivateCompleted(IActivateAudioInterfaceAsyncOperation *operation) override {
        ComPtr<IUnknown> unknown;
        HRESULT activation = E_FAIL;
        result = operation->GetActivateResult(&activation, &unknown);
        if (SUCCEEDED(result)) result = activation;
        if (SUCCEEDED(result)) result = unknown.As(&client);
        SetEvent(ready.value);
        return S_OK;
    }
};
}

extern "C" void kaede_capture_run(const CaptureOptions *options, const CaptureCallbacks *cb) {
    try {
        ComApartment apartment;
        check(apartment.result, "Could not initialize Windows audio capture.");
        DWORD pid = options->process;
        if (pid == UINT32_MAX) {
            const HWND window = reinterpret_cast<HWND>(static_cast<uintptr_t>(options->window));
            pid = 0;
            if (!IsWindow(window) || !GetWindowThreadProcessId(window, &pid) || !pid)
                throw std::runtime_error("The selected window has closed. Choose it again.");
        }
        Handle process{pid ? OpenProcess(SYNCHRONIZE, FALSE, pid) : nullptr};
        if (pid && !process.value)
            throw std::runtime_error("Windows could not access the selected app's audio. Share without audio or choose another app.");
        auto activation = Microsoft::WRL::Make<Activation>();
        if (!activation || !activation->ready.value)
            throw std::runtime_error("Could not initialize screen audio capture.");
        activation->parameters.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
        activation->parameters.ProcessLoopbackParams.TargetProcessId = pid ? pid : GetCurrentProcessId();
        activation->parameters.ProcessLoopbackParams.ProcessLoopbackMode = pid
            ? PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
            : PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE;
        activation->variant.vt = VT_BLOB;
        activation->variant.blob.cbSize = sizeof(activation->parameters);
        activation->variant.blob.pBlobData = reinterpret_cast<BYTE *>(&activation->parameters);
        ComPtr<IActivateAudioInterfaceAsyncOperation> operation;
        check(ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            __uuidof(IAudioClient), &activation->variant, activation.Get(), &operation),
            "Screen audio needs Windows build 20348 or later (including Windows 11). Share without audio on this Windows version.");
        DWORD waited = 0;
        while (WaitForSingleObject(activation->ready.value, 20) == WAIT_TIMEOUT) {
            if (cb->stopped(cb->context)) return;
            if ((waited += 20) >= 10000)
                throw std::runtime_error("Windows did not respond to the audio capture request.");
        }
        check(activation->result,
            "Windows could not capture application audio. This requires Windows build 20348 or later and permission to capture this app.");
        auto client = activation->client;
        WAVEFORMATEX format{};
        format.wFormatTag = WAVE_FORMAT_PCM;
        format.nChannels = 2;
        format.nSamplesPerSec = 48000;
        format.wBitsPerSample = 16;
        format.nBlockAlign = 4;
        format.nAvgBytesPerSec = 192000;
        check(client->Initialize(AUDCLNT_SHAREMODE_SHARED,
            AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
            AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY, 200000, 0, &format, nullptr),
            "Windows could not start stereo screen audio capture.");
        ComPtr<IAudioCaptureClient> capture;
        check(client->GetService(IID_PPV_ARGS(&capture)), "Could not open the shared audio stream.");
        check(client->Start(), "Could not start the shared audio stream.");
        struct StopClient { IAudioClient *client; ~StopClient() { client->Stop(); } } stop{client.Get()};
        cb->status(cb->context, nullptr);
        while (!cb->stopped(cb->context)) {
            if (process.value && WaitForSingleObject(process.value, 0) == WAIT_OBJECT_0)
                throw std::runtime_error("The shared app closed. Screen sharing has stopped.");
            UINT32 count = 0;
            check(capture->GetNextPacketSize(&count), "Screen audio capture ended unexpectedly.");
            while (count && !cb->stopped(cb->context)) {
                BYTE *data = nullptr;
                UINT32 frames = 0;
                DWORD flags = 0;
                check(capture->GetBuffer(&data, &frames, &flags, nullptr, nullptr),
                    "Windows could not read screen audio.");
                // GetBuffer must always be paired with ReleaseBuffer, including silence.
                if (flags & AUDCLNT_BUFFERFLAGS_SILENT) {
                    const std::vector<int16_t> silence(static_cast<size_t>(frames) * 2, 0);
                    cb->audio(cb->context, silence.data(), silence.size());
                } else if (data) {
                    cb->audio(cb->context, reinterpret_cast<int16_t *>(data), static_cast<size_t>(frames) * 2);
                }
                capture->ReleaseBuffer(frames);
                check(capture->GetNextPacketSize(&count), "Screen audio capture ended unexpectedly.");
            }
            Sleep(2);
        }
    } catch (const std::exception &error) { cb->status(cb->context, error.what()); }
}

extern "C" void kaede_audio_apps(void *, void (*)(void *, uint32_t, const char *)) {}
