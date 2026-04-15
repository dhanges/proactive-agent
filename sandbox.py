import docker
import tempfile
import shutil
import os

client = docker.from_env()

def run_in_sandbox(original_file: str, patched_code: str) -> dict:
    """
    Takes the path to the original file and the patched code as a string.
    Runs the patched code in an isolated container.
    Returns a dict with passed, output, error.
    """
    tmp_dir = None
    container = None

    try:
        # create a temp directory to mount into the container
        tmp_dir = tempfile.mkdtemp()

        # write the patched code into the temp directory
        filename = os.path.basename(original_file)
        patched_path = os.path.join(tmp_dir, filename)
        with open(patched_path, "w") as f:
            f.write(patched_code)

        print(f"  [Sandbox] Running {filename} in container...")

        container = client.containers.run(
            image="python:3.11-slim",
            command=f"python /code/{filename}",
            volumes={tmp_dir: {"bind": "/code", "mode": "ro"}},
            detach=False,
            stdout=True,
            stderr=True,
            remove=True,          # auto-destroy after run
            mem_limit="128m",     # memory cap
            network_disabled=True # no internet access
        )

        output = container.decode("utf-8") if isinstance(container, bytes) else str(container)

        print(f"  [Sandbox] Passed.")
        return {
            "passed": True,
            "output": output,
            "error": None
        }

    except docker.errors.ContainerError as e:
        error_output = e.stderr.decode("utf-8") if e.stderr else str(e)
        print(f"  [Sandbox] Failed — container error.")
        return {
            "passed": False,
            "output": None,
            "error": error_output
        }

    except Exception as e:
        print(f"  [Sandbox] Failed — unexpected error: {e}")
        return {
            "passed": False,
            "output": None,
            "error": str(e)
        }

    finally:
        # clean up temp directory
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)