"""Whether two questions (or a question and a rule) are about the same thing, without a model.

Small models ask what you've already answered, in new words: "Should the app support
splitting by percentage or equal amounts?" comes back as "Split by Percentage or Equal
Amounts". Words are cut to their first five letters (split, splitting -> "split") and the
small words dropped; two texts are the same when most of the shorter one's words are in
the other, or when their options are the same set.
"""

import re

SMALL = set("""a an the and or of to in on for by with is are be it its this that these should would could
can will do does app user users you your we our there any some what which who how when whether want need
include includes including support supports option options have has""".split())
STEM = 5
OVERLAP = 0.66


def words(text):
    return {w[:STEM] for w in re.findall(r"[a-z0-9%$]+", (text or "").lower()) if w not in SMALL}


def same_text(a, b):
    """Most of the shorter text's words are in the longer one (two words at least, or the same words)."""
    x, y = words(a), words(b)
    if not x or not y:
        return False
    shorter = min(len(x), len(y))
    if shorter < 2:
        return x == y
    return len(x & y) / shorter >= OVERLAP


def same_options(a, b):
    norm = lambda opts: {" ".join(sorted(words(o))) for o in opts or [] if words(o)}
    x, y = norm(a), norm(b)
    return len(x) >= 2 and x == y


def same_question(text, options, other_text, other_options=()):
    return same_text(text, other_text) or same_options(options, other_options)


def settled(text, options, answered, rules=()):
    """The answered question this one repeats ({text, options, answer}), a rule it repeats (a str), or None."""
    for q in answered:
        if same_question(text, options, q["text"], q.get("options", ())):
            return q
    for rule in rules:
        if same_text(text, rule):
            return rule
    return None
