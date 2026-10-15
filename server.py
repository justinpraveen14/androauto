#!/usr/bin/env python3
"""Flowpilot local Android recorder. Python 3.10+, Android platform-tools; no pip dependencies."""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PACKAGE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$')
VARIABLE = re.compile(r'^[A-Z][A-Z0-9_]{0,59}$')

class UserError(Exception):
    pass

def require(condition, message):
    if not condition:
        raise UserError(message)

def bounds(node):
    values = re.findall(r'-?\d+', node.get('bounds', ''))
    return tuple(map(int, values)) if len(values) == 4 else (0, 0, 0, 0)

def nodes_from_xml(xml):
    root = ET.fromstring(xml)
    return [dict(n.attrib) for n in root.iter('node') if bounds(n)[2] > bounds(n)[0] and bounds(n)[3] > bounds(n)[1]]

def match_nodes(nodes, selector):
    require(isinstance(selector, dict) and bool(selector), 'No element selector supplied.')
    require(set(selector) <= {'resource-id', 'text', 'content-desc', 'class', 'package'}, 'Invalid selector field.')
    return [n for n in nodes if all(n.get(k) == v for k, v in selector.items())]

def unique_selector(nodes, node):
    # Prefer IDs; combine labels/class only when an ID is reused in a list.
    for keys in [('resource-id',), ('resource-id', 'text'), ('resource-id', 'content-desc'), ('content-desc',), ('text', 'class')]:
        if 'text' in keys and (node.get('password') == 'true' or 'EditText' in node.get('class', '')):
            continue
        selector = {k: node[k] for k in keys if node.get(k)}
        if len(selector) == len(keys) and len(match_nodes(nodes, selector)) == 1:
            return selector
    return None

def selector_at(nodes, x, y):
    candidates = [n for n in nodes if bounds(n)[0] <= x < bounds(n)[2] and bounds(n)[1] <= y < bounds(n)[3]]
    candidates.sort(key=lambda n: (bounds(n)[2]-bounds(n)[0])*(bounds(n)[3]-bounds(n)[1]))
    for n in candidates:
        selector = unique_selector(nodes, n)
        if selector:
            return selector
    return None

class ADB:
    def __init__(self, executable='adb'):
        self.executable = executable

    def command(self, args, serial=None, timeout=25):
        command = [self.executable] + (['-s', serial] if serial else []) + args
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError:
            raise UserError('ADB was not found. Install Android platform-tools and add adb to PATH.')
        except subprocess.TimeoutExpired:
            raise UserError('ADB timed out. Unlock the phone and check the USB connection.')
        if result.returncode:
            error = result.stderr.decode('utf-8', errors='replace').strip()[:350]
            raise UserError('ADB failed: ' + error)
        return result.stdout

    def shell(self, serial, *args, timeout=25):
        # adb shell sends a remote shell command. Quote EVERY argument for that shell.
        return self.command(['shell', shlex.join([str(a) for a in args])], serial, timeout).decode('utf-8', errors='replace')

    def devices(self):
        lines = self.command(['devices', '-l']).decode().splitlines()[1:]
        devices = []
        for line in lines:
            parts = line.split()
            if len(parts) < 2 or parts[0].startswith('*'):
                continue
            details = dict(p.split(':', 1) for p in parts[2:] if ':' in p)
            devices.append({'serial': parts[0], 'state': parts[1], 'model': details.get('model', parts[0]).replace('_', ' ')})
        return devices

    def validate_device(self, serial):
        require(any(d['serial'] == serial and d['state'] == 'device' for d in self.devices()),
                'Device not ready. Connect USB, enable USB debugging, unlock, and approve the RSA prompt.')

    def frame(self, serial):
        png = self.command(['exec-out', 'screencap', '-p'], serial)
        require(png.startswith(b'\x89PNG\r\n\x1a\n') and len(png) >= 24, 'Device did not return a PNG screenshot.')
        width, height = struct.unpack('>II', png[16:24])
        return {'image': base64.b64encode(png).decode(), 'width': width, 'height': height}

    def hierarchy(self, serial):
        # Remove stale dump so failed commands cannot silently reuse an old UI tree.
        path = '/data/local/tmp/flowpilot-window.xml'
        self.shell(serial, 'rm', '-f', path)
        output = self.shell(serial, 'uiautomator', 'dump', path, timeout=18)
        require('dumped' in output.lower(), 'UI hierarchy unavailable. Wait for animations to finish and try again.')
        xml = self.shell(serial, 'cat', path)
        return nodes_from_xml(xml)

    def launch(self, serial, package):
        require(bool(PACKAGE.fullmatch(package)), 'Invalid Android package name.')
        component = self.shell(serial, 'cmd', 'package', 'resolve-activity', '--brief', '-c', 'android.intent.category.LAUNCHER', package)
        components = [line.strip() for line in component.splitlines() if '/' in line and not line.startswith(' ')]
        require(bool(components), 'No launcher activity found. Check the package is installed on this device.')
        result = self.shell(serial, 'am', 'start', '-W', '-n', components[-1])
        require('Error' not in result and 'Exception' not in result, 'Android could not launch this package: ' + result[:200])

    def text(self, serial, value):
        require(isinstance(value, str) and 0 < len(value) <= 2000, 'Enter between 1 and 2,000 characters.')
        require(all(32 <= ord(c) < 127 for c in value) and '%' not in value,
                'This ADB text input supports printable ASCII except %. Unicode/percent input needs an IME integration.')
        self.shell(serial, 'input', 'text', value.replace(' ', '%s'))

