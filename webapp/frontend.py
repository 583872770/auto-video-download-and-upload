"""Resolve locally built frontend assets; no external scripts in production."""
import json
from pathlib import Path


def assets(static_folder):
    root = Path(static_folder) / 'frontend'
    manifest_path = root / '.vite' / 'manifest.json'
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    entry = manifest['src/main.ts']
    styles, preload, seen = [], [], set()

    def visit(chunk):
        for dependency in chunk.get('imports', []):
            if dependency not in seen:
                seen.add(dependency)
                visit(manifest[dependency])
                preload.append('frontend/' + manifest[dependency]['file'])
        for path in chunk.get('css', []):
            if 'frontend/' + path not in styles:
                styles.append('frontend/' + path)

    visit(entry)
    return dict(script='frontend/' + entry['file'], styles=styles, preload=preload)


def render_workspace(app):
    """Share the authenticated Vue shell without exposing runtime configuration."""
    from flask import g, render_template, session
    from webapp.cobalt_catalog import catalog
    bundle = assets(app.static_folder)
    if bundle is None:
        return None
    return render_template('frontend.html', assets=bundle, bootstrap={
        'csrf': session['csrf'], 'fleetEnabled': app.extensions.get('fleet') is not None,
        'role': g.console_user['role'] if getattr(g, 'console_user', None) else 'admin',
        'cobaltCatalog': catalog(),
    })
