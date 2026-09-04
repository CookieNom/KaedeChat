import Flutter
import UIKit
import CallKit
import AVFAudio
import PushKit
import CryptoKit

@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {
  private static let appGroup = "group.chat.kaede.mobile"
  private var screenShareChannel: FlutterMethodChannel?
  private var systemCallChannel: FlutterMethodChannel?
  private var pushStateChannel: FlutterMethodChannel?
  private var voipRegistry: PKPushRegistry?
  private var voipToken: String?
  private var pendingVoipTokenResult: FlutterResult?
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
  private var callDetails: [String: (channel: String, caller: String)] = [:]
  private var pendingCallActions: [UUID: String] = [:]

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    let registry = PKPushRegistry(queue: .main)
    registry.delegate = self
    registry.desiredPushTypes = [.voIP]
    voipRegistry = registry
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

    let pushState = FlutterMethodChannel(
      name: "chat.kaede.mobile/push_state",
      binaryMessenger: engineBridge.applicationRegistrar.messenger()
    )
    pushState.setMethodCallHandler { call, result in
      guard let directory = FileManager.default.containerURL(
        forSecurityApplicationGroupIdentifier: Self.appGroup
      ) else {
        result(FlutterError(code: "APP_GROUP_UNAVAILABLE", message: "Kaede's app group is unavailable.", details: nil))
        return
      }
      let stateURL = directory.appendingPathComponent("push-state.json")
      switch call.method {
      case "setRelayState":
        guard let values = call.arguments as? [String: String],
              let home = values["home"], !home.isEmpty,
              let installationID = values["installationId"], !installationID.isEmpty,
              let routeID = values["routeId"], !routeID.isEmpty,
              let wakeSecret = values["wakeSecret"], !wakeSecret.isEmpty else {
          result(FlutterError(code: "INVALID_PUSH_STATE", message: "The relay state is incomplete.", details: nil))
          return
        }
        do {
          var state: [String: String] = [
            "home": home,
            "installation_id": installationID,
            "route_id": routeID,
            "wake_secret": wakeSecret,
          ]
          state["voip_route_id"] = values["voipRouteId"]
          state["voip_wake_secret"] = values["voipWakeSecret"]
          let data = try JSONSerialization.data(withJSONObject: state)
          try data.write(to: stateURL, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
          result(nil)
        } catch {
          result(FlutterError(code: "PUSH_STATE_WRITE_FAILED", message: error.localizedDescription, details: nil))
        }
      case "clearRelayState":
        try? FileManager.default.removeItem(at: stateURL)
        result(nil)
      case "voipToken":
        if let token = self.voipToken {
          result(token)
        } else {
          self.pendingVoipTokenResult = result
        }
      default:
        result(FlutterMethodNotImplemented)
      }
    }
    pushStateChannel = pushState
  }

  private func reportIncoming(callID: String, caller: String, result: @escaping FlutterResult) {
    if let uuid = callIDs[callID] {
      let update = CXCallUpdate()
      update.remoteHandle = CXHandle(type: .generic, value: caller)
      update.localizedCallerName = caller
      update.hasVideo = true
      callProvider.reportCall(with: uuid, updated: update)
      result(nil)
      return
    }
    let uuid = UUID()
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

extension AppDelegate: PKPushRegistryDelegate {
  func pushRegistry(
    _ registry: PKPushRegistry,
    didUpdate pushCredentials: PKPushCredentials,
    for type: PKPushType
  ) {
    guard type == .voIP else { return }
    let token = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
    voipToken = token
    pendingVoipTokenResult?(token)
    pendingVoipTokenResult = nil
  }

  func pushRegistry(
    _ registry: PKPushRegistry,
    didInvalidatePushTokenFor type: PKPushType
  ) {
    guard type == .voIP else { return }
    voipToken = nil
  }

  func pushRegistry(
    _ registry: PKPushRegistry,
    didReceiveIncomingPushWith payload: PKPushPayload,
    for type: PKPushType,
    completion: @escaping () -> Void
  ) {
    guard type == .voIP,
          let wake = VoipWake(payload.dictionaryPayload),
          let state = nativeRelayState(),
          wake.routeID == state.voipRouteID,
          authenticate(wake: wake, secret: state.voipWakeSecret) else {
      completion()
      return
    }
    let uuid = UUID()
    callIDs[wake.deliveryID] = uuid
    let update = CXCallUpdate()
    update.remoteHandle = CXHandle(type: .generic, value: "Kaede caller")
    update.localizedCallerName = "Kaede caller"
    update.hasVideo = true
    callProvider.reportNewIncomingCall(with: uuid, update: update) { error in
      completion()
      guard error == nil else {
        self.callIDs.removeValue(forKey: wake.deliveryID)
        return
      }
      self.redeemVoipWake(wake, state: state, uuid: uuid)
    }
  }

  private func redeemVoipWake(_ wake: VoipWake, state: NativeRelayState, uuid: UUID) {
    guard let url = URL(string: "https://\(state.home)/api/v1/users/@me/push-devices/notifications/redeem-wake") else { return }
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.timeoutInterval = 8
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try? JSONSerialization.data(withJSONObject: [
      "installation_id": state.installationID,
      "version": 2,
      "route_id": wake.routeID,
      "event_token": wake.eventToken,
      "delivery_id": wake.deliveryID,
      "expires_at": wake.expiresAt,
      "wake_mac": wake.mac,
    ])
    URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
      DispatchQueue.main.async {
        guard let self, let response = response as? HTTPURLResponse,
              response.statusCode == 200, let data,
              let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              result["kind"] as? String == "call",
              let callID = result["event_ref"] as? String,
              let channel = result["channel_ref"] as? String,
              let caller = result["title"] as? String else {
          self?.callProvider.reportCall(with: uuid, endedAt: Date(), reason: .failed)
          return
        }
        self.callIDs.removeValue(forKey: wake.deliveryID)
        self.callIDs[callID] = uuid
        self.callDetails[callID] = (channel, caller)
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: caller)
        update.localizedCallerName = caller
        update.hasVideo = true
        self.callProvider.reportCall(with: uuid, updated: update)
        if let action = self.pendingCallActions.removeValue(forKey: uuid) {
          self.emitCallAction(action, callID: callID)
        }
      }
    }.resume()
  }

  private func emitCallAction(_ action: String, callID: String) {
    var arguments = ["callId": callID]
    if let details = callDetails[callID] {
      arguments["channelRef"] = details.channel
      arguments["callerName"] = details.caller
    }
    systemCallChannel?.invokeMethod(action, arguments: arguments)
  }

  private func nativeRelayState() -> NativeRelayState? {
    guard let directory = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: Self.appGroup),
          let data = try? Data(contentsOf: directory.appendingPathComponent("push-state.json")) else { return nil }
    return try? JSONDecoder().decode(NativeRelayState.self, from: data)
  }

  private func authenticate(wake: VoipWake, secret: String) -> Bool {
    guard let key = Data(base64URLEncoded: secret),
          let supplied = Data(base64URLEncoded: wake.mac) else { return false }
    let canonical = Data("2\n\(wake.routeID)\n\(wake.eventToken)\n\(wake.deliveryID)\n\(wake.expiresAt)".utf8)
    let calculated = Data(HMAC<SHA256>.authenticationCode(for: canonical, using: SymmetricKey(data: key)))
    return calculated.count == supplied.count && zip(calculated, supplied).reduce(0) { $0 | ($1.0 ^ $1.1) } == 0
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
    if callID.contains("@") {
      emitCallAction("answer", callID: callID)
    } else {
      pendingCallActions[action.callUUID] = "answer"
    }
    action.fulfill()
  }

  func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
    guard let entry = callIDs.first(where: { $0.value == action.callUUID }) else {
      action.fulfill()
      return
    }
    callIDs.removeValue(forKey: entry.key)
    if entry.key.contains("@") {
      emitCallAction("decline", callID: entry.key)
    } else {
      pendingCallActions[action.callUUID] = "decline"
    }
    action.fulfill()
  }

  func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {}
  func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {}
}

