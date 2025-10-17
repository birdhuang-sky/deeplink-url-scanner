# src/org_scanner/github_search_deeplink_url.py
#!/usr/bin/env python3
import argparse, csv, json, os, sys, time, requests, threading, urllib.parse, hashlib, re, random
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List
from pathlib import Path
from org_scanner.repo_filter import ExcludeRepoFilter


API_BASE = "https://api.github.com"
VERIFY: bool | str = True
EXCLUDE_FILE_TYPE_LIST = ['PROTO']

@dataclass
class Hit:
    repo: str
    path: str
    url: str

class RateLimiter:
    """Simple sliding-window request limiter."""
    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self.lock = threading.Lock()
        self.times = deque()

    def acquire(self):
        with self.lock:
            now = time.time()
            while self.times and now - self.times[0] > self.window:
                self.times.popleft()
            if len(self.times) >= self.limit:
                sleep_for = self.window - (now - self.times[0]) + 0.001
                if sleep_for > 0:
                    time.sleep(sleep_for)
                # After sleep, cleanup again
                now = time.time()
                while self.times and now - self.times[0] > self.window:
                    self.times.popleft()
            self.times.append(time.time())

# --- added for multi-token rotation ---
TOKEN_LIST: list[str] = []
_TOKEN_IDX = 0
_TOKEN_LOCK = threading.Lock()

def get_token() -> str | None:
    """Round-robin select a token for this request."""
    global _TOKEN_IDX
    if not TOKEN_LIST:
        return None
    with _TOKEN_LOCK:
        tok = TOKEN_LIST[_TOKEN_IDX]
        _TOKEN_IDX = (_TOKEN_IDX + 1) % len(TOKEN_LIST)
        return tok
# --- end added ---

# --- added: token+index helper for logging ---
def get_token_idx() -> tuple[str | None, int]:
    """Return (token, index_used) and advance round-robin."""
    global _TOKEN_IDX
    if not TOKEN_LIST:
        return None, -1
    with _TOKEN_LOCK:
        idx = _TOKEN_IDX
        tok = TOKEN_LIST[idx]
        _TOKEN_IDX = (_TOKEN_IDX + 1) % len(TOKEN_LIST)
        return tok, idx
# --- end added ---

def headers(tok:str|None):
    h={"Accept":"application/vnd.github.v3+json",
       "User-Agent":"deeplink-url-scanner/4.0"}
    if tok: h["Authorization"]=f"Bearer {tok}"
    return h

def _search_endpoint():
    return f"{API_BASE}/search/code"

def _org_repos_endpoint(org:str):
    return f"{API_BASE}/orgs/{org}/repos"

# def ensure_exact(q: str) -> str:
#     if ('"' not in q) and any(c in q for c in "_-. "):
#         return f'"{q}"'
#     return q

def is_file_type_excluded(path: str) -> bool:
    ext = path.rsplit('.', 1)[-1].upper() if '.' in path else ''
    is_exclude = ext in EXCLUDE_FILE_TYPE_LIST
    if is_exclude:
        print(f"is_file_type_excluded: {path} -> {is_exclude}", file=sys.stderr)
    return is_exclude

def search_org(org, query, tok, max_pages=10):
    hits=[]
    session=requests.Session()
    # query = ensure_exact(query)
    q=f"org:{org} {query} in:file"
    print(f"q = {q}", file=sys.stderr)
    for page in range(1,max_pages+1):
        r=session.get(_search_endpoint(),
                      params={"q":q,"per_page":100,"page":page},
                      headers=headers(get_token()),  # changed: rotate token
                      timeout=30, verify=VERIFY)
        if r.status_code!=200:
            print(f"[org-search] HTTP {r.status_code} {r.text[:150]}",file=sys.stderr)
            break
        data=r.json()
        if page==1:
            print(f"[org-search] total_count={data.get('total_count')}",file=sys.stderr)
        items=data.get("items",[])
        for it in items:
            if is_file_type_excluded(it["path"]):
                continue
            repo=it["repository"]["full_name"]; path=it["path"]; url=it["html_url"]
            hits.append(Hit(repo,path,url))
        if len(items)<100: break
    return hits

