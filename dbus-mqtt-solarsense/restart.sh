#!/bin/bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
SERVICE_NAME=$(basename $SCRIPT_DIR)

echo
echo "Restarting $SERVICE_NAME..."

# Restart via daemontools so it works regardless of the exact command line
# (e.g. whether python is invoked with extra flags). svc -t sends SIGTERM and
# supervise restarts the service.
if [ -e /service/$SERVICE_NAME ]; then
    svc -t /service/$SERVICE_NAME
    echo "done."
else
    echo "Service /service/$SERVICE_NAME not found. Run install.sh first."
fi

echo
