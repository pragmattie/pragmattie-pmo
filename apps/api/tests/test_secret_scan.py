"""The secret scan over every tracked file, and proof that it catches each kind of secret.

The fake secrets are assembled at runtime from pieces, so no tracked file ever holds one.
"""

from pathlib import Path

import pytest
from secret_scan import Finding, repo_root, scan_files, scan_text, tracked_files

HERE = Path(__file__).resolve().parent


def _fakes() -> dict[str, tuple[str, str, str]]:
    """name -> (file name, file content, expected finding kind). Built from pieces."""
    dashes = "-" * 5
    key_header = f"{dashes}BEGIN RSA PRIVATE {'KEY'}{dashes}"
    return {
        "pem file": ("server" + ".pem", "certificate text\n", "key or certificate file"),
        "key file": ("deploy" + ".key", "key text\n", "key or certificate file"),
        "p12 file": ("bundle" + ".p12", "\0binary", "key or certificate file"),
        "private key header": ("notes.txt", f"{key_header}\nMIIEabc\n", "private key"),
        "github token": ("a.py", f"TOKEN = '{'gh' + 'p_'}{'A1b2' * 9}'\n", "GitHub token"),
        "anthropic key": ("b.py", f"KEY = '{'sk-' + 'ant-'}{'x9Y8' * 8}'\n", "Anthropic API key"),
        "aws key id": ("c.ini", f"id = {'AK' + 'IA'}{'Q7' * 8}\n", "AWS access key id"),
        "slack token": ("d.js", f"const t = '{'xo' + 'xb-'}{'1234567890' * 2}'\n", "Slack token"),
        "password literal": (
            "e.py",
            f'db_{"pass" + "word"} = "{"hun" + "ter2"}"\n',
            "password literal",
        ),
        "upper-case password": ("f.py", f"{'PASS' + 'WORD'} = 'swordfish'\n", "password literal"),
        "env password": (".env.local", f"{'DB_PASS' + 'WORD'}=s3cret-value\n", "password literal"),
        "shell export": ("run.sh", f'export {"PASS" + "WD"}="opensesame"\n', "password literal"),
    }


@pytest.fixture(params=sorted(_fakes()))
def planted(request, tmp_path) -> tuple[Path, str, str]:
    """A folder holding one planted fake secret; returns (folder, file name, expected kind)."""
    name, content, kind = _fakes()[request.param]
    (tmp_path / name).write_bytes(content.encode())
    return tmp_path, name, kind


def test_the_repository_has_no_secrets():
    root = repo_root(HERE)
    names = tracked_files(root)
    # A scan of nothing would pass, so prove there was something to scan.
    assert len(names) > 0
    assert "apps/api/app/seed.py" in names
    findings = scan_files(root, names)
    assert not findings, "\n".join(map(str, findings))


def test_the_scan_fails_on_a_planted_fake_secret(planted):
    folder, name, kind = planted
    findings = scan_files(folder, [name])
    assert kind in {f.kind for f in findings}, findings


def test_the_scan_catches_a_secret_in_a_test_file(tmp_path):
    # Tests are not exempt by path.
    folder = tmp_path / "tests"
    folder.mkdir()
    token = "gh" + "p_" + "Z" * 36
    (folder / "test_x.py").write_text(f"TOKEN = '{token}'\n")
    assert [f.kind for f in scan_files(tmp_path, ["tests/test_x.py"])] == ["GitHub token"]


def test_a_finding_never_shows_the_secret():
    token = "sk-" + "ant-" + "q" * 30
    findings = scan_text("x.py", f"KEY = '{token}'\n")
    assert findings == [Finding("x.py", 1, "Anthropic API key")]
    assert token not in str(findings[0])


@pytest.mark.parametrize(
    "line",
    [
        'login(password="typed")',  # a keyword argument, not an assignment
        "    db_password=settings.value,",
        "password = settings.password",  # not a literal
        "password_hash = hash_password(password)",
        "MYSQL_PASSWORD=change-me",  # an env placeholder
        "DB_PASSWORD: ${MYSQL_PASSWORD}",
        "PASSWORD = 'made-up-for-a-test'  # secret-scan: allow",
        'password = ""',
    ],
)
def test_what_is_not_a_finding(line):
    assert scan_text("x", line) == []


@pytest.mark.parametrize(
    "line",
    # Assembled from pieces so this file holds no password literal itself.
    [
        "Pass" + "word = 'x1'",
        "admin_pass" + 'wd = "x2"',
        "self.pass" + "word = 'x3'",
        "MYSQL_ROOT_PASS" + "WORD=real-looking-value",
    ],
)
def test_password_assignments_are_found_in_any_case(line):
    assert [f.kind for f in scan_text("x", line)] == ["password literal"]
