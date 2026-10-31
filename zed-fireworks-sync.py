#!/usr/bin/env python3
"""Sync models.dev Fireworks metadata into Zed Agent. Python 3.9+, no dependencies.

Version: 2026-10-11-effort-fix
Reasoning models default to high effort; explicit existing values take precedence.
Zed exposes a generic effort menu, not a model-specific supported-level list.

Usage:
  python3 zed-fireworks-sync.py --dry-run
  python3 zed-fireworks-sync.py --install-timer   # Linux systemd user timer
  python3 zed-fireworks-sync.py                  # one refresh

Settings default: $XDG_CONFIG_HOME/zed/settings.json or ~/.config/zed/settings.json.
Override with --settings PATH. No API key is read or written by this helper.
The generated available_models array is managed: comments inside it are replaced.
Other settings/comments are preserved. Existing non-catalog model options survive.
For persistent overrides of catalog fields, put {"MODEL_ID": {"field": value}} in
fireworks-model-overrides.json next to settings.json. Use capabilities for nested
capability overrides. Models absent from the latest catalog are retained, so a
catalog omission cannot remove a selected or private model.

Status: systemctl --user status zed-fireworks-sync.timer
Logs: journalctl --user -u zed-fireworks-sync.service
Stop: systemctl --user disable --now zed-fireworks-sync.timer
Backups: settings.json.fireworks-sync.bak (previous changed version).
This imports metadata, not OpenCode runtime logic. Account access and actual tool
execution depend on Fireworks/model compatibility and Zed's Write profile.
"""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

URL = 'https://models.dev/api.json'
ENDPOINT = 'https://api.fireworks.ai/inference/v1'
TOKEN = re.compile(r'\s+|//[^\r\n]*|/\*[\s\S]*?\*/|"(?:[^"\\\x00-\x1f]|\\.)*"|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null|[{}\[\]:,]')

class Node:
    def __init__(self, start, end, value, children=None):
        self.start, self.end, self.value = start, end, value
        self.children = children

def parse(text):
    tokens, pos = [], 0
    while pos < len(text):
        m = TOKEN.match(text, pos)
        if not m:
            raise ValueError('Invalid JSONC at character %d' % pos)
        s = m.group()
        if not s.isspace() and not s.startswith(('//', '/*')):
            tokens.append((s, pos, m.end()))
        pos = m.end()
    i = 0
    def take(wanted=None):
        nonlocal i
        if i >= len(tokens): raise ValueError('Unexpected end of JSONC')
        t = tokens[i]
        if wanted is not None and t[0] != wanted: raise ValueError('Expected ' + wanted)
        i += 1
        return t
    def value():
        nonlocal i
        token, start, end = take()
        if token not in ('{', '['):
            return Node(start, end, json.loads(token))
        obj = token == '{'
        result, children = ({}, {}) if obj else ([], [])
        close = '}' if obj else ']'
        while i < len(tokens) and tokens[i][0] != close:
            if obj:
                key = json.loads(take()[0])
                if not isinstance(key, str) or key in children:
                    raise ValueError('Invalid or duplicate JSON key')
                take(':')
                n = value()
                result[key], children[key] = n.value, n
            else:
                n = value()
                result.append(n.value)
                children.append(n)
            if i < len(tokens) and tokens[i][0] == close: break
            take(',')
        end = take(close)[2]
        return Node(start, end, result, children)
    root = value()
    if i != len(tokens) or not isinstance(root.value, dict):
        raise ValueError('Settings must be one JSONC object')
    return root

def set_path(text, path, new_value):
    node = parse(text)
    for index, key in enumerate(path):
        if not isinstance(node.value, dict): raise ValueError('Non-object at ' + key)
        if key not in node.children:
            wrapped = new_value
            for part in reversed(path[index + 1:]): wrapped = {part: wrapped}
            # Insert first; all pre-existing text including comments stays intact.
            indent = '    ' * (index + 1)
            rendered = json.dumps({key: wrapped}, indent=4, ensure_ascii=False)
            lines = rendered.splitlines()[1:-1]
            addition = '\n' + '\n'.join(indent + line[4:] for line in lines)
            addition += ',' if node.children else ''
            addition += '\n'
            return text[:node.start + 1] + addition + text[node.start + 1:]
        node = node.children[key]
    if node.value == new_value: return text
    indent = '    ' * len(path)
    rendered = json.dumps(new_value, indent=4, ensure_ascii=False).replace('\n', '\n' + indent)
    return text[:node.start] + rendered + text[node.end:]

def merge(a, b):
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out

