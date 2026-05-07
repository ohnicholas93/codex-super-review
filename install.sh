#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage:' \
    '  ./install.sh' \
    '  ./install.sh --uninstall' \
    '  ./install.sh --help'
}

if (($# > 1)); then
  usage >&2
  exit 2
fi

case "${1:-}" in
  "" )
    ;;
  --uninstall )
    ;;
  --help | -h )
    usage
    exit 0
    ;;
  * )
    usage >&2
    exit 2
    ;;
esac

: "${HOME:?HOME must be set}"

project_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
source_path="$project_dir/bin/codex-super-review"
install_dir="${HOME}/.local/bin"
install_path="$install_dir/codex-super-review"
wrapper_marker="# codex-super-review installer wrapper"
wrapper_source="# source: $source_path"

is_owned_wrapper() {
  [[ -f "$install_path" && ! -L "$install_path" ]] \
    && grep -Fxq -- "$wrapper_marker" "$install_path" \
    && grep -Fxq -- "$wrapper_source" "$install_path"
}

if [[ "${1:-}" == "--uninstall" ]]; then
  if [[ -L "$install_path" ]]; then
    link_target="$(readlink -- "$install_path")"
    if [[ "$link_target" == "$source_path" ]]; then
      rm -- "$install_path"
      printf 'Removed %s\n' "$install_path"
      exit 0
    fi
    printf 'Refusing to remove %s; it points to %s, not %s\n' "$install_path" "$link_target" "$source_path" >&2
    exit 1
  fi

  if [[ -e "$install_path" ]]; then
    if is_owned_wrapper; then
      rm -- "$install_path"
      printf 'Removed %s\n' "$install_path"
      exit 0
    fi
    printf 'Refusing to remove %s; it is not this project install\n' "$install_path" >&2
    exit 1
  fi

  printf 'Nothing to uninstall at %s\n' "$install_path"
  exit 0
fi

if [[ ! -f "$source_path" ]]; then
  printf 'Missing executable: %s\n' "$source_path" >&2
  exit 1
fi

mkdir -p "$install_dir"

if [[ -e "$install_path" || -L "$install_path" ]]; then
  if [[ -L "$install_path" && "$(readlink -- "$install_path")" == "$source_path" ]]; then
    rm -- "$install_path"
  elif ! is_owned_wrapper; then
    printf 'Refusing to overwrite existing %s\n' "$install_path" >&2
    exit 1
  fi
fi

tmp_path="$(mktemp "$install_dir/.codex-super-review.XXXXXX")"
{
  printf '#!/usr/bin/env bash\n'
  printf 'set -euo pipefail\n'
  printf '%s\n' "$wrapper_marker"
  printf '%s\n' "$wrapper_source"
  printf 'CODEX_SUPER_REVIEW_SOURCE=%q\n' "$source_path"
  printf 'exec python3 "$CODEX_SUPER_REVIEW_SOURCE" "$@"\n'
} > "$tmp_path"
chmod 755 "$tmp_path"
mv -f -- "$tmp_path" "$install_path"

printf 'Installed %s\n' "$install_path"
