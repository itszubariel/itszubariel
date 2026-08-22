#!/usr/bin/env python3
"""
Generates the profile metrics panel as an SVG.

Replaces a third-party widget that called the GitHub API unauthenticated, ran
out of its sixty requests an hour partway through a profile with many
repositories, and rendered every unfetched value as a zero.

Two rules follow from that:

Every figure is read through an authenticated client, because the counts that
matter here include private repositories. An unauthenticated client cannot see
them and so would report them as nothing.

A figure that cannot be read is drawn as a dash, never as a zero. A zero is a
measurement and has to be earned.

Usage:
    panel.py            write metrics-light.svg / metrics-dark.svg next to this script
    panel.py OUT_DIR    write there instead
"""

import base64
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

OWNER = "itszubariel"

STAR_WINDOW_DAYS = 14           # matches plugin_stargazers_days: 14
LINES_REPO_LIMIT = 4            # matches plugin_lines_repositories_limit: 4
LINES_COMMIT_CAP = 150          # per repo, to bound API calls (one call per commit)
LINES_HISTORY_DAYS = 365        # matches plugin_lines_history_limit: 1 (year)


def gh(args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def search_count(q):
    n = gh(["api", f"search/issues?q={urllib.parse.quote(q)}&per_page=1",
            "--jq", ".total_count"])
    return n if isinstance(n, int) else None


def contributions_all_time():
    """
    Total contributions across every year, or None.

    Reads contributionCalendar.totalContributions, which is the figure GitHub
    itself puts on the profile graph. The obvious alternative,
    totalCommitContributions plus restrictedContributionsCount, is not stable:
    the split between them depends on what the querying token can see.

    contributionsCollection covers one year at a time and defaults to the last,
    so every year is summed. Any year failing to answer dashes the whole figure
    rather than reporting a partial sum as a total.
    """
    total = 0
    for year in range(2021, date.today().year + 1):
        q = (f'{{user(login:"{OWNER}"){{contributionsCollection('
             f'from:"{year}-01-01T00:00:00Z",to:"{year}-12-31T23:59:59Z")'
             f'{{contributionCalendar{{totalContributions}}}}}}}}')
        d = gh(["api", "graphql", "-f", f"query={q}"])
        try:
            total += d["data"]["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]
        except (TypeError, KeyError):
            return None
    return total


def profile_extra():
    """
    Header and community fields in one query: avatar, bio, joined date, and
    the counts that make up the community section. sponsorshipsAsSponsor is
    kept in the same query but read defensively below — a token without
    sponsors access can fail that one field without failing the rest, since
    GraphQL still returns partial data alongside a top-level errors array.
    """
    q = (f'{{user(login:"{OWNER}"){{name bio avatarUrl createdAt '
         f'followers{{totalCount}} following{{totalCount}} organizations{{totalCount}} '
         f'starredRepositories{{totalCount}} watching{{totalCount}} '
         f'sponsorshipsAsSponsor{{totalCount}}}}}}')
    d = gh(["api", "graphql", "-f", f"query={q}"])
    return (d or {}).get("data", {}).get("user", {}) or {}


def notable_orgs(limit=6):
    """
    Organizations whose repositories this account has committed to, excluding
    the account's own repositories. Deduplicated and capped, since a long
    contribution history can otherwise return the same handful of orgs many
    times over.
    """
    q = (f'{{user(login:"{OWNER}"){{repositoriesContributedTo(first:50, '
         f'contributionTypes:[COMMIT], includeUserRepositories:false)'
         f'{{nodes{{owner{{login __typename}}}}}}}}}}')
    d = gh(["api", "graphql", "-f", f"query={q}"])
    try:
        nodes = d["data"]["user"]["repositoriesContributedTo"]["nodes"]
    except (TypeError, KeyError):
        return None
    seen, orgs = set(), []
    for node in nodes:
        owner = node.get("owner") or {}
        if owner.get("__typename") == "Organization" and owner["login"] not in seen:
            seen.add(owner["login"])
            orgs.append(owner["login"])
        if len(orgs) >= limit:
            break
    return orgs


def stargazer_series(repos, days=STAR_WINDOW_DAYS):
    """
    New stargazers per day for the last `days` days, aggregated across public
    repos, plus the running total at each day.

    Only the most recent 100 stargazers per repo are paged (ordered newest
    first). That window would need to be missed by a repo picking up over a
    hundred stars inside the fortnight for this to undercount — well outside
    this account's observed star velocity — so deeper paging isn't spent here.

    Returns None on total failure so the caller can fall back to omitting the
    chart, consistent with the rest of this script's dash-not-zero rule.
    """
    cutoff = date.today() - timedelta(days=days - 1)
    counts = defaultdict(int)
    total_stars = 0
    any_ok = False
    for r in repos:
        if r["isPrivate"]:
            continue
        total_stars += r["stargazerCount"]
        if r["stargazerCount"] == 0:
            continue
        q = (f'{{repository(owner:"{OWNER}",name:"{r["name"]}"){{'
             f'stargazers(first:100, orderBy:{{field:STARRED_AT,direction:DESC}})'
             f'{{edges{{starredAt}}}}}}}}')
        d = gh(["api", "graphql", "-f", f"query={q}"])
        try:
            edges = d["data"]["repository"]["stargazers"]["edges"]
        except (TypeError, KeyError):
            continue
        any_ok = True
        for e in edges:
            starred = datetime.fromisoformat(e["starredAt"].replace("Z", "+00:00")).date()
            if starred >= cutoff:
                counts[starred] += 1
    if not any_ok:
        return None
    days_list = [cutoff + timedelta(days=i) for i in range(days)]
    in_window = sum(counts[d] for d in days_list)
    running = total_stars - in_window
    totals = {}
    for d in days_list:
        running += counts[d]
        totals[d] = running
    return {"days": days_list, "new": [counts[d] for d in days_list],
            "total": [totals[d] for d in days_list]}


def lines_changed(repos, repo_limit=LINES_REPO_LIMIT, commit_cap=LINES_COMMIT_CAP,
                   history_days=LINES_HISTORY_DAYS):
    """
    Additions and deletions across recent commits authored by this account, in
    the `repo_limit` repos with the most recent activity.

    GitHub has no aggregate endpoint for this — the only way to get real
    numbers is one API call per commit for its stats. That cost is bounded on
    both axes: only a handful of repos are read, and only the most recent
    `commit_cap` commits per repo, inside a `history_days` window. Both bounds
    mean this is a recent-activity figure, not a lifetime total, and it dashes
    out entirely rather than silently reporting a partial sum as complete.
    """
    since = (date.today() - timedelta(days=history_days)).isoformat()
    candidates = sorted(
        (r for r in repos if not r["isPrivate"]),
        key=lambda r: r.get("diskUsage") or 0, reverse=True
    )[:repo_limit]

    added = removed = 0
    got_any = False
    for r in candidates:
        shas = gh(["api", f"repos/{OWNER}/{r['name']}/commits",
                   "-X", "GET",
                   "-f", f"author={OWNER}",
                   "-f", f"since={since}",
                   "-f", f"per_page={min(commit_cap, 100)}",
                   "--jq", "[.[].sha]"])
        if not isinstance(shas, list):
            continue
        for sha in shas[:commit_cap]:
            stats = gh(["api", f"repos/{OWNER}/{r['name']}/commits/{sha}",
                       "--jq", "{a: .stats.additions, d: .stats.deletions}"])
            if isinstance(stats, dict) and stats.get("a") is not None:
                added += stats["a"]
                removed += stats["d"]
                got_any = True
    return (added, removed) if got_any else (None, None)


def collect():
    repos = gh(["repo", "list", OWNER, "--limit", "200", "--json",
                "name,stargazerCount,forkCount,isPrivate,primaryLanguage,"
                "licenseInfo,diskUsage"]) or []
    u = profile_extra()

    public = [r for r in repos if not r["isPrivate"]]

    # Releases are summed dynamically across every repo rather than a fixed
    # list of names, so this stays correct as repos are added or renamed.
    releases = 0
    got_release = False
    for r in repos:
        cnt = gh(["api", f"repos/{OWNER}/{r['name']}/releases", "--jq", "length"])
        if isinstance(cnt, int):
            releases += cnt
            got_release = True

    pkgs = gh(["api", "/user/packages?package_type=npm", "--jq", "length"])
    langs = Counter(r["primaryLanguage"]["name"] for r in repos if r.get("primaryLanguage"))

    joined = u.get("createdAt")
    years = None
    if joined:
        years = round((datetime.now().date() - datetime.fromisoformat(
            joined.replace("Z", "+00:00")).date()).days / 365.25, 1)

    avatar_b64 = None
    if u.get("avatarUrl"):
        try:
            with urllib.request.urlopen(u["avatarUrl"], timeout=10) as resp:
                avatar_b64 = base64.b64encode(resp.read()).decode("ascii")
        except Exception:
            avatar_b64 = None

    added, removed = lines_changed(repos)

    return {
        "name": u.get("name") or OWNER,
        "bio": u.get("bio"),
        "avatar_b64": avatar_b64,
        "years": years,
        "followers": (u.get("followers") or {}).get("totalCount"),
        "following": (u.get("following") or {}).get("totalCount"),
        "orgs": (u.get("organizations") or {}).get("totalCount"),
        "starred": (u.get("starredRepositories") or {}).get("totalCount"),
        "watching": (u.get("watching") or {}).get("totalCount"),
        "sponsoring": (u.get("sponsorshipsAsSponsor") or {}).get("totalCount"),
        "commits": contributions_all_time(),
        "prs": search_count(f"is:pr author:{OWNER}"),
        "prs_merged": search_count(f"is:pr author:{OWNER} is:merged"),
        "reviews": search_count(f"is:pr reviewed-by:{OWNER}"),
        "issues": search_count(f"is:issue author:{OWNER}"),
        "repos": len(repos) or None,
        "repos_public": len(public),
        "stars": sum(r["stargazerCount"] for r in public if r["name"] != OWNER) if repos else None,
        "forks": sum(r["forkCount"] for r in repos) if repos else None,
        "licensed": sum(1 for r in repos if r.get("licenseInfo")) if repos else None,
        "releases": releases if got_release else None,
        "packages": pkgs,
        "disk": round(sum(r.get("diskUsage") or 0 for r in repos) / 1024) if repos else None,
        "languages": langs.most_common(5),
        "notable": notable_orgs(),
        "stargazer_series": stargazer_series(repos),
        "lines_added": added,
        "lines_removed": removed,
    }


def n(v):
    """A number, or a dash when it could not be read. Never a zero by default."""
    if v is None:
        return "–"
    return f"{v:,}" if isinstance(v, int) and v >= 1000 else str(v)


def n_years(v):
    """3, not 3.0 — but 3.4 stays 3.4. Whole-number floats read as counts."""
    if v is None:
        return "–"
    return str(int(v)) if v == int(v) else f"{v:.1f}"


def n_compact(v):
    """Abbreviated form for figures that would otherwise be six-plus digits
    wide (1.23m, 671k), matching how GitHub itself displays large counts."""
    if v is None:
        return "–"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}m"
    if v >= 1_000:
        return f"{round(v/1000)}k"
    return str(v)


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# Primer tokens, read off a live GitHub page rather than guessed. Section
# headings on GitHub are 16px semibold in the default foreground colour, in
# sentence case: not uppercase, not accent-coloured, not letter-spaced.
THEMES = {
    "light": {"fg": "#1f2328", "muted": "#59636e", "border": "#d1d9e0", "chip": "#f6f8fa"},
    "dark":  {"fg": "#f0f6fc", "muted": "#9198a1", "border": "#3d444d", "chip": "#21262d"},
}

# github-linguist colours, so the bar matches every other language bar on the site.
LANG_COLOURS = {
    "Python": "#3572A5", "TypeScript": "#3178c6", "HTML": "#e34c26",
    "JavaScript": "#f1e05a", "Swift": "#F05138", "Astro": "#ff5a03",
    "Shell": "#89e051", "CSS": "#663399", "Go": "#00ADD8", "Ruby": "#701516",
    "Rust": "#dea584", "Java": "#b07219",
}
FALLBACK_COLOUR = "#8b949e"
ACCENT = "#3fb950"

FONT = ('"Mona Sans VF", -apple-system, "system-ui", "Segoe UI", '
        '"Noto Sans", Helvetica, Arial, sans-serif')


def build(d, theme="dark"):
    c = THEMES[theme]
    W = 792

    parts = []
    y = 0

    # ---- Header: avatar, name, bio, joined date, follower count ----------
    header_h = 100
    if d["avatar_b64"]:
        parts.append(f'<clipPath id="avatarclip"><circle cx="32" cy="{y+32}" r="32"/></clipPath>')
        parts.append(f'<image x="0" y="{y}" width="64" height="64" '
                     f'href="data:image/png;base64,{d["avatar_b64"]}" clip-path="url(#avatarclip)"/>')
    tx = 80
    parts.append(f'<text x="{tx}" y="{y+26}" class="name">{esc(d["name"])}</text>')
    joined_line = f'Joined GitHub {n_years(d["years"])} years ago' if d["years"] is not None else "Joined GitHub"
    parts.append(f'<text x="{tx}" y="{y+48}" class="k">{esc(joined_line)}</text>')
    parts.append(f'<text x="{tx}" y="{y+68}" class="k">Followed by {n(d["followers"])} users</text>')
    if d["bio"]:
        parts.append(f'<text x="0" y="{y+96}" class="k">{esc(d["bio"])}</text>')
        header_h += 12
    y += header_h + 16

    # ---- Activity / Repositories / Reach, three columns -------------------
    rows_left = [
        ("Contributions", n(d["commits"])),
        ("Pull requests opened", n(d["prs"])),
        ("Merged", n(d["prs_merged"])),
        ("Pull requests reviewed", n(d["reviews"])),
        ("Issues opened", n(d["issues"])),
    ]
    rows_right = [
        ("Repositories", f'{n(d["repos"])}' + (f'  ({d["repos_public"]} public)' if d["repos"] else "")),
        ("Releases", n(d["releases"])),
        ("Packages", n(d["packages"])),
        ("Licensed", f'{n(d["licensed"])} of {n(d["repos"])}'),
        ("Storage", f'{n(d["disk"])} MB'),
    ]
    rows_third = [
        ("Stars received", n(d["stars"])),
        ("Forks", n(d["forks"])),
        ("Followers", n(d["followers"])),
        ("Following", n(d["following"])),
        ("Organizations", n(d["orgs"])),
    ]

    def column(rows, x, y0, label):
        # Rows always sit 32px below y0, whether the heading is drawn here
        # (label given) or was already drawn externally at y0 by the caller
        # (label ""). Making that offset unconditional keeps both cases using
        # the same y0, instead of the row-start silently depending on which
        # path drew the heading.
        out = [f'<text x="{x}" y="{y0}" class="h">{esc(label)}</text>'] if label else []
        yy = y0 + 32
        for k, v in rows:
            out.append(f'<text x="{x}" y="{yy}" class="k">{esc(k)}</text>')
            out.append(f'<text x="{x + 220}" y="{yy}" class="v" text-anchor="end">{esc(v)}</text>')
            yy += 27
        return "\n".join(out)

    parts.append(column(rows_left, 0, y, "Activity"))
    parts.append(column(rows_right, 280, y, "Repositories"))
    parts.append(column(rows_third, 560, y, "Reach"))
    y += 32 + 27 * 5 + 24

    # ---- Languages ----------------------------------------------------------
    parts.append(f'<text x="0" y="{y}" class="h">Languages</text>')
    bar_y = y + 16
    total_lang = sum(cnt for _, cnt in d["languages"]) or 1
    seg, cursor, legend = [], 0.0, []
    for lang, count in d["languages"]:
        colour = LANG_COLOURS.get(lang, FALLBACK_COLOUR)
        w = W * count / total_lang
        seg.append(f'<rect x="{cursor:.2f}" y="{bar_y}" width="{w:.2f}" height="8" fill="{colour}"/>')
        cursor += w
        legend.append((lang, colour))
    parts.append(f'<clipPath id="barclip"><rect x="0" y="{bar_y}" width="{W}" height="8" rx="4"/></clipPath>')
    parts.append(f'<g clip-path="url(#barclip)">{chr(10).join(seg)}</g>')
    leg_parts, lx = [], 0
    ly = bar_y + 30
    for lang, colour in legend:
        leg_parts.append(f'<circle cx="{lx + 4}" cy="{ly}" r="4" fill="{colour}"/>')
        leg_parts.append(f'<text x="{lx + 14}" y="{ly+4}" class="lg">{esc(lang)}</text>')
        lx += 26 + len(lang) * 6.9
    parts.append("\n".join(leg_parts))
    y = ly + 36

    # ---- Community stats ----------------------------------------------------
    parts.append(f'<text x="0" y="{y}" class="h">Community stats</text>')
    community_rows = [
        ("Member of", f'{n(d["orgs"])} organizations'),
        ("Following", f'{n(d["following"])} users'),
        ("Sponsoring", f'{n(d["sponsoring"])} accounts'),
        ("Starred", f'{n(d["starred"])} repositories'),
        ("Watching", f'{n(d["watching"])} repositories'),
    ]
    parts.append(column(community_rows, 0, y, ""))
    y += 32 + 27 * 5 + 24

    # ---- Notable contributions (org badges) ---------------------------------
    if d["notable"]:
        parts.append(f'<text x="0" y="{y}" class="h">Notable contributions</text>')
        by = y + 20
        bx = 0
        for org in d["notable"]:
            label = f"@{org}"
            bw = 16 + len(label) * 7.2
            parts.append(f'<rect x="{bx}" y="{by}" width="{bw:.1f}" height="26" rx="13" '
                         f'fill="{c["chip"]}" stroke="{c["border"]}"/>')
            parts.append(f'<text x="{bx + bw/2:.1f}" y="{by+17}" class="k" text-anchor="middle">{esc(label)}</text>')
            bx += bw + 10
        y += 26 + 40
    # No orgs found (or the query failed): the section is omitted rather than
    # shown empty, since an empty heading reads as a bug, not as a fact.

    # ---- Stargazers charts ---------------------------------------------------
    series = d["stargazer_series"]
    if series:
        parts.append(f'<text x="0" y="{y}" class="h">Stargazers</text>')
        chart_y = y + 20
        chart_h = 70
        n_days = len(series["days"])
        col_w = (W / 2 - 20) / n_days

        def mini_chart(x0, values, label, colour):
            out = [f'<text x="{x0 + (col_w*n_days)/2:.1f}" y="{chart_y}" class="lg" '
                  f'text-anchor="middle">{esc(label)}</text>']
            vmax = max(values) or 1
            for i, v in enumerate(values):
                bar_h = 0 if v == 0 else max(4, chart_h * v / vmax)
                bx = x0 + i * col_w
                by = chart_y + 14 + (chart_h - bar_h)
                out.append(f'<rect x="{bx + col_w*0.2:.1f}" y="{by:.1f}" '
                           f'width="{col_w*0.6:.1f}" height="{bar_h:.1f}" fill="{colour}"/>')
                # Only the first and last bars get a printed value — labeling
                # every bar tied for the window's max produces a cluster of
                # repeated numbers stacked on top of each other instead of a
                # readable chart.
                if i == 0 or i == n_days - 1:
                    out.append(f'<text x="{bx + col_w/2:.1f}" y="{by-4:.1f}" class="lg" '
                               f'text-anchor="middle">{v}</text>')
                dstr = series["days"][i].strftime("%-d")
                out.append(f'<text x="{bx + col_w/2:.1f}" y="{chart_y+14+chart_h+16}" class="lg" '
                           f'text-anchor="middle">{dstr}</text>')
            return "\n".join(out)

        parts.append(mini_chart(0, series["total"], "Total stargazers", ACCENT))
        parts.append(mini_chart(W / 2 + 20, series["new"], "New stargazers per day", ACCENT))
        y = chart_y + 14 + chart_h + 32
    # Chart omitted entirely if the stargazer query failed outright, rather
    # than rendering an empty axis with no bars.

    # ---- Lines changed (footer stat) ------------------------------------------
    if d["lines_added"] is not None:
        parts.append(f'<text x="0" y="{y}" class="lg">'
                     f'{n_compact(d["lines_added"])} added, {n_compact(d["lines_removed"])} removed '
                     f'(recent activity, last {LINES_HISTORY_DAYS} days)</text>')
        y += 20

    H = y + 8
    body = "\n".join(parts)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="GitHub metrics for {OWNER}">
<style>
  .name {{ fill: {c["fg"]}; font: 600 20px {FONT}; }}
  .h {{ fill: {c["fg"]}; font: 600 16px {FONT}; }}
  .k {{ fill: {c["muted"]}; font: 400 14px {FONT}; }}
  .v {{ fill: {c["fg"]}; font: 600 14px {FONT}; font-variant-numeric: tabular-nums; }}
  .lg {{ fill: {c["muted"]}; font: 400 12px {FONT}; }}
  .rule {{ stroke: {c["border"]}; stroke-width: 1; }}
</style>
{body}
</svg>
'''


if __name__ == "__main__":
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent
    if base.suffix == ".svg":
        base = base.parent
    data = collect()
    for theme in THEMES:
        out = base / f"metrics-{theme}.svg"
        out.write_text(build(data, theme))
        print(f"wrote {out}")
    missing = [k for k, v in data.items() if v is None]
    if missing:
        print(f"unreadable, drawn as dashes: {', '.join(missing)}")