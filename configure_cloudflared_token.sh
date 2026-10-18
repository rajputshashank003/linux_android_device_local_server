#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
umask 077
TOKEN_DIR="$HOME/.config/cloudflared"
TOKEN_FILE="$TOKEN_DIR/token"

if [ -e "$TOKEN_FILE" ]; then
    printf 'A tunnel token file already exists at %s. Leaving it unchanged.\n' "$TOKEN_FILE" >&2
    exit 1
fi
mkdir -p "$TOKEN_DIR"
chmod 700 "$TOKEN_DIR"
printf 'Paste the Cloudflare tunnel token here; input is hidden: '
IFS= read -r -s token
printf '\n'
if [ "${#token}" -lt 40 ] || [[ "$token" == *[[:space:]]* ]]; then
    unset token
    printf 'The input does not look like a complete tunnel token; nothing was saved.\n' >&2
    exit 1
fi
printf '%s' "$token" > "$TOKEN_FILE"
unset token
chmod 600 "$TOKEN_FILE"
if [ ! -s "$TOKEN_FILE" ]; then
    rm -f "$TOKEN_FILE"
    printf 'Could not save the token.\n' >&2
    exit 1
fi
printf 'Token saved with private file permissions. Its value was not displayed.\n'
