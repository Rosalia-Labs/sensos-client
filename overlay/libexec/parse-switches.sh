#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

declare -A __cli_options_help
declare -A __cli_options_defaults
declare -A __cli_options_is_bool

register_option() {
    local opt="$1"
    local varname="$2"
    local help="$3"
    local default="$4"

    local safe_varname="${varname//-/_}"
    __cli_options_help["$opt"]="$help"
    __cli_options_defaults["$safe_varname"]="$default"

    case "$default" in
        true|false) __cli_options_is_bool["$opt"]=1 ;;
        *) __cli_options_is_bool["$opt"]=0 ;;
    esac

    if [[ -z "${!safe_varname+x}" ]]; then
        declare -g "$safe_varname"
        printf -v "$safe_varname" '%s' "$default"
    fi
}

parse_switches() {
    local script_name="$1"
    shift

    local opt val varname safe_varname
    local -a remaining_args=()

    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "--" ]]; then
            shift
            remaining_args=("$@")
            break
        fi

        case "$1" in
            --help)
                show_usage "$script_name"
                exit 0
                ;;
            --no-*)
                opt="--${1#--no-}"
                if [[ -v __cli_options_help["$opt"] && ${__cli_options_is_bool["$opt"]:-0} -eq 1 ]]; then
                    varname="${opt#--}"
                    safe_varname="${varname//-/_}"
                    printf -v "$safe_varname" '%s' "false"
                else
                    echo "[ERROR] Unknown or non-boolean negated option: $1"
                    show_usage "$script_name"
                    exit 1
                fi
                shift
                continue
                ;;
            --*=*)
                opt="${1%%=*}"
                val="${1#*=}"
                ;;
            --*)
                opt="$1"
                if [[ $# -gt 1 && "$2" != --* ]]; then
                    val="$2"
                    shift
                else
                    if [[ -v __cli_options_help["$opt"] ]]; then
                        if [[ ${__cli_options_is_bool["$opt"]:-0} -eq 1 ]]; then
                            val="true"
                        else
                            echo "[ERROR] Option '$opt' expects a value. Use '$opt=<value>' or '$opt <value>'."
                            show_usage "$script_name"
                            exit 1
                        fi
                    else
                        echo "[ERROR] Unknown option: $opt"
                        show_usage "$script_name"
                        exit 1
                    fi
                fi
                ;;
            *)
                echo "[ERROR] Unknown argument: $1"
                show_usage "$script_name"
                exit 1
                ;;
        esac

        varname="${opt#--}"
        safe_varname="${varname//-/_}"

        if [[ -v __cli_options_help["$opt"] ]]; then
            printf -v "$safe_varname" '%s' "$val"
        else
            echo "[ERROR] Unknown option: $opt"
            show_usage "$script_name"
            exit 1
        fi
        shift
    done

    REMAINING_ARGS=("${remaining_args[@]}")
}

show_usage() {
    local script_name="$1"
    echo "Usage: $script_name [options]"
    echo
    echo "Options:"

    local -a keys=("${!__cli_options_help[@]}")
    IFS=$'\n' keys=($(sort <<<"${keys[*]}"))
    unset IFS

    local opt varname safe_varname default help hint
    for opt in "${keys[@]}"; do
        varname="${opt#--}"
        safe_varname="${varname//-/_}"
        default="${__cli_options_defaults[$safe_varname]}"
        help="${__cli_options_help[$opt]}"

        hint=""
        if [[ ${__cli_options_is_bool["$opt"]:-0} -eq 1 ]]; then
            hint=" (boolean; use --no-${varname} to negate)"
        fi
        printf "  %-24s %-50s %s\n" "$opt [value]" "$help$hint" "(default: $default)"
    done

    echo "  --help                  Show this help message"
}
