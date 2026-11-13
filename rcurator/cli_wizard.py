"""Interactive terminal curation wizard and rich CLI output formatters."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from rcurator.analytics import FeedHealthAuditor
from rcurator.cdp_client import CDPError, acquire_reddit_session, activate_following_feed
from rcurator.engine.ontology import DEFAULT_ONTOLOGY_PATH, OntologyRepository, normalize_manifest
from rcurator.engine.pipeline import AutoPresetGenerator
from rcurator.mixer import DEFAULT_PRESETS_DIR, PresetMixer
from rcurator.nlp.tokenizer import TechnicalTokenizer
from rcurator.reddit_api import RedditAPIClient
from rcurator.sources.github import GitHubMiner
from rcurator.sources.resume import ResumeParser
from rcurator.sources.types import SignalEvidence
from rcurator.sources.workspace import WorkspaceScanner
from rcurator.state import SnapshotManager


def _parse_ratio_input(raw: str) -> tuple[int, int, int]:
    """Parse 'A,C,D' percentage string into 3 integers summing to 100."""
    text = raw.strip()
    if not text:
        return (40, 35, 25)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) != 3:
        raise ValueError("Ratio must have exactly 3 comma-separated numbers (e.g., 40,35,25).")
    vals = [float(p) for p in parts]
    if any(v < 0 for v in vals) or sum(vals) <= 0:
        raise ValueError("Ratio values must be non-negative with a positive sum.")
    total = sum(vals)
    a = round(vals[0] * 100.0 / total)
    c = round(vals[1] * 100.0 / total)
    d = 100 - a - c
    if d < 0:
        c = max(0, c + d)
        d = 100 - a - c
    return (int(a), int(c), int(d))


def format_presets_table(presets: Sequence[dict[str, Any]]) -> str:
    """Render built-in presets into a clean aligned terminal table."""
    if not presets:
        return "No presets found."
    lines = [
        f"Available Curated Presets ({len(presets)}):",
        "-" * 84,
        f"  {'#':>3}  {'KEY':<30} {'ANCHORS':>7} {'PRIMING':>7} {'FEEDS':>5}  {'TITLE'}",
        "-" * 84,
    ]
    for idx, p in enumerate(presets, start=1):
        row_num = p.get("_index", idx)
        lines.append(
            f"  {int(row_num):>3}  {str(p.get('id', '')):<30} "
            f"{int(p.get('anchor_count', 0)):>7} "
            f"{int(p.get('domain_priming_count', 0)):>7} "
            f"{int(p.get('custom_feeds_count', 0)):>5}  "
            f"{p.get('name', '')}"
        )
    return "\n".join(lines)


def format_snapshots_table(snapshots: Sequence[dict[str, Any]]) -> str:
    """Render saved .curator_snapshots metadata into a readable table."""
    if not snapshots:
        return "No snapshots found in .curator_snapshots/."
    lines = [
        f"Saved Account Snapshots ({len(snapshots)}):",
        "-" * 86,
        f"  {'SNAPSHOT ID':<42} {'USER':<16} {'SUBS':>5} {'FEEDS':>5}  {'CREATED AT'}",
        "-" * 86,
    ]
    for s in snapshots:
        lines.append(
            f"  {str(s.get('snapshot_id', '')):<42} "
            f"{str(s.get('username', '')):<16} "
            f"{int(s.get('subscriptions_count', 0)):>5} "
            f"{int(s.get('custom_feeds_count', 0)):>5}  "
            f"{str(s.get('created_at', ''))[:19]}"
        )
    return "\n".join(lines)


def format_audit_summary(audit: dict[str, Any], title: str | None = None) -> str:
    """Format a FeedHealthAuditor report for terminal inspection."""
    header = title or "Information Diet Health Audit"
    actual = audit.get("actual_velocity_distribution_pct", {})
    target = audit.get("target_distribution_pct", {})
    counts = audit.get("pillar_counts", {})
    lines = [
        header,
        "=" * 64,
        f"  Tier-1 Communities : {audit.get('subreddit_count', 0)} "
        f"(aes={counts.get('aesthetic_leisure', 0)}, "
        f"cog={counts.get('cognitive_curiosity', 0)}, "
        f"dom={counts.get('domain_priming', 0)})",
        f"  Daily Velocity     : {audit.get('total_daily_velocity', 0.0):.1f} posts/day",
        f"  Composite Health   : {audit.get('composite_health_score', 0.0):.2f} / 100",
        f"  Golden Ratio Score : {audit.get('golden_ratio_fidelity_score', 0.0):.2f} / 100 "
        f"(TVD drift={audit.get('golden_ratio_tvd_drift', 0.0):.4f})",
        f"  Domain Entropy     : {audit.get('shannon_domain_entropy_norm', 0.0):.4f} "
        f"| Gini={audit.get('gini_velocity_concentration', 0.0):.4f}",
        f"  Dominance Cap      : {audit.get('max_subreddit_dominance_pct', 0.0):.2f}% "
        f"(top: r/{audit.get('dominant_subreddit') or 'none'})",
        f"  Target Ratio (%)   : {target.get('aesthetic_leisure', 40):.1f} / "
        f"{target.get('cognitive_curiosity', 35):.1f} / "
        f"{target.get('domain_priming', 25):.1f}",
        f"  Actual Ratio (%)   : {actual.get('aesthetic_leisure', 0):.1f} / "
        f"{actual.get('cognitive_curiosity', 0):.1f} / "
        f"{actual.get('domain_priming', 0):.1f}",
    ]
    unrec = audit.get("unrecognized_subreddits") or []
    if unrec:
        lines.append(f"  Unrecognized Subs  : {', '.join(f'r/{s}' for s in unrec[:10])}")
    recs = audit.get("recommendations") or []
    if recs:
        lines.append("  Recommendations:")
        for rec in recs:
            lines.append(f"    * {rec}")
    return "\n".join(lines)


def format_diff_summary(diff: dict[str, Any]) -> str:
    """Format a SnapshotManager.compute_diff() result for terminal display."""
    to_sub = diff.get("to_subscribe", [])
    already = diff.get("already_subscribed", [])
    to_unsub = diff.get("to_unsubscribe", [])
    kept = diff.get("unmanaged_kept", [])
    feeds = diff.get("custom_feeds", {})
    lines = [
        "Subscription & Custom Feed Diff Preview",
        "=" * 64,
        f"  + Subscribe ({len(to_sub)})        : {', '.join(f'r/{s}' for s in to_sub) or 'none'}",
        f"  = Already Subscribed ({len(already)})",
        f"  - Prune ({len(to_unsub)})           : {', '.join(f'r/{s}' for s in to_unsub) or 'none'}",
        f"  ~ Unmanaged Kept ({len(kept)})",
    ]
    if feeds:
        lines.append("  Custom Feeds:")
        for fname, info in feeds.items():
            lines.append(
                f"    * {fname} [{info.get('action', 'unknown')}]: "
                f"+{len(info.get('added_subreddits', []))} / "
                f"-{len(info.get('removed_subreddits', []))} "
                f"({len(info.get('final_subreddits', []))} total)"
            )
    lines.append(f"  Pending Changes    : {'yes' if diff.get('has_changes') else 'no'}")
    return "\n".join(lines)


def format_manifest_summary(manifest: dict[str, Any], saved_path: Path | None = None) -> str:
    """Format generated/mixed manifest details including top extracted terms and diet health."""
    anchors = manifest.get("anchor_subreddits", [])
    priming = manifest.get("domain_priming_subreddits", [])
    feeds = manifest.get("custom_feeds", [])
    prov = manifest.get("curation_provenance", {})

    lines = [
        f"Manifest: {manifest.get('name', 'Curated Diet')}",
        "=" * 64,
    ]
    if saved_path is not None:
        lines.append(f"  Saved To           : {saved_path}")
    if prov.get("sources_used"):
        lines.append(f"  Sources Used       : {', '.join(prov['sources_used'])}")
    top_terms = prov.get("top_extracted_terms") or []
    if top_terms:
        formatted_terms = ", ".join(
            f"{t['term']} ({t['weight']:.2f})" for t in top_terms[:10]
        )
        lines.append(f"  Top Terms          : {formatted_terms}")

    anchor_names = [a["name"] if isinstance(a, dict) else str(a) for a in anchors]
    priming_names = [p["name"] if isinstance(p, dict) else str(p) for p in priming]
    lines.append(f"  Tier-1 Anchors ({len(anchor_names)}) : {', '.join(f'r/{n}' for n in anchor_names)}")
    lines.append(f"  Tier-1 Priming ({len(priming_names)}) : {', '.join(f'r/{n}' for n in priming_names)}")

    if feeds:
        lines.append(f"  Tier-2 Custom Feeds ({len(feeds)}):")
        for feed in feeds:
            subs = feed.get("subreddits", [])
            lines.append(
                f"    * {feed.get('feed_name')}: {len(subs)} subs "
                f"({', '.join(f'r/{s}' for s in subs[:6])}{'...' if len(subs) > 6 else ''})"
            )

    diet_health = prov.get("diet_health")
    if isinstance(diet_health, dict):
        lines.append("")
        lines.append(format_audit_summary(diet_health, title="Diet Health Summary"))
    return "\n".join(lines)


def _append_verification_details(
    lines: list[str], ver: dict[str, Any]
) -> None:
    """Append missing subscriptions and custom feed completeness details to summary lines."""
    subs = ver.get("subscriptions", {}) if isinstance(ver, dict) else {}
    missing_anchors = subs.get("missing_anchors") or []
    missing_priming = subs.get("missing_priming") or []
    if missing_anchors:
        lines.append(
            f"  Missing Anchors    : {', '.join(f'r/{s}' for s in missing_anchors)}"
        )
    if missing_priming:
        lines.append(
            f"  Missing Priming    : {', '.join(f'r/{s}' for s in missing_priming)}"
        )

    custom_feeds = ver.get("custom_feeds")
    if isinstance(custom_feeds, dict) and custom_feeds:
        lines.append("  Custom Feeds Check :")
        for fname, finfo in custom_feeds.items():
            if not isinstance(finfo, dict):
                continue
            if not finfo.get("exists", False):
                missing = finfo.get("missing_subreddits") or []
                lines.append(
                    f"    * {fname}: MISSING ({len(missing)} expected)"
                )
            elif not finfo.get("is_complete", False):
                missing = finfo.get("missing_subreddits") or []
                extra = finfo.get("extra_subreddits") or []
                parts: list[str] = []
                if missing:
                    parts.append(f"missing {', '.join(f'r/{s}' for s in missing[:5])}")
                if extra:
                    parts.append(f"extra {', '.join(f'r/{s}' for s in extra[:5])}")
                detail = "; ".join(parts) if parts else "mismatch"
                lines.append(
                    f"    * {fname}: INCOMPLETE ({finfo.get('subreddits_count', 0)}/"
                    f"{finfo.get('expected_count', 0)}; {detail})"
                )
            else:
                lines.append(
                    f"    * {fname}: OK ({finfo.get('subreddits_count', 0)}/"
                    f"{finfo.get('expected_count', 0)})"
                )


def format_run_summary(result: dict[str, Any]) -> str:
    """Format sync, dry-run, or verify execution output for human readers."""
    if result.get("mode") == "offline_dry_run":
        lines = [
            "Offline Dry-Run Preview (Chrome CDP not connected)",
            "=" * 64,
            f"  Manifest           : {result.get('manifest_name')}",
            f"  Feed Mode          : {result.get('feed_mode', 'following')}",
            f"  Desired Anchors    : {len(result.get('desired_anchors', []))} "
            f"({', '.join(f'r/{s}' for s in result.get('desired_anchors', []))})",
            f"  Desired Priming    : {len(result.get('desired_priming', []))} "
            f"({', '.join(f'r/{s}' for s in result.get('desired_priming', []))})",
            f"  Custom Feeds       : {len(result.get('custom_feeds', []))}",
        ]
        if isinstance(result.get("analytics"), dict):
            lines.append("")
            lines.append(format_audit_summary(result["analytics"], title="Manifest Diet Health"))
        return "\n".join(lines)

    if "verification" in result and "username" not in result:
        ver = result["verification"]
        subs = ver.get("subscriptions", {})
        lines = [
            f"Verification Report for u/{result.get('session', 'unknown')}",
            "=" * 64,
            f"  Total Subscribed   : {subs.get('total_subscribed', 0)}",
            f"  Anchors Present    : {subs.get('anchors_present', 0)} / {subs.get('anchors_total', 0)}",
            f"  Priming Present    : {subs.get('priming_present', 0)} / {subs.get('priming_total', 0)}",
            f"  All Healthy        : {ver.get('all_healthy', False)}",
        ]
        _append_verification_details(lines, ver)
        if isinstance(result.get("analytics"), dict):
            lines.append("")
            lines.append(format_audit_summary(result["analytics"], title="Diet Health"))
        return "\n".join(lines)

    ver = result.get("verification")
    subs = ver.get("subscriptions", {}) if isinstance(ver, dict) else {}
    lines = [
        f"Curation Sync Summary for u/{result.get('username', 'unknown')}",
        "=" * 64,
    ]
    if result.get("snapshot_id"):
        lines.append(f"  Pre-Sync Snapshot  : {result['snapshot_id']}")
    if isinstance(ver, dict):
        lines.extend(
            [
                f"  Anchors Active     : {len(result.get('anchors_subscribed', []))} "
                f"({subs.get('anchors_present', 0)}/{subs.get('anchors_total', 0)} verified)",
                f"  Priming Active     : {len(result.get('priming_subscribed', []))} "
                f"({subs.get('priming_present', 0)}/{subs.get('priming_total', 0)} verified)",
                f"  Following Locked   : {result.get('reloaded_following', False)}",
                f"  All Healthy        : {ver.get('all_healthy', False)}",
            ]
        )
    else:
        lines.extend(
            [
                f"  Anchors Active     : {len(result.get('anchors_subscribed', []))}",
                f"  Priming Active     : {len(result.get('priming_subscribed', []))}",
                f"  Following Locked   : {result.get('reloaded_following', False)}",
            ]
        )

    sub_sync = result.get("subscription_sync") or {}
    failed_subs = sub_sync.get("failed") or []
    if failed_subs:
        lines.append(f"  Failed Subs ({len(failed_subs)}):")
        for item in failed_subs:
            if isinstance(item, dict):
                lines.append(
                    f"    * r/{item.get('name', '?')} "
                    f"({item.get('status_code') or 'error'}): {item.get('error', '')}"
                )

    custom_feed = result.get("custom_feed") or {}
    if isinstance(custom_feed, dict):
        failed_feeds = [
            (fname, finfo)
            for fname, finfo in custom_feed.items()
            if isinstance(finfo, dict) and finfo.get("status") == "failed"
        ]
        if failed_feeds:
            lines.append(f"  Failed Feeds ({len(failed_feeds)}):")
            for fname, finfo in failed_feeds:
                lines.append(
                    f"    * {fname} ({finfo.get('http_code') or 'error'}): {finfo.get('error', '')}"
                )

    if isinstance(ver, dict):
        _append_verification_details(lines, ver)

    if isinstance(result.get("analytics"), dict):
        lines.append("")
        lines.append(format_audit_summary(result["analytics"], title="Diet Health"))
    return "\n".join(lines)


def format_rollback_summary(result: dict[str, Any]) -> str:
    """Format snapshot rollback result."""
    lines = [
        f"Snapshot Rollback ({result.get('snapshot_id')}) for u/{result.get('username')}",
        "=" * 64,
        f"  Dry-Run            : {result.get('dry_run', False)}",
        f"  Restored Subs      : {len(result.get('restored_subscriptions', []))}",
        f"  Removed Subs       : {len(result.get('removed_subscriptions', []))}",
        f"  Deleted Feeds      : {len(result.get('deleted_custom_feeds', []))}",
        f"  Success            : {result.get('success', False)}",
    ]
    failed_subs = result.get("failed_subscriptions") or []
    if failed_subs:
        lines.append(f"  Failed Subs ({len(failed_subs)}):")
        for item in failed_subs:
            if isinstance(item, dict):
                lines.append(
                    f"    * r/{item.get('name', '?')} "
                    f"({item.get('status_code') or 'error'}): {item.get('error', '')}"
                )

    restored_feeds = result.get("restored_custom_feeds") or {}
    failed_feeds = result.get("failed_custom_feeds")
    if not isinstance(failed_feeds, dict) and isinstance(restored_feeds, dict):
        failed_feeds = {
            k: v
            for k, v in restored_feeds.items()
            if isinstance(v, dict) and v.get("status") == "failed"
        }
    if isinstance(failed_feeds, dict) and failed_feeds:
        lines.append(f"  Failed Feeds ({len(failed_feeds)}):")
        for fname, finfo in failed_feeds.items():
            lines.append(
                f"    * {fname} ({finfo.get('http_code') or 'error'}): {finfo.get('error', '')}"
            )

    failed_deletions = result.get("failed_deletions") or []
    if failed_deletions:
        lines.append(
            f"  Failed Deletions   : {', '.join(str(x) for x in failed_deletions)}"
        )
    if result.get("error"):
        lines.append(f"  Error              : {result['error']}")
    return "\n".join(lines)


def _list_preset_summaries(presets_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not presets_dir.is_dir():
        return items
    for path in sorted(presets_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items.append(
            {
                "id": path.stem,
                "name": data.get("name", path.stem),
                "description": data.get("description", ""),
                "anchor_count": len(data.get("anchor_subreddits", [])),
                "domain_priming_count": len(data.get("domain_priming_subreddits", [])),
                "custom_feeds_count": len(data.get("custom_feeds", [])),
                "path": str(path),
            }
        )
    return items


def _default_sync_manifest(
    manifest: dict[str, Any],
    dry_run: bool,
    cdp_port: int,
    ontology: OntologyRepository,
    snapshots_dir: Path,
) -> dict[str, Any]:
    """Execute dry-run (online or offline fallback) or live CDP sync for wizard."""
    anchors = [a["name"] if isinstance(a, dict) else str(a) for a in manifest.get("anchor_subreddits", [])]
    priming = [p["name"] if isinstance(p, dict) else str(p) for p in manifest.get("domain_priming_subreddits", [])]
    desired: list[str] = []
    seen: set[str] = set()
    for name in anchors + priming:
        if name and name.lower() not in seen:
            seen.add(name.lower())
            desired.append(name)

    try:
        session = acquire_reddit_session(cdp_port)
    except Exception as exc:
        if not dry_run:
            raise
        audit = FeedHealthAuditor(ontology).audit_manifest(manifest)
        return {
            "mode": "offline_dry_run",
            "dry_run": True,
            "cdp_reachable": False,
            "reason": str(exc),
            "manifest_name": manifest.get("name"),
            "feed_mode": manifest.get("feed_mode", "following"),
            "desired_anchors": anchors,
            "desired_priming": priming,
            "custom_feeds": manifest.get("custom_feeds", []),
            "analytics": audit,
        }

    api = RedditAPIClient(session.access_token, session.username)
    snapshot_id: str | None = None
    if not dry_run:
        live_subs = api.fetch_all_subscriptions()
        live_feeds = api.fetch_custom_feeds()
        snap = SnapshotManager(snapshots_dir).create_snapshot(
            session.username, live_subs, live_feeds, manifest
        )
        snapshot_id = snap["snapshot_id"]

    sub_res = api.sync_subscriptions(desired, dry_run=dry_run)
    feed_res = api.sync_custom_feeds(list(manifest.get("custom_feeds") or []), dry_run=dry_run)
    following_ok = True if dry_run else bool(activate_following_feed(session.ws_url).get("success", False))

    active_low = {s.lower() for s in (sub_res["already_subscribed"] + sub_res["subscribed"])}
    return {
        "mode": "live_dry_run" if dry_run else "live_sync",
        "dry_run": dry_run,
        "username": session.username,
        "snapshot_id": snapshot_id,
        "anchors_subscribed": [a for a in anchors if a.lower() in active_low],
        "priming_subscribed": [p for p in priming if p.lower() in active_low],
        "subscription_sync": sub_res,
        "custom_feed": feed_res,
        "reloaded_following": following_ok,
        "analytics": FeedHealthAuditor(ontology).audit_manifest(manifest),
    }


def _prompt(
    prompt_text: str,
    input_fn: Callable[[str], str],
    out: TextIO,
    default: str | None = None,
) -> str:
    suffix = f" [{default}]" if default is not None else ""
    full_prompt = f"{prompt_text}{suffix}: "
    if input_fn is not input:
        out.write(full_prompt)
        out.flush()
    raw = input_fn(full_prompt)
    cleaned = raw.strip()
    if not cleaned and default is not None:
        return default
    return cleaned


def _filter_presets_with_index(
    presets: Sequence[dict[str, Any]], keyword: str
) -> list[dict[str, Any]]:
    """Filter presets by keyword while preserving their 1-based table index."""
    kw = keyword.strip().lower()
    matched: list[dict[str, Any]] = []
    for idx, p in enumerate(presets, start=1):
        haystack = " ".join(
            [
                str(p.get("id", "")),
                str(p.get("name", "")),
                str(p.get("description", "")),
            ]
        ).lower()
        if not kw or kw in haystack:
            matched.append({**p, "_index": idx})
    return matched


def _save_manifest_with_confirm(
    manifest: dict[str, Any],
    input_fn: Callable[[str], str],
    out: TextIO,
) -> Path | None:
    """Prompt user to save manifest JSON, confirming before overwriting existing files."""
    while True:
        save_raw = _prompt(
            "Save manifest to file path (e.g. manifest.json, or 'skip')",
            input_fn,
            out,
            default="skip",
        )
        if save_raw.lower() in ("skip", "no", "n", ""):
            return None
        candidate = Path(save_raw).expanduser()
        if candidate.exists():
            confirm = _prompt(
                f"File {candidate} already exists. Overwrite? (y/n)",
                input_fn,
                out,
                default="n",
            )
            if confirm.lower() not in ("y", "yes"):
                out.write("  Skipped overwrite; choose another path or 'skip'.\n")
                continue
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return candidate


def _collect_mode2_evidences(
    ontology: OntologyRepository,
    input_fn: Callable[[str], str],
    out: TextIO,
    github_token: str | None,
) -> list[SignalEvidence]:
    """Collect Mode 2 signal evidences with invalid-path re-prompts and zero-evidence guard."""
    while True:
        evidences: list[SignalEvidence] = []

        while True:
            ws_raw = _prompt(
                "Workspace directory path (leave blank to skip)",
                input_fn,
                out,
                default="",
            )
            if not ws_raw:
                break
            ws_path = Path(ws_raw).expanduser()
            if ws_path.is_dir():
                try:
                    evidences.extend(WorkspaceScanner().scan(ws_path))
                    break
                except Exception as exc:
                    out.write(
                        f"  Error scanning workspace {ws_path} ({exc}); try another path or leave blank to skip.\n"
                    )
            else:
                out.write(
                    f"  Workspace directory not found: {ws_path}; enter a valid path or leave blank to skip.\n"
                )

        while True:
            res_raw = _prompt(
                "Resume path (.pdf/.md/.txt/.tex, blank to skip)",
                input_fn,
                out,
                default="",
            )
            if not res_raw:
                break
            res_path = Path(res_raw).expanduser()
            if res_path.is_file():
                try:
                    evidences.extend(ResumeParser().parse_file(res_path))
                    break
                except Exception as exc:
                    out.write(
                        f"  Error parsing resume {res_path} ({exc}); try another path or leave blank to skip.\n"
                    )
            else:
                out.write(
                    f"  Resume file not found: {res_path}; enter a valid path or leave blank to skip.\n"
                )

        gh_raw = _prompt(
            "GitHub username (leave blank to skip)", input_fn, out, default=""
        )
        if gh_raw:
            try:
                evidences.extend(GitHubMiner(token=github_token).mine(gh_raw))
            except Exception as exc:
                out.write(
                    f"  Warning: GitHub mining failed ({exc}); continuing with other sources.\n"
                )

        kw_raw = _prompt(
            "Technical keywords/query (leave blank to skip)", input_fn, out, default=""
        )
        if kw_raw:
            tokenizer = TechnicalTokenizer(known_phrases=ontology.known_phrases)
            for tok, w in tokenizer.tokenize(kw_raw):
                evidences.append(
                    SignalEvidence(
                        term=tok,
                        raw_term=tok,
                        weight=float(w) * 2.2,
                        source_type="query",
                        source_detail="wizard_query",
                        signal_kind="keyword",
                    )
                )

        if evidences:
            return evidences

        retry = _prompt(
            "No curation signals collected from any source. Re-enter sources? (y/n)",
            input_fn,
            out,
            default="y",
        )
        if retry.lower() in ("n", "no"):
            return evidences


def run_wizard(
    *,
    input_fn: Callable[[str], str] = input,
    output_stream: TextIO | None = None,
    presets_dir: Path | None = None,
    ontology_path: Path | None = None,
    snapshots_dir: Path | None = None,
    cdp_port: int = 9222,
    github_token: str | None = None,
    sync_callback: Callable[[dict[str, Any], bool, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the interactive step-by-step terminal curation wizard."""
    out = output_stream or sys.stdout
    pdir = Path(presets_dir) if presets_dir is not None else DEFAULT_PRESETS_DIR
    opath = Path(ontology_path) if ontology_path is not None else DEFAULT_ONTOLOGY_PATH
    sdir = Path(snapshots_dir) if snapshots_dir is not None else Path(".curator_snapshots")
    ontology = OntologyRepository.load(opath)
    presets = _list_preset_summaries(pdir)

    try:
        out.write("\nReddit Cognitive Curator v2.0 — Interactive Terminal Wizard\n")
        out.write("=" * 64 + "\n")
        out.write("Choose curation strategy:\n")
        out.write(f"  [1] Curated Domain Preset ({len(presets)} built-in presets)\n")
        out.write("  [2] Multi-Source Auto-Curate (Workspace / Resume / GitHub / Keywords)\n")
        out.write("  [3] Convex Preset Mixer (Blend 2+ presets with weights)\n")

        mode = ""
        default_mode = "1" if presets else "2"
        while mode not in ("1", "2", "3"):
            mode = _prompt("Select mode (1/2/3)", input_fn, out, default=default_mode)
            if mode not in ("1", "2", "3"):
                out.write("  Please enter 1, 2, or 3.\n")
                continue
            if mode in ("1", "3") and not presets:
                out.write(
                    f"  No presets found in {pdir}; select [2] Auto-Curate.\n"
                )
                mode = ""

        # Step 2: Golden Ratio & Tier-1 count
        ratio = (40, 35, 25)
        while True:
            ratio_raw = _prompt(
                "Golden Ratio % (aesthetic,cognitive,domain)",
                input_fn,
                out,
                default="40,35,25",
            )
            try:
                ratio = _parse_ratio_input(ratio_raw)
                break
            except ValueError as exc:
                out.write(f"  Invalid ratio: {exc}\n")

        tier1_count = 18
        if mode in ("2", "3"):
            while True:
                k_raw = _prompt(
                    "Target Tier-1 Following count (>=3)",
                    input_fn,
                    out,
                    default="18",
                )
                try:
                    k_val = int(k_raw)
                    if k_val < 3:
                        raise ValueError("Must be >= 3.")
                    tier1_count = k_val
                    break
                except ValueError as exc:
                    out.write(f"  Invalid count: {exc}\n")

        manifest: dict[str, Any]
        if mode == "1":
            if not presets:
                raise FileNotFoundError(f"No presets found in {pdir}.")
            out.write("\n" + format_presets_table(presets) + "\n")
            by_id = {p["id"].lower(): p for p in presets}
            while True:
                sel = _prompt(
                    f"Enter preset key, number (1-{len(presets)}), or ?keyword to filter",
                    input_fn,
                    out,
                    default=presets[0]["id"],
                )
                if sel.startswith("?"):
                    filtered = _filter_presets_with_index(presets, sel[1:])
                    if filtered:
                        out.write("\n" + format_presets_table(filtered) + "\n")
                    else:
                        out.write(f"  No presets matched {sel[1:].strip()!r}.\n")
                    continue
                if sel.isdigit() and 1 <= int(sel) <= len(presets):
                    chosen = presets[int(sel) - 1]
                    break
                if sel.lower() in by_id:
                    chosen = by_id[sel.lower()]
                    break
                out.write(f"  Unknown preset {sel!r}; try again.\n")
            raw_manifest = json.loads(Path(chosen["path"]).read_text(encoding="utf-8"))
            manifest = normalize_manifest(raw_manifest, ontology)
            manifest["golden_ratio_distribution"] = {
                "aesthetic_leisure_target_pct": ratio[0],
                "cognitive_curiosity_target_pct": ratio[1],
                "domain_priming_target_pct": ratio[2],
            }
            audit = FeedHealthAuditor(ontology).audit_manifest(manifest)
            manifest.setdefault("curation_provenance", {})["diet_health"] = audit

        elif mode == "2":
            evidences = _collect_mode2_evidences(ontology, input_fn, out, github_token)
            manifest = AutoPresetGenerator(ontology).generate(
                evidences,
                name="Wizard Auto-Generated Curation",
                target_ratio=ratio,
                tier1_count=tier1_count,
            )

        else:
            if not presets:
                raise FileNotFoundError(f"No presets found in {pdir}.")
            out.write("\n" + format_presets_table(presets) + "\n")
            by_id = {p["id"].lower(): p for p in presets}
            default_mix = (
                "systems_engineering:0.6,rust_and_compilers:0.4"
                if "systems_engineering" in by_id and "rust_and_compilers" in by_id
                else f"{presets[0]['id']}:1.0"
            )
            weights: dict[str, float] = {}
            while not weights:
                mix_raw = _prompt(
                    "Preset mix spec (key:weight or #:weight, e.g. 1:0.6,16:0.4; ?keyword to filter)",
                    input_fn,
                    out,
                    default=default_mix,
                )
                if mix_raw.startswith("?"):
                    filtered = _filter_presets_with_index(presets, mix_raw[1:])
                    if filtered:
                        out.write("\n" + format_presets_table(filtered) + "\n")
                    else:
                        out.write(f"  No presets matched {mix_raw[1:].strip()!r}.\n")
                    continue
                try:
                    parsed: dict[str, float] = {}
                    for chunk in mix_raw.split(","):
                        if ":" not in chunk:
                            raise ValueError(f"Expected preset:weight in {chunk!r}")
                        raw_ref, pw = chunk.split(":", 1)
                        ref = raw_ref.strip()
                        if ref.isdigit():
                            idx = int(ref)
                            if not (1 <= idx <= len(presets)):
                                raise ValueError(
                                    f"Preset index {idx} out of range (1-{len(presets)})."
                                )
                            pname = presets[idx - 1]["id"]
                        elif ref.lower() in by_id:
                            pname = by_id[ref.lower()]["id"]
                        else:
                            pname = ref
                        w_val = float(pw.strip())
                        if w_val <= 0:
                            raise ValueError("Weights must be positive.")
                        parsed[pname] = parsed.get(pname, 0.0) + w_val
                    manifest = PresetMixer(ontology, pdir).mix(
                        parsed, target_ratio=ratio, tier1_count=tier1_count
                    )
                    weights = parsed
                except Exception as exc:
                    out.write(f"  Invalid mix spec: {exc}\n")

        # Step 3: Save manifest optionally
        saved_path = _save_manifest_with_confirm(manifest, input_fn, out)

        out.write("\n" + format_manifest_summary(manifest, saved_path=saved_path) + "\n\n")

        # Step 4: Dry-Run / Live Sync / Exit
        out.write("Next action:\n")
        out.write("  [1] Preview Dry-Run (auto-falls back to offline preview if CDP closed)\n")
        out.write("  [2] Execute Live CDP Sync (--one-shot)\n")
        out.write("  [3] Exit\n")

        action = ""
        while action not in ("1", "2", "3"):
            action = _prompt("Select action (1/2/3)", input_fn, out, default="3")
            if action not in ("1", "2", "3"):
                out.write("  Please enter 1, 2, or 3.\n")

        action_name = {"1": "dry_run", "2": "live_sync", "3": "exit"}[action]
        sync_result: dict[str, Any] | None = None
        if action in ("1", "2"):
            is_dry = action == "1"
            runner = sync_callback or (
                lambda m, dr, port: _default_sync_manifest(m, dr, port, ontology, sdir)
            )
            try:
                sync_result = runner(manifest, is_dry, cdp_port)
                out.write("\n" + format_run_summary(sync_result) + "\n")
            except (CDPError, ConnectionError, OSError, RuntimeError) as exc:
                if is_dry:
                    raise
                out.write(f"\n  Live CDP sync unavailable: {exc}\n")
                out.write("  Fallback options (your manifest is preserved):\n")
                out.write("    [1] Run offline dry-run preview\n")
                out.write("    [2] Save manifest to file\n")
                out.write("    [3] Exit\n")
                fb = ""
                while fb not in ("1", "2", "3"):
                    fb = _prompt("Select fallback (1/2/3)", input_fn, out, default="1")
                if fb == "1":
                    action_name = "dry_run"
                    sync_result = runner(manifest, True, cdp_port)
                    out.write("\n" + format_run_summary(sync_result) + "\n")
                elif fb == "2":
                    action_name = "save"
                    new_saved = _save_manifest_with_confirm(manifest, input_fn, out)
                    if new_saved is not None:
                        saved_path = new_saved
                else:
                    action_name = "exit"

        return {
            "mode": mode,
            "saved_path": str(saved_path) if saved_path is not None else None,
            "manifest": manifest,
            "action": action_name,
            "sync_result": sync_result,
        }
    except (KeyboardInterrupt, EOFError):
        out.write("\nWizard cancelled.\n")
        return {
            "mode": None,
            "saved_path": None,
            "manifest": None,
            "action": "cancelled",
            "sync_result": None,
        }