def list_repos(org,tok):
    out=[]; page=1
    while True:
        r=requests.get(_org_repos_endpoint(org),
                       headers=headers(get_token()),  # changed
                       params={"per_page":100,"page":page,"type":"all"},
                       timeout=30, verify=VERIFY)
        if r.status_code!=200: break
        chunk=r.json()
        if not chunk: break
        out+=[c["full_name"] for c in chunk if c.get("full_name")]
        page+=1
    return out

def load_keywords(path: Path) -> list[str]:
    kws = []
    if not path.exists():
        print(f"[keywords] file not found: {path}", file=sys.stderr)
        return kws
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kws.append(line)
    print(f"[keywords] loaded {len(kws)} keywords from {path}", file=sys.stderr)
    return kws

def load_repo_list(path: Path) -> list[str]:
    repos: list[str] = []
    if not path.exists():
        print(f"[repos] file not found: {path}", file=sys.stderr)
        return repos
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        repos.append(line)
    print(f"[repos] loaded {len(repos)} repos from {path}", file=sys.stderr)
    return repos

def preload_repo_set(path: Path) -> set[str]:
    """Load pre-specified repos (one per line) OR from a CSV produced earlier.
    Accept lines like:
      repo_name
      repo_name,3,url1 | url2
    Only the first comma-separated field is treated as the repo.
    """
    if not path.exists():
        print(f"[preload] path {path} not exist.", file=sys.stderr)
        return set()
    repos: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # If CSV header or malformed, skip
        first_field = line.split(",", 1)[0]
        if first_field.lower() == "repo":
            continue
        repos.add(first_field)
    if repos:
        print(f"[preload] loaded {len(repos)} repos from {path}; skipping org search", file=sys.stderr)
    else:
        print(f"[preload] file {path} empty; proceeding with normal org search", file=sys.stderr)
    return repos

def keyword_query_fragment(kw: str) -> str:
    # Quote keyword if it contains spaces or special chars
    if any(c.isspace() for c in kw) or any(c in kw for c in '"\''):
        return f'"{kw}"'
    return kw

def build_repo_keyword_query(repo: str, keyword: str, extra_filters: str = "") -> str:
    """Compose the GitHub code search query for a repo/keyword combination."""
    parts = [f"repo:{repo}", keyword_query_fragment(keyword)]
    extra = extra_filters.strip()
    if extra:
        parts.append(extra)
    return " ".join(parts)

def build_code_search_url(query: str) -> str:
    """Return a GitHub web URL for a ready-to-use code search."""
    return f"https://github.com/search?q={urllib.parse.quote(query)}&type=code"

def build_api_search_url(query: str) -> str:
    """Return a GitHub REST API URL for executing the search query."""
    return f"{_search_endpoint()}?q={urllib.parse.quote(query)}"

# --- added stats & adaptive helpers ---
token_stats = {
    "requests": [0, 0],   # per token index
    "403": [0, 0],
    "last_log": time.time(),
    "window_extended": False
}

def log_token_stats_if_needed():
    if not TOKEN_LIST:
        return
    total_requests = sum(token_stats["requests"])
    if total_requests % 200 == 0 and total_requests > 0:
        r0 = token_stats["requests"][0] if len(token_stats["requests"]) > 0 else 0
        r1 = token_stats["requests"][1] if len(token_stats["requests"]) > 1 else 0
        f0 = token_stats["403"][0] if len(token_stats["403"]) > 0 else 0
        f1 = token_stats["403"][1] if len(token_stats["403"]) > 1 else 0
        print(f"[tokens-stats] reqs={total_requests} token0(req={r0},403={f0}) token1(req={r1},403={f1})",
              file=sys.stderr)

def adaptive_sleep_for_403(r, idx, attempt):
    # Returns (sleep_seconds, reason)
    remaining = r.headers.get("X-RateLimit-Remaining")
    reset = r.headers.get("X-RateLimit-Reset")
    retry_after = r.headers.get("Retry-After")
    msg = ""
    try:
        j = r.json()
        msg = (j.get("message") or "").lower()
    except Exception:
        pass
    if remaining == "0" and reset:
        try:
            reset_ts = int(reset)
            wait = max(0, reset_ts - int(time.time()) + 2)
            return wait, "primary-rate-limit"
        except ValueError:
            pass
    if retry_after:
        try:
            wait = int(retry_after) + 1
            return wait, "retry-after"
        except ValueError:
            pass
    if "abuse" in msg or "abuse detection" in msg:
        # Exponential backoff with jitter
        wait = min(60, (2 ** attempt)) + random.uniform(0.5, 1.5)
        return wait, "abuse-detection"
    # Generic fallback
    return 3 + random.uniform(0.2, 0.8), "generic-403"
