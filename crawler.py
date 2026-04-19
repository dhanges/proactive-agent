import os
from parser import parse_file
from graph_writer import write_graph
from db import close_driver
from test_generator import generate_tests_for_repo


def find_python_files(directory):
    python_files = []
    for root, dirs, files in os.walk(directory):
        # skip hidden folders, venv, generated tests, and pycache
        dirs[:] = [d for d in dirs
                   if not d.startswith('.')
                   and d not in ('venv', 'generated_tests', '__pycache__')]
        for file in files:
            # skip test files
            if file.endswith('.py') and not file.startswith('test_'):
                full_path = os.path.join(root, file)
                python_files.append(full_path)
    return python_files


def crawl(directory):
    print(f"Crawling: {directory}")
    print("─" * 40)
    print("Phase 0: Clearing existing graph...")
    from db import get_driver
    driver = get_driver()
    with driver.session() as session:
        count_result = session.run("""
        MATCH (n)
        WHERE n.file STARTS WITH $directory
        RETURN count(n) AS total
    """, directory=directory)
        total = count_result.single()["total"]
    
        session.run("""
        MATCH (n)
        WHERE n.file STARTS WITH $directory
        DETACH DELETE n
    """, directory=directory)
    print(f"  Cleared {total} existing nodes.")
    files = find_python_files(directory)
    print(f"Found {len(files)} Python files\n")
    parsed_results = []

    # Phase 1 — parse all files first(pass2)
    print("Phase 1: Parsing all files...")
    for filepath in files:
        result = parse_file(filepath)
        parsed_results.append(result)
        if result["error"]:
            print(f"  ERROR: {filepath} — {result['error']}")
        else:
            print(f"  Parsed: {filepath} — "
                  f"{len(result['classes'])} classes, "
                  f"{len(result['functions'])} functions")

    # write all nodes and relationship(pass1)
    print("\nPhase 2: Writing to Neo4j...")
    for result in parsed_results:
        write_graph(result)

    print("\nPhase 3: Analysing complexity...")
    from complexity_analyzer import update_complexity_in_graph
    for filepath in [r["filepath"] for r in parsed_results if not r["error"]]:
        update_complexity_in_graph(filepath)
    print("\n─" * 40)
    print(f"Done. {len(parsed_results)} files written to Structure DB.")

    print("\nPhase 4: Generating tests...")
    generate_tests_for_repo(directory)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python crawler.py <directory>")
        sys.exit(1)
    crawl(sys.argv[1])