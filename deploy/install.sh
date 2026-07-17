#!/usr/bin/env bash
# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

repository="SchmiedmayerLab/heartwood"
root="${PWD}"
installer_release="__HEARTWOOD_RELEASE_VERSION__"
platform="auto"
bundle=""
checksums=""
dry_run="false"
minimum_free_gib="8"
stage_number=0
stage_count=7

stage() {
  stage_number=$((stage_number + 1))
  printf '\n[%d/%d] %s\n' "${stage_number}" "${stage_count}" "$1"
}

usage() {
  cat <<'EOF'
Usage: heartwood-installer [options]

  --root PATH          Installation root (default: current directory)
  --platform NAME      auto, carina, or generic
  --bundle PATH        Use a local heartwood-native.tar.gz
  --checksums PATH     SHA256SUMS for a local bundle
  --minimum-free-gib N Required free space in GiB (default: 8)
  --dry-run            Verify and display the installation without changing it
EOF
}

while (($#)); do
  case "$1" in
    --root) root="${2:?missing installation root}"; shift 2 ;;
    --platform) platform="${2:?missing platform}"; shift 2 ;;
    --bundle) bundle="${2:?missing bundle}"; shift 2 ;;
    --checksums) checksums="${2:?missing checksum manifest}"; shift 2 ;;
    --minimum-free-gib) minimum_free_gib="${2:?missing minimum free space}"; shift 2 ;;
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

root_preexisting="true"
if [[ ! -e "${root}" ]]; then
  root_preexisting="false"
fi
umask 077
mkdir -p "${root}"
root="$(cd "${root}" && pwd -P)"
installer_state="${root}/.installer"
installer_directories=(
  "${installer_state}"
  "${installer_state}/home"
  "${installer_state}/tmp"
  "${installer_state}/cache/uv"
  "${installer_state}/cache/xdg"
  "${installer_state}/cache/mamba"
  "${installer_state}/cache/pip"
  "${installer_state}/cache/huggingface"
  "${installer_state}/cache/torch"
  "${installer_state}/cache/cuda"
  "${installer_state}/cache/numba"
  "${installer_state}/cache/triton"
  "${installer_state}/config"
  "${installer_state}/data"
  "${installer_state}/state"
)
mkdir -p "${installer_directories[@]}"
chmod 700 "${root}" "${installer_directories[@]}"
export HOME="${installer_state}/home"
export TMPDIR="${installer_state}/tmp"
export TMP="${TMPDIR}"
export TEMP="${TMPDIR}"
export XDG_CACHE_HOME="${installer_state}/cache/xdg"
export XDG_CONFIG_HOME="${installer_state}/config"
export XDG_DATA_HOME="${installer_state}/data"
export XDG_STATE_HOME="${installer_state}/state"
export UV_CACHE_DIR="${installer_state}/cache/uv"
export MAMBA_ROOT_PREFIX="${installer_state}/cache/mamba"
export PIP_CACHE_DIR="${installer_state}/cache/pip"
export HF_HOME="${installer_state}/cache/huggingface"
export TORCH_HOME="${installer_state}/cache/torch"
export CUDA_CACHE_PATH="${installer_state}/cache/cuda"
export NUMBA_CACHE_DIR="${installer_state}/cache/numba"
export TRITON_CACHE_DIR="${installer_state}/cache/triton"

workspace="$(mktemp -d "${TMPDIR}/heartwood-installer.XXXXXX")"
installation_staging=""
installation_attempted="false"
installation_succeeded="false"
cleanup() {
  rm -rf "${workspace}"
  if [[ -n "${installation_staging}" && -d "${installation_staging}" ]]; then
    rm -rf "${installation_staging}"
  fi
  if [[ "${installation_succeeded}" == "true" || "${installation_attempted}" == "false" ]]; then
    rm -rf "${installer_state}"
    if [[ "${root_preexisting}" == "false" ]]; then
      rmdir "${root}" >/dev/null 2>&1 || true
    fi
  fi
}
trap cleanup EXIT

if [[ ! "${minimum_free_gib}" =~ ^[1-9][0-9]*$ ]]; then
  echo "--minimum-free-gib must be a positive integer" >&2
  exit 64
