"""Real OpenCode binary + loopback synthetic model. No external model calls."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

from h2bridge.runner import Runner, Task, write_json

MARKER = "H2_NATIVE_PROTOCOL_SMOKE_OK"
requests = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append({"path": self.path, "body": body})
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        base = {"id": "chatcmpl-local-fixture", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": "fixture"}
        chunks = [
            base | {"choices": [{"index": 0, "delta": {"role": "assistant", "content": MARKER}, "finish_reason": None}]},
            base | {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        ]
        wire = "".join("data: " + json.dumps(x) + "\n\n" for x in chunks) + "data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(wire.encode())))
        self.end_headers()
        self.wfile.write(wire.encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--opencode", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        provider = {"providers": {"poc-local": {
            "name": "Local protocol fixture", "package": "@opencode/ai/providers/openai-compatible",
            "settings": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1"},
            "models": {"fixture": {"name": "Deterministic fixture"}},
        }}}
        runner = Runner(Task.load(project / "examples/signed-total/task.json"), args.output,
                        [args.opencode], "poc-local/fixture", timeout=30, fixture=True, provider_config=provider)
        rd = runner.execute(project / "examples/base-harness", "native-smoke")
        result = json.loads((rd / "result.json").read_text())
        assert result["status"] == "completed", result
        assert MARKER in (rd / "answer.txt").read_text()
        assert requests, "OpenCode did not call the local fixture endpoint"
        combined = json.dumps(requests)
        assert "Inspect the task, make a small implementation change" in combined, "Harness text was not loaded"
        assert str(rd / "workspace") in combined, "The CLI used the wrong workspace"
        report = {"status": "passed", "real_opencode": True, "real_model": False,
                  "requests_to_loopback": len(requests), "harness_text_loaded": True,
                  "workspace_correct": True, "session_ids": result["session_ids"],
                  "tokens_reported": result["tokens_reported"]}
        write_json(runner.root / "native-smoke-report.json", report)
        print(json.dumps(report, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    main()
