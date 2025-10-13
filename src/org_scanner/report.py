import os
import json
from typing import List
from .repo_scanner import RepoResult

def ensure_dir(d: str):
    os.makedirs(d, exist_ok=True)

def write_markdown(results: List[RepoResult], path: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write("| repository | parameters_detected | count | sample_files |\\n")
        f.write("|------------|---------------------|-------|--------------|\\n")
        for r in sorted(results, key=lambda x: x.repo.lower()):
            params_sorted = sorted(r.parameters)
            files = ", ".join([os.path.relpath(h.file_path)[:60] for h in r.file_hits[:3]])
            f.write(f"| {r.repo} | {', '.join(params_sorted)} | {len(params_sorted)} | {files} |\\n")

def write_csv(results: List[RepoResult], path: str):
    import csv
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["repository", "parameter", "sample_files"])
        for r in results:
            sample_files = ";".join(h.file_path for h in r.file_hits[:5])
            for p in sorted(r.parameters):
                writer.writerow([r.repo, p, sample_files])

def write_json(results: List[RepoResult], path: str):
    data = []
    for r in results:
        data.append({
            "repository": r.repo,
            "parameters": sorted(r.parameters),
            "files": [
                {
                    "file": h.file_path,
                    "params": sorted(h.params),
                    "lines": h.lines
                } for h in r.file_hits
            ]
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)