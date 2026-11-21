"""Toolchain smoke tests: cdk.json flag and the CDK-free `core` package."""

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "src" / "tokenization_infra" / "core"
FORBIDDEN_ROOTS = frozenset({"aws_cdk", "constructs"})
RESTRICT_DEFAULT_SG_FLAG = "@aws-cdk/aws-ec2:restrictDefaultSecurityGroup"


def _imported_roots(source: str) -> set[str]:
    """Return the top-level package names imported by `source`."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_cdk_json_disables_restrict_default_security_group() -> None:
    """Requirement 1.13: the flag is false so no custom resource is created."""
    context = json.loads((REPO_ROOT / "cdk.json").read_text(encoding="utf-8"))["context"]

    assert RESTRICT_DEFAULT_SG_FLAG in context
    assert context[RESTRICT_DEFAULT_SG_FLAG] is False


def test_core_modules_do_not_import_cdk() -> None:
    """Requirement 1.1: `core` is pure Python and never imports aws_cdk or constructs."""
    offenders = {
        str(path.relative_to(REPO_ROOT)): sorted(roots & FORBIDDEN_ROOTS)
        for path in sorted(CORE_DIR.rglob("*.py"))
        if (roots := _imported_roots(path.read_text(encoding="utf-8"))) & FORBIDDEN_ROOTS
    }

    assert offenders == {}


def test_import_scan_detects_forbidden_imports() -> None:
    """The scanner flags every import form and ignores relative and unrelated imports."""
    source = (
        "import aws_cdk.aws_s3 as s3\n"
        "from constructs import Construct\n"
        "from aws_cdk import Stack\n"
        "import json\n"
        "from . import sibling\n"
    )

    assert _imported_roots(source) & FORBIDDEN_ROOTS == {"aws_cdk", "constructs"}
    assert _imported_roots("import json\nfrom . import sibling\n") == {"json"}
