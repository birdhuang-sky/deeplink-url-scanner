# src/org_scanner/github_search_deeplink_url.py
#!/usr/bin/env python3
import argparse, csv, os, sys, time, requests, threading, urllib.parse
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List
from pathlib import Path
from org_scanner.repo_filter import ExcludeRepoFilter


API_BASE = "https://api.github.com"   # may be changed at runtime via --base-url
VERIFY: bool | str = True              # requests verify flag (bool or CA path)
EXCLUDE_FILE_TYPE_LIST = ['PROTO']

@dataclass
class Hit:
    repo: str
    path: str
    url: str

class RateLimiter:
    """Sliding-window limiter: allow at most `limit` requests per `window` seconds.
    Thread-safe and suitable for moderate concurrency (do not exceed ~32 workers).
    """
    def __init__(self, limit:int=10, window:int=60):
        self.limit = max(1, limit)
        self.window = window
        self.lock = threading.Lock()
        self.ts = deque()
    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                while self.ts and now - self.ts[0] >= self.window:
                    self.ts.popleft()
                if len(self.ts) < self.limit:
                    self.ts.append(now)
                    return
                sleep_for = self.window - (now - self.ts[0]) + 0.01
            time.sleep(max(0.05, sleep_for))

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
        r=session.get(_search_endpoint(), params={"q":q,"per_page":100,"page":page},
                      headers=headers(tok), timeout=30, verify=VERIFY)
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
        r=requests.get(_org_repos_endpoint(org), headers=headers(tok),
                       params={"per_page":100,"page":page,"type":"all"}, timeout=30, verify=VERIFY)
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

def preload_repo_set(path: Path) -> set[str]:
    """Load pre-specified repos (one per line) to skip initial org search."""
    if not path.exists():
        print(f"[preload] path {path} not exist.", file=sys.stderr)
        return set()
    # FIX: avoid walrus inside comprehension (SyntaxError in this context)
    repos = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        repos.add(line)
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

def repo_has_keyword(repo: str, kw: str, tok: str | None, limiter: RateLimiter | None) -> bool:
    q = f"repo:{repo} {keyword_query_fragment(kw)} in:file"
    params = {"q": q, "per_page": 1, "page": 1}
    if limiter:
        limiter.acquire()
    try:
        r = requests.get(_search_endpoint(), params=params, headers=headers(tok), timeout=30, verify=VERIFY)
    except requests.RequestException as e:
        print(f"[kw-check] {repo} '{kw}' network error: {e}", file=sys.stderr)
        return False
    if r.status_code == 200:
        data = r.json()
        print(f"[kw-check] {repo} '{kw}' -> {data.get('total_count', 0)} hits", file=sys.stderr)
        return bool(data.get("items"))
    if r.status_code in (429, 500, 502, 503, 504):
        # Simple single retry after short sleep
        time.sleep(1.2)
        return repo_has_keyword(repo, kw, tok, limiter)
    return False

def detect_keywords_in_repo(repo: str, keywords: list[str], tok: str | None,
                            limiter: RateLimiter, max_keywords: int | None = None) -> list[str]:
    matched = []
    for kw in keywords:
        if repo_has_keyword(repo, kw, tok, limiter):
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

def search_repo(repo, query, tok, limiter:RateLimiter|None=None, retries:int=3, backoff_base:float=1.6):
    q=f"repo:{repo} {query} in:file"
    params={"q":q,"per_page":5,"page":1}
    for attempt in range(retries+1):
        if limiter:
            limiter.acquire()
        try:
            r=requests.get(_search_endpoint(), params=params, headers=headers(tok), timeout=30, verify=VERIFY)
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
    limiter = RateLimiter(limit=max(1, a.rate), window=60)
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
    limiter_kw = RateLimiter(limit=max(1, a.rate), window=60)
    repo_keyword_rows: list[tuple[str, list[str]]] = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {
            ex.submit(
                detect_keywords_in_repo,
                repo,
                keywords,
                tok,
                limiter_kw,
                (a.keywords_limit if a.keywords_limit > 0 else None),
            ): repo
            for repo in sorted(repo_set)
        }
        for i, f in enumerate(as_completed(futs), 1):
            repo = futs[f]
            try:
                matched = f.result()
                if matched:
                    repo_keyword_rows.append((repo, matched))
            except Exception as e:
                print(f"[kw-scan] {repo} error: {e}", file=sys.stderr)
            if i % 25 == 0 or i == len(futs):
                print(f"[kw-progress] {i}/{len(futs)} repos processed", file=sys.stderr)
    write_repo_keywords_csv(repo_keyword_rows, a.out_keywords)
    print(f"[keywords] {len(repo_keyword_rows)} repos with matches; written to {a.out_keywords}")

def parse_args():
    p=argparse.ArgumentParser()
    p.add_argument("--org",default="skyscanner")
    p.add_argument("--query",default='"deeplink_url"')
    p.add_argument("--token",default=os.getenv("GITHUB_TOKEN"))
    p.add_argument("--out",default="results/repos_with_keyword.csv")
    p.add_argument("--max-pages",type=int,default=1000)
    p.add_argument("--max-repos", type=int, default=500, help="Scan at most this many repos in fallback mode (0=all)")
    p.add_argument("--workers", type=int, default=4, help="Concurrent workers for fallback per-repo scans")
    p.add_argument("--rate", type=int, default=4, help="Max code_search requests per minute (GitHub default ~10)")
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

    print(f"base-url: {p.parse_args().base_url}", file=sys.stderr)
    return p.parse_args()

def main():
    global API_BASE, VERIFY
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
    if not tok:
        print("Missing GITHUB_TOKEN", file=sys.stderr)

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

    scan_keywords(args, tok, repo_set)

if __name__=="__main__":
    main()