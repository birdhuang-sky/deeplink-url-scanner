import re
from typing import Iterable, Set

PARAM_LITERAL_RE = re.compile(r'["\\\'](utm_[a-zA-Z0-9_]+|[a-zA-Z0-9]*_id|associate_id|tracking_id)["\\\']')

LANG_PATTERNS = {
    "js_urlsearchparams": re.compile(r'searchParams\\.get\\((["\\\'])([^"\\\']+)\\1\\)'),
    "python_parse_qs": re.compile(r'parse_qs\([^\n]*["\']([a-zA-Z0-9_]+)["\']'),
    "python_dict_access": re.compile(
        r'\[["\'](utm_[A-Za-z0-9_]+|associate_id|associateid|associateId|associateID)["\']\]'
    ),
    "java_uri": re.compile(r'getQueryParameter\\((["\\\'])([^"\\\']+)\\1\\)'),
    "swift_queryitems": re.compile(r'name\\s*==\\s*\\"([^\\"]+)\\"'),
    "go_query_get": re.compile(r'Query\\(\\)\\.Get\\((["\\\'])([^"\\\']+)\\1\\)'),
}

DEEPLINK_CONTEXT_HINTS = [
    "deeplink_url",
    "transport_deeplink",
    "skippy_api",
    "deeplink",
]

def extract_params_from_line(line: str) -> Set[str]:
    found: Set[str] = set()
    for m in PARAM_LITERAL_RE.finditer(line):
        found.add(m.group(1))
    for pattern in LANG_PATTERNS.values():
        for m in pattern.finditer(line):
            for g in m.groups()[1:]:
                if g and len(g) < 128 and re.match(r'[a-zA-Z0-9_\\-]+', g):
                    if g.startswith("utm_") or g.endswith("_id") or "utm" in g or "id" in g:
                        found.add(g)
    return found

def merge_keywords(builtin: Iterable[str], extra: Iterable[str]) -> Set[str]:
    s = {k.strip() for k in builtin if k.strip()}
    s.update({k.strip() for k in extra if k.strip() and not k.strip().startswith("#")})
    return s