#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

repository="SchmiedmayerLab/heartwood"
root="${HOME}/.local/share/heartwood"
version="latest"
platform="auto"
bundle=""
checksums=""
dry_run="false"

usage() {
  cat <<'EOF'
Usage: heartwood-installer [options]

  --root PATH          Installation and runtime root
  --version VERSION    GitHub release tag, or latest (default)
  --platform NAME      auto, carina, or generic
  --bundle PATH        Use a local heartwood-native.tar.gz
  --checksums PATH     SHA256SUMS for a local bundle
  --dry-run            Verify and display the installation without changing it
EOF
}

while (($#)); do
  case "$1" in
    --root) root="${2:?missing installation root}"; shift 2 ;;
    --version) version="${2:?missing version}"; shift 2 ;;
    --platform) platform="${2:?missing platform}"; shift 2 ;;
    --bundle) bundle="${2:?missing bundle}"; shift 2 ;;
    --checksums) checksums="${2:?missing checksum manifest}"; shift 2 ;;
    --dry-run) dry_run="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ "${platform}" == "auto" ]]; then
  if [[ -n "${SLURM_CLUSTER_NAME:-}" || -n "${HEARTWOOD_CARINA:-}" || "${HEARTWOOD_PLATFORM:-}" == "carina" ]]; then
    platform="carina"
  else
    platform="generic"
  fi
fi
if [[ "${platform}" != "carina" && "${platform}" != "generic" ]]; then
  echo "unsupported native platform: ${platform}" >&2
  exit 64
fi

workspace="$(mktemp -d)"
installation_staging=""
cleanup() {
  rm -rf "${workspace}"
  if [[ -n "${installation_staging}" && -d "${installation_staging}" ]]; then
    rm -rf "${installation_staging}"
  fi
}
trap cleanup EXIT

if [[ -z "${bundle}" ]]; then
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to retrieve a GitHub Release" >&2
    exit 69
  fi
  if [[ "${version}" == "latest" ]]; then
    release_root="https://github.com/${repository}/releases/latest/download"
  else
    release_root="https://github.com/${repository}/releases/download/${version}"
  fi
  bundle="${workspace}/heartwood-native.tar.gz"
  checksums="${workspace}/SHA256SUMS"
  curl --fail --location --silent --show-error "${release_root}/heartwood-native.tar.gz" --output "${bundle}"
  curl --fail --location --silent --show-error "${release_root}/SHA256SUMS" --output "${checksums}"
elif [[ -z "${checksums}" ]]; then
  echo "--checksums is required with --bundle" >&2
  exit 64
fi

if [[ ! -f "${bundle}" || ! -f "${checksums}" ]]; then
  echo "bundle and checksum manifest must be regular files" >&2
  exit 66
fi
checksum_line_count="$(wc -l <"${checksums}" | tr -d ' ')"
checksum_line="$(cat "${checksums}")"
if [[ "${checksum_line_count}" != "1" || ! "${checksum_line}" =~ ^[0-9a-f]{64}[[:space:]][[:space:]]heartwood-native\.tar\.gz$ ]]; then
  echo "checksum manifest must contain exactly heartwood-native.tar.gz" >&2
  exit 66
fi
if [[ "${bundle}" != "${workspace}/heartwood-native.tar.gz" ]]; then
  cp "${bundle}" "${workspace}/heartwood-native.tar.gz"
fi
if [[ "${checksums}" != "${workspace}/SHA256SUMS" ]]; then
  cp "${checksums}" "${workspace}/SHA256SUMS"
fi
(
  cd "${workspace}"
  sha256sum --check --strict SHA256SUMS
)

release_version="$(tar -xOf "${workspace}/heartwood-native.tar.gz" heartwood/HEARTWOOD_VERSION)"
if [[ ! "${release_version}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$ ]]; then
  echo "bundle contains an unsafe release version" >&2
  exit 66
fi

printf 'Heartwood native installation\n\n'
printf 'Platform: %s\n' "${platform}"
printf 'Version: %s\n' "${release_version}"
printf 'Root: %s\n' "${root}"
if [[ "${dry_run}" == "true" ]]; then
  printf 'Result: verified dry run; no files changed\n'
  exit 0
fi

versions_root="${root}/versions"
source_root="${versions_root}/${release_version}"
runtime_root="${root}/runtimes/${release_version}"
mkdir -p "${versions_root}" "${root}/runtimes" "${root}/bin"
if [[ ! -d "${source_root}" ]]; then
  installation_staging="$(mktemp -d "${versions_root}/.heartwood-${release_version}.XXXXXX")"
  tar -xzf "${workspace}/heartwood-native.tar.gz" -C "${installation_staging}" --strip-components=1
  mv "${installation_staging}" "${source_root}"
  installation_staging=""
fi

if [[ "${platform}" == "carina" ]]; then
  (
    cd "${source_root}"
    deploy/carina/bootstrap.sh --environment-root "${runtime_root}"
  )
else
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required for a generic native installation" >&2
    exit 69
  fi
  (
    cd "${source_root}"
    UV_PROJECT_ENVIRONMENT="${runtime_root}/heartwood" uv sync --locked --no-dev --all-extras
  )
fi

ln -sfn "${source_root}" "${root}/current"
command_path="${runtime_root}/heartwood/bin/heartwood"
if [[ ! -x "${command_path}" ]]; then
  echo "installed heartwood command is unavailable" >&2
  exit 70
fi
printf '#!/usr/bin/env bash\nexport HEARTWOOD_INSTALL_ROOT=%q\nexport HEARTWOOD_NATIVE_ROOT=%q\nexport HEARTWOOD_NATIVE_VERSION=%q\nexport HEARTWOOD_VERSION=%q\nexport HEARTWOOD_HOME=%q\nexec %q "$@"\n' \
  "${source_root}" "${root}" "${release_version}" "${release_version}" "${root}/state" "${command_path}" \
  >"${root}/bin/heartwood"
chmod +x "${root}/bin/heartwood"
printf '\nInstalled %s\nAdd %s to PATH.\n' "${release_version}" "${root}/bin"
