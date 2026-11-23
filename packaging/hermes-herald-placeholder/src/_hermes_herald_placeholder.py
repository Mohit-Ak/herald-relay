"""Placeholder. Herald Bridge is published on PyPI as ``herald-bridge``.

``hermes-herald`` exists only so nobody else can claim the name. It contains
no code, makes no network calls and depends on nothing.
"""

MESSAGE = (
    "hermes-herald is a placeholder, not Herald Bridge.\n"
    "Install Herald Bridge into its own venv (the only place it updates itself):\n"
    "\n"
    "    python3 -m venv ~/.herald-relay && "
    "~/.herald-relay/bin/pip install herald-bridge && "
    "~/.herald-relay/bin/herald-tunnel setup\n"
    "\n"
    "Docs: https://github.com/Mohit-Ak/herald-relay#readme"
)


def main() -> int:
    """`hermes-herald` console script: print the install command, exit 1."""
    print(MESSAGE)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
