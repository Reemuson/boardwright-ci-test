#!/bin/bash
# SPDX-License-Identifier: MIT
# Original template material copyright Nguyen Vincent.
# Modified for Boardwright by Ryan Hicks.

set -uo pipefail

# ANSI color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default options
variant="CHECKED"
output_dir="."
kibot_config="boardwright_resources/kibot/yaml/kibot_main.yaml"
kicad_major_override=""
revision=""
costs_flag=false
server_flag=false
server_port=8000
pid_file="/tmp/kibot_server.pid"

# Display help
function display_help() {
    echo -e "USAGE"
    echo -e "  ./scripts/kibot_launch.sh [OPTIONS]"
    echo
    echo -e "OPTIONS"
    echo -e "  -v, --variant VARIANT       Specify a variant name. Supported variants:"
    echo -e "                              RELEASED, DRAFT, PRELIMINARY, CHECKED, or others."
    echo -e "  --version VERSION           Specify a version to use in the command."
    echo -e "  --costs                     Replace draft_group or all_group with xlsx_bom, and enforce specific skip-pre options."
    echo -e "  --kicad MAJOR               Force the KiCad major version (9 or 10) instead of detecting it."
    echo -e "  --server [PORT]             Start an HTTP server on the specified port (default: 8000)."
    echo -e "  --stop-server               Stop the running HTTP server."
    echo -e "  -h, --help                  Display this help message."
    echo
    echo -e "EXAMPLES"
    echo -e "  ./scripts/kibot_launch.sh                        Run with default options."
    echo -e "  ./scripts/kibot_launch.sh -v RELEASED            Run with RELEASED variant."
    echo -e "  ./scripts/kibot_launch.sh --costs                Compute XLSX costs spreadsheet. Results in Manufacturing/Assembly folder"
    echo -e "  ./scripts/kibot_launch.sh -v DRAFT               Run with DRAFT variant."
    echo -e "  ./scripts/kibot_launch.sh -v PRELIMINARY         Run with PRELIMINARY variant."
    echo -e "  ./scripts/kibot_launch.sh -v CUSTOM_VARIANT      Run with a custom variant, saved in the Variants folder."
    echo -e "  ./scripts/kibot_launch.sh --server               Start an HTTP server on port 8000."
    echo -e "  ./scripts/kibot_launch.sh --server 8080          Start an HTTP server on port 8080."
    echo -e "  ./scripts/kibot_launch.sh --stop-server          Stop the running HTTP server."
    echo
    echo -e "VARIANT DESCRIPTIONS"
    echo -e "  DRAFT: only schematic in progress, will only generate schematic PDF, netlist and BoM"
    echo -e "  PRELIMINARY: will generate both schematic and PCB documents, but no ERC/DRC"
    echo -e "  CHECKED: will generate both schematic and PCB documents, with ERC/DRC"
    echo -e "  RELEASED: similar to CHECKED but should only be used for releases"
    echo -e "  Other variants: will be saved in the Variants folder"
    exit 0
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --variant|-v)
            if [[ -n ${2:-} && $2 != -* ]]; then
                variant="$2"
                shift
            else
                echo -e "${YELLOW}Warning: --variant|-v requires a value.${NC}"
                exit 1
            fi
            ;;
        --version)
            if [[ -n ${2:-} && $2 != -* ]]; then
                revision="$2"
                shift
            else
                echo -e "${YELLOW}Warning: --version requires a value.${NC}"
                exit 1
            fi
            ;;
        --kicad)
            if [[ -n ${2:-} && $2 != -* ]]; then
                kicad_major_override="$2"
                shift
            else
                echo -e "${YELLOW}Warning: --kicad requires a value.${NC}"
                exit 1
            fi
            ;;
        --costs)
            costs_flag=true
            ;;
        --server)
            server_flag=true
            if [[ -n ${2:-} && $2 != -* ]]; then
                server_port="$2"
                shift
            fi
            if [[ -f $pid_file ]]; then
                pid=$(cat $pid_file)
                if kill -0 $pid 2>/dev/null; then
                    echo -e "${YELLOW}A server is already running on PID $pid. Please stop it first with --stop-server.${NC}"
                    exit 1
                else
                    echo -e "${YELLOW}Stale PID file detected. Removing it.${NC}"
                    rm -f $pid_file
                fi
            fi
            ;;
        --stop-server)
            if [[ -f $pid_file ]]; then
                pid=$(cat $pid_file)
                if kill -0 $pid 2>/dev/null; then
                    echo -e "${GREEN}Stopping HTTP server with PID $pid...${NC}"
                    kill $pid
                    rm -f $pid_file
                    echo -e "${GREEN}Server stopped.${NC}"
                    exit 0
                else
                    echo -e "${YELLOW}No running server found. Removing stale PID file.${NC}"
                    rm -f $pid_file
                    exit 1
                fi
            else
                echo -e "${YELLOW}No server is running.${NC}"
                exit 1
            fi
            ;;
        -h|--help)
            display_help
            ;;
        *)
            echo -e "${YELLOW}Warning: Unrecognized argument: $1${NC}"
            display_help
            ;;
    esac
    shift
