"""For disposable CI runners: real Codex + loopback Responses fixture; no API key needed."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from unittest.mock import patch

from h2bridge import codex
from h2bridge.runner import Runner, Task, write_json

MARKER = "H2_CODEX_PROTOCOL_SMOKE_OK"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, "body": body})
            if self.path != "/v1/responses":
                self.send_error(404)
                return
            content = {"type": "output_text", "text": MARKER, "annotations": []}
            item = {"id": "msg_fixture", "type": "message", "role": "assistant",
                    "status": "completed", "content": [content]}
            response = {"id": "resp_fixture", "object": "response", "created_at": 1,
                        "status": "completed", "model": body["model"], "output": [item],
                        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                                  "input_tokens_details": {"cached_tokens": 0},
                                  "output_tokens_details": {"reasoning_tokens": 0}}}
            events = [
                {"type": "response.created", "response": response | {"status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": item | {"status": "in_progress", "content": []}},
                {"type": "response.content_part.added", "output_index": 0, "item_id": item["id"],
                 "content_index": 0, "part": content | {"text": ""}},
                {"type": "response.output_text.delta", "output_index": 0, "item_id": item["id"],
                 "content_index": 0, "delta": MARKER},
                {"type": "response.output_text.done", "output_index": 0, "item_id": item["id"],
                 "content_index": 0, "text": MARKER},
                {"type": "response.content_part.done", "output_index": 0, "item_id": item["id"],
                 "content_index": 0, "part": content},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            wire = "".join(f"event: {event['type']}\ndata: {json.dumps(event | {'sequence_number': i})}\n\n"
                           for i, event in enumerate(events)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    original_prepare = codex.prepare

    def prepare_fixture(*params):
        argv, env = original_prepare(*params)
        # Override transport only in this test; never use production credentials or endpoint.
        env.pop("CODEX_API_KEY", None)
        overrides = {
            "model_provider": '"fixture"',
            "model_providers.fixture.name": '"Loopback fixture"',
            "model_providers.fixture.base_url": json.dumps(f"http://127.0.0.1:{server.server_port}/v1"),
            "model_providers.fixture.wire_api": '"responses"',
            "model_providers.fixture.requires_openai_auth": "false",
            "model_providers.fixture.request_max_retries": "0",
            "model_providers.fixture.stream_max_retries": "0",
        }
        extra = [arg for key, value in overrides.items() for arg in ("-c", f"{key}={value}")]
        return argv[:-2] + extra + argv[-2:], env

    try:
        project = Path(__file__).resolve().parents[1]
        runner = Runner(Task.load(project / "examples/signed-total/task.json"), args.output,
                        [str(Path(args.codex).resolve())], "fixture-model", executor="codex", fixture=True, timeout=60)
        with patch.object(codex, "prepare", prepare_fixture):
            rd = runner.execute(project / "examples/base-harness", "native-smoke")
        result = json.loads((rd / "result.json").read_text())
        assert result["status"] == "completed", result
        assert MARKER in (rd / "answer.txt").read_text()
        request_text = json.dumps(requests)
        assert requests and "Inspect the task, make a small implementation change" in request_text
        assert str(rd / "workspace") in request_text
        assert result["session_ids"] and result["usage_reported"]
        report = {"status": "passed", "real_codex": True, "real_model": False,
                  "requests_to_loopback": len(requests), "harness_text_loaded": True,
                  "workspace_correct": True, "tokens_reported": result["tokens_reported"]}
        write_json(runner.root / "native-smoke-report.json", report)
        print(json.dumps(report, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


if __name__ == "__main__":
    main()
