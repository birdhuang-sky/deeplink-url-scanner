import argparse
import asyncio
import os
from typing import List, Set

from .github_client import GitHubClient
from .patterns import merge_keywords
from .repo_scanner import RepoResult
from .report import ensure_dir, write_markdown, write_csv, write_json

BUILTIN_KEYWORDS = [
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "associate_id", "tracking_id", "affiliate_id", "partner_id",
    "deep_link_id", "referrer", "ref", "campaign_id"
]

async def scan_api(org: str, keywords: Set[str], max_repos: int, include_archived: bool, concurrency: int, timeout: int) -> List[RepoResult]:
    results: List[RepoResult] = []
    sem = asyncio.Semaphore(concurrency)
    async with GitHubClient(timeout=timeout) as gh:
        repos = await gh.list_org_repos(org, include_archived=include_archived, limit=max_repos if max_repos else None)
        repo_map = {r["name"]: RepoResult(repo=r["name"]) for r in repos}

        async def search_param(param: str):
            async with sem:
                q = f'\"{param}\" org:{org}'
                hits = await gh.code_search(q, per_page=50, max_items=400)
                for h in hits:
                    repo_name = h["repository"]["name"]
                    if repo_name in repo_map:
                        repo_map[repo_name].parameters.add(param)

        tasks = [asyncio.create_task(search_param(p)) for p in keywords]
        await asyncio.gather(*tasks)

        for r in repo_map.values():
            if r.parameters:
                results.append(r)
    return results

def load_extra_keywords(path: str) -> List[str]:
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default="Skyscanner")
    ap.add_argument("--mode", choices=["api", "clone"], default="api")
    ap.add_argument("--keywords-file", default=None)
    ap.add_argument("--output-dir", default="reports")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--max-repos", type=int, default=0)
    ap.add_argument("--include-archived", action="store_true")
    ap.add_argument("--timeout", type=int, default=15)
    ap.add_argument("--clone-base", default=".cache/repos")
    return ap.parse_args()

def main():
    args = parse_args()
    extra = load_extra_keywords(args.keywords_file)
    keywords = merge_keywords(BUILTIN_KEYWORDS, extra)
    if args.mode == "api":
        results = asyncio.run(
            scan_api(
                org=args.org,
                keywords=keywords,
                max_repos=args.max_repos,
                include_archived=args.include_archived,
                concurrency=args.concurrency,
                timeout=args.timeout,
            )
        )
    else:
        print("Clone 模式尚未在 CLI 中完整实现；未来可补充。")
        results = []
    ensure_dir(args.output_dir)
    write_markdown(results, os.path.join(args.output_dir, "parameters_report.md"))
    write_csv(results, os.path.join(args.output_dir, "parameters_report.csv"))
    write_json(results, os.path.join(args.output_dir, "raw_result.json"))
    print(f"Done. Repos with matches: {len(results)}")

if __name__ == "__main__":
    main()