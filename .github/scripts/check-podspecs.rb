# Check vendored iOS specs before spending time building native dependencies.
require 'cocoapods-core'
require 'digest'

root = File.expand_path('../..', __dir__)
lock = Pod::Lockfile.from_file(Pathname.new("#{root}/mobile/ios/Podfile.lock"))
paths = Dir.glob("#{root}/mobile/vendor/*/ios/*.podspec") +
        Dir.glob("#{root}/mobile/native/webrtc/*.podspec.json")

paths.each do |path|
  spec = Pod::Specification.from_file(path)
  # CocoaPods stores local Ruby specs as JSON in its sandbox before hashing.
  checksum = Digest::SHA1.hexdigest(spec.to_pretty_json)
  next if lock.checksum(spec.name) == checksum

  abort "Podfile.lock has a stale checksum for #{spec.name}. " \
        'Run pod install on macOS and commit the updated lockfile.'
end

puts 'Vendored iOS podspec checksums match Podfile.lock.'
