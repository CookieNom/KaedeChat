import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  private static let appGroup = "group.chat.kaede.mobile"
  private var screenShareChannel: FlutterMethodChannel?

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }

  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    let channel = FlutterMethodChannel(
      name: "chat.kaede.mobile/screen_share",
      binaryMessenger: engineBridge.applicationRegistrar.messenger()
    )
    channel.setMethodCallHandler { call, result in
      switch call.method {
      case "setCaptureProfile":
        guard let arguments = call.arguments as? [String: Any],
              let width = arguments["width"] as? Int,
              let height = arguments["height"] as? Int,
              let frameRate = arguments["frameRate"] as? Int,
              (640...3840).contains(width),
              (360...2160).contains(height),
              (5...60).contains(frameRate),
              let preferences = UserDefaults(suiteName: Self.appGroup) else {
          result(FlutterError(
            code: "INVALID_SCREEN_PROFILE",
            message: "Kaede could not prepare the iOS broadcast profile.",
            details: nil
          ))
          return
        }
        preferences.set(width, forKey: "screenShare.captureWidth")
        preferences.set(height, forKey: "screenShare.captureHeight")
        preferences.set(frameRate, forKey: "screenShare.frameRate")
        result(nil)
      case "stopBroadcast":
        CFNotificationCenterPostNotification(
          CFNotificationCenterGetDarwinNotifyCenter(),
          CFNotificationName(rawValue: "iOS_BroadcastRequestStop" as CFString),
          nil,
          nil,
          true
        )
        result(nil)
      default:
        result(FlutterMethodNotImplemented)
      }
    }
    screenShareChannel = channel
  }
}
