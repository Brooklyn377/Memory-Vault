#!/usr/bin/env bash
# Shared by runner-proxmox.sh (it loads this next to itself or from the repository): the blue dialog screens and the checked
# questions that install.sh uses, so the GPU-server installer looks and behaves the same way. Not meant to be run on its own.
#
# Needs from the script that loads it: YES, PLAIN, WORK (a folder for temporary files), and the functions say, die and have.

# How questions are asked: dialog boxes (whiptail, as on every Proxmox host), plain text lines, or not at all.
INTERACTIVE=0; (( YES )) || [[ ! -r /dev/tty ]] || INTERACTIVE=1
UI=text
# a test that feeds answers through RUNNER_TTY always gets plain lines
if (( INTERACTIVE && ! PLAIN )) && [[ -z "${RUNNER_TTY:-}" ]] && { [[ -t 0 ]] || [[ -t 2 ]]; } && have whiptail; then UI=dialog; fi
ASKING=$INTERACTIVE   # 0 after "Default install": the defaults are taken, but still checked
BACKTITLE="Memory agent GPU server installer"
UI_TITLE="Memory agent"

# cancelled: ends the installer; boxes run in a subshell, so the exit trap prints the message after the screen is reset.
cancelled() { : >"$WORK/cancelled"; exit 1; }
# backdrop [message]: paints the terminal itself blue with the installer's name, and the message in the middle.
# whiptail draws each box on a screen of its own and shows the terminal again when the box closes; with this the
# terminal between two boxes looks the same as behind them.
BACKDROP=0
backdrop() {
    local rows=24 cols=80 c
    read -r rows cols < <(stty size </dev/tty 2>/dev/null) || true
    printf '\e[0;97;44m\e[2J\e[1;1H%s\e[?25l' "$BACKTITLE" >/dev/tty
    if [[ -n "${1:-}" ]]; then
        c=$(( (cols - ${#1}) / 2 + 1 )); (( c > 0 )) || c=1
        printf '\e[%d;%dH%s' $(( rows / 2 )) "$c" "$1" >/dev/tty
    fi
    BACKDROP=1
}
# restore_screen: back to the terminal's own colors, after the boxes or when stopping during them.
restore_screen() {
    (( BACKDROP )) || return 0
    printf '\e[0m\e[2J\e[H\e[?25h' >/dev/tty
    BACKDROP=0
}
# wt ARGS...: runs whiptail on the terminal and prints the answer; Cancel or Esc ends the installer.
wt() { whiptail --backtitle "$BACKTITLE" "$@" 3>&1 1>&2 2>&3 </dev/tty || cancelled; backdrop; }

# prompt "question" default "error": asks once and prints the answer; the error of the last answer is shown above.
prompt() {
    local q="$1" def="$2" err="$3" ans=""
    if [[ $UI == dialog ]]; then
        [[ -n "$err" ]] && q="Not accepted: $err"$'\n\n'"$q"
        wt --title "$UI_TITLE" --inputbox "$q" 14 76 "$def"
    else
        [[ -n "$err" ]] && echo "  not accepted: $err" >/dev/tty
        read -r -p "$q${def:+ [$def]}: " ans </dev/tty || true
        printf '%s' "${ans:-$def}"
    fi
}
# check "validator args" value: runs the validator with the value last; it prints why a value is wrong and fails.
check() {
    local -a c
    read -ra c <<<"$1"
    (( ${#c[@]} )) || return 0
    "${c[@]}" "$2"
}
# ask VAR "question" "default" "validator args": a value from the command line is kept if the validator accepts it. Without
# one, the default is taken when not asking (--yes, or "Default install"); with no default, or when asking, the question is
# asked again until the validator accepts the answer.
ask() {
    local var="$1" q="$2" def="${3:-}" v="${4:-}" ans="${!1}" err=""
    if [[ -n "$ans" ]]; then
        err="$(check "$v" "$ans")" || die "$q: $err"
        return 0
    fi
    if (( YES || ! ASKING )) && [[ -n "$def" ]]; then
        err="$(check "$v" "$def")" || die "$q: $err"
        printf -v "$var" '%s' "$def"; return 0
    fi
    (( ! YES )) || die "--yes needs a default for: $q; give it as an option"
    (( INTERACTIVE )) || die "no terminal to ask on; give the options on the command line (see --help)"
    while :; do
        ans="$(prompt "$q" "$def" "$err")" || exit 1   # Cancel; set -e may be off here (ask after &&)
        if [[ -z "$ans" ]]; then err="an answer is needed"; continue; fi
        if err="$(check "$v" "$ans")"; then printf -v "$var" '%s' "$ans"; return 0; fi
        def="$ans"
    done
}
# choose VAR "question" default name description ...: like ask, but the answer must be one of the names, picked
# from a menu in a dialog box. Without names (a dry run off Proxmox) anything is taken.
choose() {
    local var="$1" q="$2" def="$3" ans="${!1}" names=() i
    shift 3
    for (( i = 1; i <= $#; i += 2 )); do names+=("${!i}"); done
    if (( ASKING )) && [[ $UI == dialog && -z "$ans" ]] && (( ${#names[@]} )); then
        ans="$(wt --title "$UI_TITLE" --default-item "$def" --menu "$q" 18 76 8 "$@")" || exit 1
        printf -v "$var" '%s' "$ans"; return
    fi
    ask "$var" "$q" "$def" "${names[*]:+one_of ${names[*]}}"
}

# Validators: each takes the value last, prints what is wrong with it and fails, or prints nothing.
one_of() {
    local v="${!#}" o
    for o in "${@:1:$#-1}"; do [[ "$v" == "$o" ]] && return 0; done
    echo "'$v' is not one of: ${*:1:$#-1}"; return 1
}
posint() {   # a whole number above 0
    [[ "$1" =~ ^[1-9][0-9]{0,8}$ ]] && return 0
    echo "type a whole number above 0"; return 1
}
ipv4() {    # true for a dotted IPv4 address
    local o p=()
    local n='(0|[1-9][0-9]{0,2})'
    [[ "$1" =~ ^$n(\.$n){3}$ ]] || return 1
    IFS=. read -ra p <<<"$1"
    for o in "${p[@]}"; do (( 10#$o <= 255 )) || return 1; done
}
ip2int() { local p=(); IFS=. read -ra p <<<"$1"; echo $(( (10#${p[0]} << 24) | (10#${p[1]} << 16) | (10#${p[2]} << 8) | 10#${p[3]} )); }
valid_ctid() {
    if ! [[ "$1" =~ ^[1-9][0-9]{2,8}$ ]]; then echo "the container ID is a number from 100 to 999999999"; return 1; fi
    if have pvesh && ! pvesh get /cluster/nextid --vmid "$1" >/dev/null 2>&1; then
        echo "ID $1 is already used by another container or VM"; return 1
    fi
}
valid_hostname() {
    local label='[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
    [[ "$1" =~ ^$label(\.$label)*$ ]] && return 0
    echo "use letters, digits and hyphens (not at the start or end), e.g. llama-runner"; return 1
}
valid_ip() {
    [[ "$1" == dhcp ]] && return 0
    if [[ "$1" != */* ]] && ipv4 "$1"; then echo "$1 needs its prefix length, e.g. $1/24"; return 1; fi
    ipv4 "${1%/*}" && [[ "${1#*/}" =~ ^[1-9][0-9]?$ ]] && (( 10#${1#*/} >= 1 && 10#${1#*/} <= 32 )) && return 0
    echo "type dhcp, or an address with its prefix length, e.g. 192.168.1.60/24"; return 1
}
valid_gw() {   # the gateway must be in the network of the fixed address in IP
    local a="${IP%/*}" n="${IP#*/}"
    ipv4 "$1" || { echo "a fixed address needs a gateway, e.g. 192.168.1.1"; return 1; }
    [[ "$1" != "$a" ]] || { echo "the gateway cannot be the container's own address"; return 1; }
    (( ( ($(ip2int "$1") ^ $(ip2int "$a")) >> (32 - 10#$n) ) == 0 )) && return 0
    echo "$1 is not in the network of $IP"; return 1
}
valid_dns() {   # "host", or one or more addresses separated by spaces
    local a list=()
    [[ "$1" == host ]] && return 0
    read -ra list <<<"$1"
    for a in "${list[@]}"; do ipv4 "$a" || list=(); done
    (( ${#list[@]} )) && return 0
    echo "type a DNS server address, e.g. 192.168.1.1, or host"; return 1
}

# the picked cards and software as one line for the summary
gpu_summary() {
    local i out=""
    (( ${#SEL[@]} )) || { echo CPU; return; }
    for i in "${SEL[@]}"; do out+="${out:+, }$(gpu_label "$i")"; done
    echo "GPU $out, ${BACKEND^}"
}
