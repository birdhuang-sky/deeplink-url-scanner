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
    if( is_exclude ):
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

    print(f"base-url: {p.parse_args().base_url}", file=sys.stderr)
    return p.parse_args()

def main():
    global API_BASE, VERIFY
    a = parse_args()

    # TLS settings
    VERIFY = True
    if a.insecure:
        VERIFY = False
    elif a.ca_bundle:
        VERIFY = a.ca_bundle

    # API base normalization (GHES)
    API_BASE = normalize_api_base(a.base_url)

    tok = a.token
    if not tok:
        print("Missing GITHUB_TOKEN", file=sys.stderr)

    # Load exclude filter once
    exclude_filter = ExcludeRepoFilter(Path(a.exclude_file))

    # Org-level search
    hits = search_org(a.org, a.query, tok, a.max_pages)

    if hits:
        before = len(hits)
        hits = [h for h in hits if not exclude_filter.is_excluded(h.repo)]
        after = len(hits)
        if after != before:
            print(f"[exclude] org-search hits: {before} -> {after} after applying exclude list ({a.exclude_file})",
                  file=sys.stderr)
    else:
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
                    res = f.result(); results.extend(res)
                except Exception as e:
                    print(f"[repo-search] {repo} raised {e}", file=sys.stderr)
                if i % 25 == 0 or i == len(futs):
                    print(f"[progress] {i}/{len(futs)} repos scanned", file=sys.stderr)
        hits = results

    rows = aggregate(hits)
    write_csv(rows, a.out)
    print(f"Found {len(rows)} repos; written to {a.out}")
    # print("| Repo | Matches | Samples ||---|---:|---|")
    # for r, c, u in rows[:40]:
    #     print(f"| {r} | {c} | {'<br/>'.join(u)} |")

if __name__=="__main__": main()
