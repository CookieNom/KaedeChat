#import <Flutter/Flutter.h>
#import <WebRTC/WebRTC.h>

@interface KaedeVideoPip : NSObject
- (instancetype)initWithMessenger:(NSObject<FlutterBinaryMessenger>*)messenger
                   trackResolver:(RTCVideoTrack* (^)(NSString*))resolver;
- (void)dispose;
@end
