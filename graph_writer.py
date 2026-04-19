from db import get_driver
import os

def write_file_node(tx, filepath):
    tx.run("""
        MERGE (f:File {path: $path})
        SET f.name = $name
    """, path=filepath,
         name=filepath.split("/")[-1])

def write_class(tx, cls, filepath):
    tx.run("""
        MERGE (c:Class {name: $name, file: $file})
        SET c.start_line = $start_line,
            c.end_line = $end_line,
            c.is_buggy = false
    """, name=cls["name"],
         file=filepath,
         start_line=cls["start_line"],
         end_line=cls["end_line"])

def write_method(tx, method, class_name, filepath):
    tx.run("""
        MERGE (m:Method {name: $name, class: $class_name, file: $file})
        SET m.start_line = $start_line,
            m.end_line = $end_line,
            m.params = $params,
            m.is_buggy = false,
            m.complexity = $complexity
    """, name=method["name"],
         class_name=class_name,
         file=filepath,
         start_line=method["start_line"],
         end_line=method["end_line"],
         params=method["params"],
         complexity="unknown")

def write_function(tx, fn, filepath):
    tx.run("""
        MERGE (f:Function {name: $name, file: $file})
        SET f.start_line = $start_line,
            f.end_line = $end_line,
            f.params = $params,
            f.is_buggy = false,
            f.complexity = $complexity
    """, name=fn["name"],
         file=filepath,
         start_line=fn["start_line"],
         end_line=fn["end_line"],
         params=fn["params"],
         complexity="unknown")

def write_inherits(tx, child_class, parent_class, filepath):
    tx.run("""
        MATCH (child:Class {name: $child, file: $file})
        MATCH (parent:Class {name: $parent})
        MERGE (child)-[:INHERITS]->(parent)
    """, child=child_class,
         parent=parent_class,
         file=filepath)

def write_has_member(tx, class_name, method_name, filepath):
    tx.run("""
        MATCH (c:Class {name: $class_name, file: $file})
        MATCH (m:Method {name: $method_name, class: $class_name, file: $file})
        MERGE (c)-[:HAS_MEMBER]->(m)
    """, class_name=class_name,
         method_name=method_name,
         file=filepath)

def write_calls(tx, caller_name, caller_class, callee_name, filepath):
    tx.run("""
        MATCH (caller:Method {name: $caller, class: $caller_class, file: $file})
        MATCH (callee:Method {name: $callee})
        MERGE (caller)-[:CALLS]->(callee)
    """, caller=caller_name,
         caller_class=caller_class,
         callee=callee_name,
         file=filepath)

def write_file_contains(tx, filepath, entity_name, entity_type):
    tx.run(f"""
        MATCH (f:File {{path: $filepath}})
        MATCH (e:{entity_type} {{name: $name, file: $filepath}})
        MERGE (f)-[:CONTAINS]->(e)
    """, filepath=filepath, name=entity_name)
def cleanup_stale_nodes(filepath: str, current_function_names: list):
    """
    Remove Neo4j nodes for functions that no longer exist in the file.
    Called by crawler after parsing to keep graph in sync.
    """
    driver = get_driver()
    with driver.session() as session:
        result = session.run("""
            MATCH (n)
            WHERE n.file = $filepath
            AND (n:Function OR n:Method)
            RETURN n.name AS name
        """, filepath=filepath)

        graph_names = {row['name'] for row in result}
        current_names = set(current_function_names)
        stale_names = graph_names - current_names

        if stale_names:
            print(f"  [Graph] Removing stale nodes: {stale_names}")
            for name in stale_names:
                session.run("""
                    MATCH (n)
                    WHERE n.file = $filepath AND n.name = $name
                    DETACH DELETE n
                """, filepath=filepath, name=name)

def write_graph(parsed_data):
    driver = get_driver()
    filepath = parsed_data["filepath"]
    filepath = parsed_data.get('filepath', '')
    if 'generated_tests' in filepath:
        return
    if os.path.basename(filepath).startswith('test_'):
        return

    if parsed_data["error"]:
        print(f"  Skipping {filepath} — parse error: {parsed_data['error']}")
        return

    with driver.session() as session:

        # pass 1 — all nodes
        session.execute_write(write_file_node, filepath)

        for cls in parsed_data["classes"]:
            session.execute_write(write_class, cls, filepath)
            for method in cls["methods"]:
                session.execute_write(write_method, method, cls["name"], filepath)

        for fn in parsed_data["functions"]:
            session.execute_write(write_function, fn, filepath)

        # pass 2 — all relationships
        for cls in parsed_data["classes"]:
            for parent in cls["superclasses"]:
                session.execute_write(write_inherits, cls["name"], parent, filepath)
            for method in cls["methods"]:
                session.execute_write(write_has_member, cls["name"], method["name"], filepath)
                session.execute_write(write_file_contains, filepath, cls["name"], "Class")
                for call in method["calls"]:
                    session.execute_write(write_calls, method["name"], cls["name"], call, filepath)

        for fn in parsed_data["functions"]:
            session.execute_write(write_file_contains, filepath, fn["name"], "Function")

    print(f"  Written: {filepath} — {len(parsed_data['classes'])} classes, {len(parsed_data['functions'])} functions")
    current_names = (
        [f['name'] for f in parsed_data['functions']] +
        [m['name'] for cls in parsed_data['classes']
         for m in cls['methods']]
    )
    cleanup_stale_nodes(parsed_data['filepath'], current_names)