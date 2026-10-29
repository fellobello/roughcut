"""A page in parts, so a fix rewrites only the part that's wrong.

index.html is split into its markup (the <main> element) and its script (the one
<script type="module">); styles.css is the third part. When the page check or a test
user finds a problem, the designer rewrites one part and the others are kept exactly,
which is cheaper and can't break what already worked.

Some problems need no model at all: results hidden with the hidden attribute, or
example numbers written into the results, are repaired here directly.
"""

from __future__ import annotations

import re

SCRIPT = re.compile(r'(<script\s+type="module"[^>]*>)(.*?)(</script>)', re.S)
MAIN = re.compile(r"<main\b.*?</main>", re.S)
RESULT_CLASSES = ("stats", "stat", "stat-value", "list")
NUMBER_IN_VALUE = re.compile(r'(<div class="stat-value[^"]*"[^>]*>)([^<]*\d[^<]*)(</div>)')


def get(part, html, css=""):
    """The text of one part, or None when the page doesn't have it in the expected shape."""
    if part == "styles":
        return css
    if part == "script":
        m = SCRIPT.search(html)
        return m.group(2).strip() if m else None
    if part == "markup":
        m = MAIN.search(html)
        return m.group(0) if m else None
    return html


def put(part, html, new):
    """index.html with one part replaced (markup or script). Returns None if the part isn't there."""
    new = new.strip()
    if part == "script":
        new = re.sub(r"^<script[^>]*>|</script>$", "", new).strip()
        m = SCRIPT.search(html)
        if not m:
            return None
        return html[: m.start(2)] + "\n" + new + "\n" + html[m.end(2):]
    if part == "markup":
        found = MAIN.search(new)
        if not found:
            return None
        m = MAIN.search(html)
        if not m:
            return None
        return html[: m.start()] + found.group(0) + html[m.end():]
    raise ValueError(f"put() takes markup or script, not {part}")


def repair(html, check):
    """Fix what the page check found without a model call. Returns (html, what was done) or (html, "")."""
    done = []
    if check.get("hidden"):
        def unhide(m):
            tag = m.group(0)
            classes = re.search(r'class="([^"]*)"', tag)
            if classes and set(classes.group(1).split()) & set(RESULT_CLASSES) or re.search(r"<output\b", tag):
                return re.sub(r"\s+hidden(=\"[^\"]*\")?(?=[\s>/])", "", tag)
            return tag

        fixed = re.sub(r"<[a-z]+\b[^>]*\shidden\b[^>]*>", unhide, html)
        if fixed != html:
            html = fixed
            done.append("removed the hidden attribute from the results")
    probe = check.get("probe") or {}
    if probe.get("prefilled"):
        fixed = NUMBER_IN_VALUE.sub(lambda m: m.group(1) + "—" + m.group(3), html)
        if fixed != html:
            html = fixed
            done.append("replaced example numbers in the results with —")
    return html, "; ".join(done)


MODULE_SCRIPT = re.compile(r"<script[^>]*type=[\"']module[\"'][^>]*>(.*?)</script>", re.S | re.I)
BY_ID = re.compile(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""")
BY_SELECTOR = re.compile(r"""querySelector(?:All)?\(\s*(['"`])(.+?)\1\s*\)""")


def missing_targets(html):
    """What the page script looks up that the markup doesn't have, like "#total" or "[aria-pressed=\\"false\\"]".

    Only the simple selectors a page uses are checked (an id, a class, an attribute, a tag
    with one of those); anything else, or anything with ${...} in it, is taken on trust.
    """
    scripts = MODULE_SCRIPT.findall(html)
    markup = MODULE_SCRIPT.sub("", html)
    code = re.sub(r"/\*.*?\*/|(?<![:'\"])//[^\n]*", "", "\n".join(scripts), flags=re.S)  # comments don't count
    ids = set(re.findall(r"""\sid=["']([^"']+)["']""", markup))
    classes = {c for group in re.findall(r"""\sclass=["']([^"']*)["']""", markup) for c in group.split()}
    missing = [f"#{i}" for i in BY_ID.findall(code) if i not in ids]
    for _, selector in BY_SELECTOR.findall(code):
        if "${" in selector or "," in selector or " " in selector.strip() or ">" in selector:
            continue
        m = re.fullmatch(r"([a-zA-Z][\w-]*)?(?:#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:\s*=\s*[\"']?([^\"'\]]*)[\"']?)?\])", selector.strip())
        if not m:
            continue
        tag, id_, cls, attr, value = m.groups()
        if id_ is not None:
            ok = id_ in ids
        elif cls is not None:
            ok = cls in classes
        else:
            any_tag = r"[a-zA-Z][\w-]*"
            pattern = rf"""<{tag or any_tag}\b[^>]*\s{re.escape(attr)}""" + (
                rf"""\s*=\s*["']{re.escape(value)}["']""" if value is not None else r"""\b""")
            ok = re.search(pattern, markup) is not None
        if not ok:
            missing.append(selector.strip())
    return list(dict.fromkeys(missing))
