import os
import shutil
import tempfile
from sandbox import run_in_sandbox
from test_generator import get_test_file
from agent_state import ValidationResult


def prepare_sandbox_package(original_filepath: str,
                             new_file_content: str,
                             test_file: str) -> str:
    """
    Creates a temp directory with:
    - The fixed file
    - The test file
    - Any other files in the same directory (dependencies)
    Returns the temp directory path.
    """
    tmp_dir = tempfile.mkdtemp(dir='/tmp')

    # copy all Python files from the source directory
    
    source_dir = os.path.dirname(original_filepath)
    for fname in os.listdir(source_dir):
        if fname.endswith('.py'):
            src = os.path.join(source_dir, fname)
            dst = os.path.join(tmp_dir, fname)
            shutil.copy2(src, dst)

    
    filename = os.path.basename(original_filepath)
    fixed_path = os.path.join(tmp_dir, filename)
    with open(fixed_path, "w") as f:
        f.write(new_file_content)

    # copy the test file
    test_filename = os.path.basename(test_file)
    test_dst = os.path.join(tmp_dir, test_filename)
    shutil.copy2(test_file, test_dst)

    return tmp_dir


def run_tests_in_sandbox(tmp_dir: str, test_filename: str) -> dict:
    """
    Runs pytest inside a Docker container on the prepared directory.
    Returns dict with passed, output, error.
    """
    import docker
    client = docker.from_env()

    print(f"  [Validator] Running tests in sandbox...")

    try:
        output = client.containers.run(
        image="python:3.11-slim",
        command=f"sh -c 'pip install pytest -q 2>/dev/null && pytest {test_filename} -v --tb=short || true'",
        volumes={tmp_dir: {"bind": "/code", "mode": "rw"}},
        working_dir="/code",
        detach=False,
        stdout=True,
        stderr=True,
        remove=True,
        mem_limit="256m",
        network_mode="bridge"
        )

        output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        

        # parse pytest output for pass/fail counts
        passed  = 0
        failed  = 0
        total   = 0

        for line in output_str.split('\n'):
            # look for the summary line: "X passed" or "X passed, Y failed"
            if ' passed' in line or ' failed' in line or ' error' in line:
                parts = line.strip().split()
                for i, part in enumerate(parts):
                    if part == 'passed,' or part == 'passed':
                        try:
                            passed = int(parts[i - 1])
                        except (ValueError, IndexError):
                            pass
                    if part in ('failed,', 'failed', 'error,', 'error'):
                        try:
                            failed += int(parts[i - 1])
                        except (ValueError, IndexError):
                            pass

        total = passed + failed
        overall_passed = failed == 0 and passed > 0

        return {
            "passed": overall_passed,
            "output": output_str,
            "error": None,
            "tests_run": total,
            "tests_passed": passed
        }

    except docker.errors.ContainerError as e:
        error_output = e.stderr.decode("utf-8") if e.stderr else str(e)

        # even on container error, parse for test results
        passed  = 0
        failed  = 0
        for line in error_output.split('\n'):
            if 'passed' in line:
                parts = line.strip().split()
                for i, part in enumerate(parts):
                    if part == 'passed':
                        try:
                            passed = int(parts[i - 1])
                        except (ValueError, IndexError):
                            pass
            if 'failed' in line or 'error' in line:
                parts = line.strip().split()
                for i, part in enumerate(parts):
                    if part in ('failed', 'error'):
                        try:
                            failed += int(parts[i - 1])
                        except (ValueError, IndexError):
                            pass

        return {
            "passed": False,
            "output": error_output,
            "error": error_output,
            "tests_run": passed + failed,
            "tests_passed": passed
        }

    except Exception as e:
        return {
            "passed": False,
            "output": None,
            "error": str(e),
            "tests_run": 0,
            "tests_passed": 0
        }

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def validation_agent(state) -> object:
    """
    Main validation agent function.
    Takes state.fix (new file content), runs tests in sandbox,
    writes ValidationResult to state.
    """
    from agent_state import PipelineState

    print(f"\n[Validator] Validating fix for: {state.issue_report.affected_file}")
    state.state = PipelineState.VALIDATING

    test_file = get_test_file(state.issue_report.affected_file)

    if not os.path.exists(test_file):
        print(f"  [Validator] No test file found at {test_file}")
        print(f"  [Validator] Run crawler first to generate tests.")
        state.validation_result = ValidationResult(
            passed=False,
            sandbox_output="No test file found",
            error_message="Test file missing — run crawler to generate tests"
        )
        return state

    print(f"  [Validator] Using test file: {test_file}")

    tmp_dir = prepare_sandbox_package(
        original_filepath=state.issue_report.affected_file,
        new_file_content=state.fix,
        test_file=test_file
    )

    test_filename = os.path.basename(test_file)
    result = run_tests_in_sandbox(tmp_dir, test_filename)

    state.validation_result = ValidationResult(
        passed=result["passed"],
        sandbox_output=result["output"],
        error_message=result.get("error"),
        tests_run=result["tests_run"],
        tests_passed=result["tests_passed"]
    )

    if result["passed"]:
        print(f"  [Validator] PASSED — {result['tests_passed']}/{result['tests_run']} tests")
    else:
        print(f"  [Validator] FAILED — {result['tests_passed']}/{result['tests_run']} tests")
        if result.get("error"):
            # show last 5 lines of error for context
            error_lines = result["error"].strip().split('\n')
            for line in error_lines[-5:]:
                print(f"    {line}")

    return state