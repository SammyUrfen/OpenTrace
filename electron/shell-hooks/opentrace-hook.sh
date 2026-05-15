source ~/.bash-preexec.sh

opentrace_preexec() {
    local cmd="$1"

    if [[ "$OPENTRACE_TRACING" != "1" ]]; then
        return
    fi

    eval "$HOME/.opentrace/trace-wrapper.sh \"$cmd\""
    return 130
}

preexec_functions+=(opentrace_preexec)