# --- end added helpers ---

def repo_keyword_search(repo: str, kw: str, tok: str | None, limiter: RateLimiter | None,
                        extra_filters: str = "", include_payload: bool = False):
    """Execute a repo keyword search, returning hits (and optionally the raw JSON payload)."""
    max_attempts = 6
    for attempt in range(1, max_attempts + 1):
        if limiter:
            limiter.acquire()
        tok_used, idx = get_token_idx()
        q = build_repo_keyword_query(repo, kw, extra_filters)
        params = {"q": q, "per_page": 100, "page": 1}
        try:
            r = requests.get(_search_endpoint(),
                             params=params,
                             headers=headers(tok_used),
                             timeout=30, verify=VERIFY)
        except requests.RequestException as e:
            print(f"[kw-check] (token#{idx}) {repo} '{kw}' network error attempt={attempt}/{max_attempts}: {e}",
                  file=sys.stderr)
            time.sleep(min(2 * attempt, 20))
            continue

        # Stats
        while len(token_stats["requests"]) < len(TOKEN_LIST):
            token_stats["requests"].append(0)
            token_stats["403"].append(0)
        token_stats["requests"][idx] += 1

        if r.status_code == 200:
            data = r.json()
            hits = int(data.get("total_count", 0) or 0)
            # fixed syntax error in f-string
            print(f"[kw-check] (token#{idx}) {repo} '{kw}' -> {hits} hits", file=sys.stderr)
            log_token_stats_if_needed()
            if include_payload:
                return hits, data
            return hits

        if r.status_code in (429, 500, 502, 503, 504):
            print(f"[kw-check] (token#{idx}) {repo} '{kw}' transient {r.status_code} attempt={attempt}/{max_attempts}",
                  file=sys.stderr)
            time.sleep(min(2 ** attempt, 30) + random.uniform(0.2, 0.8))
            continue

        if r.status_code == 403:
            token_stats["403"][idx] += 1
            sleep_s, reason = adaptive_sleep_for_403(r, idx, attempt)
            print(f"[kw-check] (token#{idx}) {repo} '{kw}' HTTP 403 reason={reason} sleep={sleep_s:.1f}s "
                  f"attempt={attempt}/{max_attempts}", file=sys.stderr)

            # If too many 403 overall, widen limiter window once (slow down globally)
            total_403 = sum(token_stats["403"])
            total_req = sum(token_stats["requests"])
            if total_req > 50 and total_403 / total_req > 0.3 and not token_stats["window_extended"] and limiter:
                limiter.window *= 1.3
                token_stats["window_extended"] = True
                print(f"[adaptive] Increased limiter window to {limiter.window:.1f}s due to 403 ratio",
                      file=sys.stderr)

            if attempt == max_attempts:
                print(f"[kw-check] (token#{idx}) {repo} '{kw}' giving up after {max_attempts} attempts", file=sys.stderr)
                return (-1, None) if include_payload else -1
            time.sleep(sleep_s)
            continue

        print(f"[kw-check] (token#{idx}) {repo} '{kw}' HTTP {r.status_code} abort", file=sys.stderr)
        return (-1, None) if include_payload else -1

    return (-1, None) if include_payload else -1  # fallback

def repo_keyword_hits(repo: str, kw: str, tok: str | None, limiter: RateLimiter | None,
                      extra_filters: str = "") -> int:
    """Backwards-compatible wrapper returning only hit counts."""
    return repo_keyword_search(repo, kw, tok, limiter, extra_filters, include_payload=False)

def detect_keywords_in_repo(repo: str, keywords: list[str], tok: str | None,
                            limiter: RateLimiter, max_keywords: int | None = None) -> list[str]:
    matched = []
    for kw in keywords:
        if repo_keyword_hits(repo, kw, tok, limiter) > 0:
            matched.append(kw)
            if max_keywords and len(matched) >= max_keywords:
                break
    return matched

def write_repo_keywords_csv(rows: list[tuple[str, list[str]]], out_path: str):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "keywords"])
        for repo, kws in rows:
            w.writerow([repo, " | ".join(kws)])

