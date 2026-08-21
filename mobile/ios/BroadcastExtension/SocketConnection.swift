import Darwin
import Foundation
import OSLog

final class SocketConnection: NSObject, StreamDelegate {
  var didOpen: (() -> Void)?
  var didClose: ((Error?) -> Void)?
  var streamHasSpaceAvailable: (() -> Void)?

  private let filePath: String
  private var socketHandle: Int32
  private var inputStream: InputStream?
  private var outputStream: OutputStream?
  private var networkRunLoop: CFRunLoop?

  init?(filePath: String) {
    self.filePath = filePath
    socketHandle = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
    guard socketHandle >= 0 else { return nil }
    super.init()
  }

  func open() -> Bool {
    guard inputStream == nil,
          FileManager.default.fileExists(atPath: filePath),
          filePath.utf8.count < MemoryLayout<sockaddr_un>.size - 2 else {
      return false
    }
    var address = sockaddr_un()
    address.sun_family = sa_family_t(AF_UNIX)
    let copied = filePath.withCString { path in
      withUnsafeMutablePointer(to: &address.sun_path.0) { destination in
        strlcpy(destination, path, MemoryLayout.size(ofValue: address.sun_path))
      }
    }
    guard copied < MemoryLayout.size(ofValue: address.sun_path) else { return false }
    let connected = withUnsafePointer(to: &address) { pointer in
      pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
        Darwin.connect(socketHandle, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
      }
    }
    guard connected == 0 else { return false }

    var read: Unmanaged<CFReadStream>?
    var write: Unmanaged<CFWriteStream>?
    CFStreamCreatePairWithSocket(kCFAllocatorDefault, socketHandle, &read, &write)
    inputStream = read?.takeRetainedValue()
    outputStream = write?.takeRetainedValue()
    inputStream?.delegate = self
    outputStream?.delegate = self
    inputStream?.setProperty(
      kCFBooleanTrue,
      forKey: Stream.PropertyKey(kCFStreamPropertyShouldCloseNativeSocket as String)
    )
    outputStream?.setProperty(
      kCFBooleanTrue,
      forKey: Stream.PropertyKey(kCFStreamPropertyShouldCloseNativeSocket as String)
    )

    let input = inputStream
    let output = outputStream
    let thread = Thread { [weak self] in
      self?.networkRunLoop = CFRunLoopGetCurrent()
      input?.schedule(in: .current, forMode: .common)
      output?.schedule(in: .current, forMode: .common)
      input?.open()
      output?.open()
      RunLoop.current.run()
    }
    thread.name = "KaedeBroadcastSocket"
    thread.qualityOfService = .userInitiated
    thread.start()
    return true
  }

  func close() {
    let streamsOwnSocket = inputStream != nil || outputStream != nil
    inputStream?.delegate = nil
    outputStream?.delegate = nil
    inputStream?.close()
    outputStream?.close()
    inputStream = nil
    outputStream = nil
    if !streamsOwnSocket && socketHandle >= 0 {
      Darwin.close(socketHandle)
    }
    socketHandle = -1
    if let networkRunLoop { CFRunLoopStop(networkRunLoop) }
    networkRunLoop = nil
  }

  func write(_ bytes: UnsafePointer<UInt8>, length: Int) -> Int {
    outputStream?.write(bytes, maxLength: length) ?? -1
  }

  func stream(_ stream: Stream, handle event: Stream.Event) {
    switch event {
    case .openCompleted where stream === outputStream:
      didOpen?()
    case .hasSpaceAvailable where stream === outputStream:
      streamHasSpaceAvailable?()
    case .hasBytesAvailable where stream === inputStream:
      var byte: UInt8 = 0
      if inputStream?.read(&byte, maxLength: 1) == 0 {
        close()
        didClose?(nil)
      }
    case .errorOccurred:
      let error = stream.streamError
      close()
      didClose?(error)
    case .endEncountered:
      close()
      didClose?(nil)
    default:
      break
    }
  }
}
