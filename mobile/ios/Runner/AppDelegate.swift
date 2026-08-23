import Flutter
import UIKit
import CallKit
import AVFAudio

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  private static let appGroup = "group.chat.kaede.mobile"
  private var screenShareChannel: FlutterMethodChannel?
  private var systemCallChannel: FlutterMethodChannel?
  private lazy var callProvider: CXProvider = {
    let configuration = CXProviderConfiguration(localizedName: "Kaede Chat")
    configuration.supportsVideo = true
    configuration.maximumCallsPerCallGroup = 1
    configuration.supportedHandleTypes = [.generic]
    let provider = CXProvider(configuration: configuration)
    provider.setDelegate(self, queue: nil)
    return provider
  }()
  private let callController = CXCallController()
  private var callIDs: [String: UUID] = [:]

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

    let calls = FlutterMethodChannel(
      name: "chat.kaede.mobile/system_calls",
      binaryMessenger: engineBridge.applicationRegistrar.messenger()
    )
    calls.setMethodCallHandler { [weak self] call, result in
      guard let self,
            let arguments = call.arguments as? [String: Any],
            let callID = arguments["callId"] as? String,
            !callID.isEmpty else {
        result(FlutterError(code: "INVALID_CALL", message: "A call identifier is required.", details: nil))
        return
      }
      switch call.method {
      case "showIncoming":
        let caller = arguments["callerName"] as? String ?? "Kaede caller"
        self.reportIncoming(callID: callID, caller: caller, result: result)
      case "setActive":
        if let uuid = self.callIDs[callID] {
          self.callProvider.reportOutgoingCall(with: uuid, connectedAt: Date())
        }
        result(nil)
      case "end":
        self.endSystemCall(callID: callID, result: result)
      default:
        result(FlutterMethodNotImplemented)
      }
    }
    systemCallChannel = calls
  }

  private func reportIncoming(callID: String, caller: String, result: @escaping FlutterResult) {
    let uuid = callIDs[callID] ?? UUID()
    callIDs[callID] = uuid
    let update = CXCallUpdate()
    update.remoteHandle = CXHandle(type: .generic, value: caller)
    update.localizedCallerName = caller
    update.hasVideo = true
    callProvider.reportNewIncomingCall(with: uuid, update: update) { error in
      DispatchQueue.main.async {
        if let error {
          self.callIDs.removeValue(forKey: callID)
          result(FlutterError(code: "CALLKIT_INCOMING_FAILED", message: error.localizedDescription, details: nil))
        } else {
          result(nil)
        }
      }
    }
  }

  private func endSystemCall(callID: String, result: @escaping FlutterResult) {
    guard let uuid = callIDs[callID] else {
      result(nil)
      return
    }
    let transaction = CXTransaction(action: CXEndCallAction(call: uuid))
    callController.request(transaction) { error in
      DispatchQueue.main.async {
        if let error {
          result(FlutterError(code: "CALLKIT_END_FAILED", message: error.localizedDescription, details: nil))
        } else {
          result(nil)
        }
      }
    }
  }
}

extension AppDelegate: CXProviderDelegate {
  func providerDidReset(_ provider: CXProvider) {
    let ended = Array(callIDs.keys)
    callIDs.removeAll()
    ended.forEach { systemCallChannel?.invokeMethod("ended", arguments: ["callId": $0]) }
  }

  func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
    guard let callID = callIDs.first(where: { $0.value == action.callUUID })?.key else {
      action.fail()
      return
    }
    systemCallChannel?.invokeMethod("answer", arguments: ["callId": callID])
    action.fulfill()
  }

  func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
    guard let entry = callIDs.first(where: { $0.value == action.callUUID }) else {
      action.fulfill()
      return
    }
    callIDs.removeValue(forKey: entry.key)
    systemCallChannel?.invokeMethod("decline", arguments: ["callId": entry.key])
    action.fulfill()
  }

  func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {}
  func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {}
}
