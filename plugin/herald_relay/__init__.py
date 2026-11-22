"""Herald Relay plugin for Hermes — connects your local Hermes to Herald Cloud."""
__version__ = "0.8.0"


def is_local_door(path: str) -> bool:
    """A path the tunnel answers itself (``/_herald/...``), never Hermes.

    One list for both transports (SSE forward and the edge socket), so a new
    door can't be wired into one and silently missed in the other.
    """
    from herald_relay import agent_info, local_bridge, local_passwords, local_screen  # noqa: PLC0415
    return bool(local_screen.matches(path) or local_passwords.matches(path)
                or agent_info.matches(path) or local_bridge.matches(path))