# --- added: per-repo keyword state persistence helpers ---
def load_repo_keyword_state(fn: Path) -> dict[str, int]:
    """Load previously scanned keywords for a repo.
    Supports lines:
      kw: <keyword> hits:<count>
    Legacy:
      keywords: k1 | k2 -> treated as hits=1
    """
    state: dict[str, int] = {}
    if not fn.exists():
        return state
    try:
        for line in fn.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("kw:"):
                m = re.match(r'^kw:\s*(.*?)\s+hits:(-?\d+)$', line)
                if m:
                    k = m.group(1).strip()
                    c = int(m.group(2))
                    state[k] = c
            elif line.startswith("keywords:"):
                raw = line.split("keywords:", 1)[1].strip()
                if raw and raw != "(none)":
                    for k in raw.split("|"):
                        kk = k.strip()
                        if kk:
                            state[kk] = 1
    except Exception as e:
        print(f"[kw-load] failed parsing {fn.name}: {e}", file=sys.stderr)
    return state

def write_repo_keyword_state(repo: str, state: dict[str, int], fn: Path):
    """Rewrite the repo state file atomically."""
    tmp = fn.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"repo: {repo}\n")
            if not state:
                f.write("keywords: (none)\n")
            else:
                for k in sorted(state.keys()):
                    f.write(f"kw: {k} hits:{state[k]}\n")
        tmp.replace(fn)
    except Exception as e:
        print(f"[kw-write] {repo} error writing file: {e}", file=sys.stderr)
# --- end added helpers ---

