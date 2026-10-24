import json
from types import SimpleNamespace

from invoiceiq.cli import main
from invoiceiq.llm import ClaudeExtractor
from invoiceiq.pipeline import run
from tests.test_local import US
from invoiceiq.local import LocalExtractor


class FakeClient:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []
        self.messages = self

    def create(self, model, max_tokens, messages):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.replies.pop(0))])


def test_claude_extractor_repairs_with_feedback():
    good = LocalExtractor().extract(US)
    bad = dict(good, total="1.00")
    fc = FakeClient(["Sure!\n" + json.dumps(bad), json.dumps(good)])
    r = run(ClaudeExtractor(client=fc), US)
    assert r.status == "ok" and r.attempts == 2
    assert "failed these checks" not in fc.prompts[0] and "!= total" in fc.prompts[1]


def test_claude_garbage_reply_is_review():
    r = run(ClaudeExtractor(client=FakeClient(["no json"] * 3)), US)
    assert r.status == "needs_review"


def test_cli_extract_and_exit_codes(tmp_path, capsys):
    f = tmp_path / "a.txt"
    f.write_text(US)
    assert main(["extract", str(f)]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["total"] == "1102.14"
    f.write_text(US.replace("81.64", "81.84"))
    assert main(["extract", str(f)]) == 2


def test_cli_eval_json(capsys):
    assert main(["eval", "--set", "dev", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["n"] == 60
