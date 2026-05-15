autoload -Uz add-zsh-hook

opentrace_preexec() {
    local cmd="$1"

    if [[ "$OPENTRACE_TRACING" != "1" ]]; then
        return
    fi

    eval "$HOME/.opentrace/trace-wrapper.sh \"$cmd\""
    return 130
}

add-zsh-hook preexec opentrace_preexec