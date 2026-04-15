import sys
import argparse
from dotenv import load_dotenv
load_dotenv()


def cmd_watch(args):
    """Start the file watcher on a directory."""
    from watcher import watch
    watch(args.path)


def cmd_scan(args):
    """One-time scan of a directory — parse, analyze, run pipeline."""
    from crawler import crawl
    from orchestrator import run_pipeline
    import os

    print(f"Scanning: {args.path}")
    crawl(args.path)

    for root, dirs, files in os.walk(args.path):
        dirs[:] = [d for d in dirs
                   if not d.startswith('.')
                   and d != 'venv'
                   and d != 'generated_tests']
        for file in files:
            if file.endswith('.py') and not file.startswith('test_'):
                filepath = os.path.join(root, file)
                run_pipeline(filepath)


def cmd_log(args):
    """Display the audit ledger in a formatted table."""
    from ledger import query_log, query_by_entity

    if args.entity:
        rows = query_by_entity(args.entity)
        print(f"\nAudit log for entity: '{args.entity}'")
    else:
        rows = query_log(limit=args.limit)
        print(f"\nAudit log (last {args.limit} entries)")

    if not rows:
        print("  No entries found.")
        return

    print("─" * 80)
    for row in rows:
        status = "✓PASS" if row.validation_passed else "✗ FAIL"
        complexity = f"{row.complexity_before} → {row.complexity_after}" \
                     if row.complexity_before else "bug fix"
        print(f"{status}  {row.timestamp.strftime('%Y-%m-%d %H:%M')}  "
              f"{row.affected_file}")
        print(f"       {row.issue_type} | {complexity} | "
              f"tests: {row.tests_passed}/{row.tests_run} | "
              f"retries: {row.retry_count}")
        if hasattr(row, 'entities_changed') and row.entities_changed:
            import json
            try:
                entities = json.loads(row.entities_changed) \
                    if isinstance(row.entities_changed, str) \
                    else row.entities_changed
                print(f"       entities: {', '.join(entities)}")
            except Exception:
                pass
        print("─" * 80)


def cmd_status(args):
    """Show current state of the Structure DB."""
    from db import get_driver
    driver = get_driver()

    with driver.session() as session:
        result = session.run("""
            MATCH (n)
            WHERE n.complexity_score IS NOT NULL
            RETURN labels(n)[0] AS type,
                   count(n) AS count,
                   sum(CASE WHEN n.is_buggy = true THEN 1 ELSE 0 END) AS buggy,
                   sum(CASE WHEN n.complexity_score > 4 THEN 1 ELSE 0 END) AS high_complexity
        """)
        rows = list(result)

    print("\nStructure DB Status")
    print("─" * 40)
    total_buggy = 0
    total_complex = 0
    for row in rows:
        print(f"  {row['type']:<12} {row['count']:>4} nodes  "
              f"buggy: {row['buggy']}  high complexity: {row['high_complexity']}")
        total_buggy += row['buggy']
        total_complex += row['high_complexity']
    print("─" * 40)
    print(f"  Total flagged: {total_buggy} buggy, {total_complex} high complexity")


def main():
    parser = argparse.ArgumentParser(
        prog="proactive-agent",
        description="Proactive coding agent — autonomous code optimization"
    )
    subparsers = parser.add_subparsers(dest="command")

    # watch command
    watch_parser = subparsers.add_parser("watch", help="Watch a directory")
    watch_parser.add_argument("path", help="Directory to watch")

    # scan command
    scan_parser = subparsers.add_parser("scan", help="One-time scan")
    scan_parser.add_argument("path", help="Directory to scan")

    # log command
    log_parser = subparsers.add_parser("log", help="Show audit log")
    log_parser.add_argument("--limit", type=int, default=20,
                            help="Number of entries to show")
    log_parser.add_argument("--entity", type=str, default=None,
                            help="Filter by function/entity name")

    # status command
    subparsers.add_parser("status", help="Show Structure DB status")

    args = parser.parse_args()

    if args.command == "watch":
        cmd_watch(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "log":
        cmd_log(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()