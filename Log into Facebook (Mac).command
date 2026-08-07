#!/bin/bash
# Opens Facebook so you can log in again, without starting a search.
#
# The app keeps its own browser login, separate from Safari and Chrome, so
# logging in with your normal browser doesn't renew it. Double-click this
# whenever a run says the login has expired.

here="${0%/*}"
[ -z "$here" ] || [ "$here" = "$0" ] && here="."
cd "$here" || exit 1

exec "./Start Faceplace (Mac).command" --login
