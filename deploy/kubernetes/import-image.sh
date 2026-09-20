#!/bin/sh
# Install root-owned as /usr/local/sbin/kaede-import-image. This helper accepts
# only an image archive on stdin; callers cannot supply commands or host paths.
set -eu
[ "$#" -eq 0 ] || { echo 'Image import accepts stdin only' >&2; exit 2; }
exec /usr/local/bin/k3s ctr --namespace k8s.io images import -
