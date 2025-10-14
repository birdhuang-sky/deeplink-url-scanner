#!/usr/bin/env python3
"""
Search all repositories under an org (e.g. skyscanner) for code containing a keyword.
Falls back to per-repo REST searches when org-level search returns 0.

Usage:
  python github_search_deeplink_url.py --org skyscanner --query '"deeplink_url"'
"""
import argparse, csv, os, sys, time, requests
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Set

API = "https://api.github.com"
SEARCH = f"{API}/search/code"

@dataclass
class Hit:
    repo: str
    path: str
    url: str

def headers(tok:str|None):
    h={"Accept":"application/vnd.github.v3+json",
       "User-Agent":"deeplink-url-scanner/3.0"}
    if tok: h["Authorization"]=f"Bearer {tok}"
    return h

def search_org(org, query, tok, max_pages=10):
    hits=[]
    session=requests.Session()
    q=f"org:{org} {query} in:file fork:true"
    for page in range(1,max_pages+1):
        r=session.get(SEARCH,params={"q":q,"per_page":100,"page":page},
                      headers=headers(tok),timeout=30)
        if r.status_code!=200:
            print(f"[org-search] HTTP {r.status_code} {r.text[:150]}",file=sys.stderr)
            break
        data=r.json()
        if page==1:
            print(f"[org-search] total_count={data.get('total_count')}",file=sys.stderr)
        items=data.get("items",[])
        for it in items:
            repo=it["repository"]["full_name"]; path=it["path"]; url=it["html_url"]
            hits.append(Hit(repo,path,url))
        if len(items)<100: break
    return hits

def list_repos(org,tok):
    out=[]; page=1
    while True:
        r=requests.get(f"{API}/orgs/{org}/repos",
                       headers=headers(tok),
                       params={"per_page":100,"page":page,"type":"all"},timeout=30)
        if r.status_code!=200: break
        chunk=r.json()
        if not chunk: break
        out+=[c["full_name"] for c in chunk]
        page+=1
    return out

def search_repo(repo, query, tok):
    q=f"repo:{repo} {query} in:file fork:true"
    r=requests.get(SEARCH,params={"q":q,"per_page":5,"page":1},
                   headers=headers(tok),timeout=30)
    if r.status_code!=200:
        print(f"[repo-search] {repo} -> {r.status_code}",file=sys.stderr);return []
    data=r.json()
    return [Hit(repo,it["path"],it["html_url"]) for it in data.get("items",[])]

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

def parse_args():
    p=argparse.ArgumentParser()
    p.add_argument("--org",default="skyscanner")
    p.add_argument("--query",default='"deeplink_url"')
    p.add_argument("--token",default=os.getenv("GITHUB_TOKEN"))
    p.add_argument("--out",default="repos_with_keyword.csv")
    p.add_argument("--max-pages",type=int,default=10)
    p.add_argument("--max-repos",type=int,default=200)
    p.add_argument("--sleep",type=float,default=6.5)
    return p.parse_args()

def main():
    a=parse_args()
    tok=a.token
    if not tok: print("Missing GITHUB_TOKEN",file=sys.stderr)
    # 1️⃣ org-level search
    hits=search_org(a.org,a.query,tok,a.max_pages)
    if not hits:
        print("[fallback] org search empty; scanning repos individually…",file=sys.stderr)
        repos=list_repos(a.org,tok)
        for i,r in enumerate(repos[:a.max_repos],1):
            hits+=search_repo(r,a.query,tok)
            print(f"[{i}/{len(repos)}] {r}",file=sys.stderr)
            time.sleep(a.sleep)
    rows=aggregate(hits)
    write_csv(rows,a.out)
    print(f"Found {len(rows)} repos; written to {a.out}")
    print("| Repo | Matches | Samples |\\n|---|---:|---|")
    for r,c,u in rows[:40]:
        print(f"| {r} | {c} | {'<br/>'.join(u)} |")

if __name__=="__main__": main()
