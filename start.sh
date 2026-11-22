#!/usr/bin/env sh
# Runs the bot and restarts it if it dies. Ctrl+C stops it.
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
    echo "The bot is not set up yet. Run ./setup.sh first."
    exit 1
fi

# Restart delay in seconds. Doubles (max 300) each time the bot dies before coming
# online, and resets to 5 after any run that did come online.
DELAY=5
trap 'echo; echo "Stopped."; exit 0' INT TERM

while true; do
    .venv/bin/python -X utf8 bot.py "$@"
    CODE=$?
    case "$CODE" in
        0)
            echo "Bot stopped cleanly."
            exit 0
            ;;
        78)
            echo "Configuration problem - see the message above. Not restarting."
            exit 78
            ;;
        130|143)
            exit 0
            ;;
        2)
            echo "Bot failed before coming online. Restarting in ${DELAY}s. Ctrl+C to stop."
            sleep "$DELAY"
            DELAY=$((DELAY * 2))
            [ "$DELAY" -gt 300 ] && DELAY=300
            ;;
        *)
            DELAY=5
            echo "Bot exited with code $CODE. Restarting in 5s. Ctrl+C to stop."
            sleep 5
            ;;
    esac
done
