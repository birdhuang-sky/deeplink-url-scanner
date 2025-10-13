import asyncio
import os
import re
import tempfile
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional

from .patterns import extract_params_from_line, DEEPLINK_CONTEXT_HINTS

@dataclass
class FileHit:
    file_path: str
    params: Set[str]
    lines: List[str] = field(default_factory=list)

@dataclass
class RepoResult:
    repo: str
    parameters: Set[str] = field(default_factory=set)
    file_hits: List[FileHit] = field(default_factory=list)

class CloneScanner:
    def __init__(self, keywords: Set[str], context_hints=None):
        self.keywords = keywords
        self.context_hints = context_hints or DEEPLINK_CONTEXT_HINTS

    def scan_repo(self, clone_url: str, name: str, branch: Optional[str]) -> RepoResult:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "clone", "--depth", "1", clone_url, td], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if branch:
                subprocess.run(["git", "-C", td, "checkout", branch], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            result = RepoResult(repo=name)
            rg = subprocess.run(["which", "rg"], stdout=subprocess.PIPE)
            candidate_files: List[str] = []
            if rg.returncode == 0:
                pattern = "(" + "|".join([re.escape(k) for k in list(self.keywords) + self.context_hints]) + ")"
                cmd = ["rg", "-i", "-n", "-H", pattern, td]
                out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for line in out.stdout.splitlines():
                    parts = line.split(":", 2)
                    if len(parts) >= 3:
                        fpath = parts[0]
                        candidate_files.append(fpath)
            else:
                for root, _dirs, files in os.walk(td):
                    for f in files:
                        p = os.path.join(root, f)
                        if os.path.getsize(p) > 512_000:
                            continue
                        candidate_files.append(p)

            seen_files = set()
            for f in candidate_files:
                if f in seen_files:
                    continue
                seen_files.add(f)
                try:
                    with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                        raw = fh.readlines()
                except Exception:
                    continue
                file_params: Set[str] = set()
                lines_hit: List[str] = []
                for ln in raw:
                    line_lower = ln.lower()
                    if any(h in line_lower for h in self.context_hints) or any(k in ln for k in self.keywords):
                        params = extract_params_from_line(ln)
                        for kw in self.keywords:
                            if kw in ln:
                                file_params.add(kw)
                        file_params.update(params)
                        if params or any(k in ln for k in self.keywords):
                            lines_hit.append(ln.strip()[:300])
                if file_params:
                    result.parameters.update(file_params)
                    result.file_hits.append(FileHit(file_path=f, params=file_params, lines=lines_hit[:5]))
            return result