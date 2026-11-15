#import "KaedeVideoPip.h"
#import <AVKit/AVKit.h>
#import <stdatomic.h>
#import <WebRTC/RTCYUVHelper.h>
#import <WebRTC/RTCYUVPlanarBuffer.h>

// AVSampleBufferDisplayLayer continues presenting remote WebRTC frames while
// Flutter's texture rendering is suspended in the background.
@interface KaedePipVideoView : UIView <RTCVideoRenderer>
@end
@implementation KaedePipVideoView {
  atomic_bool _framePending;
}
+ (Class)layerClass { return AVSampleBufferDisplayLayer.class; }
- (void)setSize:(CGSize)size {}
- (void)renderFrame:(RTCVideoFrame*)frame {
  if (!frame || atomic_load(&_framePending)) return;
  id<RTCI420Buffer> source = [frame.buffer toI420];
  BOOL rotated = frame.rotation == RTCVideoRotation_90 || frame.rotation == RTCVideoRotation_270;
  id<RTCI420Buffer> pixels = [[RTCI420Buffer alloc]
      initWithWidth:rotated ? source.height : source.width
      height:rotated ? source.width : source.height];
  [RTCYUVHelper I420Rotate:source.dataY srcStrideY:source.strideY
      srcU:source.dataU srcStrideU:source.strideU srcV:source.dataV srcStrideV:source.strideV
      dstY:(uint8_t*)pixels.dataY dstStrideY:pixels.strideY
      dstU:(uint8_t*)pixels.dataU dstStrideU:pixels.strideU
      dstV:(uint8_t*)pixels.dataV dstStrideV:pixels.strideV
      width:source.width height:source.height mode:frame.rotation];
  CVPixelBufferRef buffer = NULL;
  if (CVPixelBufferCreate(kCFAllocatorDefault, pixels.width, pixels.height,
      kCVPixelFormatType_420YpCbCr8BiPlanarFullRange,
      (__bridge CFDictionaryRef)@{(id)kCVPixelBufferIOSurfacePropertiesKey: @{}}, &buffer) != kCVReturnSuccess) return;
  CVPixelBufferLockBaseAddress(buffer, 0);
  [RTCYUVHelper I420ToNV12:pixels.dataY srcStrideY:pixels.strideY
      srcU:pixels.dataU srcStrideU:pixels.strideU srcV:pixels.dataV srcStrideV:pixels.strideV
      dstY:CVPixelBufferGetBaseAddressOfPlane(buffer, 0)
      dstStrideY:(int)CVPixelBufferGetBytesPerRowOfPlane(buffer, 0)
      dstUV:CVPixelBufferGetBaseAddressOfPlane(buffer, 1)
      dstStrideUV:(int)CVPixelBufferGetBytesPerRowOfPlane(buffer, 1)
      width:pixels.width height:pixels.height];
  CVPixelBufferUnlockBaseAddress(buffer, 0);
  CMVideoFormatDescriptionRef format = NULL;
  CMSampleBufferRef sample = NULL;
  CMSampleTimingInfo timing = { kCMTimeInvalid, CMTimeMakeWithSeconds(CACurrentMediaTime(), 1000000000), kCMTimeInvalid };
  OSStatus status = CMVideoFormatDescriptionCreateForImageBuffer(kCFAllocatorDefault, buffer, &format);
  if (status == noErr) status = CMSampleBufferCreateReadyWithImageBuffer(kCFAllocatorDefault, buffer, format, &timing, &sample);
  if (format) CFRelease(format);
  CVPixelBufferRelease(buffer);
  if (status != noErr || !sample) return;
  CFArrayRef attachments = CMSampleBufferGetSampleAttachmentsArray(sample, YES);
  CFDictionarySetValue((CFMutableDictionaryRef)CFArrayGetValueAtIndex(attachments, 0),
                      kCMSampleAttachmentKey_DisplayImmediately, kCFBooleanTrue);
  // Bound queued frames: do not retain an unbounded main-thread backlog.
  atomic_store(&_framePending, true);
  dispatch_async(dispatch_get_main_queue(), ^{
    AVSampleBufferDisplayLayer* layer = (AVSampleBufferDisplayLayer*)self.layer;
    layer.videoGravity = AVLayerVideoGravityResizeAspect;
    if (layer.status == AVQueuedSampleBufferRenderingStatusFailed) [layer flush];
    if (layer.readyForMoreMediaData) [layer enqueueSampleBuffer:sample];
    CFRelease(sample);
    atomic_store(&self->_framePending, false);
  });
}
@end