def write_keyword_payload(repo_dir: Path, keyword: str, payload: dict | list | None):
    """Persist the full search payload for a single repo/keyword combination."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    target = repo_dir / safe_keyword_filename(keyword)
    try:
        with open(target, "w", encoding="utf-8") as f:
            json.dump(payload if payload is not None else {"error": "no_payload"}, f, indent=2)
    except Exception as e:
        print(f"[kw-write-json] {repo_dir.name}:{keyword} error writing file: {e}", file=sys.stderr)

def keyword_csv_filename(keyword: str) -> str:
    base = safe_keyword_filename(keyword)
    if base.endswith(".json"):
        base = base[:-5]
    return base + ".csv"

def filter_payload_items(payload: dict | list | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items") or []
    filtered: list[dict] = []
    excluded_suffixes = (".xml", ".md", ".proto", ".json")
    for item in items:
        path = (item.get("path") or "")
        if "test" in path.lower():
            continue
        if path.lower().endswith(excluded_suffixes):
            continue
        filtered.append(item)
    return filtered

def write_keyword_items_csv(repo_dir: Path, keyword: str, items: list[dict]):
    repo_dir.mkdir(parents=True, exist_ok=True)
    target = repo_dir / keyword_csv_filename(keyword)
    try:
        with open(target, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["name", "path", "url", "git_url", "html_url"])
            for item in items:
                writer.writerow([
                    item.get("name", ""),
                    item.get("path", ""),
                    item.get("url", ""),
                    item.get("git_url", ""),
                    item.get("html_url", ""),
                ])
    except Exception as e:
        print(f"[kw-write-csv] {repo_dir.name}:{keyword} error writing file: {e}", file=sys.stderr)

def search_repo(repo, query, tok, limiter:RateLimiter|None=None, retries:int=3, backoff_base:float=1.6):
    q=f"repo:{repo} {query} in:file"
    params={"q":q,"per_page":100,"page":1}
    for attempt in range(retries+1):
        if limiter:
            limiter.acquire()
        try:
            r=requests.get(_search_endpoint(),
                           params=params,
                           headers=headers(get_token()),  # changed
                           timeout=30, verify=VERIFY)
        except requests.RequestException as e:
            if attempt<retries:
                time.sleep((backoff_base**attempt)+0.2); continue
            print(f"[repo-search] {repo} network error: {e}",file=sys.stderr); return []
        if r.status_code==200:
            data=r.json()
            return [Hit(repo,it["path"],it["html_url"]) for it in data.get("items",[])]
        if r.status_code in (429,500,502,503,504) or (r.status_code==403 and 'rate' in r.text.lower()):
            time.sleep((backoff_base**attempt)+0.2); continue
        print(f"[repo-search] {repo} -> {r.status_code}",file=sys.stderr); return []
    return []

def aggregate(hits:List[Hit]):
    cnt=defaultdict(int); smp=defaultdict(list)
    for h in hits:
        cnt[h.repo]+=1
        if len(smp[h.repo])<5: smp[h.repo].append(h.url)
    rows=[(r,c,smp[r]) for r,c in cnt.items()]
    rows.sort(key=lambda x:(-x[1],x[0].lower()))
    return rows

def write_csv(rows,outf):
    with open(outf,"w",newline="",encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["repo","matches","sample_urls"])
        for r,c,urls in rows: w.writerow([r,c," | ".join(urls)])

def normalize_api_base(s: str) -> str:
    # Accept either REST base or web origin.
    # - For GitHub.com: always return https://api.github.com (no /api/v3 suffix).
    # - For GHES: ensure we end with /api/v3.
    if not s:
        return "https://api.github.com"
    # Allow passing bare host without scheme
    if "://" not in s:
        s = "https://" + s
    parsed = urllib.parse.urlparse(s)
    host = (parsed.netloc or "").lower()
    # Cloud github.com (and subdomains) should NOT use /api/v3 suffix
    if host.endswith("github.com"):
        return "https://api.github.com"
    # GHES: normalize and add /api/v3 if not already present
    base = (parsed.scheme + "://" + host + parsed.path).rstrip("/")
    if "/api/" in parsed.path:
        return base
    return base + "/api/v3"

def fallback_after_search_org_fail(a, exclude_filter, tok):
    print("[fallback] org search empty; scanning repos individually…", file=sys.stderr)
    repos = list_repos(a.org, tok)
    total_before = len(repos)
    repos = exclude_filter.filter(repos)
    print(f"[exclude] repos: {total_before} -> {len(repos)} after applying exclude list ({a.exclude_file})",
          file=sys.stderr)
    if a.max_repos and len(repos) > a.max_repos:
        repos = repos[:a.max_repos]
        print(f"[limit] truncated to first {len(repos)} repos due to --max-repos", file=sys.stderr)
    # replaced limiter creation (remove unconditional multiplication; use effective_rate)
    effective_rate = a.rate if a.rate_mode == "total" else a.rate * max(1, len(TOKEN_LIST))
    limiter = RateLimiter(limit=max(1, effective_rate), window=60)  # changed
    results = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(search_repo, r, a.query, tok, limiter): r for r in repos}
        for i, f in enumerate(as_completed(futs), 1):
            repo = futs[f]
            try:
                res = f.result()
                results.extend(res)
            except Exception as e:
                print(f"[repo-search] {repo} raised {e}", file=sys.stderr)
            if i % 25 == 0 or i == len(futs):
                print(f"[progress] {i}/{len(futs)} repos scanned", file=sys.stderr)
    hits = results
    return hits, repos

def scan_keywords(a, tok, repo_set):
    if not repo_set:
        print("[keywords] no repos to scan", file=sys.stderr)
        return
    keywords = load_keywords(Path(a.keywords_file))
    if not keywords:
        return

    per_repo_dir = Path(a.per_repo_dir)
    per_repo_dir.mkdir(parents=True, exist_ok=True)

    # changed: use effective_rate based on mode
    effective_rate = a.rate if a.rate_mode == "total" else a.rate * max(1, len(TOKEN_LIST))
    limiter_kw = RateLimiter(limit=max(1, effective_rate), window=60)  # changed

    to_scan = sorted(repo_set)
    print(f"[keywords] scanning {len(to_scan)} repos (resume supported)", file=sys.stderr)

    def process_repo(repo: str):
        fn = per_repo_dir / safe_repo_filename(repo)
        state = load_repo_keyword_state(fn)  # existing scanned keywords
        already = set(state.keys())
        pending = [k for k in keywords if k not in already]
        if not pending:
            # Nothing new; still ensure file exists in new format
            write_repo_keyword_state(repo, state, fn)
            return state
        # Ensure file is created even if initial state empty
        write_repo_keyword_state(repo, state, fn)
        for kw in pending:
            hits = repo_keyword_hits(repo, kw, tok, limiter_kw)
            if hits >= 0:
                state[kw] = hits
                write_repo_keyword_state(repo, state, fn)  # incremental update
            if a.keywords_limit > 0:
                matched_positive = sum(1 for c in state.values() if c > 0)
                if matched_positive >= a.keywords_limit:
                    break
        return state

    if to_scan:
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            futs = {ex.submit(process_repo, repo): repo for repo in to_scan}
            for i, f in enumerate(as_completed(futs), 1):
                repo = futs[f]
                try:
                    _ = f.result()
                except Exception as e:
                    print(f"[kw-scan] {repo} error: {e}", file=sys.stderr)
                if i % 25 == 0 or i == len(futs):
                    print(f"[kw-progress] {i}/{len(futs)} repos processed", file=sys.stderr)

    # Aggregate all per-repo files (include only keywords with hits>0)
    repo_keyword_rows: list[tuple[str, list[tuple[str, int]]]] = []
    for f in per_repo_dir.glob("*.txt"):
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
            if not lines:
                continue
            repo_line = lines[0]
            if not repo_line.startswith("repo:"):
                continue
            repo = repo_line.split("repo:", 1)[1].strip()
            # Parse kw lines
            pairs: list[tuple[str, int]] = []
            for line in lines[1:]:
                line = line.strip()
                if line.startswith("kw:"):
                    m = re.match(r'^kw:\s*(.*?)\s+hits:(-?\d+)$', line)
                    if m:
                        k = m.group(1).strip()
                        c = int(m.group(2))
                        if c > 0:
                            pairs.append((k, c))
                elif line.startswith("keywords:"):
                    raw = line.split("keywords:", 1)[1].strip()
                    if raw and raw != "(none)":
                        for k in raw.split("|"):
                            kk = k.strip()
                            if kk:
                                pairs.append((kk, 1))
            if pairs:
                repo_keyword_rows.append((repo, pairs))
        except Exception as e:
            print(f"[kw-aggregate] failed reading {f.name}: {e}", file=sys.stderr)

    # Write CSV: one row per (repo, keyword)
    with open(a.out_keywords, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "keyword", "hits", "search_url"])
        for repo, pairs in repo_keyword_rows:
            for k, c in pairs:
                search_url = build_single_keyword_search_url(repo, k)
                w.writerow([repo, k, c, search_url])
    print(f"[keywords] {sum(len(pairs) for _, pairs in repo_keyword_rows)} repo-keyword rows written to {a.out_keywords}")

def scan_keywords_without_test(a, tok):
    """Re-scan previously matched keywords while excluding paths that contain 'test'."""
    repo_list = load_repo_list(Path(a.repos_file))
    if not repo_list:
        print("[keywords-no-test] no repos specified for no-test scan", file=sys.stderr)
        return

    keywords = load_keywords(Path(a.keywords_file))
    if not keywords:
        print("[keywords-no-test] no keywords available for no-test scan", file=sys.stderr)
        return

    # Deduplicate while preserving order from files
    deduped_repos = list(dict.fromkeys(repo_list))
    deduped_keywords = list(dict.fromkeys(keywords))

    effective_rate = a.rate if a.rate_mode == "total" else a.rate * max(1, len(TOKEN_LIST))
    limiter_kw = RateLimiter(limit=max(1, effective_rate), window=60)

    per_repo_dir = Path(a.per_repo_no_test_dir)
    repo_keyword_map: dict[str, list[tuple[str, int, str, str, str]]] = defaultdict(list)

    for repo in deduped_repos:
        keywords_for_repo = deduped_keywords
        repo_dir = per_repo_dir / safe_repo_slug(repo)
        for keyword in keywords_for_repo:
            extra_filter = "-path:test"
            keyword_payload_path = repo_dir / safe_keyword_filename(keyword)
            keyword_csv_path = repo_dir / keyword_csv_filename(keyword)

            payload: dict | list | None = None

            if keyword_csv_path.exists() and keyword_payload_path.exists():
                try:
                    payload = json.loads(keyword_payload_path.read_text(encoding="utf-8"))
                    filtered_items = []
                    with open(keyword_csv_path, newline="", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            filtered_items.append({
                                "name": row.get("name", ""),
                                "path": row.get("path", ""),
                                "url": row.get("url", ""),
                                "git_url": row.get("git_url", ""),
                                "html_url": row.get("html_url", ""),
                            })
                except Exception as e:
                    print(f"[keywords-no-test] failed to reuse cached CSV for {repo}:{keyword}: {e}",
                          file=sys.stderr)
                    payload = None
                    filtered_items = []

                if payload is not None:
                    hits = len(filtered_items)
                    query = build_repo_keyword_query(repo, keyword, extra_filter)
                    url = build_code_search_url(query)
                    api_url = build_api_search_url(query)
                    if hits > 0:
                        repo_keyword_map[repo].append((keyword, hits, query, url, api_url))
                    continue

            filtered_items = []
            if keyword_payload_path.exists():
                try:
                    payload = json.loads(keyword_payload_path.read_text(encoding="utf-8"))
                except Exception as e:
                    print(f"[keywords-no-test] failed to read cached payload for {repo}:{keyword}: {e}",
                          file=sys.stderr)
                    payload = None

            if payload is None:
                _, payload = repo_keyword_search(
                    repo, keyword, tok, limiter_kw, extra_filter, include_payload=True
                )
                if payload is not None:
                    write_keyword_payload(repo_dir, keyword, payload)

            filtered_items = filter_payload_items(payload)
            if payload is not None:
                write_keyword_items_csv(repo_dir, keyword, filtered_items)
                hits = len(filtered_items)
            else:
                hits = -1

            query = build_repo_keyword_query(repo, keyword, extra_filter)
            url = build_code_search_url(query)
            api_url = build_api_search_url(query)
            if hits > 0:
                repo_keyword_map[repo].append((keyword, hits, query, url, api_url))

    out_csv = Path(a.out_keywords_no_test)
    try:
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["repo", "keywords", "total_hits", "queries", "search_urls", "api_urls"])
            for repo, entries in sorted(repo_keyword_map.items()):
                keywords_str = " | ".join(e[0] for e in entries)
                total_hits = sum(e[1] for e in entries)
                queries_str = " | ".join(e[2] for e in entries)
                search_urls_str = " | ".join(e[3] for e in entries)
                api_urls_str = " | ".join(e[4] for e in entries)
                writer.writerow([repo, keywords_str, total_hits, queries_str, search_urls_str, api_urls_str])
        rows_written = sum(len(entries) > 0 for entries in repo_keyword_map.values())
        print(f"[keywords-no-test] {rows_written} repo rows written to {out_csv}")
    except Exception as e:
        print(f"[keywords-no-test] failed writing {out_csv}: {e}", file=sys.stderr)

def build_single_keyword_search_url(repo: str, keyword: str) -> str:
    """GitHub code search URL for a single keyword within a repo."""
    q = build_repo_keyword_query(repo, keyword)
    return build_code_search_url(q)

def safe_repo_slug(repo: str) -> str:
    """Convert full repo name org/name -> safe slug usable for filenames/dirs."""
    base = repo.replace("/", "__")
    if len(base) > 120:
        h = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:16]
        base = base[:80] + "__" + h
    return base

def safe_repo_filename(repo: str) -> str:
    """Safe filename (txt) for repo keyword state persistence."""
    return safe_repo_slug(repo) + ".txt"

def safe_keyword_filename(keyword: str) -> str:
    """Return a safe filename for keyword-specific JSON payloads."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", keyword).strip("_")
    if not slug:
        slug = "keyword"
    if len(slug) > 80:
        digest = hashlib.sha256(keyword.encode("utf-8")).hexdigest()[:16]
        slug = slug[:60] + "__" + digest
    return slug + ".json"
    return base + ".txt"

