# vconsole-python

Python client for Valve's Source 2 VConsole2 protocol — the same binary TCP protocol that `vconsole2.exe` uses to mirror a game's console and send commands over the wire.

Works with Deadlock, CS2 and Dota 2 (anything Source 2 that listens on `tcp://127.0.0.1:29000` after being launched with `-vconsole`). Pure Python 3.11+, standard library only, no dependencies.

## Why this exists

Valve ships `vconsole2.exe` but no library. The only prior Python port ([uilton-oliveira/VConsoleLib.python](https://github.com/uilton-oliveira/VConsoleLib.python)) is Python 2, unmaintained since 2023, and uses a CMND packet version that CS2 and Deadlock reject outright (`Version Mismatch: Expected 212, Got 210`).

This is a from-scratch reimplementation that actually works against current (2026) builds.

## Install

Copy `vconsole_client.py` into your project. One file, stdlib only.

## Usage

Launch your Source 2 game with `-vconsole` in the Steam launch options (and `-dev -insecure` if you want cheats enabled locally):

```
-vconsole -dev -insecure +sv_cheats 1
```

Then:

```python
from vconsole_client import VConsoleClient

def on_print(channel, message):
    print(f"[{channel}] {message.strip()}")

client = VConsoleClient(on_print=on_print)
client.connect()          # 127.0.0.1:29000
client.send_command("echo hello from python")
client.send_command("noclip")
# ... do stuff ...
client.disconnect()
```

The client runs a background thread that reads the log stream from the game and dispatches `on_print(channel, message)` for every `PRNT` packet. `send_command` is thread-safe and returns immediately — you don't wait for the game to acknowledge.

### Callbacks

| callback              | when                                  |
|-----------------------|---------------------------------------|
| `on_print(ch, msg)`   | every log line from the game          |
| `on_channels(list)`   | on initial channel list packet        |
| `on_cvar(cvar)`       | for each cvar dumped during handshake |
| `on_addon(name)`      | when an addon is loaded               |
| `on_disconnect(err)`  | on socket close or error              |
| `on_packet(raw)`      | every packet, for debugging           |

## Protocol notes

Wire framing for every packet:

```
msg_type [4 bytes, ASCII]     e.g. "CMND", "PRNT", "AINF"
version  [uint32 big-endian]  byte pattern depends on message type
length   [uint16 big-endian]  total packet length including header
handle   [uint16 big-endian]  pipe handle, 0 for commands
body     [length - 12 bytes]  type-specific payload
```

Common message types:

| type   | direction       | meaning                                |
|--------|-----------------|----------------------------------------|
| `AINF` | server → client | game/session info on connect           |
| `PRNT` | server → client | console log line                       |
| `CHAN` | server → client | list of log channels                   |
| `CVAR` | server → client | cvar definition                        |
| `CFGV` | server → client | config dump finished                   |
| `ADON` | server → client | addon loaded                           |
| `CMND` | client → server | execute console command                |

### The version byte trap

`CMND` must use version bytes `00 D4 00 00` on the wire (uint32 BE `0x00D40000`, or `htonl(0x00D40000)` in C). Older ports shipped `0x00D20000` which the modern server rejects. The value isn't really a "version" in the semver sense — it's a magic discriminator the server checks verbatim.

## Example: running a benchmark demo

```python
from vconsole_client import VConsoleClient
import time

c = VConsoleClient()
c.connect()
c.send_command("sv_cheats 1")
c.send_command("playdemo bench")
time.sleep(1.5)                    # let the demo load
c.send_command("demo_gototick 640") # skip warm-up
# start your FPS recorder here
time.sleep(60)
c.send_command("disconnect")
c.disconnect()
```

## Limitations

- Commands that require an active match with `sv_cheats` enforced (e.g. `noclip`) will be silently ignored on matchmaking servers. You need a local listen-server or sandbox map.
- `record <name>` does not work while watching a demo; Source 2 rejects it with `Can't record during demo playback`.
- No authentication is implemented. VConsole2 has no password mechanism — if port 29000 is exposed to the network, anyone can run commands. Keep it on `127.0.0.1` (the default) unless you know what you're doing.

## References

- [theokyr/CS2RemoteConsole](https://github.com/theokyr/CS2RemoteConsole) — C++ reference implementation, cleanest modern protocol spec.
- [Penguinwizzard/VConsoleLib](https://github.com/Penguinwizzard/VConsoleLib) — original C implementation, predates CS2.
- [xezno/VConsoleLib](https://github.com/xezno/VConsoleLib) — C# port, read-only (no CMND support).

## License

MIT.
