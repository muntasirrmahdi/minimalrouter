# systemd user unit

`install.sh --systemd` writes a **user** unit to
`${XDG_CONFIG_HOME:-~/.config}/systemd/user/minimalrouter.service` and runs
`systemctl --user daemon-reload`. It does not enable or start the service — an
always-on listener is your decision.

```sh
sh install.sh --systemd
systemctl --user enable --now minimalrouter
systemctl --user status minimalrouter
journalctl --user -u minimalrouter -f
```

The unit runs `~/.local/bin/minimalrouter serve` with
`MINIMALROUTER_PORT=8790` and restarts on failure. A user unit starts at login,
not at boot; for a desktop tool that is the correct target. If you need it
before login, use a system unit with the same `ExecStart` and the absolute
paths shown in your unit file.

To remove it:

```sh
systemctl --user disable --now minimalrouter
rm ~/.config/systemd/user/minimalrouter.service
systemctl --user daemon-reload
```