class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, package TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS flows(id TEXT PRIMARY KEY, project TEXT, name TEXT, steps TEXT, created REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, project TEXT, flow TEXT, name TEXT, status TEXT, started REAL, ended REAL, results TEXT, error TEXT)')
            db.execute("UPDATE runs SET status='interrupted', error='Companion stopped before the run completed.', ended=? WHERE status='running'", (time.time(),))

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def execute(self, sql, args=()):
        with self.connect() as db:
            db.execute(sql, args)

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def project(self, ident):
        rows = self.rows('SELECT * FROM projects WHERE id=?', (ident,))
        require(bool(rows), 'Project not found.')
        return rows[0]

    def flow(self, ident):
        rows = self.rows('SELECT * FROM flows WHERE id=?', (ident,))
        require(bool(rows), 'Flow not found.')
        flow = rows[0]
        flow['steps'] = json.loads(flow['steps'])
        return flow

class Studio:
    def __init__(self, store, adb):
        self.store, self.adb = store, adb
        self.lock = threading.RLock()
        self.device_lock = threading.RLock()
        self.recording = None
        self.active_run = None
        self.cancel = threading.Event()

    def state(self):
        with self.lock:
            projects = self.store.rows('SELECT * FROM projects ORDER BY rowid')
            flows = self.store.rows('SELECT * FROM flows ORDER BY created DESC')
            for f in flows:
                f['steps'] = json.loads(f['steps'])
            runs = self.store.rows('SELECT * FROM runs ORDER BY started DESC LIMIT 100')
            for r in runs:
                r['results'] = json.loads(r['results'])
            return {'projects': projects, 'flows': flows, 'runs': runs, 'recording': copy.deepcopy(self.recording),
                    'active_run': self.active_run, 'ai_configured': bool(os.getenv('FLOWPILOT_AI_BASE_URL') and os.getenv('FLOWPILOT_AI_MODEL'))}

    def idle(self):
        require(not self.active_run, 'Wait for the current run to finish or stop it.')

    def project_create(self, data):
        name, package = str(data.get('name', '')).strip(), str(data.get('package', '')).strip()
        require(0 < len(name) <= 80, 'Enter a project name (up to 80 characters).')
        require(bool(PACKAGE.fullmatch(package)), 'Enter a package such as com.example.app.')
        ident = uuid.uuid4().hex
        self.store.execute('INSERT INTO projects VALUES(?,?,?)', (ident, name, package))
        return {'id': ident}

    def start_recording(self, data):
        with self.lock, self.device_lock:
            self.idle()
            require(not self.recording, 'A recording is already in progress.')
            project = self.store.project(data.get('project'))
            serial = data.get('serial')
            self.adb.validate_device(serial)
            self.adb.launch(serial, project['package'])
            self.recording = {'project': project['id'], 'serial': serial, 'steps': [{'action': 'launch', 'package': project['package'], 'label': 'Launch app'}]}
        return {'ok': True}

    def save_recording(self, data):
        with self.lock:
            require(self.recording is not None, 'No recording to save.')
            name = str(data.get('name', '')).strip()
            require(0 < len(name) <= 80, 'Enter a flow name (up to 80 characters).')
            ident = uuid.uuid4().hex
            self.store.execute('INSERT INTO flows VALUES(?,?,?,?,?)', (ident, self.recording['project'], name, json.dumps(self.recording['steps']), time.time()))
            self.recording = None
            return {'id': ident}

    def frame(self, serial):
        require(isinstance(serial, str) and bool(serial), 'Select a device first.')
        with self.device_lock:
            return self.adb.frame(serial)

    @staticmethod
    def xy(data, width, height, prefix=''):
        try:
            x, y = float(data[prefix+'x']), float(data[prefix+'y'])
        except (KeyError, ValueError, TypeError):
            raise UserError('Invalid touch coordinates.')
        require(0 <= x <= 1 and 0 <= y <= 1, 'Touch is outside the display.')
        return min(width-1, int(x*width)), min(height-1, int(y*height))

    def action(self, data):
        with self.lock, self.device_lock:
            self.idle()
            serial = data.get('serial')
            self.adb.validate_device(serial)
            if self.recording:
                require(serial == self.recording['serial'], 'Keep the recording on its original device.')
            if self.recording:
                require(len(self.recording['steps']) < 500, '500-step recording limit reached. Save this flow.')
            action = data.get('action')
            step = {'action': action}
            warning = None
            if action in ('tap', 'swipe'):
                frame = self.adb.frame(serial)
                width, height = frame['width'], frame['height']
                x, y = self.xy(data, width, height)
                step.update(x=data['x'], y=data['y'], width=width, height=height)
                if action == 'tap':
                    selector = None
                    try:
                        selector = selector_at(self.adb.hierarchy(serial), x, y)
                    except (UserError, ET.ParseError):
                        warning = 'UI tree unavailable; this tap uses coordinates.'
                    step.update(selector=selector, label='Tap element' if selector else 'Tap coordinates')
                    if not selector:
                        warning = warning or 'No unique element found; this tap uses coordinates.'
                    self.adb.shell(serial, 'input', 'tap', x, y)
                else:
                    x2, y2 = self.xy(data, width, height, 'end_')
                    duration = min(2000, max(100, int(data.get('duration', 400))))
                    step.update(end_x=data['end_x'], end_y=data['end_y'], duration=duration, label='Swipe display')
                    self.adb.shell(serial, 'input', 'swipe', x, y, x2, y2, duration)
            elif action == 'text':
                variable = str(data.get('variable', '')).strip()
                require(bool(VARIABLE.fullmatch(variable)), 'Use a variable name such as LOGIN_EMAIL. Values are not stored.')
                nodes = self.adb.hierarchy(serial)
                focused = [n for n in nodes if n.get('focused') == 'true' and ('EditText' in n.get('class', '') or n.get('password') == 'true')]
                require(len(focused) == 1, 'Tap a text input first. Its focused field must be visible in the UI tree.')
                # Never include the existing value of a password or editable field in its locator.
                node = focused[0]
                selector = None
                for key in ('resource-id', 'content-desc'):
                    if node.get(key) and len(match_nodes(nodes, {key: node[key]})) == 1:
                        selector = {key: node[key]}
                        break
                require(selector is not None, 'Text input needs a unique resource ID or accessibility description.')
                self.adb.text(serial, data.get('value'))
                step.update(variable=variable, selector=selector, label='Type ${'+variable+'}')
            elif action == 'key':
                key = data.get('key')
                require(key in ('BACK', 'HOME', 'ENTER'), 'Unsupported navigation key.')
                self.adb.shell(serial, 'input', 'keyevent', 'KEYCODE_'+key)
                step.update(key=key, label='Press '+key.title())
            elif action == 'wait':
                seconds = float(data.get('seconds', 1))
                require(0.1 <= seconds <= 30, 'Wait must be between 0.1 and 30 seconds.')
                step.update(seconds=seconds, label=f'Wait {seconds:g}s')
            elif action == 'assert':
                text = str(data.get('text', '')).strip()
                require(0 < len(text) <= 300, 'Enter exact visible text to check.')
                nodes = self.adb.hierarchy(serial)
                require(any(n.get('text') == text for n in nodes), 'That exact text is not currently visible. Assertion was not recorded.')
                step.update(selector={'text': text}, label='Assert text is visible')
            elif action == 'launch':
                project = self.store.project(self.recording['project'] if self.recording else data.get('project'))
                self.adb.launch(serial, project['package'])
                step.update(package=project['package'], label='Launch app')
            else:
                raise UserError('Unsupported action.')
            if self.recording:
                self.recording['steps'].append(step)
            return {'ok': True, 'warning': warning}

    def wait_for(self, serial, selector, unique=True, timeout=10):
        deadline = time.monotonic()+timeout
        while True:
            if self.cancel.is_set():
                raise UserError('Run stopped by user.')
            nodes = self.adb.hierarchy(serial)
            found = match_nodes(nodes, selector)
            if found and (not unique or len(found) == 1):
                return found[0]
            if len(found) > 1 and unique:
                raise UserError('Selector matches several elements. Replay stopped to avoid tapping the wrong one.')
            if time.monotonic() >= deadline:
                raise UserError('Element not found before timeout: '+json.dumps(selector))
            self.cancel.wait(0.4)

    def execute_step(self, serial, step, variables, coordinates):
        action = step['action']
        if action == 'launch':
            self.adb.launch(serial, step['package'])
        elif action == 'tap' and step.get('selector'):
            n = self.wait_for(serial, step['selector'])
            x1, y1, x2, y2 = bounds(n)
            self.adb.shell(serial, 'input', 'tap', (x1+x2)//2, (y1+y2)//2)
        elif action in ('tap', 'swipe'):
            require(coordinates, 'Coordinate steps are disabled. Enable them explicitly for this run.')
            frame = self.adb.frame(serial)
            require((frame['width'], frame['height']) == (step['width'], step['height']),
                    'Display dimensions or orientation changed. Re-record coordinate gestures for this device.')
            x, y = self.xy(step, frame['width'], frame['height'])
            if action == 'tap':
                self.adb.shell(serial, 'input', 'tap', x, y)
            else:
                x2, y2 = self.xy(step, frame['width'], frame['height'], 'end_')
                self.adb.shell(serial, 'input', 'swipe', x, y, x2, y2, step['duration'])
        elif action == 'text':
            n = self.wait_for(serial, step['selector'])
            require(n.get('focused') == 'true', 'Text target is not focused. Re-record a tap on the field before typing.')
            self.adb.text(serial, variables[step['variable']])
        elif action == 'key':
            self.adb.shell(serial, 'input', 'keyevent', 'KEYCODE_'+step['key'])
        elif action == 'wait':
            if self.cancel.wait(step['seconds']):
                raise UserError('Run stopped by user.')
        elif action == 'assert':
            self.wait_for(serial, step['selector'], unique=False)
        else:
            raise UserError('Unknown recorded action.')

    def start_run(self, data):
        with self.lock:
            self.idle()
            require(not self.recording, 'Save or discard the recording before replay.')
            flow = self.store.flow(data.get('flow'))
            variables = data.get('variables', {})
            require(isinstance(variables, dict), 'Invalid run variables.')
            for step in flow['steps']:
                if step['action'] == 'text':
                    require(isinstance(variables.get(step['variable']), str) and bool(variables[step['variable']]), 'Supply '+step['variable']+' for this run.')
            serial = data.get('serial')
            with self.device_lock:
                self.adb.validate_device(serial)
            coordinates = data.get('allow_coordinates') is True
            require(coordinates or not any(s['action']=='swipe' or (s['action']=='tap' and not s.get('selector')) for s in flow['steps']),
                    'This flow has coordinate gestures. Enable them explicitly to run on the same display size.')
            ident = uuid.uuid4().hex
            self.store.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)', (ident, flow['project'], flow['id'], flow['name'], 'running', time.time(), None, '[]', None))
            self.active_run = ident
            self.cancel.clear()
            threading.Thread(target=self._run, args=(ident, flow, serial, dict(variables), coordinates), daemon=True).start()
            return {'id': ident}

    def _run(self, ident, flow, serial, variables, coordinates):
        results, error, status = [], None, 'passed'
        try:
            for index, step in enumerate(flow['steps']):
                if self.cancel.is_set():
                    raise UserError('Run stopped by user.')
                started = time.monotonic()
                try:
                    with self.device_lock:
                        self.execute_step(serial, step, variables, coordinates)
                    results.append({'step': index, 'status': 'passed', 'duration': round(time.monotonic()-started, 2)})
                except Exception:
                    results.append({'step': index, 'status': 'failed', 'duration': round(time.monotonic()-started, 2)})
                    raise
                self.store.execute('UPDATE runs SET results=? WHERE id=?', (json.dumps(results), ident))
                self.cancel.wait(0.3)
            if self.cancel.is_set():
                raise UserError('Run stopped by user.')
        except Exception as exc:
            status = 'cancelled' if self.cancel.is_set() else 'failed'
            error = str(exc) if isinstance(exc, UserError) else 'Run failed unexpectedly; inspect the local server terminal.'
        finally:
            variables.clear()
            self.store.execute('UPDATE runs SET status=?,ended=?,results=?,error=? WHERE id=?', (status, time.time(), json.dumps(results), error, ident))
            with self.lock:
                self.active_run = None

    def review(self, flow_id):
        flow = self.store.flow(flow_id)
        base, model = os.getenv('FLOWPILOT_AI_BASE_URL', ''), os.getenv('FLOWPILOT_AI_MODEL', '')
        require(base and model, 'Configure FLOWPILOT_AI_BASE_URL and FLOWPILOT_AI_MODEL, then restart the companion.')
        parsed = urlparse(base)
        require(parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')), 'Use HTTPS for remote AI providers, or HTTP on localhost.')
        # Share structure only: no screens, package/flow names, text values, or locator values.
        summary = [{'index': i+1, 'action': s['action'], 'selector_fields': sorted(s.get('selector', {})), 'coordinate_based': s['action']=='swipe' or (s['action']=='tap' and not s.get('selector'))} for i, s in enumerate(flow['steps'])]
        payload = {'model': model, 'messages': [{'role': 'system', 'content': 'You review Android UI tests. Based only on the supplied step structure, give at most five concise actionable suggestions. Do not invent app behavior or claim a test was executed. Do not output commands or code. You cannot modify or run tests.'}, {'role': 'user', 'content': json.dumps(summary)}], 'max_tokens': 650}
        headers = {'Content-Type': 'application/json'}
        if os.getenv('FLOWPILOT_AI_API_KEY'):
            headers['Authorization'] = 'Bearer '+os.environ['FLOWPILOT_AI_API_KEY']
        request = urllib.request.Request(base.rstrip('/')+'/chat/completions', json.dumps(payload).encode(), headers)
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
            content = result['choices'][0]['message']['content']
            require(isinstance(content, str) and bool(content), 'AI provider returned no text.')
            return {'review': content}
        except (urllib.error.URLError, KeyError, ValueError, IndexError):
            raise UserError('AI request failed. Check the endpoint, model, credentials, and provider availability.')

class Handler(BaseHTTPRequestHandler):
    server_version = 'Flowpilot/0.2'

    def log_message(self, fmt, *args):
        # Do not log query strings or request bodies containing device identifiers or inputs.
        pass

    def respond(self, payload, status=200, content_type='application/json'):
        data = json.dumps(payload).encode() if content_type == 'application/json' else payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorize(self, api=True):
        port = self.server.server_address[1]
        allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
        require(self.headers.get('Host') in allowed, 'Invalid host.')
        origin = self.headers.get('Origin')
        require(not origin or origin in {'http://'+h for h in allowed}, 'Cross-origin access is not allowed.')
        if api:
            require(secrets.compare_digest(self.headers.get('X-Flowpilot-Token', ''), self.server.token), 'Reload the local UI to authenticate.')

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            self.authorize(path.startswith('/api/'))
            if path == '/':
                html = (ROOT/'web/index.html').read_text().replace('__TOKEN__', self.server.token)
                return self.respond(html.encode(), content_type='text/html; charset=utf-8')
            if path in ('/app.js', '/style.css'):
                return self.respond((ROOT/'web'/path[1:]).read_bytes(), content_type='text/javascript' if path.endswith('js') else 'text/css')
            if path == '/api/state':
                return self.respond(self.server.studio.state())
            if path == '/api/devices':
                return self.respond({'devices': self.server.studio.adb.devices()})
            self.respond({'error': 'Not found.'}, 404)
        except UserError as exc:
            self.respond({'error': str(exc)}, 400)
        except Exception:
            self.respond({'error': 'Unexpected error. Check companion configuration.'}, 500)

    def do_POST(self):
        try:
            self.authorize()
            require(self.headers.get('Content-Type', '').split(';')[0] == 'application/json', 'Expected JSON.')
            length = int(self.headers.get('Content-Length', 0))
            require(0 < length <= 200_000, 'Invalid request size.')
            data = json.loads(self.rfile.read(length))
            require(isinstance(data, dict), 'Expected a JSON object.')
            studio = self.server.studio
            routes = {'/api/projects': studio.project_create, '/api/record/start': studio.start_recording,
                      '/api/record/save': studio.save_recording, '/api/action': studio.action, '/api/run': studio.start_run,
                      '/api/frame': lambda d: studio.frame(d.get('serial')), '/api/review': lambda d: studio.review(d.get('flow'))}
            if self.path in routes:
                return self.respond(routes[self.path](data))
            if self.path == '/api/record/discard':
                with studio.lock:
                    studio.recording = None
                return self.respond({'ok': True})
            if self.path == '/api/run/stop':
                studio.cancel.set()
                return self.respond({'ok': True})
            self.respond({'error': 'Not found.'}, 404)
        except (UserError, ValueError, TypeError, KeyError) as exc:
            self.respond({'error': str(exc) if isinstance(exc, UserError) else 'Invalid request data.'}, 400)
        except Exception:
            self.respond({'error': 'Unexpected error. Check your device and local configuration.'}, 500)

def create_server(port, directory, adb=None):
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    studio = Studio(Store(directory/'flowpilot.db'), adb or ADB())
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.studio = studio
    server.token = secrets.token_urlsafe(32)
    return server

def main():
    parser = argparse.ArgumentParser(description='Flowpilot local Android automation studio')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=Path.home()/'.local/share/flowpilot')
    args = parser.parse_args()
    os.umask(0o077)
    server = create_server(args.port, args.data_dir)
    print(f'Flowpilot is ready: http://127.0.0.1:{server.server_address[1]}', flush=True)
    print('Connect an authorized Android device. Press Ctrl+C to stop.', flush=True)
    if not shutil.which('adb'):
        print('ADB not found. Install Android platform-tools and restart.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.studio.cancel.set()
        print('\nStopping Flowpilot.')
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
