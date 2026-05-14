# =============================================================================
# bashrc — opinionated baseline for EC2 / Linux dev shells
# -----------------------------------------------------------------------------
# Fixes the usual "up arrow doesn't work, no history" symptoms and adds the
# day-to-day quality-of-life bits (prompt, aliases, completion, env hooks).
#
# INSTALL ON THE TARGET HOST
# --------------------------
#   1. Copy this file to the user's home as ~/.bashrc:
#         scp bashrc ec2-user@host:~/.bashrc
#         # or for root:    scp bashrc host:/root/.bashrc
#
#   2. Make sure ~/.bash_profile sources it (login shells need this):
#         cat > ~/.bash_profile <<'EOF'
#         [ -f ~/.bashrc ] && . ~/.bashrc
#         EOF
#
#   3. Re-login with a proper login shell:  sudo -i   (NOT sudo su)
#         exit
#         sudo -i
#         # press up-arrow → previous command should appear
# =============================================================================

# ----- 0. Bail out for non-interactive shells --------------------------------
case $- in
    *i*) ;;
      *) return ;;
esac

# ----- 1. Terminal & locale --------------------------------------------------
# Fallback if the SSH client didn't set TERM (this alone fixes the up-arrow
# problem in many busted environments).
[ -z "$TERM" ] || [ "$TERM" = "dumb" ] && export TERM=xterm-256color
export LANG=${LANG:-en_US.UTF-8}
export LC_ALL=${LC_ALL:-en_US.UTF-8}

# ----- 2. History — large, deduped, append, share across sessions ------------
HISTSIZE=50000
HISTFILESIZE=100000
HISTCONTROL=ignoredups:erasedups:ignorespace
HISTTIMEFORMAT='%F %T  '
HISTIGNORE='ls:ll:la:cd:pwd:exit:clear:history'
shopt -s histappend                       # append, don't overwrite
shopt -s cmdhist                          # multi-line commands as one entry
# After every command: append the line to HISTFILE and reload it so all
# open shells share history immediately.
PROMPT_COMMAND='history -a; history -n'

# ----- 3. Shell options ------------------------------------------------------
shopt -s checkwinsize                     # update LINES/COLUMNS after resize
shopt -s globstar 2>/dev/null             # ** matches directories recursively
shopt -s autocd 2>/dev/null               # cd by typing a directory name
shopt -s cdspell dirspell 2>/dev/null     # autocorrect typos in paths

# ----- 4. Readline tweaks (arrow-key history search, etc.) -------------------
# These are normally read from /etc/inputrc or ~/.inputrc; we mirror the
# best defaults here so up/down do prefix-search if you've started typing.
bind '"\e[A": history-search-backward' 2>/dev/null
bind '"\e[B": history-search-forward'  2>/dev/null
bind 'set completion-ignore-case on'   2>/dev/null
bind 'set show-all-if-ambiguous on'    2>/dev/null
bind 'set colored-stats on'            2>/dev/null

# ----- 5. Colourful, informative prompt --------------------------------------
# user@host  cwd  (git-branch)
parse_git_branch() {
    git branch --show-current 2>/dev/null | sed 's/^/ (/;s/$/)/'
}
if [ -t 1 ]; then
    PS1='\[\e[1;32m\]\u@\h\[\e[0m\]:\[\e[1;34m\]\w\[\e[1;33m\]$(parse_git_branch)\[\e[0m\]\$ '
fi

# ----- 6. Standard aliases ---------------------------------------------------
alias ll='ls -alFh --color=auto'
alias la='ls -A --color=auto'
alias l='ls -CF --color=auto'
alias ls='ls --color=auto'
alias grep='grep --color=auto'
alias egrep='egrep --color=auto'
alias fgrep='fgrep --color=auto'
alias ..='cd ..'
alias ...='cd ../..'
alias h='history'
alias df='df -h'
alias du='du -h'
alias free='free -h'
alias ports='ss -tulnp'

# Safer defaults
alias rm='rm -i'
alias cp='cp -i'
alias mv='mv -i'

# Git shortcuts
alias gs='git status'
alias gd='git diff'
alias gco='git checkout'
alias gp='git pull --rebase'
alias gl='git log --oneline --graph --decorate -n 20'

# ----- 7. PATH additions -----------------------------------------------------
# Prepend common user-local bin dirs if they exist.
for dir in \
    "$HOME/.local/bin" \
    "$HOME/bin" \
    "/usr/local/bin" \
    "$HOME/.cargo/bin" \
    "$HOME/.poetry/bin" ; do
    [ -d "$dir" ] && case ":$PATH:" in *":$dir:"*) ;; *) PATH="$dir:$PATH" ;; esac
done
export PATH

# ----- 8. Python / pyenv / uv hooks (only if installed) ----------------------
if [ -d "$HOME/.pyenv" ]; then
    export PYENV_ROOT="$HOME/.pyenv"
    case ":$PATH:" in *":$PYENV_ROOT/bin:"*) ;; *) PATH="$PYENV_ROOT/bin:$PATH" ;; esac
    eval "$(pyenv init --path 2>/dev/null)"
    eval "$(pyenv init - 2>/dev/null)"
fi

# uv (modern Python/version manager)
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"

# ----- 9. AWS conveniences ---------------------------------------------------
# Show the active AWS profile/region in the prompt when set.
aws_ctx() {
    [ -n "$AWS_PROFILE" ] && printf '%s' " [aws:$AWS_PROFILE]"
}
PS1=${PS1/\\\$ /\$\(aws_ctx\)\\\$ }
alias awswho='aws sts get-caller-identity'

# ----- 10. Bash completion ---------------------------------------------------
if ! shopt -oq posix; then
    if [ -f /usr/share/bash-completion/bash_completion ]; then
        . /usr/share/bash-completion/bash_completion
    elif [ -f /etc/bash_completion ]; then
        . /etc/bash_completion
    fi
fi

# AWS CLI completion (if aws_completer is on PATH)
command -v aws_completer >/dev/null 2>&1 && complete -C aws_completer aws

# ----- 11. Editor & pager defaults -------------------------------------------
export EDITOR=${EDITOR:-vi}
export VISUAL=$EDITOR
export PAGER=${PAGER:-less}
export LESS='-RFX'

# ----- 12. Per-host overrides (kept out of source control) -------------------
[ -f "$HOME/.bashrc.local" ] && . "$HOME/.bashrc.local"
