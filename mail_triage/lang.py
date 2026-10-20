"""Is this email English enough for the English checkpoint? Standard library only.

Non-Latin script or accented letters (Czech, German, …) mean "not English". Plain-ASCII text
counts as English unless it clearly lacks English function words.
"""

import re

_ENGLISH = set("the and you your for are with this that have from not will our please can is to of in on "
               "it we be at by or as an if all new now get more here".split())
_WORDS = re.compile(r"[a-z']+")


def is_english(state: dict) -> bool:
    text = " ".join(str(state.get(key, "")) for key in ("subject", "body"))
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 20:
        return True
    if sum(1 for c in letters if ord(c) > 127) / len(letters) > 0.01:
        return False
    words = _WORDS.findall(text.lower())
    if len(words) < 8:
        return True
    return sum(word in _ENGLISH for word in words) / len(words) >= 0.05
