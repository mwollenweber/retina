import os
import subprocess
import sys

from django.conf import settings


def _import_production(**extra_env):
    # A subprocess, because production settings mutate dicts shared with the test settings.
    env = {k: v for k, v in os.environ.items() if k not in ("DJANGO_SECRET_KEY", "DJANGO_SETTINGS_MODULE")}
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", "import retina.settings.production"],
        cwd=settings.BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def test_production_refuses_to_start_without_secret_key():
    proc = _import_production()
    assert proc.returncode != 0
    assert "DJANGO_SECRET_KEY must be set" in proc.stderr


def test_production_starts_with_secret_key():
    proc = _import_production(DJANGO_SECRET_KEY="x" * 50)
    assert proc.returncode == 0, proc.stderr