private struct NativeRelayState: Decodable {
  let home: String
  let installationID: String
  let voipRouteID: String
  let voipWakeSecret: String

  enum CodingKeys: String, CodingKey {
    case home
    case installationID = "installation_id"
    case voipRouteID = "voip_route_id"
    case voipWakeSecret = "voip_wake_secret"
  }
}

private struct VoipWake {
  let routeID: String
  let eventToken: String
  let deliveryID: String
  let expiresAt: Int
  let mac: String

  init?(_ value: [AnyHashable: Any]) {
    guard String(describing: value["sync_version"] ?? "") == "2",
          let routeID = value["route_id"] as? String,
          let eventToken = value["event_token"] as? String,
          let deliveryID = value["delivery_id"] as? String,
          let expiresAt = Int(String(describing: value["expires_at"] ?? "")),
          let mac = value["wake_mac"] as? String,
          expiresAt >= Int(Date().timeIntervalSince1970),
          expiresAt <= Int(Date().timeIntervalSince1970) + 600 else { return nil }
    self.routeID = routeID
    self.eventToken = eventToken
    self.deliveryID = deliveryID
    self.expiresAt = expiresAt
    self.mac = mac
  }
}

private extension Data {
  init?(base64URLEncoded value: String) {
    var encoded = value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    encoded.append(String(repeating: "=", count: (4 - encoded.count % 4) % 4))
    self.init(base64Encoded: encoded)
  }
}
