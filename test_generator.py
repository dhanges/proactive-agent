import os
import re
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI()


def get_tests_dir(filepath: str) -> str:
    """
    Returns the generated_tests directory for this file.
    Tests live INSIDE the repo directory, not in a global folder.
    e.g. /app/repo2/utils.py → /app/repo2/generated_tests/
    This prevents test files from different repos overwriting each other.
    """
    repo_dir = os.path.dirname(os.path.abspath(filepath))
    return os.path.join(repo_dir, "generated_tests")


def get_test_file(filepath: str) -> str:
    """
    Returns the absolute path for the generated test file.
    """
    stem      = os.path.basename(filepath).replace(".py", "")
    tests_dir = get_tests_dir(filepath)
    return os.path.join(tests_dir, f"test_{stem}.py")


def generate_tests_for_file(filepath: str) -> str:
    tests_dir = get_tests_dir(filepath)
    os.makedirs(tests_dir, exist_ok=True)

    with open(filepath, "r") as f:
        source_code = f.read()

    filename  = os.path.basename(filepath)
    stem      = os.path.basename(filepath).replace(".py", "")
    test_path = os.path.join(tests_dir, f"test_{stem}.py")

    print(f"  [TestGen] Generating tests for: {filepath}")

    prompt = f"""You are an expert Python test engineer.

Write pytest test cases for ALL functions in this file.

FILE: {filename}

SOURCE CODE:
{source_code}

REQUIREMENTS:
1. Write 2-3 test cases per function — normal input, edge cases (empty list, zero, None)
2. Test the INTENDED correct behavior — what the function SHOULD do, not what it currently does
   - If a function should return False for empty input, test that it returns False
   - If a function should return None for empty input, test that it returns None
   - Do NOT write tests that expect exceptions unless the function explicitly raises them
3. Import correctly: from {stem} import <function_names>
4. Add this at the top so imports work inside Docker sandbox:
   import sys, os
   sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
5. Each test function must start with test_
6. Do not use any external libraries beyond pytest
7. Do not test private methods (starting with _)
8. One assertion per test where possible
9. For class methods, instantiate the class properly

CRITICAL: Only import pytest at the top if you actually use pytest.raises().
Otherwise just import the functions directly — pytest discovers tests without needing to import pytest.

Respond with ONLY valid Python code. No markdown, no backticks, no explanation.
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
    Skips test files, __init__.py, and generated_tests directories.
    """
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs
                   if not d.startswith('.')
                   and d not in ('venv', 'generated_tests', '__pycache__')]
        for file in files:
            if not file.endswith('.py'):
                continue
            if file.startswith('test_'):
                continue
            if file == '__init__.py':
                continue
            filepath = os.path.join(root, file)
            generate_tests_for_file(filepath)