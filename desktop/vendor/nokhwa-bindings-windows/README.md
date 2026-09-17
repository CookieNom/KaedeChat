# nokhwa-bindings-windows
This crate is the MediaFoundation bindings for the `nokhwa` crate.

It is not meant for general consumption. If you are looking for a Windows camera capture crate, consider using `nokhwa` with feature `input-native`.

No support or API stability will be given. Subject to change at any time.

## Kaede patch

Vendored from crates.io `nokhwa-bindings-windows` 0.4.6. Windows camera scans
and capture now hold COM/Media Foundation guards on their own threads instead
of sharing global initialization and camera counters. COM interfaces drop before
their guard; existing STA initialization is borrowed without uninitializing it.
This is required because Kaede scans on pool threads and captures on a dedicated
thread. Enumeration also releases the returned COM arrays, interface references,
and allocated names instead of leaking them on each hotplug scan. Keep camera
construction, capture, and destruction on that thread.

When updating this dependency, retain this fix unless upstream provides equivalent
thread-local ownership. The Windows-only runtime tests need no camera hardware.
