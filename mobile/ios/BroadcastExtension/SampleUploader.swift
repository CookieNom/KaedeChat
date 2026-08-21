import CoreImage
import Foundation
import ReplayKit

final class SampleUploader {
  private static let imageContext = CIContext(options: [.cacheIntermediates: false])
  private static let maximumChunkLength = 10_240

  @Atomic private var ready = false
  private let connection: SocketConnection
  private let maximumWidth: Int
  private let maximumHeight: Int
  private let queue = DispatchQueue(label: "chat.kaede.broadcast.upload", qos: .userInitiated)
  private var pendingData: Data?
  private var byteIndex = 0

  init(connection: SocketConnection, maximumWidth: Int, maximumHeight: Int) {
    self.connection = connection
    self.maximumWidth = maximumWidth
    self.maximumHeight = maximumHeight
    connection.didOpen = { [weak self] in self?.ready = true }
    connection.streamHasSpaceAvailable = { [weak self] in
      self?.queue.async { self?.sendNextChunk() }
    }
  }

  @discardableResult
  func send(_ sampleBuffer: CMSampleBuffer) -> Bool {
    guard ready else { return false }
    ready = false
    guard let data = prepare(sampleBuffer) else {
      ready = true
      return false
    }
    queue.async { [weak self] in
      self?.pendingData = data
      self?.byteIndex = 0
      self?.sendNextChunk()
    }
    return true
  }

  private func sendNextChunk() {
    guard let data = pendingData else {
      ready = true
      return
    }
    let remaining = data.count - byteIndex
    guard remaining > 0 else {
      pendingData = nil
      byteIndex = 0
      ready = true
      return
    }
    let requested = min(remaining, Self.maximumChunkLength)
    let written = data[byteIndex..<(byteIndex + requested)].withUnsafeBytes { raw -> Int in
      guard let bytes = raw.bindMemory(to: UInt8.self).baseAddress else { return -1 }
      return connection.write(bytes, length: requested)
    }
    guard written > 0 else { return }
    byteIndex += written
    if byteIndex == data.count {
      pendingData = nil
      byteIndex = 0
      ready = true
    }
  }

  private func prepare(_ sampleBuffer: CMSampleBuffer) -> Data? {
    guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return nil }
    let sourceWidth = CVPixelBufferGetWidth(pixelBuffer)
    let sourceHeight = CVPixelBufferGetHeight(pixelBuffer)
    guard sourceWidth > 0, sourceHeight > 0 else { return nil }
    let scale = min(
      1,
      min(
        CGFloat(maximumWidth) / CGFloat(sourceWidth),
        CGFloat(maximumHeight) / CGFloat(sourceHeight)
      )
    )
    let width = max(2, Int((CGFloat(sourceWidth) * scale).rounded(.down))) & ~1
    let height = max(2, Int((CGFloat(sourceHeight) * scale).rounded(.down))) & ~1
    let orientation = CMGetAttachment(
      sampleBuffer,
      key: RPVideoSampleOrientationKey as CFString,
      attachmentModeOut: nil
    )?.uintValue ?? 0
    let sourceImage = CIImage(cvPixelBuffer: pixelBuffer)
    let image = sourceImage.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
    let colorSpace = image.colorSpace ?? CGColorSpaceCreateDeviceRGB()
    guard let jpeg = Self.imageContext.jpegRepresentation(
      of: image,
      colorSpace: colorSpace,
      options: [
        kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.92
      ]
    ) else { return nil }

    let response = CFHTTPMessageCreateResponse(
      nil,
      200,
      nil,
      kCFHTTPVersion1_1
    ).takeRetainedValue()
    CFHTTPMessageSetHeaderFieldValue(response, "Content-Length" as CFString, "\(jpeg.count)" as CFString)
    CFHTTPMessageSetHeaderFieldValue(response, "Buffer-Width" as CFString, "\(width)" as CFString)
    CFHTTPMessageSetHeaderFieldValue(response, "Buffer-Height" as CFString, "\(height)" as CFString)
    CFHTTPMessageSetHeaderFieldValue(response, "Buffer-Orientation" as CFString, "\(orientation)" as CFString)
    CFHTTPMessageSetBody(response, jpeg as CFData)
    return CFHTTPMessageCopySerializedMessage(response)?.takeRetainedValue() as Data?
  }
}