fi

stage "Resolve and verify the release assets"
if [[ -z "${bundle}" ]]; then
  if [[ "${installer_release}" == __HEARTWOOD_* ]]; then
    echo "the source installer is not release-stamped; use --bundle for local testing" >&2
    exit 64
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to retrieve a GitHub Release" >&2
    exit 69
  fi
  release_root="https://github.com/${repository}/releases/download/${installer_release}"
  bundle="${workspace}/heartwood-native.tar.gz"
  checksums="${workspace}/SHA256SUMS"
  curl --fail --location --show-error --progress-bar "${release_root}/heartwood-native.tar.gz" --output "${bundle}"
  curl --fail --location --show-error --progress-bar "${release_root}/SHA256SUMS" --output "${checksums}"
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
if [[ "${installer_release}" != __HEARTWOOD_* && "${release_version}" != "${installer_release}" ]]; then
  echo "installer release ${installer_release} does not match bundle ${release_version}" >&2
  exit 66
fi

printf 'Heartwood native installation\n\n'
printf 'Platform: %s\n' "${platform}"
printf 'Version: %s\n' "${release_version}"
printf 'Root: %s\n' "${root}"
if [[ "${dry_run}" == "true" ]]; then
  installation_succeeded="true"
  printf 'Result: verified dry run; no files changed\n'
  exit 0
fi

stage "Check persistent storage"
existing_parent="${root}"
while [[ ! -e "${existing_parent}" && "${existing_parent}" != "/" ]]; do
  existing_parent="$(dirname "${existing_parent}")"
done
available_kib="$(df -Pk "${existing_parent}" | awk 'NR == 2 {print $4}')"
required_kib=$((minimum_free_gib * 1024 * 1024))
if [[ -z "${available_kib}" || "${available_kib}" -lt "${required_kib}" ]]; then
  echo "installation requires at least ${minimum_free_gib} GiB free under ${existing_parent}" >&2
  exit 73
fi
printf 'Available: %d GiB; installation minimum: %d GiB\n' \
  "$((available_kib / 1024 / 1024))" "${minimum_free_gib}"

versions_root="${root}/versions"
source_root="${versions_root}/${release_version}"
runtime_root="${root}/runtimes/${release_version}"
stage "Create the Heartwood installation layout"
mkdir -p \
  "${versions_root}" \
  "${root}/runtimes" \
  "${root}/bin"
chmod 700 \
  "${root}" \
  "${root}/bin" \
  "${versions_root}" \
  "${root}/runtimes"

stage "Install the immutable Heartwood source"
if [[ ! -d "${source_root}" ]]; then
  installation_staging="$(mktemp -d "${versions_root}/.heartwood-${release_version}.XXXXXX")"
  tar -xzf "${workspace}/heartwood-native.tar.gz" -C "${installation_staging}" --strip-components=1
  mv "${installation_staging}" "${source_root}"
  installation_staging=""
fi

stage "Install the locked application and inference runtimes; this can take several minutes"
installation_attempted="true"
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

stage "Publish and verify the Heartwood commands"
ln -sfn "${source_root}" "${root}/current"
command_path="${runtime_root}/heartwood/bin/heartwood"
if [[ ! -x "${command_path}" ]]; then
  echo "installed heartwood command is unavailable" >&2
  exit 70
fi
if [[ "${platform}" == "carina" ]]; then
  printf '#!/usr/bin/env bash\nexport HEARTWOOD_PLATFORM=carina\nexec %q "$@"\n' \
    "${command_path}" >"${root}/bin/heartwood"
else
  printf '#!/usr/bin/env bash\nexec %q "$@"\n' "${command_path}" >"${root}/bin/heartwood"
fi
chmod +x "${root}/bin/heartwood"
if [[ -x "${runtime_root}/vllm/bin/hf" ]]; then
  ln -sfn "${runtime_root}/vllm/bin/hf" "${root}/bin/hf"
fi

stage "Installation complete"
installation_succeeded="true"
rm -rf "${installer_state}"
printf 'Installed %s in %d seconds.\n' "${release_version}" "${SECONDS}"
printf 'Add %s to PATH, then run: heartwood doctor\n' "${root}/bin"
