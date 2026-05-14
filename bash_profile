# =============================================================================
# bash_profile — login-shell entry point
# -----------------------------------------------------------------------------
# On login shells (SSH, `sudo -i`, etc.) bash reads ~/.bash_profile but NOT
# ~/.bashrc. This file makes sure ~/.bashrc is always sourced so history,
# aliases, and prompt work everywhere.
#
# INSTALL:
#   scp bash_profile host:~/.bash_profile
# =============================================================================

# Source the user's bashrc if it exists
[ -f ~/.bashrc ] && . ~/.bashrc

# Per-user environment that should only apply to login shells goes here.
# Example:
#   umask 022
