"""Freeze browser route order, methods, paths and names before adapter refactors."""
from pathlib import Path
from tempfile import TemporaryDirectory
import os
import unittest

from fastapi.routing import APIRoute
from repairlib.app import create_app
from support.workspace import clone_workspace

GOLDEN = Path(__file__).with_name('golden') / 'web_routes.tsv'


def api_routes(routes, prefix=''):
    """Yield (full path, route); FastAPI 0.164 keeps included routers lazy instead of flattening them."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, 'original_router'):
            yield from api_routes(route.original_router.routes, prefix + route.include_context.prefix)


def owning_list(routes, target):
    if target in routes:
        return routes
    for route in routes:
        if hasattr(route, 'original_router'):
            found = owning_list(route.original_router.routes, target)
            if found is not None:
                return found


def routes(app):
    return ''.join(f"{','.join(sorted(route.methods))}\t{path}\t{route.name}\n"
                   for path, route in api_routes(app.routes)
                   if not path.startswith('/api/') and path != '/health')


class WebRouteGoldenTests(unittest.TestCase):
    def app(self):
        # An uninitialized workspace mounts no browser routes, so the golden needs a real one.
        directory = TemporaryDirectory(prefix='repairlib-route-golden-')
        self.addCleanup(directory.cleanup)
        workspace = Path(directory.name) / 'workspace'
        clone_workspace(workspace, 'synthetic route golden passphrase')
        return create_app(workspace=workspace)

    def test_web_routes_match_the_reviewed_snapshot(self):
        text = routes(self.app())
        if os.environ.get('UPDATE_GOLDEN') == '1':
            GOLDEN.write_text(text, encoding='utf8')
        self.assertEqual(text, GOLDEN.read_text(encoding='utf8'))

    def test_guard_detects_changed_and_added_routes(self):
        app = self.app()
        baseline = routes(app)
        route = next(route for path, route in api_routes(app.routes)
                     if path == '/customers' and 'GET' in route.methods)
        original = route.path
        route.path = '/renamed-customers'
        self.assertNotEqual(routes(app), baseline)
        route.path = original
        route.name = 'renamed_route'
        self.assertNotEqual(routes(app), baseline)
        route.name = 'customer_list'
        self.assertEqual(routes(app), baseline)
        methods = route.methods
        route.methods = {'POST'}
        self.assertNotEqual(routes(app), baseline)
        route.methods = methods
        owner = owning_list(app.routes, route)
        position = owner.index(route)
        owner.remove(route)
        self.assertNotEqual(routes(app), baseline)
        owner.insert(position, route)
        self.assertEqual(routes(app), baseline)
        app.add_api_route('/synthetic-new-route', lambda: None, methods=['POST'])
        self.assertNotEqual(routes(app), baseline)
