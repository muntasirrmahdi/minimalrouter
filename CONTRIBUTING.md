# Contributing

Small, focused changes are welcome.

## Ground rules

- This project is one Python file using only the standard library. A pull
  request that adds a dependency will be closed.
- The server stays bound to `127.0.0.1` and stays unauthenticated. It is a
  local tool, not a hosted service.
- No web UI and no desktop wrapper. The dashboard is the terminal.
- Keep the code readable: no clever abstractions for a 700-line file.

## Before you open a pull request

1. Run it: `python3 minimalrouter.py -h`
2. Start it: `python3 minimalrouter.py serve` and check `curl 127.0.0.1:8790/health`
3. Test the flow you changed, including one failure case (bad key, dead host,
   or a row that rate-limits) so fallback still works
4. Exercise the dashboard keys you touched; boot and quit is not enough

## Reporting bugs

Use the issue template and include:

- your OS and `python3 -V`
- the row you were using with the key removed
- the exact error text from `~/.minimalrouter/minimalrouter.log`
- what you expected instead

## Security

Never paste a real API key into an issue. Redact it. If you find a security
problem, describe it without a working exploit.
