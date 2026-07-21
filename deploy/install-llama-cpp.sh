#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

target="${1:?target directory is required}"
architecture="${2:-$(uname -m)}"
version="b9937"
metadata_name=".heartwood-runtime"
archive_name=".heartwood-runtime.tar.gz"

verify_runtime() {
  local runtime="$1"
  if ! LD_LIBRARY_PATH="${runtime}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" \
    "${runtime}/llama-server" --version >/dev/null; then
    echo "llama.cpp cannot start on this host; verify Linux compatibility and required runtime libraries, including libgomp.so.1" >&2
    exit 69
  fi
}

runtime_metadata() {
  printf 'version=%s\nasset=%s\narchive_sha256=%s\n' "${version}" "${asset}" "${sha256}"
}

verify_archive() {
  local archive="$1"
  printf '%s  %s\n' "${sha256}" "${archive}" | sha256sum --check --strict --quiet
}

verify_fresh_installation() {
  local runtime="$1"
  [ -x "${runtime}/llama-server" ] || return 1
  [ -f "${runtime}/${metadata_name}" ] || return 1
  [ -f "${runtime}/${archive_name}" ] || return 1
  [ "$(cat "${runtime}/${metadata_name}")" = "$(runtime_metadata)" ] || return 1
  verify_archive "${runtime}/${archive_name}" || return 1
  verify_runtime "${runtime}"
}

case "${architecture}" in
  amd64|x86_64)
    asset="llama-${version}-bin-ubuntu-x64.tar.gz"
    sha256="937e10a3fb6c4b1791f943230525e91bea168d1305c1d21079970acb70205df3"
    ;;
  arm64|aarch64)
    asset="llama-${version}-bin-ubuntu-arm64.tar.gz"
    sha256="c8212588514e33150dcff64fbadbf151d978fd9c4de05d0f66b2267b24310ac4"
    ;;
  *)
    echo "unsupported llama.cpp architecture: ${architecture}" >&2
    exit 64
    ;;
esac

cached_archive=""
if [ -e "${target}" ]; then
  if [ ! -d "${target}" ]; then
    echo "llama.cpp target already exists but is not a directory: ${target}" >&2
    exit 73
  fi
  if find "${target}" -mindepth 1 -maxdepth 1 -print -quit | grep --quiet .; then
    if [ ! -f "${target}/${archive_name}" ]; then
      echo "existing llama.cpp runtime has no pinned upstream archive: ${target}" >&2
      exit 73
    fi
    cached_archive="${target}/${archive_name}"
  else
    rmdir "${target}"
  fi
fi

parent="$(dirname "${target}")"
mkdir -p "${parent}"
workspace="$(mktemp -d "${parent}/.heartwood-llama.XXXXXX")"
archive="${workspace}/${asset}"
staging="${workspace}/runtime"
cleanup() {
  rm -rf "${workspace}"
}
trap cleanup EXIT

if [ -n "${cached_archive}" ]; then
  cp "${cached_archive}" "${archive}"
else
  curl --fail --location --show-error --retry 5 --retry-delay 2 \
    --retry-connrefused --connect-timeout 15 --max-time 600 \
    "https://github.com/ggml-org/llama.cpp/releases/download/${version}/${asset}" \
    --output "${archive}"
fi
verify_archive "${archive}"
mkdir -m 700 "${staging}"
tar -xzf "${archive}" -C "${staging}" --strip-components=1
if [ ! -x "${staging}/llama-server" ]; then
  echo "verified llama.cpp archive does not contain llama-server" >&2
  exit 70
fi
runtime_metadata >"${staging}/${metadata_name}"
cp "${archive}" "${staging}/${archive_name}"
chmod 444 "${staging}/${metadata_name}" "${staging}/${archive_name}"
verify_fresh_installation "${staging}"
chmod 755 "${staging}"
if [ -d "${target}" ]; then
  previous="${workspace}/previous-runtime"
  mv "${target}" "${previous}"
  if ! mv "${staging}" "${target}"; then
    mv "${previous}" "${target}"
    exit 73
  fi
  rm -rf "${previous}"
  printf 'Rebuilt llama.cpp %s for %s from its pinned archive.\n' \
    "${version}" "${architecture}"
else
  mv "${staging}" "${target}"
  printf 'Installed llama.cpp %s for %s.\n' "${version}" "${architecture}"
fi