done

# Validate user-supplied values: they are passed to KiBot as arguments, never
# evaluated by the shell.
if [[ ! "$variant" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo -e "${YELLOW}Error: invalid variant name '$variant'.${NC}" >&2
    exit 2
fi

# Get version if not specified
if [[ -z "$revision" ]]; then
    if ! revision=$(python3 boardwright_resources/kibot/resources/scripts/get_changelog_version.py -f CHANGELOG.md); then
        echo -e "${YELLOW}Warning: Unable to determine version from CHANGELOG.md. Defaulting to empty revision.${NC}"
        revision=""
    fi
fi

# Select the KiBot output group for the installed KiCad major version.
# Unknown versions are an error rather than a silent fallback.
if [[ -n "$kicad_major_override" ]]; then
    kicad_major="$kicad_major_override"
else
    if ! kicad_version=$(kicad-cli --version 2>/dev/null); then
        echo -e "${YELLOW}Error: kicad-cli not found; install KiCad or pass --kicad MAJOR.${NC}" >&2
        exit 2
    fi
    kicad_major="${kicad_version%%.*}"
fi
case "$kicad_major" in
    9) all_group="all_group_k9" ;;
    10) all_group="all_group_k10" ;;
    *)
        echo -e "${YELLOW}Error: unsupported KiCad major version '$kicad_major' (supported: 9, 10).${NC}" >&2
        exit 2
        ;;
esac

# Handle server flag
if [[ "$server_flag" == true ]]; then
    echo -e "${GREEN}Starting HTTP server on port $server_port...${NC}"
    python3 -m http.server "$server_port" &
    echo $! > $pid_file
    sleep 1
    echo -e "${GREEN}Server running. Navigate to: http://localhost:$server_port${NC}"
    exit 0
fi

# Determine output directory based on variant
case "$variant" in
    DRAFT|PRELIMINARY|CHECKED|RELEASED)
        output_dir="."
        ;;
    *)
        output_dir="Variants"
        ;;
esac

common=(-c "$kibot_config" -d "$output_dir" -g "variant=$variant" -E "REVISION=$revision")

run_kibot() {
    echo -e "${GREEN}Running: kibot $*${NC}"
    kibot "$@"
    local status=$?
    if [[ $status -ne 0 ]]; then
        echo -e "${YELLOW}KiBot failed with exit code $status.${NC}" >&2
        exit "$status"
    fi
}

if [[ "$costs_flag" == true ]]; then
    run_kibot --skip-pre erc,drc,draw_fancy_stackup "${common[@]}"         -E KICOST_CONFIG=boardwright_resources/kibot/yaml/kicost_config_local.yaml xlsx_bom
    exit 0
fi

case "$variant" in
    DRAFT)
        run_kibot --skip-pre set_text_variables,draw_fancy_stackup,erc,drc "${common[@]}" md_readme
        run_kibot --skip-pre draw_fancy_stackup,erc,drc "${common[@]}" draft_group
        ;;
    PRELIMINARY)
        run_kibot --skip-pre erc,drc "${common[@]}" notes
        run_kibot --skip-pre erc,drc "${common[@]}" "$all_group"
        ;;
    *)
        run_kibot --skip-pre set_text_variables,draw_fancy_stackup,erc,drc "${common[@]}" notes
        run_kibot "${common[@]}" "$all_group"
        ;;
esac
