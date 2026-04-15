import time
import sys
_recently_committed = {}  # filepath → timestamp
COMMIT_COOLDOWN = 10      # seconds to ignore a file after committing
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from threading import Timer
from parser import parse_file
from graph_writer import write_graph
from orchestrator import run_pipeline
from db import close_driver

DEBOUNCE_DELAY = 0.5
_timers = {}

class CodeChangeHandler(FileSystemEventHandler):

    def on_modified(self, event):
        if event.is_directory:
            return
        if not event.src_path.endswith('.py'):
            return
        self._debounce(event.src_path)

    def mark_committed(filepath: str):
        _recently_committed[filepath] = time.time()

    def on_created(self, event):
        if event.is_directory:
            return
        if not event.src_path.endswith('.py'):
            return
        self._debounce(event.src_path)

    def _debounce(self, filepath):
        if filepath in _timers:
            _timers[filepath].cancel()
        t = Timer(DEBOUNCE_DELAY, self._process, args=[filepath])
        _timers[filepath] = t
        t.start()

    def _process(self, filepath):
        try:
            from api import add_feed_message
            add_feed_message('info', f'change detected: <strong>{filepath}</strong>')
        except ImportError:
            pass
   
        
        last_commit = _recently_committed.get(filepath, 0)
        if time.time() - last_commit < COMMIT_COOLDOWN:
            print(f"  [Watcher] Skipping {filepath} — recently committed.")
            return

    
        print(f"\n[CHANGE DETECTED] {filepath}")
        #first we parse the graph
        print(f"  Parsing and updating graph...")
        result = parse_file(filepath)
        if result["error"]:
            print(f"  Parse error: {result['error']}")
            return
        write_graph(result)

        # complexity_analysr
        from complexity_analyzer import update_complexity_in_graph
        update_complexity_in_graph(filepath)

        # step 3 — fire pipeline
        from orchestrator import run_pipeline
        run_pipeline(filepath)
    
def watch(directory):
    print(f"Watching: {directory}")
    print("Save any .py file to trigger the pipeline.")
    print("Press Ctrl+C to stop.\n")

    event_handler = CodeChangeHandler()
    observer = Observer()
    observer.schedule(event_handler, path=directory, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping watcher...")
        observer.stop()
        close_driver()

    observer.join()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python watcher.py <directory>")
        sys.exit(1)
    watch(sys.argv[1])