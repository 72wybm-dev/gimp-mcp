# Security

GIMP MCP exposes local control over a running GIMP process. Treat the socket as
a privileged local automation interface.

## Defaults

- The bridge listens on a Unix socket, not a TCP port.
- The socket is created with `0600` permissions for the current user.
- The MCP server can auto-start a private headless GIMP process for automation.

## Guidance

- Do not expose the socket path to untrusted users.
- Do not run this bridge as root.
- Prefer the default Unix socket transport over TCP.
- Review any future raw Python execution feature carefully before enabling it.

## Reporting Issues

Open a GitHub issue with a clear reproduction and include:

- GIMP version
- Operating system
- MCP client
- Whether the bridge was started headless or from the visible GIMP app
