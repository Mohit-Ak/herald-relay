"""Skills `herald-tunnel setup` installs into the user's own Hermes.

The agent must always know two things exist (contract:
herald docs/internal/plans/screen-passwords-contract.md):
  * herald-screen: a screen the user can watch, offered only when a step
    needs a person, and set up only after the user says yes;
  * herald-passwords: their Chrome passwords in Hermes' vault, imported by
    the agent from an export file the user gives it, filled with
    browser_vault_fill. The agent never sees a password.

Written into ~/.hermes/skills/herald/<name>/SKILL.md, and the same under
every profile in ~/.hermes/profiles/ (each profile is its own agent with its
own skills and its own vault). Re-running setup (or `herald-tunnel skills`)
rewrites them; nothing else in the skills dir is touched.

The import command in the skill is written out in full for THIS install
(`{{herald_tunnel}}`): herald-tunnel lives in its own venv, which is not on the
agent's PATH.
"""
from __future__ import annotations

import os
from pathlib import Path

SCREEN_SKILL = """---
name: herald-screen
description: Show the user a screen they can watch or take over, when a step needs their hands (sign-in, CAPTCHA, 2FA, a GUI-only setting).
version: 1.1.0
author: Herald
metadata:
  hermes:
    tags: [Herald, screen, desktop, VNC, Bot Screen, sign-in, CAPTCHA, takeover]
---

# Herald: screen sharing on this computer

Whenever a step needs the user's own hands or eyes (a sign-in that wants a
code from their phone, a CAPTCHA, a consent screen, a setting only a GUI
offers), you can give them a screen to watch and take over.

## First, check what this computer supports

```
uname -s
hermes computer-use screen status 2>&1 | head -5
```

* **Linux:** Hermes' Bot Screen is a private desktop just for you (not the
  user's own screen). It needs a one-time install with sudo.
* **"'screen' is not a `hermes computer-use` command":** this Hermes is too
  old to have Bot Screen. Say so in one sentence and offer to update Hermes
  (`hermes update`); don't try to build a screen some other way.
* **macOS or Windows:** there is no private desktop for an agent here.
  Sharing would mean the user's real screen, which needs their approval in
  system settings. Tell them in one sentence it isn't available on this
  computer, and do the step another way or ask them to do it themselves.

## Ask before setting anything up

Never install or start it on your own. Ask in one sentence, for example:
"I need you to sign in to the bank. Want me to set up a screen you can watch
and take over? It's a one-time install that asks for your password once."

Only after a clear yes, on Linux:

```
hermes computer-use screen install     # once; asks for sudo
hermes computer-use screen start
```

Then do your browsing with the browser shown on that screen, get as far as
you can, and say in one or two short sentences what you need, for example:
"I'm on the bank's sign-in page. Tap Open screen, take over and sign in,
then hand it back." Put this exact marker on its own at the end of your
reply:

[[herald:connect:screen]]

The Herald app turns it into an Open screen button (the user never sees the
marker); there they watch, Take over and Hand back. While they have control
your screen tools are refused: wait for them. At the computer itself, the
Hermes Desktop app shows the same screen under Bot Screen.

After they hand it back, look at the screen again before carrying on.

## Never

* Never install or start a screen without the user's yes in this
  conversation.
* Never ask the user to type a password or code into the chat.
* Stop the screen when the task is done: `hermes computer-use screen stop`.
"""

PASSWORDS_SKILL = """---
name: herald-passwords
description: Sign in to websites with the user's saved Chrome passwords (vault), and import their Chrome password export into the vault.
version: 1.0.0
author: Herald
metadata:
  hermes:
    tags: [Herald, passwords, Chrome, vault, login, sign in, credentials, import]
---

# Herald: saved passwords

Saved logins live in Hermes' encrypted vault. You see which sites and
usernames are there, never the passwords.

## Signing in to a site

1. `browser_vault_list` shows saved logins (site, username, handle).
2. Type the username yourself, then `browser_vault_fill` with the handle for
   that exact site. Hermes types the password; you never see it.
3. A code from their phone: `browser_vault_enter_code`.

## They use 1Password or Bitwarden

Nothing to import, as long as that manager's command-line tool (`op` or
`bw`) is installed on this computer. Hermes reads it directly:
`browser_vault_list` shows those logins too, and says when it is locked. `browser_vault_unlock` asks the user
for their master password on their own screen; you never see it.

## Importing their Chrome passwords

When the user wants you to use their Chrome passwords, or a login you need
isn't saved:

1. Tell them how to export, in plain words: Chrome, then Settings > Passwords
   and autofill > Google Password Manager > Settings > Export passwords. Once
   per Chrome profile. Ask where they saved the file.
2. Import it (prints counts only, and deletes the export afterwards so the
   plain-text file doesn't stay on disk):

```
{{herald_tunnel}} passwords import --file "<path they gave>" --profile "<profile name>" --delete
```

Run it exactly as written; it is not on PATH under a shorter name.

3. Tell them how many logins were saved. For another Chrome profile, repeat.
   `not_a_password_export` means that file isn't a password export (it is
   left untouched): ask them for the right one.

Check how many are saved: `{{herald_tunnel}} passwords count`.

## Never

* Never open, read, print, copy or summarise the export file or the vault
  files. Only the import command touches them.
* Never ask for, accept, repeat or type a password yourself, even if the
  user offers it in chat.
"""

SKILLS = {"herald-screen": SCREEN_SKILL, "herald-passwords": PASSWORDS_SKILL}


def homes(hermes_home: Path) -> list[Path]:
    """This Hermes home and every profile under it."""
    root = Path(hermes_home)
    out = [root]
    try:
        out += sorted(p for p in (root / "profiles").iterdir()
                      if p.is_dir() and not p.name.startswith("."))
    except OSError:
        pass
    return out


def install(hermes_home: Path, command: str = "herald-tunnel",
            all_profiles: bool = False) -> list[Path]:
    """Write the skills; returns the files written. `command` is how to run
    herald-tunnel on this machine. A profile that can't be written is
    skipped, not fatal."""
    written = []
    first = True
    for home in (homes(hermes_home) if all_profiles else [Path(hermes_home)]):
        try:
            for name, text in SKILLS.items():
                d = home / "skills" / "herald" / name
                d.mkdir(parents=True, exist_ok=True)
                p = d / "SKILL.md"
                tmp = p.with_suffix(".md.tmp")
                tmp.write_text(text.replace("{{herald_tunnel}}", command), encoding="utf-8")
                os.replace(tmp, p)
                written.append(p)
        except OSError:
            if first:
                raise
        first = False
    return written
