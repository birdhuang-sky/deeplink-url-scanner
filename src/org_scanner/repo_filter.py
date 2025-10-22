# src/org_scanner/repo_filter.py
from pathlib import Path
from typing import Iterable, List

class ExcludeRepoFilter:
    def __init__(self, exclude_file: Path):
        self.patterns = self._load_patterns(exclude_file)

    @staticmethod
    def _load_patterns(path: Path) -> list[str]:
        patterns: list[str] = []
        if not path.exists():
            return patterns
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line.lower())
        return patterns

    def is_excluded(self, repo_name: str) -> bool:
        name = repo_name.lower()
        # Match full name or substring occurrence
        return any(p == name or p in name for p in self.patterns)

    def filter(self, repos: Iterable[str]) -> List[str]:
        return [r for r in repos if not self.is_excluded(r)]


# Example integration (adjust according to existing code structure)
def search_repos(repos: list[str], exclude_file: str = "res/exclude-repo.txt"):
    exclude_filter = ExcludeRepoFilter(Path(exclude_file))
    target_repos = exclude_filter.filter(repos)
    # proceed with search only on target_repos
    for repo in target_repos:
        perform_search(repo)  # replace with actual search logic


# Placeholder for existing search logic
def perform_search(repo: str):
    print(f"Searching in {repo}")