def parse_args():
    p=argparse.ArgumentParser()
    p.add_argument("--org",default="skyscanner")
    p.add_argument("--query",default='"deeplink_url"')
    p.add_argument("--token",default=os.getenv("GITHUB_TOKEN"))
    p.add_argument("--token2", default=os.getenv("GITHUB_TOKEN2"),
                   help="Optional second GitHub token to increase parallel search throughput")
    p.add_argument("--out",default="results/repos_with_keyword.csv")
    p.add_argument("--max-pages",type=int,default=1000)
    p.add_argument("--max-repos", type=int, default=500, help="Scan at most this many repos in fallback mode (0=all)")
    p.add_argument("--workers", type=int, default=8, help="Concurrent workers for fallback per-repo scans")
    p.add_argument("--rate", type=int, default=9, help="Max code_search requests per minute (GitHub default ~10)")
    p.add_argument("--sleep", type=float, default=0.0, help="Unused when workers>1 (kept for backward compat)")
    p.add_argument("--base-url", default=os.getenv("GITHUB_BASE_URL", "https://api.github.com"),
                   help="REST API base or web origin. Examples: https://api.github.com OR https://github.skyscannertools.net")
    p.add_argument("--insecure", action="store_true", help="Disable TLS verification (NOT recommended)")
    p.add_argument("--ca-bundle", default=os.getenv("GIT_SSL_CAINFO", ""),
                   help="Path to custom CA bundle for corporate proxies")
    p.add_argument("--exclude-file", default="res/exclude-repo.txt",
                   help="Path to repo exclusion list (one pattern per line)")

    # keywords param
    p.add_argument("--keywords-file", default="res/keywords.txt",
                   help="Path to keyword list (one per line)")
    p.add_argument("--out-keywords", default="results/repo_keywords.csv",
                   help="Output CSV listing repo->matched keywords")
    p.add_argument("--keywords-limit", type=int, default=0,
                   help="If >0, stop after this many matches per repo to save requests")
    p.add_argument("--per-repo-dir", default="results/repo",
                   help="Directory to store per-repo keyword scan results for resume")
    p.add_argument("--per-repo-no-test-dir", default="results/repo-without-test",
                   help="Directory for per-repo keyword results excluding test paths")
    p.add_argument("--out-keywords-no-test", default="results/repo_keywords_no_test.csv",
                   help="Output CSV for keywords re-scanned with '-path:test' filter applied")
    p.add_argument("--repos-file", default="res/repos.txt",
                   help="List of repositories (one per line) to scan during the no-test pass")
    p.add_argument("--rate-mode", choices=["total","per-token"], default="total",
                   help="Interpret --rate as total allowed per minute (total) or per token (per-token)")

    print(f"base-url: {p.parse_args().base_url}", file=sys.stderr)
    return p.parse_args()