@interface KaedeVideoPip () <AVPictureInPictureControllerDelegate>
@property(nonatomic, strong) FlutterMethodChannel* channel;
@property(nonatomic, copy) RTCVideoTrack* (^resolver)(NSString*);
@property(nonatomic, strong) RTCVideoTrack* track;
@property(nonatomic, strong) KaedePipVideoView* video;
@property(nonatomic, strong) AVPictureInPictureController* controller;
@end

@implementation KaedeVideoPip
- (instancetype)initWithMessenger:(NSObject<FlutterBinaryMessenger>*)messenger
                   trackResolver:(RTCVideoTrack* (^)(NSString*))resolver {
  self = [super init];
  if (!self) return nil;
  self.resolver = resolver;
  self.channel = [FlutterMethodChannel methodChannelWithName:@"chat.kaede.mobile/video_pip" binaryMessenger:messenger];
  __weak KaedeVideoPip* weakSelf = self;
  [self.channel setMethodCallHandler:^(FlutterMethodCall* call, FlutterResult result) {
    KaedeVideoPip* owner = weakSelf;
    if (![call.method isEqualToString:@"configure"]) { result(FlutterMethodNotImplemented); return; }
    if (@available(iOS 15.0, *)) {
      if (![AVPictureInPictureController isPictureInPictureSupported]) { result(@NO); return; }
      NSDictionary* args = call.arguments;
      NSString* trackId = [args[@"trackId"] isKindOfClass:NSString.class] ? args[@"trackId"] : nil;
      RTCVideoTrack* track = [args[@"enabled"] boolValue] && trackId ? owner.resolver(trackId) : nil;
      [owner configureTrack:track];
      result(@YES);
    } else { result(@NO); }
  }];
  return self;
}

- (void)configureTrack:(RTCVideoTrack*)track API_AVAILABLE(ios(15.0)) {
  if (self.track != track) {
    if (self.video) [self.track removeRenderer:self.video];
    self.track = track;
    if (!self.video) self.video = [[KaedePipVideoView alloc] initWithFrame:CGRectMake(0, 0, 640, 360)];
    [track addRenderer:self.video];
  }
  if (!track) {
    self.controller.canStartPictureInPictureAutomaticallyFromInline = NO;
    [self.controller stopPictureInPicture];
    [(AVSampleBufferDisplayLayer*)self.video.layer flushAndRemoveImage];
    // Retain the controller until its stop callback reports actual visibility.
    return;
  }
  if (!self.controller) {
    UIWindow* window = nil;
    for (UIScene* scene in UIApplication.sharedApplication.connectedScenes) {
      if ([scene isKindOfClass:UIWindowScene.class] && scene.activationState == UISceneActivationStateForegroundActive) {
        for (UIWindow* candidate in ((UIWindowScene*)scene).windows) {
          if (candidate.isKeyWindow) window = candidate;
        }
      }
    }
    UIView* source = window.rootViewController.view;
    if (!source) return;
    AVPictureInPictureVideoCallViewController* content = [AVPictureInPictureVideoCallViewController new];
    content.preferredContentSize = CGSizeMake(640, 360);
    self.video.frame = content.view.bounds;
    self.video.autoresizingMask = UIViewAutoresizingFlexibleWidth | UIViewAutoresizingFlexibleHeight;
    [content.view addSubview:self.video];
    AVPictureInPictureControllerContentSource* contentSource =
        [[AVPictureInPictureControllerContentSource alloc] initWithActiveVideoCallSourceView:source contentViewController:content];
    self.controller = [[AVPictureInPictureController alloc] initWithContentSource:contentSource];
    self.controller.delegate = self;
  }
  self.controller.canStartPictureInPictureAutomaticallyFromInline = YES;
}
- (void)pictureInPictureControllerWillStartPictureInPicture:(AVPictureInPictureController*)controller {
  [self.channel invokeMethod:@"state" arguments:@YES];
}
- (void)pictureInPictureControllerDidStopPictureInPicture:(AVPictureInPictureController*)controller {
  [self.channel invokeMethod:@"state" arguments:@NO];
}
- (void)pictureInPictureController:(AVPictureInPictureController*)controller failedToStartPictureInPictureWithError:(NSError*)error {
  [self.channel invokeMethod:@"state" arguments:@NO];
}
- (void)pictureInPictureController:(AVPictureInPictureController*)controller restoreUserInterfaceForPictureInPictureStopWithCompletionHandler:(void (^)(BOOL))completionHandler {
  completionHandler(YES);
}
- (void)dispose {
  [self.channel setMethodCallHandler:nil];
  if (self.video) [self.track removeRenderer:self.video];
  self.track = nil;
  if (@available(iOS 15.0, *)) {
    self.controller.canStartPictureInPictureAutomaticallyFromInline = NO;
  }
  [self.controller stopPictureInPicture];
  self.controller = nil;
}
@end
