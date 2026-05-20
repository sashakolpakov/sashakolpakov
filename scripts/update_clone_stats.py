#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


API_ROOT = "https://api.github.com"
README_START = "<!-- CLONE-STATS:START -->"
README_END = "<!-- CLONE-STATS:END -->"


def truthy(value: str | None) -> bool:
    return value is not None and value.lower() in {"1", "true", "yes", "on"}


def api_get_json(url: str, token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "sashakolpakov-clone-leaderboard",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API request failed with {exc.code} {exc.reason} for {url}: {body}"
        ) from exc


def api_get_pages(url: str, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1

    while True:
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}per_page=100&page={page}"
        data = api_get_json(page_url, token)
        if not isinstance(data, list):
            raise RuntimeError(f"Expected a list response from {page_url}")
        if not data:
            break

        items.extend(data)
        if len(data) < 100:
            break
        page += 1

    return items


def list_public_repositories(owner: str, token: str) -> list[dict[str, Any]]:
    quoted_owner = urllib.parse.quote(owner, safe="")
    url = f"{API_ROOT}/users/{quoted_owner}/repos?type=owner&sort=full_name"
    return api_get_pages(url, token)


def is_public_repository(repo: dict[str, Any]) -> bool:
    if repo.get("private"):
        return False

    visibility = repo.get("visibility")
    return visibility in {None, "public"}


def fetch_clone_traffic(owner: str, repo: str, token: str) -> dict[str, Any]:
    quoted_owner = urllib.parse.quote(owner, safe="")
    quoted_repo = urllib.parse.quote(repo, safe="")
    url = f"{API_ROOT}/repos/{quoted_owner}/{quoted_repo}/traffic/clones?per=day"
    data = api_get_json(url, token)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected an object response from {url}")
    return data


def load_history(path: Path, owner: str) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "owner": owner, "repos": {}}

    with path.open("r", encoding="utf-8") as handle:
        history = json.load(handle)

    history.setdefault("version", 1)
    history["owner"] = owner
    history.setdefault("repos", {})
    return history


def update_repo_history(
    history: dict[str, Any],
    repo: dict[str, Any],
    traffic: dict[str, Any],
    generated_at: str,
) -> None:
    repo_name = repo["name"]
    repo_history = history["repos"].setdefault(repo_name, {})
    repo_history.update(
        {
            "description": repo.get("description") or "",
            "html_url": repo.get("html_url") or "",
            "fork": bool(repo.get("fork")),
            "archived": bool(repo.get("archived")),
            "updated_at": generated_at,
        }
    )
    repo_history.pop("last_14_days", None)
    repo_history.pop("last_14_days_uniques", None)

    daily = repo_history.setdefault("daily", {})
    for entry in traffic.get("clones", []):
        timestamp = str(entry.get("timestamp", ""))
        if not timestamp:
            continue
        day = timestamp[:10]
        daily[day] = {"count": int(entry.get("count") or 0)}


def tracked_clone_total(repo_history: dict[str, Any]) -> int:
    daily = repo_history.get("daily", {})
    return sum(int(entry.get("count") or 0) for entry in daily.values())


def escape_markdown_cell(value: str) -> str:
    return value.replace("|", r"\|")


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def format_repo_cell(repo: dict[str, Any]) -> str:
    name = escape_html(repo["name"])
    label = f"<code>{name}</code>"
    return f'<a href="{escape_html(repo["url"])}">{label}</a>' if repo["url"] else label


def render_repo_cells(rank: int, repo: dict[str, Any] | None) -> list[str]:
    if repo is None:
        return [
            '<td align="right"></td>',
            "<td></td>",
            '<td align="right"></td>',
        ]
    return [
        f'<td align="right"><code>{rank}</code></td>',
        f"<td>{format_repo_cell(repo)}</td>",
        f'<td align="right"><code>{repo["total"]}</code></td>',
    ]


def render_readme_section(history: dict[str, Any], limit: int, generated_at: str) -> str:
    repos = []
    for name, repo_history in history.get("repos", {}).items():
        total = tracked_clone_total(repo_history)
        if total == 0:
            continue
        repos.append(
            {
                "name": name,
                "url": repo_history.get("html_url") or "",
                "total": total,
            }
        )

    repos.sort(
        key=lambda item: (
            -item["total"],
            item["name"].lower(),
        )
    )
    repos = repos[:limit]

    if not repos:
        return (
            "No clone data has been collected yet. The scheduled workflow will populate "
            "this section after `TRAFFIC_TOKEN` is configured and the workflow runs."
        )

    split_at = (len(repos) + 1) // 2
    left = repos[:split_at]
    right = repos[split_at:]

    rows = [
        "<table>",
        "  <thead>",
        "    <tr>",
        '      <th align="right"><code>No.</code></th>',
        "      <th><code>Repository</code></th>",
        '      <th align="right"><code>Clones</code></th>',
        '      <th align="right"><code>No.</code></th>',
        "      <th><code>Repository</code></th>",
        '      <th align="right"><code>Clones</code></th>',
        "    </tr>",
        "  </thead>",
        "  <tbody>",
    ]
    for index in range(split_at):
        left_cells = render_repo_cells(index + 1, left[index])
        right_repo = right[index] if index < len(right) else None
        right_cells = render_repo_cells(index + split_at + 1, right_repo)
        rows.append("    <tr>")
        rows.extend(f"      {cell}" for cell in left_cells + right_cells)
        rows.append("    </tr>")
    rows.extend(["  </tbody>", "</table>"])
    return "\n".join(rows)


def update_readme(path: Path, section: str) -> None:
    block = f"{README_START}\n{section}\n{README_END}"
    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:
        content = ""

    if README_START in content and README_END in content:
        before, remainder = content.split(README_START, 1)
        _, after = remainder.split(README_END, 1)
        updated = f"{before}{block}{after}"
    else:
        updated = (
            content.rstrip()
            + "\n\n## Repository Clone Leaderboard\n\n"
            + block
            + "\n"
        )

    if updated != content:
        path.write_text(updated, encoding="utf-8")


def write_history(path: Path, history: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=2, sort_keys=True)
        handle.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update the profile README with a GitHub clone leaderboard."
    )
    parser.add_argument("--owner", default=os.environ.get("GITHUB_USER", "sashakolpakov"))
    parser.add_argument("--history", type=Path, default=Path("clone_stats_history.json"))
    parser.add_argument("--readme", type=Path, default=Path("README.md"))
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("CLONE_STATS_LIMIT", "10")),
    )
    parser.add_argument(
        "--include-forks",
        action="store_true",
        default=truthy(os.environ.get("INCLUDE_FORKS")),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("TRAFFIC_TOKEN", "")
    if not token:
        print(
            "TRAFFIC_TOKEN is required. Add a repository secret with a GitHub token "
            "that can read repository traffic for the target repos.",
            file=sys.stderr,
        )
        return 1

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    generated_at = now.isoformat().replace("+00:00", "Z")

    history = load_history(args.history, args.owner)
    repositories = [
        repo
        for repo in list_public_repositories(args.owner, token)
        if is_public_repository(repo)
    ]
    if not args.include_forks:
        repositories = [repo for repo in repositories if not repo.get("fork")]

    for repo in repositories:
        traffic = fetch_clone_traffic(args.owner, repo["name"], token)
        update_repo_history(history, repo, traffic, generated_at)

    history["generated_at"] = generated_at
    section = render_readme_section(history, args.limit, generated_at)
    update_readme(args.readme, section)
    write_history(args.history, history)

    print(f"Updated clone leaderboard for {len(repositories)} repositories.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
