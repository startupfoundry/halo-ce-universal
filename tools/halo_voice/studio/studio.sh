#!/bin/sh
# Runs the voice studio as a systemd user service (it keeps running after
# logout and comes back after a reboot when the user lingers:
# loginctl show-user $USER -p Linger).
#
#   studio.sh deploy [host]   (from a checkout) copy tools/halo_voice.py and
#                             tools/halo_voice/ to host:~/halo-voice-studio/tools
#                             and (re)start the service there (default host: spark)
#   studio.sh install         write and enable the unit, start it
#   studio.sh start | stop | restart | status
#   studio.sh logs            follow its log
#   studio.sh uninstall       stop it and remove the unit (the data stays)
#
# The service listens on STUDIO_HOST:STUDIO_PORT (default 100.83.137.21:8765,
# the tailnet address only) and keeps its state in
# ~/model-workloads/halo-voice/studio.
set -eu
UNIT=halo-voice-studio
HERE=$(cd "$(dirname "$0")" && pwd)
HOST=${STUDIO_HOST:-100.83.137.21}
PORT=${STUDIO_PORT:-8765}

case "${1:-}" in
deploy)
	TARGET=${2:-spark}
	TOOLS=$(cd "$HERE/../.." && pwd)
	ssh "$TARGET" 'mkdir -p ~/halo-voice-studio/tools'
	rsync -a --delete --exclude __pycache__ --exclude .DS_Store "$TOOLS/halo_voice/" "$TARGET:halo-voice-studio/tools/halo_voice/"
	rsync -a "$TOOLS/halo_voice.py" "$TARGET:halo-voice-studio/tools/halo_voice.py"
	ssh "$TARGET" 'sh ~/halo-voice-studio/tools/halo_voice/studio/studio.sh install'
	;;
install)
	mkdir -p "$HOME/.config/systemd/user"
	cat > "$HOME/.config/systemd/user/$UNIT.service" <<EOF
[Unit]
Description=Halo voice studio (new announcer lines)

[Service]
ExecStart=/usr/bin/python3 -u $HERE/studio.py --host $HOST --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
	systemctl --user daemon-reload
	systemctl --user enable "$UNIT" >/dev/null 2>&1
	systemctl --user restart "$UNIT"
	echo "voice studio: http://$HOST:$PORT/"
	;;
start | stop | restart | status)
	systemctl --user "$1" "$UNIT"
	;;
logs)
	journalctl --user -u "$UNIT" -n 100 -f
	;;
uninstall)
	systemctl --user disable --now "$UNIT" || true
	rm -f "$HOME/.config/systemd/user/$UNIT.service"
	systemctl --user daemon-reload
	;;
*)
	sed -n '2,17p' "$0"
	exit 1
	;;
esac
