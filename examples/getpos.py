"""Тест: получаем getpos через VConsoleClient и печатаем ответ."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from vconsole_client import VConsoleClient


def main():
    received = []
    c = VConsoleClient(on_print=lambda ch, msg: received.append(msg.strip()))
    c.connect()
    time.sleep(1.0)
    print(">>> getpos")
    c.send_command("getpos")
    time.sleep(1.0)
    print("responses:")
    for m in received[-10:]:
        if "setpos" in m.lower() or "setang" in m.lower():
            print(f"  ANSWER: {m}")
    c.disconnect()


if __name__ == "__main__":
    main()
