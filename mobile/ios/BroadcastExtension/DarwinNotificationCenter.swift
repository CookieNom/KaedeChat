import Foundation

enum BroadcastNotification: String {
  case started = "iOS_BroadcastStarted"
  case stopped = "iOS_BroadcastStopped"
}

enum BroadcastNotificationCenter {
  static func post(_ notification: BroadcastNotification) {
    CFNotificationCenterPostNotification(
      CFNotificationCenterGetDarwinNotifyCenter(),
      CFNotificationName(rawValue: notification.rawValue as CFString),
      nil,
      nil,
      true
    )
  }
}
