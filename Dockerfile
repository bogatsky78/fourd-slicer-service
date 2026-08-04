# FourD Slicer Service — headless slicing behind an HTTP API.
#
# Bundles OrcaSlicer (AGPL-3.0), redistributed unmodified from its official
# release AppImage; its own licence notices stay under /opt/engines/orca.
#
# Engine is UPSTREAM OrcaSlicer, deliberately NOT the Snapmaker Orca fork: the
# fork's CLI segfaults on every 3MF project it is handed, dereferencing the GUI
# application singleton from a headless code path —
#
#   Slic3r::GUI::GUI_App::filaments_cnt() const          <- SIGSEGV
#     <- Slic3r::GUI::expand_plate_extruders(...)
#     <- Slic3r::GUI::PartPlate::get_extruders_under_cli(...)
#     <- Slic3r::CLI::run(int, char**)
#
# Reproduced identically under flatpak and Docker, with one filament and with
# three, with and without --load-settings, so it is not a packaging problem.
# Upstream ships the Snapmaker U1 profiles anyway (resources/profiles/Snapmaker),
# which was the only reason to prefer the fork.
#
# Engines live under /opt/engines/<code>/ so a second slicer is an extra fetch
# stage plus a class in engines/ — see engines/registry.py.

ARG ORCA_VERSION=2.4.2

FROM ubuntu:24.04 AS fetch-orca
ARG ORCA_VERSION
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN curl -fsSL -o orca.AppImage \
      "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage" \
    && chmod +x orca.AppImage \
    && ./orca.AppImage --appimage-extract > /dev/null \
    && rm orca.AppImage

# The AppImage ships no copy of its own licence, and redistributing an AGPL
# binary without one is not allowed. Fetched at the same pinned tag as the
# build, so the text can never drift from the version it covers.
RUN curl -fsSL -o squashfs-root/LICENSE.txt \
      "https://raw.githubusercontent.com/OrcaSlicer/OrcaSlicer/v${ORCA_VERSION}/LICENSE.txt"


FROM ubuntu:24.04
ARG ORCA_VERSION
ENV DEBIAN_FRONTEND=noninteractive

# Only what the AppImage does NOT bundle; its private libs live in
# lib/orca-runtime and get wired up by libexec/orca-slicer-env.
# libopengl0 is mandatory — that wrapper aborts outright without libOpenGL.so.0.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates locales python3 python3-pip \
        libgtk-3-0t64 libwebkit2gtk-4.1-0 libsoup-3.0-0 libsecret-1-0 \
        libgl1 libopengl0 libegl1 libglu1-mesa libgomp1 \
        libcurl4t64 libssl3t64 libglib2.0-0t64 libdbus-1-3 \
        libx11-6 libxrandr2 libxinerama1 libxcursor1 libxi6 libxxf86vm1 \
        libsm6 libice6 \
        libnotify4 libsdl2-2.0-0 libgstreamer1.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && locale-gen en_US.UTF-8

COPY --from=fetch-orca /build/squashfs-root /opt/engines/orca

WORKDIR /opt/slicer
COPY requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt
COPY repair3mf.py app.py ./
COPY engines ./engines

# LC_ALL=C is the upstream AppRun workaround for unexpected locale data.
ENV APPDIR=/opt/engines/orca \
    LC_ALL=C \
    PYTHONPATH=/opt/slicer \
    SLICER_WORKDIR=/work \
    ORCA_VERSION=${ORCA_VERSION}

# Each slice writes hundreds of MB of temp; keep it on a known path the caller
# can mount and clean.
RUN mkdir -p /work

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
