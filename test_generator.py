import os
import re
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI()

TESTS_DIR = "generated_tests"


def get_test_file(filepath: str) -> str:
    """
    Returns the absolute path for the generated test file.
    """
    stem = os.path.basename(filepath).replace(".py", "")
   
    project_root = os.path.dirname(os.path.abspath(__file__))
    tests_dir = os.path.join(project_root, TESTS_DIR)
    return os.path.join(tests_dir, f"test_{stem}.py")


def generate_tests_for_file(filepath: str) -> str:
    project_root = os.path.dirname(os.path.abspath(__file__))
    tests_dir_abs = os.path.join(project_root, TESTS_DIR)
    os.makedirs(tests_dir_abs, exist_ok=True)

    with open(filepath, "r") as f:
        source_code = f.read()

    filename  = os.path.basename(filepath)
    stem      = os.path.basename(filepath).replace(".py", "")
    test_path = os.path.join(tests_dir_abs, f"test_{stem}.py")
    print(f"  [TestGen] Generating tests for: {filepath}")

    prompt = f"""You are an expert Python test engineer.

Write comprehensive pytest test cases for ALL functions and methods in this file.

FILE: {filename}

SOURCE CODE:
{source_code}

Requirements:
1. Write 2-3 test cases per function/method
2. Cover: normal input, edge cases (empty, zero, None where applicable)
3. For methods, instantiate the class properly before testing
4. Import correctly from '{stem}' module
5. Add this at the top for imports to work:
   import sys, os
   sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
6. Each test function must start with 'test_'
7. Do not use any external libraries beyond pytest and standard library
8. Do not test private methods (starting with _)
9. Keep tests simple and focused — one assertion per test where possible

Respond ONLY with valid Python test code.
No markdown, no backticks, no explanation whatsoever.
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2000,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}]
    )

    test_code = response.choices[0].message.content
    test_code = re.sub(r'```python|```', '', test_code).strip()

    with open(test_path, "w") as f:
        f.write(test_code)

    print(f"  [TestGen] Tests written to: {test_path}")
    return test_path


def generate_tests_for_repo(directory: str):
    """
    Generate tests for every Python file in a directory.
    Called by crawler during Phase 4.
    Skips test files and __init__.py
    """
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith('.')
                   and d != 'venv'
                   and d != 'generated_tests']
        for file in files:
            if not file.endswith('.py'):
                continue
            if file.startswith('test_'):
                continue
            if file == '__init__.py':
                continue
            filepath = os.path.join(root, file)
            generate_tests_for_file(filepath)