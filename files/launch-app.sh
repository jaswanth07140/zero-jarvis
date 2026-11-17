#!/bin/bash
# Launch the OpenJarvis desktop app detached, with a clean log.
cd /home/jaswanth072012 || exit 1
nohup /usr/bin/openjarvis-desktop > /tmp/opencode/openjarvis-desktop.log 2>&1 &
