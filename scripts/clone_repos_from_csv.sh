#!/usr/bin/env bash
set -euo pipefail

CSV_PATH="repo_keywords_no_test.csv"

if [[ ! -f "${CSV_PATH}" ]]; then
  echo "CSV not found: ${CSV_PATH}" >&2
  exit 1
fi

awk -F',' 'NR>1 && $1 != "" {print $1}' "${CSV_PATH}" \
  | tr -d '\r' \
  | sort -u \
  | while read -r repo; do
      [[ -z "${repo}" ]] && continue
      name="${repo##*/}"
      if [[ -d "${name}" ]]; then
        echo "Skipping ${repo} (directory '${name}' already exists)"
        continue
      fi
      git_url="git@github.com:${repo}.git"
      echo "Cloning ${git_url}"
      git clone "${git_url}"
    done
