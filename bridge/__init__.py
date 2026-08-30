"""The laptop half of agent-bridge: ssh forwards, supervised, with a web UI.

`gateway/` is the service on the cluster; `client/` talks to it; this keeps the
ssh forward in between alive. Kept a separate package because it is the only
part that runs on the operator's own machine and the only part that starts
processes on their behalf.
"""
from client._version import __version__

__all__ = ["__version__"]
