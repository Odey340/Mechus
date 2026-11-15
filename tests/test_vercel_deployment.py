import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vercel_config_routes_static_dashboard_and_backend_bridge():
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))

    assert config["framework"] is None
    assert config["buildCommand"] == "node scripts/build-vercel.mjs"
    assert config["outputDirectory"] == "public"
    assert config["functions"]["api/proxy.js"]["maxDuration"] == 60

    rewrites = {route["source"]: route["destination"] for route in config["rewrites"]}
    assert rewrites["/"] == "/dashboard.html"
    assert rewrites["/dashboard"] == "/dashboard.html"
    assert rewrites["/meetings"] == "/meetings.html"
    assert rewrites["/r/:token"] == "/report.html"
    assert rewrites["/api/:path*"] == "/api/proxy?path=/api/:path*"
    assert rewrites["/health"] == "/health.json"
    assert rewrites["/tenants"] == "/dashboard.html"
    assert rewrites["/checks/:path*"] == "/api/proxy?path=/checks/:path*"
    assert "/webhooks/:path*" not in rewrites


def test_vercel_build_generates_deployable_site(tmp_path):
    output = tmp_path / "site"
    env = os.environ.copy()
    env.update(
        {
            "MECHUS_API_ORIGIN": "https://api.mechus.example/",
            "MECHUS_VERCEL_OUTPUT_DIR": str(output),
        }
    )

    result = subprocess.run(
        ["node", "scripts/build-vercel.mjs"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "api.mechus.example" in result.stdout
    for relative_path in (
        "dashboard.html",
        "properties.html",
        "meetings.html",
        "welcome.html",
        "report.html",
        "health.json",
        "static/css/minimal.css",
        "static/js/runtime-config.js",
        "static/img/mechus-logo.png",
        "static/img/mechus-mark.png",
        "static/img/favicon.png",
    ):
        assert (output / relative_path).is_file(), relative_path

    runtime = (output / "static/js/runtime-config.js").read_text(encoding="utf-8")
    assert '"apiOrigin":"https://api.mechus.example"' in runtime
    assert not (output / ".env").exists()

    dashboard = (output / "dashboard.html").read_text(encoding="utf-8")
    assert '<script src="/static/js/runtime-config.js"></script>' in dashboard
    assert "window.MECHUS_RUNTIME" in dashboard


def test_vercel_javascript_has_valid_syntax():
    for path in (ROOT / "scripts/build-vercel.mjs", ROOT / "api/proxy.js"):
        subprocess.run(["node", "--check", str(path)], check=True, capture_output=True)


def test_vercel_bridge_fails_safely_without_backend_configuration():
    env = os.environ.copy()
    env.pop("MECHUS_API_ORIGIN", None)
    script = """
const proxy = require('./api/proxy.js');
const request = { method: 'GET', query: { path: '/api/estates' }, headers: {} };
const response = {
  status(code) { this.statusCode = code; return this; },
  json(body) {
    if (this.statusCode !== 503 || body.code !== 'backend_not_configured') process.exit(1);
  },
};
proxy(request, response).catch(() => process.exit(1));
"""
    subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