def models(catalog, existing, overrides):
    provider = catalog['fireworks-ai']
    if provider.get('api', '').rstrip('/') != ENDPOINT:
        raise ValueError('Unexpected catalog endpoint; leaving settings unchanged')
    old = {}
    for m in existing:
        if not isinstance(m, dict) or not isinstance(m.get('name'), str):
            raise ValueError('Invalid existing Fireworks model entry')
        if m['name'] in old: raise ValueError('Duplicate existing model ID')
        old[m['name']] = m
    fresh = {}
    for mid, m in provider['models'].items():
        if m.get('tool_call') is not True or m.get('status') in ('deprecated', 'retired'): continue
        context = m.get('limit', {}).get('context')
        if not isinstance(mid, str) or type(context) is not int or context <= 0:
            raise ValueError('Invalid catalog model metadata')
        interleaved = m.get('interleaved')
        # Zed's capability schema requires the full field set on every entry
        # (tools/images/parallel_tool_calls/prompt_cache_key at minimum); an
        # omitted field makes Zed reject the whole settings file.
        caps = {'tools': True, 'images': 'image' in m.get('modalities', {}).get('input', []),
                'parallel_tool_calls': True, 'prompt_cache_key': False,
                'chat_completions': True,
                'interleaved_reasoning': isinstance(interleaved, dict) and interleaved.get('field') == 'reasoning_content',
                'max_tokens_parameter': False}
        entry = {'name': mid, 'display_name': m.get('name', mid), 'max_tokens': context, 'capabilities': caps}
        # Fireworks reasoning models accept reasoning_effort; Zed requires a
        # non-null default to expose its generic thinking/effort controls.
        # Preserve explicit settings, including null, and apply overrides below.
        defaults = {'reasoning_effort': 'high'} if m.get('reasoning') is True else {}
        merged = merge(merge(defaults, old.get(mid, {})), entry)
        merged.pop('parallel_tool_calls', None)  # capability, not a model-entry field
        fresh[mid] = merged
    if not fresh: raise ValueError('No tool-capable models found; leaving settings unchanged')
    combined = {**old, **fresh}
    for mid, patch in overrides.items():
        if mid not in combined: raise ValueError('Override references unknown model: ' + mid)
        if not isinstance(patch, dict): raise ValueError('Model override must be an object')
        combined[mid] = merge(combined[mid], patch)
        if combined[mid].get('name') != mid: raise ValueError('Overrides cannot change model IDs')
    return [combined[k] for k in sorted(combined)]

def fetch():
    req = urllib.request.Request(URL, headers={'User-Agent': 'zed-fireworks-sync/1.0'})
    with urllib.request.urlopen(req, timeout=30) as response:
        body = response.read(32 * 1024 * 1024 + 1)
    if len(body) > 32 * 1024 * 1024: raise ValueError('Catalog too large')
    return json.loads(body)

def sync(settings, catalog, dry=False):
    original = settings.read_bytes()
    text = original.decode('utf-8')
    data = parse(text).value
    provider = data.get('language_models', {}).get('openai_compatible', {}).get('fireworks', {})
    if provider.get('api_url', ENDPOINT).rstrip('/') != ENDPOINT:
        raise ValueError('Existing fireworks provider uses a different endpoint')
    override_file = settings.parent / 'fireworks-model-overrides.json'
    overrides = json.loads(override_file.read_text()) if override_file.exists() else {}
    if not isinstance(overrides, dict): raise ValueError('Overrides must be an object')
    result = models(catalog, provider.get('available_models', []), overrides)
    prefix = ['language_models', 'openai_compatible', 'fireworks']
    text = set_path(text, prefix + ['api_url'], provider.get('api_url', ENDPOINT))
    text = set_path(text, prefix + ['available_models'], result)
    parse(text)
    if dry:
        print(json.dumps({'api_url': ENDPOINT, 'available_models': result}, indent=2))
        return
    if text.encode('utf-8') == original:
        print('Fireworks catalog unchanged (%d models).' % len(result))
        return
    fd, temporary = tempfile.mkstemp(prefix='.fireworks-sync-', dir=settings.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(text.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, settings.stat().st_mode & 0o777)
        if settings.read_bytes() != original:
            raise ValueError('Settings changed during sync; retry later')
        backup = settings.with_name(settings.name + '.fireworks-sync.bak')
        # A backup may contain keys from existing settings: keep it owner-only.
        bfd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(bfd, 'wb') as out: out.write(original)
        os.chmod(backup, 0o600)
        os.replace(temporary, settings)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    print('Updated Fireworks catalog: %d models. Backup: %s' % (len(result), backup))

def unit_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'

def install(settings):
    if not sys.platform.startswith('linux') or not shutil.which('systemctl'):
        raise ValueError('--install-timer requires Linux with a systemd user session')
    destination = Path.home() / '.local/lib/zed-fireworks-sync/zed-fireworks-sync.py'
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    if source != destination: shutil.copy2(source, destination)
    units = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    command = ' '.join(unit_quote(x) for x in (sys.executable, destination, '--settings', settings))
    (units / 'zed-fireworks-sync.service').write_text('[Unit]\nDescription=Refresh Zed Fireworks models from models.dev\n\n[Service]\nType=oneshot\nExecStart=' + command + '\nUMask=0077\n')
    (units / 'zed-fireworks-sync.timer').write_text('[Unit]\nDescription=Daily Zed Fireworks catalog refresh\n\n[Timer]\nOnStartupSec=2min\nOnUnitActiveSec=1d\nRandomizedDelaySec=2min\n\n[Install]\nWantedBy=timers.target\n')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'zed-fireworks-sync.timer'], check=True)
    print('Installed daily user timer. Stop with: systemctl --user disable --now zed-fireworks-sync.timer')

def main():
    default = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'zed/settings.json'
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--settings', type=Path, default=default)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--install-timer', action='store_true')
    args = p.parse_args()
    if args.dry_run and args.install_timer: p.error('Choose dry-run or install-timer')
    settings = args.settings.expanduser().resolve(strict=True)
    # Serialize our own invocations; also check for editor writes before replacement.
    with open(str(settings) + '.fireworks-sync.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sync(settings, fetch(), args.dry_run)
        if args.install_timer: install(settings)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Fireworks sync failed: ' + str(exc), file=sys.stderr)
        sys.exit(1)
