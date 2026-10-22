# Harness ffmpeg for the AgentCore Code Interpreter sandbox (video crop, reel assembly).
#
# A fully STATIC, LGPL build of FFmpeg + OpenH264, made from pinned, checksummed sources — no
# third-party binaries. Built at deploy time by HarnessAssets (infra/lib/distribution/harness-assets.ts)
# on a native arm64 CodeBuild host and staged at s3://<skills-bucket>/tools/ffmpeg/, which the
# sandbox reads by S3 key with its own IAM role (agent/src/tools/crop_video.py) — no public link.
#
#   docker build --platform linux/arm64 --target artifact --output type=local,dest=out .
#
# Versions and digests match the media toolkit's own build (media-processing-on-aws/Dockerfile).
# Only what the sandbox scripts use is needed: FFmpeg's native filters (concat, xfade, crop, scale,
# pad, fps, trim, aresample, acrossfade…), its native H.264 decoder and AAC encoder, the MP4 muxer,
# and OpenH264 as the H.264 ENCODER. GPL and nonfree parts are disabled, and --disable-autodetect
# keeps any other library from being linked in by accident.

ARG FFMPEG_VERSION=n6.1.2
ARG FFMPEG_SHA256=cc94e8901d9b57831a4357e0abbfe5414a987065b12e763ac036bee60993f2ac
ARG OPENH264_VERSION=2.6.0
ARG OPENH264_SHA256=b1ca643eadbbf16f74c86a66ab487f38691d8dbdda435683b149b2ce2c8077ec

FROM public.ecr.aws/amazonlinux/amazonlinux:2023 AS build
ARG FFMPEG_VERSION
ARG FFMPEG_SHA256
ARG OPENH264_VERSION
ARG OPENH264_SHA256
ARG TARGETARCH

RUN dnf install -y --setopt=install_weak_deps=False \
      gcc gcc-c++ make pkgconf-pkg-config unzip tar xz file findutils \
      glibc-static libstdc++-static \
    && dnf clean all

WORKDIR /src
ENV PREFIX=/opt/ffmpeg-build

# OpenH264 (BSD-2-Clause) as a static library.
RUN curl -fsSL -o openh264.zip "https://github.com/cisco/openh264/archive/refs/tags/v${OPENH264_VERSION}.zip" \
    && echo "${OPENH264_SHA256}  openh264.zip" | sha256sum -c - \
    && unzip -q openh264.zip \
    && cd "openh264-${OPENH264_VERSION}" \
    && ARCH=$([ "${TARGETARCH}" = "arm64" ] && echo aarch64 || echo x86_64) \
    && make -j"$(nproc)" OS=linux ARCH="${ARCH}" PREFIX="${PREFIX}" libraries \
    && make OS=linux ARCH="${ARCH}" PREFIX="${PREFIX}" install-static

# FFmpeg (LGPL-2.1-or-later), statically linked.
RUN curl -fsSL -o ffmpeg.zip "https://github.com/FFmpeg/FFmpeg/archive/refs/tags/${FFMPEG_VERSION}.zip" \
    && echo "${FFMPEG_SHA256}  ffmpeg.zip" | sha256sum -c - \
    && unzip -q ffmpeg.zip \
    && cd "FFmpeg-${FFMPEG_VERSION}" \
    && PKG_CONFIG_PATH="${PREFIX}/lib/pkgconfig" ./configure \
         --prefix="${PREFIX}" \
         --pkg-config-flags=--static \
         --extra-cflags="-I${PREFIX}/include -O2" \
         --extra-ldflags="-L${PREFIX}/lib -static" \
         --extra-libs="-lstdc++ -lpthread -lm" \
         --enable-static --disable-shared \
         --disable-autodetect \
         --enable-libopenh264 \
         --disable-gpl --disable-nonfree --disable-version3 \
         --disable-network \
         --disable-debug --disable-doc --disable-ffplay \
    && make -j"$(nproc)" \
    && make install

# Fail the build unless the result is what the sandbox needs.
RUN set -e; B="${PREFIX}/bin"; \
    for x in ffmpeg ffprobe; do file "$B/$x" | grep -q "statically linked" || { file "$B/$x"; echo "$x is not static"; exit 1; }; done; \
    "$B/ffmpeg" -hide_banner -encoders | grep -q " libopenh264 " || { echo "missing libopenh264 encoder"; exit 1; }; \
    "$B/ffmpeg" -hide_banner -encoders | grep -qE " aac +AAC" || { echo "missing aac encoder"; exit 1; }; \
    for f in concat xfade crop scale pad tpad fps format trim atrim apad aresample acrossfade setpts asetpts; do \
      "$B/ffmpeg" -hide_banner -filters | grep -qE "^ [A-Z.|]+ ${f} " || { echo "missing filter ${f}"; exit 1; }; \
    done; \
    "$B/ffmpeg" -hide_banner -loglevel error -f lavfi -i testsrc=size=320x240:rate=25 -f lavfi -i sine=frequency=440 \
       -t 1 -c:v libopenh264 -b:v 1M -pix_fmt yuv420p -c:a aac -shortest -y /tmp/check.mp4; \
    "$B/ffprobe" -v error -show_entries stream=codec_name -of csv=p=0 /tmp/check.mp4 | sort | tr '\n' ' ' | grep -q "aac h264" \
      || { echo "smoke encode did not produce h264+aac"; exit 1; }; \
    echo "ffmpeg checks passed"

# Package: binaries + licence texts + build configuration, and the exact source archives (LGPL
# compliance; staged beside the tarball under tools/ffmpeg/sources/).
RUN set -e; ARCHNAME=$([ "${TARGETARCH}" = "arm64" ] && echo arm64 || echo amd64); \
    mkdir -p /out /pkg/ffmpeg; \
    cp "${PREFIX}/bin/ffmpeg" "${PREFIX}/bin/ffprobe" /pkg/ffmpeg/; \
    cp "FFmpeg-${FFMPEG_VERSION}/COPYING.LGPLv2.1" /pkg/ffmpeg/LICENSE-ffmpeg-LGPL-2.1.txt; \
    cp "openh264-${OPENH264_VERSION}/LICENSE" /pkg/ffmpeg/LICENSE-openh264.txt; \
    { echo "FFmpeg ${FFMPEG_VERSION} (LGPL-2.1-or-later) with OpenH264 ${OPENH264_VERSION} (BSD-2-Clause)"; \
      echo "Static build for linux/${ARCHNAME}. Corresponding source: sources/ffmpeg-${FFMPEG_VERSION}.zip, sources/openh264-${OPENH264_VERSION}.zip"; \
      echo; /pkg/ffmpeg/ffmpeg -hide_banner -buildconf; } > /pkg/ffmpeg/README.txt; \
    tar -C /pkg -cJf "/out/ffmpeg-release-${ARCHNAME}-static.tar.xz" ffmpeg; \
    mkdir -p /out/sources; cp ffmpeg.zip "/out/sources/ffmpeg-${FFMPEG_VERSION}.zip"; cp openh264.zip "/out/sources/openh264-${OPENH264_VERSION}.zip"; \
    ls -la /out /out/sources

FROM scratch AS artifact
COPY --from=build /out/ /
