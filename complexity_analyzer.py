import re
import json
import tree_sitter_python as tspython
from tree_sitter import Language, Parser
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI()

PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

COMPLEXITY_SCORE = {
    "O(1)":       1,
    "O(log n)":   2,
    "O(n)":       3,
    "O(n log n)": 4,
    "O(n\u00b2)": 5,
    "O(n\u00b3)": 6,
    "O(2^n)":     7,
    "unknown":    0
}

# builtins that iterate over a collection -> O(n)
LINEAR_BUILTINS = {
    "set", "list", "dict", "tuple", "frozenset",
    "sum", "max", "min", "any", "all",
    "reversed", "enumerate", "zip",
    "map", "filter", "Counter", "deque",
    "heapify", "join"
}

# builtins that are O(n log n)
NLOGN_BUILTINS = {
    "sorted", "nlargest", "nsmallest"
}


def get_text(source, node):
    return source[node.start_byte:node.end_byte].decode("utf-8")


def walk_tree(node):
    yield node
    for child in node.children:
        yield from walk_tree(child)


def get_for_iterable(for_node):
    found_in = False
    for child in for_node.children:
        if child.type == "in":
            found_in = True
            continue
        if found_in and child.type not in ("comment",):
            return child
    return None


def is_constant_for_loop(source, for_node):
    iterable = get_for_iterable(for_node)
    if iterable is None or iterable.type != "call":
        return False
    func = iterable.child_by_field_name("function")
    if not func or get_text(source, func) != "range":
        return False
    args = iterable.child_by_field_name("arguments")
    if not args:
        return False
    arg_nodes = [c for c in args.children if c.is_named]
    if not arg_nodes:
        return False
    return all(a.type == "integer" for a in arg_nodes)


def count_loop_depth(node, current_depth=0):
    max_depth = current_depth
    if node.type in (
        "for_statement", "while_statement",
        "list_comprehension", "set_comprehension",
        "dictionary_comprehension", "generator_expression"
    ):
        current_depth += 1
        max_depth = current_depth
    for child in node.children:
        child_depth = count_loop_depth(child, current_depth)
        max_depth = max(max_depth, child_depth)
    return max_depth


def count_input_dependent_loop_depth(source, node, current_depth=0):
    max_depth = current_depth
    if node.type == "for_statement":
        if not is_constant_for_loop(source, node):
            current_depth += 1
            max_depth = current_depth
    elif node.type in (
        "while_statement",
        "list_comprehension", "set_comprehension",
        "dictionary_comprehension", "generator_expression"
    ):
        current_depth += 1
        max_depth = current_depth
    for child in node.children:
        child_depth = count_input_dependent_loop_depth(source, child, current_depth)
        max_depth = max(max_depth, child_depth)
    return max_depth


def has_recursion(source, node, function_name):
    if node.type == "call":
        func_node = node.child_by_field_name("function")
        if func_node and func_node.type == "identifier":
            if get_text(source, func_node) == function_name:
                return True
    for child in node.children:
        if has_recursion(source, child, function_name):
            return True
    return False


def has_while_loop(node):
    if node.type == "while_statement":
        return True
    for child in node.children:
        if has_while_loop(child):
            return True
    return False


def has_break(node):
    if node.type == "break_statement":
        return True
    for child in node.children:
        if has_break(child):
            return True
    return False


def calls_sorting(source, node):
    if node.type == "call":
        func_node = node.child_by_field_name("function")
        if func_node:
            call_text = get_text(source, func_node)
            if "sorted" in call_text or ".sort" in call_text:
                return True
            if func_node.type == "attribute":
                attr = func_node.child_by_field_name("attribute")
                if attr and get_text(source, attr) in NLOGN_BUILTINS:
                    return True
    for child in node.children:
        if calls_sorting(source, child):
            return True
    return False


def calls_linear_builtin(source, node):
    """
    Check if function calls any O(n) builtins like set(), list(),
    sum(), max(), min(), Counter(), etc.
    Only counts calls at the TOP LEVEL of the function body
    (not inside loops — those are handled by loop depth rules).
    """
    if node.type == "call":
        func_node = node.child_by_field_name("function")
        if func_node:
            # direct call: set(), sum(), list()
            if func_node.type == "identifier":
                if get_text(source, func_node) in LINEAR_BUILTINS:
                    return True
            # method call: "".join(), heapq.heapify(), collections.Counter()
            elif func_node.type == "attribute":
                attr = func_node.child_by_field_name("attribute")
                if attr and get_text(source, attr) in LINEAR_BUILTINS:
                    return True
    for child in node.children:
        if calls_linear_builtin(source, child):
            return True
    return False


