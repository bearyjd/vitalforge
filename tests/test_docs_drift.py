"""Content-drift guards for A3: assert the docs actually describe the
bearer-token feature rather than just the code. Not behavior tests -- these
read README.md/.env.example as plain text.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text()
ENV_EXAMPLE = (REPO_ROOT / ".env.example").read_text()


def test_env_example_documents_api_token():
    assert "VITALFORGE_API_TOKEN" in ENV_EXAMPLE


def test_readme_env_table_lists_api_token():
    assert "`VITALFORGE_API_TOKEN`" in README


def test_readme_tasker_section_uses_bearer():
    tasker_section = README.split("## Tasker Integration", 1)[1].split("## NFC Tag Integration", 1)[0]
    assert "Authorization: Bearer" in tasker_section


def test_readme_tasker_section_no_longer_documents_cookie_copying():
    tasker_section = README.split("## Tasker Integration", 1)[1].split("## NFC Tag Integration", 1)[0]
    assert "vf_session" not in tasker_section


def test_readme_documents_both_revocation_procedures():
    auth_section = README.split("### Authentication", 1)[1].split("## Deployment", 1)[0]
    assert "VITALFORGE_SECRET" in auth_section
    assert "VITALFORGE_API_TOKEN" in auth_section
