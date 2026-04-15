import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

def get_text(source, node):
    return source[node.start_byte:node.end_byte].decode("utf-8")

def find_calls(source, node, calls=None):
    if calls is None:
        calls = []
    if node.type == "call":
        func_node = node.child_by_field_name("function")
        if func_node:
            if func_node.type == "attribute":
                attr = func_node.child_by_field_name("attribute")
                if attr:
                    calls.append(get_text(source, attr))
            elif func_node.type == "identifier":
                calls.append(get_text(source, func_node))
        return calls
    for child in node.children:
        find_calls(source, child, calls)
    return calls

def extract_imports(source, root):
    imports = []
    for child in root.children:
        if child.type in ("import_statement", "import_from_statement"):
            imports.append(get_text(source, child))
    return imports

def extract_functions(source, root):
    functions = []
    for child in root.children:
        if child.type == "function_definition":
            name_node = child.child_by_field_name("name")
            params_node = child.child_by_field_name("parameters")
            functions.append({
                "name": get_text(source, name_node),
                "params": [
                    get_text(source, p) for p in params_node.children
                    if p.is_named and p.type == "identifier"
                ],
                "calls": find_calls(source, child),
                "start_line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
            })
    return functions

def extract_classes(source, root):
    classes = []
    for child in root.children:
        if child.type == "class_definition":
            name_node = child.child_by_field_name("name")
            class_name = get_text(source, name_node)

            superclasses = []
            args_node = child.child_by_field_name("superclasses")
            if args_node:
                for arg in args_node.children:
                    if arg.is_named and arg.type == "identifier":
                        superclasses.append(get_text(source, arg))

            methods = []
            body = child.child_by_field_name("body")
            for item in body.children:
                if item.type == "function_definition":
                    method_name_node = item.child_by_field_name("name")
                    method_params_node = item.child_by_field_name("parameters")
                    methods.append({
                        "name": get_text(source, method_name_node),
                        "params": [
                            get_text(source, p) for p in method_params_node.children
                            if p.is_named and p.type == "identifier"
                        ],
                        "calls": find_calls(source, item),
                        "start_line": item.start_point[0] + 1,
                        "end_line": item.end_point[0] + 1,
                    })

            classes.append({
                "name": class_name,
                "superclasses": superclasses,
                "methods": methods,
                "start_line": child.start_point[0] + 1,
                "end_line": child.end_point[0] + 1,
            })
    return classes

def parse_file(filepath):
    try:
        with open(filepath, "rb") as f:
            source = f.read()

        tree = parser.parse(source)
        root = tree.root_node

        return {
            "filepath": filepath,
            "imports": extract_imports(source, root),
            "functions": extract_functions(source, root),
            "classes": extract_classes(source, root),
            "error": None
        }

    except Exception as e:
        return {
            "filepath": filepath,
            "imports": [],
            "functions": [],
            "classes": [],
            "error": str(e)
        }