#!/usr/bin/env bash

# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

set -euo pipefail

version=""
channel=""
branch="gh-pages"
remote="origin"
push="false"

usage() {
  echo "usage: publish-versioned-documentation.sh --version VERSION --channel stable|preview [--branch BRANCH] [--remote REMOTE] [--push]" >&2
}

while (($#)); do
  case "$1" in
    --version) version="${2:?missing documentation version}"; shift 2 ;;
    --channel) channel="${2:?missing documentation channel}"; shift 2 ;;
    --branch) branch="${2:?missing documentation branch}"; shift 2 ;;
    --remote) remote="${2:?missing Git remote}"; shift 2 ;;
    --push) push="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 64 ;;
  esac
done

if [[ -z "${version}" ]]; then
  echo "documentation version is required" >&2
  exit 64
fi
if [[ "${channel}" != "stable" && "${channel}" != "preview" ]]; then
  echo "documentation channel must be stable or preview" >&2
  exit 64
fi

git_arguments=(--branch "${branch}" --remote "${remote}")

root_before="$(git rev-parse --verify "${branch}:index.html" 2>/dev/null || true)"
version_before="$(git rev-parse --verify "${branch}:${version}" 2>/dev/null || true)"
title="${version}"
if [[ "${channel}" == "preview" ]]; then
  title="${version} (Preview)"
fi

mike deploy \
  "${git_arguments[@]}" \
  --update-aliases \
  --alias-type redirect \
  --title "${title}" \
  --message "Publish Documentation for ${version}" \
  "${version}" "${channel}"

version_after="$(git rev-parse --verify "${branch}:${version}")"
if [[ -n "${version_before}" && "${version_after}" != "${version_before}" ]]; then
  echo "published documentation for ${version} differs from the existing version" >&2
  exit 1
fi

if [[ "${channel}" == "stable" ]]; then
  mike set-default \
    "${git_arguments[@]}" \
    --message "Set Stable Documentation to ${version}" \
    stable
elif [[ -z "${root_before}" ]]; then
  mike set-default \
    "${git_arguments[@]}" \
    --message "Initialize Documentation with ${version}" \
    preview
else
  root_after="$(git rev-parse --verify "${branch}:index.html")"
  if [[ "${root_after}" != "${root_before}" ]]; then
    echo "preview publication changed the existing documentation root" >&2
    exit 1
  fi
fi

if [[ "${push}" == "true" ]]; then
  git push "${remote}" "refs/heads/${branch}:refs/heads/${branch}"
fi