def layer1_estimate(source, func_node, function_name):
    """
    Rule-based complexity estimation.
    Returns complexity string or None (send to Layer 2).

    Rules in priority order:
    1.  Recursion                           -> None (Layer 2)
    2.  While loops                         -> None (Layer 2)
    3.  Nested input-dep + break            -> None (Layer 2)
    4.  effective_depth >= 3                -> O(n³)
    5.  effective_depth == 2                -> O(n²)
    6.  effective_depth == 1 + sort         -> O(n log n)
    7.  effective_depth == 0 + sort         -> O(n log n)
    8.  effective_depth == 1                -> O(n)
    9.  effective_depth == 0 + linear call  -> O(n)   
    10. effective_depth == 0                -> O(1)
    """
    effective_depth  = count_input_dependent_loop_depth(source, func_node)
    is_rec           = has_recursion(source, func_node, function_name)
    uses_sort        = calls_sorting(source, func_node)
    has_while        = has_while_loop(func_node)
    has_brk          = has_break(func_node)
    has_linear_call  = calls_linear_builtin(source, func_node)

    if is_rec:
        return None

    if has_while:
        return None

    if effective_depth >= 2 and has_brk:
        return None

    if effective_depth >= 3:
        return "O(n\u00b3)"

    if effective_depth == 2:
        return "O(n\u00b2)"

    if effective_depth == 1 and uses_sort:
        return "O(n log n)"

    if effective_depth == 0 and uses_sort:
        return "O(n log n)"

    if effective_depth == 1:
        return "O(n)"

    # no loops but has a linear builtin call -> O(n)
    if effective_depth == 0 and has_linear_call:
        return "O(n)"

    return "O(1)"


def layer2_estimate(source_code, function_name):
    prompt = f"""You are an expert algorithm analyst.

Analyse the time complexity of this Python function: '{function_name}'

SOURCE CODE:
{source_code}

Respond ONLY with a JSON object — no markdown, no backticks, no preamble:
{{
    "complexity": "O(?)",
    "reason": "one sentence explanation"
}}

Use standard Big-O notation: O(1), O(log n), O(n), O(n log n), O(n^2), O(n^3), O(2^n)
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=200,
        temperature=0,
        messages=[{"role": "user", "content": prompt}]
    )
    text = re.sub(r'```json|```', '', response.choices[0].message.content).strip()
    try:
        result = json.loads(text)
        return result.get("complexity", "unknown")
    except Exception:
        return "unknown"


def analyze_function_complexity(filepath, function_name, start_line, end_line):
    try:
        with open(filepath, "rb") as f:
            source = f.read()

        tree = parser.parse(source)
        root = tree.root_node

        target_node = None
        for node in walk_tree(root):
            if node.type == "function_definition":
                if node.start_point[0] + 1 == start_line:
                    target_node = node
                    break

        if not target_node:
            return {"complexity": "unknown", "complexity_score": 0, "method": "not_found"}

        func_source = source[target_node.start_byte:target_node.end_byte].decode("utf-8")

        complexity = layer1_estimate(source, target_node, function_name)
        if complexity:
            return {
                "complexity": complexity,
                "complexity_score": COMPLEXITY_SCORE.get(complexity, 0),
                "method": "static_analysis"
            }

        complexity = layer2_estimate(func_source, function_name)
        return {
            "complexity": complexity,
            "complexity_score": COMPLEXITY_SCORE.get(complexity, 0),
            "method": "llm_analysis"
        }

    except Exception as e:
        return {"complexity": "unknown", "complexity_score": 0, "method": f"error: {e}"}


def analyze_file_complexity(filepath):
    try:
        with open(filepath, "rb") as f:
            source = f.read()

        tree = parser.parse(source)
        root = tree.root_node
        results = []

        for node in walk_tree(root):
            if node.type == "function_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name       = get_text(source, name_node)
                    start_line = node.start_point[0] + 1
                    end_line   = node.end_point[0] + 1
                    result     = analyze_function_complexity(filepath, name, start_line, end_line)
                    results.append({"name": name, "start_line": start_line, "end_line": end_line, **result})

        return results

    except Exception as e:
        print(f"  [Complexity] Error analyzing {filepath}: {e}")
        return []


def update_complexity_in_graph(filepath):
    from db import get_driver
    driver = get_driver()

    print(f"  [Complexity] Analyzing: {filepath}")
    results = analyze_file_complexity(filepath)

    with driver.session() as session:
        for fn in results:
            session.run("""
                MATCH (n)
                WHERE n.name = $name AND n.file = $filepath
                SET n.complexity = $complexity,
                    n.complexity_score = $score
            """,
            name=fn["name"],
            filepath=filepath,
            complexity=fn["complexity"],
            score=fn["complexity_score"])
            print(f"    {fn['name']}: {fn['complexity']} ({fn['method']})")

    return results