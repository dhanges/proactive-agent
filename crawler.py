import os
from parser import parse_file
from graph_writer import write_graph
from db import close_driver
from test_generator import generate_tests_for_repo

def find_python_files(directory):
    python_files = []
    for root, dirs, files in os.walk(directory):
        # skip hidden folders and venv
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != 'venv']
        for file in files:
            if file.endswith('.py'):
                full_path = os.path.join(root, file)
                python_files.append(full_path)
    return python_files

def crawl(directory):
    print(f"Crawling: {directory}")
    print("─" * 40)

    files = find_python_files(directory)
    print(f"Found {len(files)} Python files\n")

    parsed_results = []

    # pass 1 — parse all files first
    print("Phase 1: Parsing all files...")
    for filepath in files:
        result = parse_file(filepath)
        parsed_results.append(result)
        if result["error"]:
            print(f"  ERROR: {filepath} — {result['error']}")
        else:
            print(f"  Parsed: {filepath} — {len(result['classes'])} classes, {len(result['functions'])} functions")

    # pass 2 — write all nodes and relationships
    print("\nPhase 2: Writing to Neo4j...")
    for result in parsed_results:
        write_graph(result)

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