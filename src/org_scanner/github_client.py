import os
import aiohttp
import asyncio
from typing import Any, Dict, List, Optional

GQL_ENDPOINT = "https://api.github.com/graphql"
REST_ENDPOINT = "https://api.github.com"

class GitHubClient:
    def __init__(self, token: Optional[str] = None, timeout: int = 15):
        self.token = token or os.getenv("GITHUB_TOKEN")
        if not self.token:
            raise RuntimeError("GITHUB_TOKEN not set")
        self._session: Optional[aiohttp.ClientSession] = None
        self.timeout = timeout

    async def __aenter__(self):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
        }
        self._session = aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout))
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()

    async def graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        assert self._session
        async with self._session.post(GQL_ENDPOINT, json={"query": query, "variables": variables}) as r:
            r.raise_for_status()
            data = await r.json()
            if "errors" in data:
                raise RuntimeError(data["errors"])
            return data["data"]

    async def list_org_repos(self, org: str, include_archived=False, limit: Optional[int]=None) -> List[Dict[str, Any]]:
        query = """
        query($org:String!, $cursor:String){
          organization(login:$org){
            repositories(first:100, after:$cursor, orderBy:{field:NAME, direction:ASC}){
              pageInfo { hasNextPage endCursor }
              nodes {
                name
                url
                isArchived
                isFork
                defaultBranchRef { name }
              }
            }
          }
        }
        """
        repos: List[Dict[str, Any]] = []
        cursor = None
        while True:
            data = await self.graphql(query, {"org": org, "cursor": cursor})
            nodes = data["organization"]["repositories"]["nodes"]
            for n in nodes:
                if not include_archived and n["isArchived"]:
                    continue
                if n["isFork"]:
                    continue
                repos.append(n)
                if limit and len(repos) >= limit:
                    return repos
            if not data["organization"]["repositories"]["pageInfo"]["hasNextPage"]:
                break
            cursor = data["organization"]["repositories"]["pageInfo"]["endCursor"]
        return repos

    async def code_search(self, query: str, per_page=50, max_items=200) -> List[Dict[str, Any]]:
        assert self._session
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            params = {"q": query, "per_page": per_page, "page": page}
            async with self._session.get(f"{REST_ENDPOINT}/search/code", params=params) as r:
                if r.status == 422:
                    break
                r.raise_for_status()
                payload = await r.json()
                for it in payload.get("items", []):
                    items.append(it)
                    if len(items) >= max_items:
                        return items
                if "incomplete_results" in payload and payload["incomplete_results"]:
                    break
                if len(payload.get("items", [])) < per_page:
                    break
                page += 1
        return items