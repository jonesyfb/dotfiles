#!/bin/sh
# Fuzzel-based askpass helper for sudo -A
# sudo passes the prompt as $1 (e.g. "[sudo] Password for nate:")
printf "" | fuzzel --dmenu --password --prompt="${1:-Password: } "
