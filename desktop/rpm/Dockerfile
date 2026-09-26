# RHEL 10 ABI baseline; EPEL supplies Tauri's WebKitGTK 4.1 and tray libraries.
FROM almalinux:10

RUN dnf install -y dnf-plugins-core epel-release \
    && dnf config-manager --set-enabled crb \
    && dnf install -y \
      alsa-lib-devel clang cmake curl-minimal dbus-devel file gcc gcc-c++ git \
      gtk3-devel libappindicator-gtk3-devel librsvg2-devel libxcb-devel \
      make openssl-devel pkgconf-pkg-config pulseaudio-libs-devel \
      systemd-devel tar unzip webkit2gtk4.1-devel \
    && dnf clean all

ARG RUST_VERSION
ARG TAURI_CLI_VERSION
ENV PATH="/root/.cargo/bin:${PATH}" CC=clang CXX=clang++ CARGO_INCREMENTAL=0
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs -o /tmp/rustup.sh \
    && sh /tmp/rustup.sh -y --profile minimal --default-toolchain "${RUST_VERSION}" \
    && cargo install tauri-cli --version "${TAURI_CLI_VERSION}" --locked \
    && rm -f /tmp/rustup.sh

WORKDIR /workspace/desktop/tauri
