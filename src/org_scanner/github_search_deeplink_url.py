#!/usr/bin/env python3
"""
Search GitHub org repos for occurrences of a keyword (default: "deeplink_url")
using GraphQL Code Search (v4), which generally aligns with the web UI results.

Fallback to REST v3 is kept (optional), but GraphQL is the default path.

Usage:
  python github_search_deeplink_url.py \
      --org skyscanner \
      --query '"deeplink_url"' \
      --out repos_with_deeplink_url.csv

Notes:
- Requires a GitHub token. For private repos, use classic PAT with `repo` scope or
  fine-grained PAT with Repository contents: Read, and SSO-authorize it for the org.
- GraphQL has higher reliability vs REST search for some enterprise/org setups.
"""
from __future__ import annotations
import argparse
import csv
import os
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests

REST_API = "https://api.github.com"
GRAPHQL_API = f"{REST_API}/graphql"
SEARCH_ENDPOINT = f"{REST_API}/search/code"
DEFAULT_PER_PAGE = 100

@dataclass
class CodeHit:
    repo_full_name: str
    file_path: str
    html_url: str


def _headers(token: str | None) -> Dict[str, str]:
    h = {
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "skyscanner-deeplink-url-search/2.0",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _gql_headers(token: str | None) -> Dict[str, str]:
    h = {
        "Accept": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "skyscanner-deeplink-url-search/2.0",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def backoff_sleep(retry: int, reset_epoch: Optional[int] = None):
    if reset_epoch:
        now = int(time.time())
        sleep_for = max(1, reset_epoch - now) + 1
    else:
        base = min(60, 2 ** retry)
        sleep_for = base + int(1000 * (os.urandom(1)[0] / 255.0)) % 3
    time.sleep(sleep_for)


def debug_auth(token: Optional[str]):
    try:
        r = requests.get(f"{REST_API}/rate_limit", headers=_headers(token), timeout=20)
        scopes = r.headers.get("X-OAuth-Scopes", "")
        print(f"[auth] OAuth scopes: {scopes or '(none)'}", file=sys.stderr)
    except Exception:
        pass


GQL_SEARCH = """
query($query: String!, $first: Int!, $after: String) {
  search(query: $query, type: CODE, first: $first, after: $after) {
    codeCount
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        ... on Code {
          repository { nameWithOwner }
          path
          url
        }
      }
    }
  }
}
"""


def search_graphql(org: str, query: str, language: Optional[str], token: Optional[str], page_size: int = 100) -> List[CodeHit]:
    hits: List[CodeHit] = []
    cursor = None
    retries = 0

    # Compose query string similar to web UI
    terms = [f"org:{org}", query, "in:file", "fork:true"]
    if language:
        terms.append(f"language:{language}")
    q = " ".join(terms)

    session = requests.Session()

    while True:
        body = {"query": GQL_SEARCH, "variables": {"query": q, "first": page_size, "after": cursor}}
        try:
            resp = session.post(GRAPHQL_API, json=body, headers=_gql_headers(token), timeout=45)
        except requests.RequestException as e:
            if retries < 5:
                retries += 1
                backoff_sleep(retries)
                continue
            raise SystemExit(f"Network error after {retries} retries (GraphQL): {e}")

        if resp.status_code == 403:
            # Rate limit or SSO
            reset = resp.headers.get("X-RateLimit-Reset")
            try:
                reset_epoch = int(reset) if reset else None
            except ValueError:
                reset_epoch = None
            if retries < 7:
                retries += 1
                backoff_sleep(retries, reset_epoch)
                continue
            raise SystemExit("GraphQL 403. Check SSO authorization and rate limits.")
        if resp.status_code >= 500:
            if retries < 5:
                retries += 1
                backoff_sleep(retries)
                continue
            raise SystemExit(f"GitHub GraphQL server error {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise SystemExit(f"GitHub GraphQL error {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if "errors" in data:
            raise SystemExit(f"GraphQL errors: {data['errors']}")
        s = data["data"]["search"]
        if cursor is None:
            print(f"[debug] GraphQL codeCount for '{q}': {s['codeCount']}", file=sys.stderr)
        for edge in s.get("edges", []):
            node = edge.get("node") or {}
            repo = node.get("repository", {}).get("nameWithOwner")
            path = node.get("path")
            url = node.get("url")
            if repo and path and url:
                hits.append(CodeHit(repo, path, url))
        if not s["pageInfo"]["hasNextPage"]:
            break
        cursor = s["pageInfo"]["endCursor"]

    return hits


def search_rest(org: str, query: str, language: Optional[str], token: Optional[str], max_pages: int) -> List[CodeHit]:
    hits: List[CodeHit] = []
    page = 1
    retries = 0

    terms = [f"org:{org}", query, "in:file", "fork:true"]
    if language:
        terms.append(f"language:{language}")
    q = " ".join(terms)

    session = requests.Session()
    total_count_reported = None

    while True:
        params = {"q": q, "per_page": DEFAULT_PER_PAGE, "page": page}
        try:
            resp = session.get(SEARCH_ENDPOINT, params=params, headers=_headers(token), timeout=30)
        except requests.RequestException as e:
            if retries < 5:
                retries += 1
                backoff_sleep(retries)
                continue
            raise SystemExit(f"Network error after {retries} retries: {e}")

        sso = resp.headers.get("X-GitHub-SSO")
        if sso and "required" in sso.lower():
            print("[error] Token NOT SSO-authorized for org. Please Authorize SSO for Skyscanner.", file=sys.stderr)

        if resp.status_code == 422:
            raise SystemExit(f"REST search rejected query '{q}'.")
        if resp.status_code == 403:
            reset = resp.headers.get("X-RateLimit-Reset")
            try:
                reset_epoch = int(reset) if reset else None
            except ValueError:
                reset_epoch = None
            if retries < 7:
                retries += 1
                backoff_sleep(retries, reset_epoch)
                continue
            raise SystemExit("REST search 403. Check limits/SSO.")
        if resp.status_code >= 500:
            if retries < 5:
                retries += 1
                backoff_sleep(retries)
                continue
            raise SystemExit(f"GitHub server error {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise SystemExit(f"GitHub REST error {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if total_count_reported is None:
            total_count_reported = data.get("total_count")
            print(f"[debug] REST total_count: {total_count_reported}", file=sys.stderr)
        items = data.get("items", [])
        for it in items:
            repo = it.get("repository", {})
            repo_full_name = repo.get("full_name") or repo.get("name")
            path = it.get("path")
            html_url = it.get("html_url")
            if repo_full_name and path and html_url:
                hits.append(CodeHit(repo_full_name, path, html_url))

        if len(items) < DEFAULT_PER_PAGE:
            break
        page += 1
        if max_pages and page > max_pages:
            break

    return hits


def aggregate_results(hits: Iterable[CodeHit]) -> List[Tuple[str, int, List[str]]]:
    counts: Dict[str, int] = defaultdict(int)
    samples: Dict[str, List[str]] = defaultdict(list)
    for h in hits:
        counts[h.repo_full_name] += 1
        if len(samples[h.repo_full_name]) < 5:
            samples[h.repo_full_name].append(h.html_url)
    rows = [(repo, counts[repo], samples[repo]) for repo in counts]
    rows.sort(key=lambda r: (-r[1], r[0].lower()))
    return rows


def write_csv(rows: List[Tuple[str, int, List[str]]], outfile: str):
    with open(outfile, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "matches", "sample_file_urls"])
        for repo, count, links in rows:
            w.writerow([repo, count, " | ".join(links)])


def print_markdown_table(rows: List[Tuple[str, int, List[str]]], limit: Optional[int] = None):
    print("| Repo | Matches | Sample files |")
    print("|---|---:|---|")
    for i, (repo, count, links) in enumerate(rows):
        if limit is not None and i >= limit:
            break
        link_md = "<br/>".join(f"<a href='{u}' target='_blank'>file</a>" for u in links)
        print(f"| {repo} | {count} | {link_md} |")


def list_org_repos(org: str, token: Optional[str]) -> List[Dict[str, str]]:
    session = requests.Session()
    page = 1
    repos: List[Dict[str, str]] = []
    while True:
        params = {"per_page": 100, "page": page, "type": "all", "sort": "full_name"}
        resp = session.get(f"{REST_API}/orgs/{org}/repos", headers=_headers(token), params=params, timeout=30)
        if resp.status_code != 200:
            raise SystemExit(f"List repos failed: {resp.status_code} {resp.text[:200]}")
        chunk = resp.json()
        if not chunk:
            break
        for r in chunk:
            repos.append({
                "full_name": r.get("full_name"),
                "private": r.get("private"),
                "fork": r.get("fork"),
                "archived": r.get("archived"),
            })
        page += 1
    return repos


def search_repo_once(full_name: str, query: str, token: Optional[str]) -> Tuple[int, List[str]]:
    # Use REST but scoped to a single repository, which often works when org-wide search is flaky.
    terms = [f"repo:{full_name}", query, "in:file", "fork:true"]
    q = " ".join(terms)
    params = {"q": q, "per_page": 10, "page": 1}
    resp = requests.get(SEARCH_ENDPOINT, params=params, headers=_headers(token), timeout=30)
    if resp.status_code != 200:
        # On failures, treat as zero but print a hint
        print(f"[warn] repo search {full_name} -> HTTP {resp.status_code}", file=sys.stderr)
        return 0, []
    data = resp.json()
    total = int(data.get("total_count", 0) or 0)
    links: List[str] = []
    for it in data.get("items", [])[:5]:
        u = it.get("html_url")
        if u:
            links.append(u)
    return total, links


def fallback_repo_scan(org: str, query: str, token: Optional[str], repo_pattern: Optional[str], max_repos: int, sleep_ms: int) -> List[CodeHit]:
    repos = list_org_repos(org, token)
    if repo_pattern:
        needle = repo_pattern.lower()
        repos = [r for r in repos if needle in (r.get("full_name") or "").lower()]
    if max_repos and len(repos) > max_repos:
        repos = repos[:max_repos]

    print(f"[fallback] scanning {len(repos)} repos via repo:<name> … (rate limited)", file=sys.stderr)

    hits: List[CodeHit] = []
    for i, r in enumerate(repos, 1):
        full = r.get("full_name")
        if not full:
            continue
        total, links = search_repo_once(full, query, token)
        if total > 0:
            # we do not fetch all items (rate limit); only keep sample links
            for u in links:
                hits.append(CodeHit(full, "…", u))
        if sleep_ms > 0 and i < len(repos):
            time.sleep(sleep_ms / 1000.0)
    return hits


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not args.token:
        print("WARNING: No token provided. You may quickly hit rate limits. Set GITHUB_TOKEN.", file=sys.stderr)

    debug_auth(args.token)

    languages: Optional[List[str]] = None
    if args.languages:
        languages = [s.strip() for s in args.languages.split(",") if s.strip()]

    # Try primary engine first
    all_hits: List[CodeHit] = []
    primary_succeeded = False
    if args.engine == "graphql":
        try:
            if languages:
                for lang in languages:
                    print(f"[gql] Searching shard language:{lang} …", file=sys.stderr)
                    all_hits.extend(search_graphql(args.org, args.query, lang, args.token))
            else:
                print("[gql] Searching without language sharding …", file=sys.stderr)
                all_hits = search_graphql(args.org, args.query, None, args.token)
            primary_succeeded = True
        except SystemExit as e:
            print(f"[gql] primary failed: {e}", file=sys.stderr)
    else:
        try:
            if languages:
                for lang in languages:
                    print(f"[rest] Searching shard language:{lang} …", file=sys.stderr)
                    all_hits.extend(search_rest(args.org, args.query, lang, args.token, args.max_pages))
            else:
                print("[rest] Searching without language sharding …", file=sys.stderr)
                all_hits = search_rest(args.org, args.query, None, args.token, args.max_pages)
            primary_succeeded = True
        except SystemExit as e:
            print(f"[rest] primary failed: {e}", file=sys.stderr)

    # If primary yielded zero or failed, optional fallback per-repo scan
    proceed_fallback = (not primary_succeeded) or (len(all_hits) == 0)
    if proceed_fallback and args.fallback == "repo-scan":
        print("[fallback] switching to per-repo scans …", file=sys.stderr)
        all_hits = fallback_repo_scan(args.org, args.query, args.token, args.repo_pattern, args.max_repos, args.sleep_ms)

    # Dedupe
    seen: Set[Tuple[str, str]] = set()
    deduped: List[CodeHit] = []
    for h in all_hits:
        key = (h.repo_full_name, h.file_path)
        if key not in seen:
            seen.add(key)
            deduped.append(h)

    rows = aggregate_results(deduped)
    write_csv(rows, args.out)

    print(f"Found {len(rows)} repositories with matches. CSV written to {args.out}", file=sys.stderr)
    print_markdown_table(rows, limit=args.md_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
