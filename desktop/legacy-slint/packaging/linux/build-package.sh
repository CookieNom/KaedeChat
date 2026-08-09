#!/usr/bin/env bash
set -euo pipefail

version="${1:?version required}"
arch="${2:-amd64}"
binary="${3:-target/release/kaede-desktop}"
root="dist/linux/root"
rm -rf dist/linux
install -Dm755 "$binary" "$root/usr/bin/kaede-desktop"
install -Dm644 packaging/linux/chat.kaede.Kaede.desktop "$root/usr/share/applications/chat.kaede.Kaede.desktop"
mkdir -p "$root/usr/share/metainfo"
sed "s/<release version=\"0.1.0\"/<release version=\"$version\"/" \
  packaging/linux/chat.kaede.Kaede.metainfo.xml \
  > "$root/usr/share/metainfo/chat.kaede.Kaede.metainfo.xml"
install -Dm644 packaging/icons/chat.kaede.Kaede.svg "$root/usr/share/icons/hicolor/scalable/apps/chat.kaede.Kaede.svg"
mkdir -p "$root/DEBIAN"
sed -e "s/@VERSION@/$version/g" -e "s/@ARCH@/$arch/g" packaging/linux/control.in > "$root/DEBIAN/control"
mkdir -p dist
dpkg-deb --root-owner-group --build "$root" "dist/kaede-desktop_${version}_${arch}.deb"
tar -C "$root/usr" -czf "dist/kaede-desktop_${version}_linux_${arch}.tar.gz" .
