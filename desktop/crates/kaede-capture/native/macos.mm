#include "capture.h"
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <AudioToolbox/AudioToolbox.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <mutex>
#include <thread>
#include <vector>

// One SCK filter governs both tracks. In particular, a single-window filter
// captures audio from its owning application, not from the entire desktop.
API_AVAILABLE(macos(14.0))
@interface KaedeShare : NSObject <SCContentSharingPickerObserver, SCStreamOutput, SCStreamDelegate> {
@public
    CaptureOptions options;
    CaptureCallbacks callbacks;
    std::atomic<bool> finished;
    std::atomic<bool> disabled;
    std::mutex statusMutex;
}
@property(nonatomic, strong) SCStream *stream;
@property(nonatomic, strong) dispatch_queue_t queue;
- (void)fail:(NSString *)message;
@end

@implementation KaedeShare
- (void)fail:(NSString *)message {
    std::lock_guard<std::mutex> guard(statusMutex);
    if (!disabled.load() && !finished.exchange(true))
        callbacks.status(callbacks.context, message.UTF8String);
}
- (void)contentSharingPickerStartDidFailWithError:(NSError *)error {
    [self fail:error.localizedDescription];
}
- (void)contentSharingPicker:(SCContentSharingPicker *)picker didCancelForStream:(SCStream *)stream {
    if (!self.stream) [self fail:@"Screen sharing was cancelled."];
}
- (void)contentSharingPicker:(SCContentSharingPicker *)picker
        didUpdateWithFilter:(SCContentFilter *)filter forStream:(SCStream *)stream {
    if (disabled.load() || finished.load()) return;
    if (self.stream) {
        // Selection changes use SCK's same stream, keeping audio scoped to video.
        [self.stream updateContentFilter:filter completionHandler:^(NSError *error) {
            if (error) [self fail:error.localizedDescription];
        }];
        return;
    }
    SCStreamConfiguration *config = [SCStreamConfiguration new];
    const CGSize size = filter.contentRect.size;
    const double scale = std::min(double(options.width) / std::max(1.0, size.width),
                                  double(options.height) / std::max(1.0, size.height));
    config.width = std::max(2, int(size.width * scale) & ~1);
    config.height = std::max(2, int(size.height * scale) & ~1);
    config.minimumFrameInterval = CMTimeMake(1, options.fps);
    config.pixelFormat = kCVPixelFormatType_32BGRA;
    config.showsCursor = YES;
    config.queueDepth = 3;
    config.capturesAudio = options.audio;
    config.sampleRate = 48000;
    config.channelCount = 2;
    config.excludesCurrentProcessAudio = YES;
    self.stream = [[SCStream alloc] initWithFilter:filter configuration:config delegate:self];
    NSError *error = nil;
    if (![self.stream addStreamOutput:self type:SCStreamOutputTypeScreen sampleHandlerQueue:self.queue error:&error] ||
        (options.audio && ![self.stream addStreamOutput:self type:SCStreamOutputTypeAudio sampleHandlerQueue:self.queue error:&error])) {
        [self fail:error.localizedDescription ?: @"Could not open the selected screen and audio."];
        return;
    }
    [self.stream startCaptureWithCompletionHandler:^(NSError *error) {
        if (error) [self fail:error.localizedDescription];
        // Video's first complete frame signals readiness to the Rust publisher.
    }];
}
- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    [self fail:error.localizedDescription ?: @"Screen sharing ended."];
}
- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sample ofType:(SCStreamOutputType)type {
    if (disabled.load() || finished.load() || !CMSampleBufferIsValid(sample)) return;
    if (type == SCStreamOutputTypeScreen) {
        CFArrayRef attachments = CMSampleBufferGetSampleAttachmentsArray(sample, false);
        if (!attachments || CFArrayGetCount(attachments) == 0) return;
        NSDictionary *info = (__bridge NSDictionary *)CFArrayGetValueAtIndex(attachments, 0);
        if ([info[SCStreamFrameInfoStatus] integerValue] != SCFrameStatusComplete) return;
        CVPixelBufferRef pixels = CMSampleBufferGetImageBuffer(sample);
        if (!pixels || CVPixelBufferLockBaseAddress(pixels, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) return;
        callbacks.video(callbacks.context, static_cast<const uint8_t *>(CVPixelBufferGetBaseAddress(pixels)),
            uint32_t(CVPixelBufferGetWidth(pixels)), uint32_t(CVPixelBufferGetHeight(pixels)), CVPixelBufferGetBytesPerRow(pixels));
        CVPixelBufferUnlockBaseAddress(pixels, kCVPixelBufferLock_ReadOnly);
        return;
    }
    if (type != SCStreamOutputTypeAudio || !options.audio) return;
    const auto *format = CMAudioFormatDescriptionGetStreamBasicDescription(CMSampleBufferGetFormatDescription(sample));
    if (!format || format->mFormatID != kAudioFormatLinearPCM || format->mSampleRate != 48000 ||
        format->mChannelsPerFrame != 2 || !(format->mFormatFlags & kAudioFormatFlagIsFloat) || format->mBitsPerChannel != 32) {
        [self fail:@"The screen audio format is unsupported. Share without audio and report this device's audio configuration."];
        return;
    }
    size_t needed = 0;
    CMBlockBufferRef retained = nullptr;
    CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(sample, &needed, nullptr, 0, nullptr, nullptr, 0, nullptr);
    if (needed < sizeof(AudioBufferList) || needed > 4096) return;
    std::vector<uint8_t> storage(needed);
    auto *list = reinterpret_cast<AudioBufferList *>(storage.data());
    if (CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(sample, nullptr, list, needed,
        nullptr, nullptr, kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment, &retained) != noErr) return;
    const size_t frames = CMSampleBufferGetNumSamples(sample);
    const bool planar = format->mFormatFlags & kAudioFormatFlagIsNonInterleaved;
    if (frames > 48000 || list->mNumberBuffers != (planar ? 2u : 1u)) {
        if (retained) CFRelease(retained);
        return;
    }
    for (uint32_t i = 0; i < list->mNumberBuffers; ++i) {
        if (!list->mBuffers[i].mData || list->mBuffers[i].mDataByteSize < frames * sizeof(float) * (planar ? 1 : 2)) {
            if (retained) CFRelease(retained);
            return;
        }
    }
    std::vector<int16_t> pcm(frames * 2);
    for (size_t i = 0; i < pcm.size(); ++i) {
        const auto *data = static_cast<const float *>(list->mBuffers[planar ? i % 2 : 0].mData);
        const float value = data[planar ? i / 2 : i];
        pcm[i] = std::isfinite(value) ? int16_t(std::clamp(value, -1.f, 1.f) * 32767.f) : 0;
    }
    callbacks.audio(callbacks.context, pcm.data(), pcm.size());
    if (retained) CFRelease(retained);
}
@end

extern "C" void kaede_capture_run(const CaptureOptions *options, const CaptureCallbacks *callbacks) {
    @autoreleasepool {
        if (@available(macOS 14.0, *)) {
            KaedeShare *share = [KaedeShare new];
            share->options = *options;
            share->callbacks = *callbacks;
            share->finished.store(false);
            share->disabled.store(false);
            share.queue = dispatch_queue_create("chat.kaede.screen-audio", DISPATCH_QUEUE_SERIAL);
            dispatch_sync(dispatch_get_main_queue(), ^{
                SCContentSharingPicker *picker = SCContentSharingPicker.sharedPicker;
                SCContentSharingPickerConfiguration *config = [SCContentSharingPickerConfiguration new];
                config.allowedPickerModes = SCContentSharingPickerModeSingleWindow | SCContentSharingPickerModeSingleDisplay;
                config.allowsChangingSelectedContent = NO;
                picker.defaultConfiguration = config;
                [picker addObserver:share];
                picker.active = YES;
                [picker present];
            });
            while (!callbacks->stopped(callbacks->context) && !share->finished.load())
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
            // Disable first, then drain the serial output queue before Rust drops
            // its callback context. Completion blocks retain share but cannot call it.
            { std::lock_guard<std::mutex> guard(share->statusMutex); share->disabled.store(true); }
            dispatch_semaphore_t stopped = dispatch_semaphore_create(0);
            dispatch_sync(dispatch_get_main_queue(), ^{
                [SCContentSharingPicker.sharedPicker removeObserver:share];
                SCContentSharingPicker.sharedPicker.active = NO;
                if (share.stream) {
                    [share.stream stopCaptureWithCompletionHandler:^(NSError *) { dispatch_semaphore_signal(stopped); }];
                    [share.stream removeStreamOutput:share type:SCStreamOutputTypeScreen error:nil];
                    if (options->audio) [share.stream removeStreamOutput:share type:SCStreamOutputTypeAudio error:nil];
                } else dispatch_semaphore_signal(stopped);
            });
            dispatch_semaphore_wait(stopped, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC));
            dispatch_sync(share.queue, ^{});
            share.stream = nil;
        } else {
            callbacks->status(callbacks->context, "Screen sharing with app audio requires macOS 14 or later.");
        }
    }
}
extern "C" void kaede_audio_apps(void *, void (*)(void *, uint32_t, const char *)) {}
