#!/usr/bin/env bash
set -euo pipefail

URL="${1:-http://127.0.0.1:8787/}"
FRAGMENT=""
if [[ "${URL}" == *\#* ]]; then
  FRAGMENT="#${URL#*\#}"
  URL="${URL%%\#*}"
fi
if [[ "${URL}" == *\?* ]]; then
  URL+="&"
else
  URL+="?"
fi
URL+="kiosk_start=$(date +%s%N)${FRAGMENT}"

DESKTOP_USER=$(ps -C gnome-shell -o user= | head -n 1 | tr -d ' ') || DESKTOP_USER=""
if [[ -z "${DESKTOP_USER}" ]]; then
  echo "No GNOME desktop session found. Open ${URL} manually in device browser."
  exit 1
fi

DESKTOP_UID=$(id -u "${DESKTOP_USER}")
DESKTOP_HOME=$(getent passwd "${DESKTOP_USER}" | cut -d: -f6)
RUNTIME="/run/user/${DESKTOP_UID}"
AUTH=$(ps -C Xwayland -o args= | awk '{for (field_index=1;field_index<=NF;field_index++) if ($field_index=="-auth") print $(field_index+1)}' | head -n 1)
DISPLAY_NAME=$(ps -C Xwayland -o args= | awk '{for (field_index=1;field_index<=NF;field_index++) if ($field_index ~ /^:[0-9]+$/) {print $field_index; break}}' | head -n 1)
PROFILE="${DESKTOP_HOME}/.local/share/pet-feeder-firefox"
install -d -o "${DESKTOP_USER}" -g "$(id -gn "${DESKTOP_USER}")" "${PROFILE}"

curl --fail --silent --show-error --max-time 5 --output /dev/null "${URL}"

COMMAND=(env \
  DISPLAY="${DISPLAY_NAME:-:0}" XAUTHORITY="${AUTH}" \
  XDG_RUNTIME_DIR="${RUNTIME}" DBUS_SESSION_BUS_ADDRESS="unix:path=${RUNTIME}/bus" \
  MOZ_ENABLE_WAYLAND=0 GDK_BACKEND=x11 LIBGL_ALWAYS_SOFTWARE=1 MOZ_WEBRENDER=0 \
  firefox --new-instance --no-remote --profile "${PROFILE}" \
  --kiosk "${URL}")

if [[ "${EUID}" -ne "${DESKTOP_UID}" ]]; then
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this launcher as the active desktop user (${DESKTOP_USER})." >&2
    exit 1
  fi
  COMMAND=(runuser -u "${DESKTOP_USER}" -- "${COMMAND[@]}")
fi

echo "Starting kiosk browser for ${DESKTOP_USER}: ${URL}"
exec "${COMMAND[@]}"
