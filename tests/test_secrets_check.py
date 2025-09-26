import subprocess, sys, json, os, pathlib

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_secrets_check_polygon_openai():
    # Ensure keys not set for this test context
    env = os.environ.copy()
    env.pop("POLYGON_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "-m"), "src.cli", "secrets_check", "--keys", "POLYGON_API_KEY,OPENAI_API_KEY"],
        capture_output=True,
        text=True,
        env=env,
    )
    # If invocation method via -m fails, fallback direct module path
    if result.returncode != 0 and not result.stdout.strip():
        result = subprocess.run(
            [sys.executable, "-m", "src.cli", "secrets_check", "--keys", "POLYGON_API_KEY,OPENAI_API_KEY"],
            capture_output=True,
            text=True,
            env=env,
        )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload.get("POLYGON_API_KEY") is False
    assert payload.get("OPENAI_API_KEY") is False
