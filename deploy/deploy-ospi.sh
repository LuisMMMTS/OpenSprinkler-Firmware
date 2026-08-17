#!/usr/bin/env bash
#
# Build and deploy this fork's OSPI firmware from git, with minimal downtime.
# Run ON the controller (e.g. `ssh opensprinkler`), from anywhere:
#
#     ~/Downloads/OpenSprinkler-Firmware-rebased/deploy/deploy-ospi.sh
#
# One-time setup on a fresh controller:
#     sudo apt install -y libmosquitto-dev libi2c-dev libssl-dev liblgpio-dev
#     git clone https://github.com/LuisMMMTS/OpenSprinkler-Firmware.git
#
# Idempotent: re-run to ship a new commit. The stored programs, options and the
# fertigation station live in /var/lib/opensprinkler and survive the restart.
#
set -euo pipefail

BRANCH="${1:-fertigation-2.2.1-5}"
PW="${OS_PW:-a6d82bced638de3def1e9bbb4983225c}"   # md5("opendoor"), the default
cd "$(dirname "$0")/.."

echo ">> Updating source to origin/$BRANCH"
git fetch origin
git checkout "$BRANCH"
git pull --ff-only

# Build to a temp binary: the running service holds ./OpenSprinkler open, so
# compiling straight over it fails with "text file busy" (ETXTBSY). This mirrors
# the OSPI branch of build.sh, minus the apt step (libraries are a one-time
# install, see the header) so a redeploy is just a compile.
echo ">> Compiling OSPI firmware -> OpenSprinkler.new"
ws=$(ls external/TinyWebsockets/tiny_websockets_lib/src/*.cpp)
otf=$(ls external/OpenThings-Framework-Firmware-Library/*.cpp)
sens=$(ls sensors/*.cpp)
g++ -o OpenSprinkler.new -DOSPI -DSMTP_OPENSSL -std=c++14 -include string.h -include cstdint \
  main.cpp OpenSprinkler.cpp program.cpp fertigation.cpp opensprinkler_server.cpp \
  utils.cpp weather.cpp gpio.cpp mqtt.cpp notifier.cpp smtp.c RCSwitch.cpp i2cd.cpp ads1115.cpp $sens \
  -Iexternal/TinyWebsockets/tiny_websockets_lib/include $ws \
  -Iexternal/OpenThings-Framework-Firmware-Library/ $otf \
  -lpthread -lmosquitto -lssl -lcrypto -li2c -llgpio

# Swap in the new binary (seconds of downtime) and restart the service.
echo ">> Swapping binary and restarting OpenSprinkler.service"
sudo systemctl stop OpenSprinkler
cp -a OpenSprinkler "OpenSprinkler.old.$(date +%s)"
mv OpenSprinkler.new OpenSprinkler
sudo systemctl start OpenSprinkler

echo -n ">> Waiting for the firmware to answer"
until curl -sf "http://127.0.0.1:8080/jc?pw=${PW}" >/dev/null 2>&1; do echo -n .; sleep 1; done
echo " up. Deployed firmware $(git rev-parse --short HEAD)."