def main():
    global API_BASE, VERIFY, TOKEN_LIST
    args = parse_args()

    # TLS settings
    VERIFY = True
    if args.insecure:
        VERIFY = False
    elif args.ca_bundle:
        VERIFY = args.ca_bundle

    # API base normalization (GHES)
    API_BASE = normalize_api_base(args.base_url)

    tok = args.token
    tok2 = args.token2
    TOKEN_LIST = [t for t in [tok, tok2] if t]
    if not TOKEN_LIST:
        print("Missing GITHUB_TOKEN (and optional --token2)", file=sys.stderr)
    elif len(TOKEN_LIST) > 1:
        print(f"[tokens] using {len(TOKEN_LIST)} tokens (round-robin)", file=sys.stderr)

    if len(TOKEN_LIST) > 1 and hasattr(sys, "stderr"):
        mode = "global-total" if getattr(args, "rate_mode", "total") == "total" else "per-token"
        print(f"[tokens] mode={mode} configured-rate={args.rate} effective-rate-per-minute="
              f"{(args.rate if mode=='global-total' else args.rate*len(TOKEN_LIST))}", file=sys.stderr)

    # Load exclude filter once
    exclude_filter = ExcludeRepoFilter(Path(args.exclude_file))

    preload_file = Path(args.out)
    repo_set = preload_repo_set(preload_file)
    preloaded = bool(repo_set)

    if preloaded:
        before = len(repo_set)
        repo_set = {r for r in repo_set if not exclude_filter.is_excluded(r)}
        after = len(repo_set)
        if after != before:
            print(f"[exclude] preload repos: {before} -> {after} after applying exclude list ({args.exclude_file})",
                  file=sys.stderr)
        hits = []  # no org search hits
    else:
        # Org-level search (original path)
        hits = search_org(args.org, args.query, tok, args.max_pages)
        if hits:
            before = len(hits)
            hits = [h for h in hits if not exclude_filter.is_excluded(h.repo)]
            after = len(hits)
            if after != before:
                print(f"[exclude] org-search hits: {before} -> {after} after applying exclude list ({args.exclude_file})",
                      file=sys.stderr)
            repo_set = {h.repo for h in hits}
        else:
            hits, repos = fallback_after_search_org_fail(args, exclude_filter, tok)
            repo_set = set(repos)

    if not preloaded:
        rows = aggregate(hits)
        write_csv(rows, args.out)
        print(f"Found {len(rows)} repos; written to {args.out}")
    else:
        print(f"[preload] skipping writing {args.out} (no fresh search performed)", file=sys.stderr)

    # scan_keywords(args, tok, repo_set)
    scan_keywords_without_test(args, tok)

if __name__=="__main__":
    main()
