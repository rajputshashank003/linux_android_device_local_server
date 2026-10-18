#!/usr/bin/env bash
set -euo pipefail

config_dir="${HOME:?}/.config/myforever"
key_file="$config_dir/openrouter_key"
mkdir -p "$config_dir"
chmod 700 "$config_dir" 2>/dev/null || true
if [[ -s "$key_file" ]]; then
    printf 'An OpenRouter key is already configured. Replace it? [y/N] '
    read -r confirm
    [[ "$confirm" == [yY] ]] || { printf 'Kept the existing key.\n'; exit 0; }
fi
printf 'Paste your OpenRouter API key (input hidden): '
IFS= read -r -s api_key
printf '\n'
if [[ ${#api_key} -lt 20 || "$api_key" == *[[:space:]]* ]]; then
    unset api_key
    printf '[FAIL] The key looks incomplete; nothing was saved.\n' >&2
    exit 1
fi
umask 077
tmp_file="$key_file.tmp.$$"
printf '%s' "$api_key" > "$tmp_file"
unset api_key
chmod 600 "$tmp_file"
mv -f "$tmp_file" "$key_file"
printf '[OK] OpenRouter key saved privately at %s. Its value was not displayed.\n' "$key_file"
