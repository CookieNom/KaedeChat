import CryptoKit
import UserNotifications

final class NotificationService: UNNotificationServiceExtension {
  private let appGroup = "group.chat.kaede.mobile"
  private var handler: ((UNNotificationContent) -> Void)?
  private var content: UNMutableNotificationContent?

  override func didReceive(
    _ request: UNNotificationRequest,
    withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void
  ) {
    handler = contentHandler
    guard let fallback = request.content.mutableCopy() as? UNMutableNotificationContent else {
      contentHandler(request.content)
      return
    }
    content = fallback
    guard let state = relayState(), let wake = wake(from: request.content.userInfo),
          state.routeID == wake.routeID, authenticate(wake, secret: state.wakeSecret),
          let url = URL(string: "https://\(state.home)/api/v1/users/@me/push-devices/notifications/redeem-wake") else {
      contentHandler(fallback)
      return
    }
    var call = URLRequest(url: url)
    call.httpMethod = "POST"
    call.timeoutInterval = 8
    call.setValue("application/json", forHTTPHeaderField: "Content-Type")
    call.httpBody = try? JSONSerialization.data(withJSONObject: [
      "installation_id": state.installationID,
      "version": 2,
      "route_id": wake.routeID,
      "event_token": wake.eventToken,
      "delivery_id": wake.deliveryID,
      "expires_at": wake.expiresAt,
      "wake_mac": wake.mac,
    ])
    URLSession.shared.dataTask(with: call) { [weak self] data, response, _ in
      guard let self, let response = response as? HTTPURLResponse,
            response.statusCode == 200, let data,
            let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let title = result["title"] as? String,
            let body = result["body"] as? String else {
        contentHandler(fallback)
        return
      }
      fallback.title = title
      fallback.body = body
      fallback.categoryIdentifier = self.category(for: result["kind"] as? String)
      if let channel = result["channel_ref"] as? String {
        fallback.threadIdentifier = channel
        fallback.userInfo["channel_ref"] = channel
      }
      if let message = result["message_ref"] as? String {
        fallback.userInfo["message_ref"] = message
      }
      contentHandler(fallback)
      self.handler = nil
    }.resume()
  }

  override func serviceExtensionTimeWillExpire() {
    if let handler, let content { handler(content) }
    handler = nil
  }

  private func category(for kind: String?) -> String {
    switch kind {
    case "direct_message": return "kaede_dms"
    case "mention": return "kaede_mentions"
    case "guild_message": return "kaede_guilds"
    case "call": return "kaede_calls"
    case "moderation": return "kaede_moderation"
    default: return "kaede_activity"
    }
  }

  private func relayState() -> RelayState? {
    guard let directory = FileManager.default.containerURL(
      forSecurityApplicationGroupIdentifier: appGroup
    ), let data = try? Data(contentsOf: directory.appendingPathComponent("push-state.json")),
      let value = try? JSONDecoder().decode(RelayState.self, from: data) else { return nil }
    return value
  }

  private func wake(from info: [AnyHashable: Any]) -> Wake? {
    guard String(describing: info["sync_version"] ?? "") == "2",
          let routeID = info["route_id"] as? String,
          let eventToken = info["event_token"] as? String,
          let deliveryID = info["delivery_id"] as? String,
          let expiresAt = Int(String(describing: info["expires_at"] ?? "")),
          let mac = info["wake_mac"] as? String,
          expiresAt >= Int(Date().timeIntervalSince1970),
          expiresAt <= Int(Date().timeIntervalSince1970) + 600 else { return nil }
    return Wake(routeID: routeID, eventToken: eventToken, deliveryID: deliveryID, expiresAt: expiresAt, mac: mac)
  }

  private func authenticate(_ wake: Wake, secret: String) -> Bool {
    guard let key = Data(base64URLEncoded: secret), let supplied = Data(base64URLEncoded: wake.mac) else { return false }
    let canonical = Data("2\n\(wake.routeID)\n\(wake.eventToken)\n\(wake.deliveryID)\n\(wake.expiresAt)".utf8)
    let calculated = Data(HMAC<SHA256>.authenticationCode(for: canonical, using: SymmetricKey(data: key)))
    return calculated.count == supplied.count && zip(calculated, supplied).reduce(0) { $0 | ($1.0 ^ $1.1) } == 0
  }
}

private struct RelayState: Decodable {
  let home: String
  let installationID: String
  let routeID: String
  let wakeSecret: String

  enum CodingKeys: String, CodingKey {
    case home
    case installationID = "installation_id"
    case routeID = "route_id"
    case wakeSecret = "wake_secret"
  }
}

private struct Wake {
  let routeID: String
  let eventToken: String
  let deliveryID: String
  let expiresAt: Int
  let mac: String
}

private extension Data {
  init?(base64URLEncoded value: String) {
    var encoded = value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    encoded.append(String(repeating: "=", count: (4 - encoded.count % 4) % 4))
    self.init(base64Encoded: encoded)
  }
}
