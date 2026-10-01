"""Hand-rolled SSRF in a shell command is caught by pattern, not by a socket.

The network guard cannot see inside ``curl``: when the model is talked into
fetching a URL, that request comes from our own process with our own
credentials. Two shapes earn an approval prompt — a cloud metadata endpoint
(a credential read the model would summarize straight back to the caller) and a
*written* request to loopback (the confused-deputy shape). A plain local read
stays ungated on purpose: prompting for every `curl 127.0.0.1/health` would
train the operator to click through.
"""

from __future__ import annotations

import pytest

from agent.middlewares.humanInTheLoop.detection import detect_dangerous_command

pytestmark = [pytest.mark.unit]


def _tags(command: str) -> list[str]:
    return [tag for _, tag in detect_dangerous_command(command)]


@pytest.mark.parametrize(
    "command",
    [
        "curl http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "curl -s http://169.254.169.254",
        "wget http://169.254.169.254/latest/meta-data/",
        "curl http://metadata.google.internal/computeMetadata/v1/",
        "python3 -c 'import urllib.request; urllib.request.urlopen(\"http://100.100.100.200/x\")'",
    ],
)
def test_cloud_metadata_endpoints_require_approval(command):
    assert "cloud_metadata_ssrf" in _tags(command), command


@pytest.mark.parametrize(
    "command",
    [
        "curl -X POST http://localhost:8080/api -d @body.json",
        "curl -d @body.json http://127.0.0.1:8080/api",
        "curl --data-raw 'x=1' http://[::1]:9000/admin",
        "wget --post-data='x=1' http://0.0.0.0:5000/",
    ],
)
def test_written_loopback_requests_require_approval(command):
    assert "loopback_write_ssrf" in _tags(command), command


@pytest.mark.parametrize(
    "command",
    [
        "curl http://127.0.0.1:8080/lane-status",
        "curl -s https://api.github.com/repos/BurntSushi/ripgrep | head",
        "curl https://example.com -o /tmp/page.html",
        "wget http://[::1]:9000/x -O out.bin",
        "git clone https://github.com/BurntSushi/ripgrep",
    ],
)
def test_ordinary_network_commands_stay_ungated(command):
    tags = _tags(command)

    assert "cloud_metadata_ssrf" not in tags, command
    assert "loopback_write_ssrf" not in tags, command
