import OSLog
import ReplayKit

final class SampleHandler: RPBroadcastSampleHandler {
  private static let appGroup = "group.chat.kaede.mobile"
  private let logger = OSLog(subsystem: "chat.kaede.mobile", category: "Broadcast")
  private var connection: SocketConnection?
  private var uploader: SampleUploader?
  private var connectTimer: DispatchSourceTimer?
  private var lastVideoTimestamp = CMTime.invalid
  private let minimumFrameInterval: Double

  override init() {
    let preferences = UserDefaults(suiteName: Self.appGroup)
    let storedWidth = preferences?.integer(forKey: "screenShare.captureWidth") ?? 0
    let storedHeight = preferences?.integer(forKey: "screenShare.captureHeight") ?? 0
    let storedFrameRate = preferences?.integer(forKey: "screenShare.frameRate") ?? 0
    let width = max(640, min(3840, storedWidth > 0 ? storedWidth : 1280))
    let height = max(360, min(2160, storedHeight > 0 ? storedHeight : 720))
    let frameRate = max(5, min(60, storedFrameRate > 0 ? storedFrameRate : 30))
    minimumFrameInterval = 1.0 / Double(frameRate)
    super.init()
    guard let container = FileManager.default.containerURL(
      forSecurityApplicationGroupIdentifier: Self.appGroup
    ) else { return }
    let socket = container.appendingPathComponent("rtc_SSFD").path
    connection = SocketConnection(filePath: socket)
    if let connection {
      uploader = SampleUploader(
        connection: connection,
        maximumWidth: width,
        maximumHeight: height
      )
      connection.didClose = { [weak self] error in
        guard let self else { return }
        let failure = error ?? NSError(
          domain: RPRecordingErrorDomain,
          code: 10_001,
          userInfo: [NSLocalizedDescriptionKey: "Screen sharing stopped"]
        )
        self.finishBroadcastWithError(failure)
      }
    }
  }

  override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
    lastVideoTimestamp = .invalid
    BroadcastNotificationCenter.post(.started)
    let timer = DispatchSource.makeTimerSource(
      queue: DispatchQueue(label: "chat.kaede.broadcast.connect")
    )
    timer.schedule(deadline: .now(), repeating: .milliseconds(100), leeway: .milliseconds(50))
    timer.setEventHandler { [weak self, weak timer] in
      guard let self else { return }
      if self.connection?.open() == true {
        timer?.cancel()
        self.connectTimer = nil
        os_log("Connected broadcast extension to Kaede", log: self.logger, type: .debug)
      }
    }
    connectTimer = timer
    timer.resume()
  }

  override func broadcastFinished() {
    connectTimer?.cancel()
    connectTimer = nil
    connection?.close()
    BroadcastNotificationCenter.post(.stopped)
  }

  override func processSampleBuffer(
    _ sampleBuffer: CMSampleBuffer,
    with sampleBufferType: RPSampleBufferType
  ) {
    guard sampleBufferType == .video else { return }
    let timestamp = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
    if lastVideoTimestamp.isValid,
       timestamp.isValid,
       CMTimeGetSeconds(CMTimeSubtract(timestamp, lastVideoTimestamp)) < minimumFrameInterval {
      return
    }
    lastVideoTimestamp = timestamp
    uploader?.send(sampleBuffer)
  }
}
