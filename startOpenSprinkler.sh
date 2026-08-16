#!/bin/bash
cd "$(dirname "$0")"

# Keep controller data out of the source tree.
#
# With no -d the firmware writes iopts.dat, prog.dat, stns.dat, fert.dat and
# the logs into its own directory -- which is a git clone. Any re-clone, git
# clean or reinstall then takes the entire configuration with it, silently.
#
# The systemd unit sets OS_DATA_DIR (and StateDirectory creates it). Running
# this script by hand without that variable keeps the old behaviour.
exec ./OpenSprinkler ${OS_DATA_DIR:+-d "$OS_DATA_DIR"}
