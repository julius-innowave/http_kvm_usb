"""USB HID keyboard usages (US layout) and how text and key names become reports.

A `#B1@` report carries one modifier byte and up to six key usages. The board
sends key *positions*, not characters, so the target's keyboard layout decides
what appears; everything here assumes US.
"""
from __future__ import annotations

#: Modifier byte bits.
MODIFIER_BITS = {
    "LCTRL": 0x01, "LSHIFT": 0x02, "LALT": 0x04, "LGUI": 0x08,
    "RCTRL": 0x10, "RSHIFT": 0x20, "RALT": 0x40, "RGUI": 0x80,
}

#: Every name a request may use for a modifier, and the bit name it means.
MODIFIER_NAMES = {
    "CTRL": "LCTRL", "CONTROL": "LCTRL", "SHIFT": "LSHIFT", "ALT": "LALT",
    "WIN": "LGUI", "GUI": "LGUI", "META": "LGUI", "SUPER": "LGUI", "CMD": "LGUI", "WINDOWS": "LGUI",
    **{name: name for name in MODIFIER_BITS},
}

#: Named keys → HID usage.
KEYS: dict[str, int] = {
    "ENTER": 0x28, "ESC": 0x29, "BACKSPACE": 0x2A, "TAB": 0x2B, "SPACE": 0x2C,
    "MINUS": 0x2D, "EQUAL": 0x2E, "LEFT_BRACKET": 0x2F, "RIGHT_BRACKET": 0x30,
    "BACKSLASH": 0x31, "NON_US_HASH": 0x32, "SEMICOLON": 0x33, "QUOTE": 0x34,
    "GRAVE": 0x35, "COMMA": 0x36, "PERIOD": 0x37, "SLASH": 0x38,
    "CAPS_LOCK": 0x39,
    **{f"F{n}": 0x3A + n - 1 for n in range(1, 13)},
    **{f"F{n}": 0x68 + n - 13 for n in range(13, 25)},
    "PRINT_SCREEN": 0x46, "SCROLL_LOCK": 0x47, "PAUSE": 0x48,
    "INSERT": 0x49, "HOME": 0x4A, "PAGE_UP": 0x4B, "DELETE": 0x4C, "END": 0x4D, "PAGE_DOWN": 0x4E,
    "RIGHT": 0x4F, "LEFT": 0x50, "DOWN": 0x51, "UP": 0x52,
    "NUM_LOCK": 0x53, "NUM_SLASH": 0x54, "NUM_ASTERISK": 0x55, "NUM_MINUS": 0x56,
    "NUM_PLUS": 0x57, "NUM_ENTER": 0x58,
    **{f"NUM_{n}": 0x59 + n - 1 for n in range(1, 10)},
    "NUM_0": 0x62, "NUM_DOT": 0x63,
    "APPLICATION": 0x65, "MENU": 0x65,
}

#: Characters → (modifier byte, HID usage).
CHARS: dict[str, tuple[int, int]] = {}
for _i, _ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    CHARS[_ch] = (0, 0x04 + _i)
    CHARS[_ch.upper()] = (MODIFIER_BITS["LSHIFT"], 0x04 + _i)
for _ch, _code in zip("1234567890", range(0x1E, 0x28)):
    CHARS[_ch] = (0, _code)
for _ch, _code in zip("!@#$%^&*()", range(0x1E, 0x28)):
    CHARS[_ch] = (MODIFIER_BITS["LSHIFT"], _code)
for _plain, _shifted, _code in (("-", "_", 0x2D), ("=", "+", 0x2E), ("[", "{", 0x2F), ("]", "}", 0x30),
                                ("\\", "|", 0x31), (";", ":", 0x33), ("'", '"', 0x34), ("`", "~", 0x35),
                                (",", "<", 0x36), (".", ">", 0x37), ("/", "?", 0x38)):
    CHARS[_plain] = (0, _code)
    CHARS[_shifted] = (MODIFIER_BITS["LSHIFT"], _code)
CHARS[" "] = (0, KEYS["SPACE"])
CHARS["\n"] = CHARS["\r"] = (0, KEYS["ENTER"])
CHARS["\t"] = (0, KEYS["TAB"])

#: Aliases the Remote Adapter API accepts for named keys (case-insensitive).
KEY_ALIASES = {
    "return": "ENTER", "escape": "ESC", "spacebar": "SPACE", "del": "DELETE", "ins": "INSERT",
    "pageup": "PAGE_UP", "pgup": "PAGE_UP", "pagedown": "PAGE_DOWN", "pgdn": "PAGE_DOWN",
    "arrowleft": "LEFT", "arrowright": "RIGHT", "arrowup": "UP", "arrowdown": "DOWN",
    "printscreen": "PRINT_SCREEN", "prtsc": "PRINT_SCREEN", "numlock": "NUM_LOCK",
    "scrolllock": "SCROLL_LOCK", "capslock": "CAPS_LOCK",
}


def report(modifiers: int = 0, *usages: int) -> str:
    """One `#B1@` keyboard report: modifier byte, reserved byte, six usages."""
    keys = list(usages[:6]) + [0] * (6 - len(usages[:6]))
    return "#B1@" + f"{modifiers:02X}00" + "".join(f"{k:02X}" for k in keys) + "!"


RELEASE = report()


def text_keys(text: str) -> tuple[list[tuple[int, int]], str]:
    """Characters → [(modifiers, usage)], plus the characters that were skipped.

    Characters the US layout cannot produce are skipped (the spec asks for the
    rest of the string to be typed rather than the whole request failing).
    """
    keys, skipped = [], []
    for ch in text:
        if ch in CHARS:
            keys.append(CHARS[ch])
        else:
            skipped.append(ch)
    return keys, "".join(dict.fromkeys(skipped))


def resolve_key(name: str) -> tuple[int, int]:
    """One key name → (modifiers, usage). Raises KeyError for an unknown key."""
    raw = name.strip()
    if len(raw) == 1:
        if raw.isalpha():
            return CHARS[raw.lower()]                  # 'A' is the a key; shift comes from the request
        if raw in CHARS:
            return CHARS[raw]
    upper = KEY_ALIASES.get(raw.lower(), raw).upper()
    if upper in KEYS:
        return 0, KEYS[upper]
    raise KeyError(name)


def parse_combo(parts: list[str]) -> tuple[int, list[int]]:
    """["CTRL", "ALT", "DELETE"] → (modifier byte, [usages]). Raises KeyError / ValueError."""
    modifiers, usages = 0, []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        bit_name = MODIFIER_NAMES.get(part.upper())
        if bit_name:
            modifiers |= MODIFIER_BITS[bit_name]
            continue
        mods, usage = resolve_key(part)
        modifiers |= mods
        if usage not in usages:
            usages.append(usage)
    if len(usages) > 6:
        raise ValueError("At most 6 non-modifier keys can be pressed together.")
    if not modifiers and not usages:
        raise ValueError("No key given.")
    return modifiers, usages
