#!/bin/sh
# Persistent, read-only process inspection. Only the owning guard sets stop_file.
interval=${1:-5}
stop_file=${2:-/nonexistent-qualified-observer-stop}
while [ ! -e "$stop_file" ]; do
    printf 'BEGIN\n' || exit 0
    for d in /proc/[0-9]*; do
        [ -r "$d/comm" ] || continue
        IFS= read -r name < "$d/comm" || continue
        case "$name" in
            python*|pypy*|pytest*|py.test*|tox*|nox*|uv|poetry|hatch|coverage*|pip*|node*|npm*|pnpm*|yarn*|cargo*|rustc*|make|gmake|cmake|ninja|dotnet*|MSBuild*|go|gcc*|g++*|clang*|cc1*|bash|dash|zsh|fish|sh|pwsh*|docker|podman) ;;
            *) continue ;;
        esac
        [ -f "$d/cmdline" ] || continue
        if [ ! -r "$d/cmdline" ]; then printf 'ERROR\t%s\n' "${d##*/}"; continue; fi
        IFS= read -r stat < "$d/stat" 2>/dev/null || continue
        rest=${stat##*) }
        set -- $rest
        ppid=$2
        shift 19
        birth=$1
        cmd=$(base64 < "$d/cmdline" 2>/dev/null | tr -d '\n')
        [ -n "$cmd" ] || continue
        printf 'ROW\t%s\t%s\t%s\t%s\t%s\n' "${d##*/}" "$ppid" "$birth" "$name" "$cmd" || exit 0
    done
    printf 'END\n' || exit 0
    sleep "$interval" || exit 0
done
