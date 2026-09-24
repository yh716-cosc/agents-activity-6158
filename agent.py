#!/usr/bin/env python3
"""Translation agent (stdlib only, Chat Completions-compatible API).

Default: Gemini 2.5 Flash. Set GEMINI_API_KEY; optionally set GEMINI_MODEL.
For OpenAI-compatible providers, set AGENT_PROVIDER=openai, OPENAI_API_KEY,
OPENAI_MODEL and optionally OPENAI_BASE_URL (ending /v1).
Run: python agent.py --budget 40
Continue: python agent.py --resume logs/run-EXAMPLE.state.json
Migrate old calls: python agent.py --budget 40 --used-calls 23
After code edits, evaluation is automatic. Requests are spaced by 13s by default.
Every HTTP attempt counts; no hidden retries. Logs include full tool output.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = pathlib.Path(__file__).resolve().parent
RUST = HERE / 'rust'
LIB = RUST / 'src' / 'lib.rs'
PYSRC = HERE / 'reference' / 'version.py'
LOGS = HERE / 'logs'
CONTEXT_CHARS = 24000
STATE: dict = {}


class TransientModelError(RuntimeError):
    """A temporary provider failure; the main loop owns counted retries."""


def clip(text, limit):
    text = str(text)
    if len(text) <= limit:
        return text
    marker = '\n...[omitted; retrieve source with read_file]...\n'
    n = max(0, (limit - len(marker)) // 2)
    return (text[:n] + marker + text[-n:])[:limit]


def code_hash():
    return hashlib.sha256(LIB.read_bytes()).hexdigest()


def model_config():
    """Keep provider credentials separate; never fall back to another API key."""
    provider = os.environ.get('AGENT_PROVIDER', 'gemini').strip().lower()
    if provider == 'gemini':
        key = os.environ.get('GEMINI_API_KEY')
        model = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
        base = 'https://generativelanguage.googleapis.com/v1beta/openai'
        required = 'GEMINI_API_KEY (and a nonempty GEMINI_MODEL if set)'
    elif provider == 'openai':
        key, model = os.environ.get('OPENAI_API_KEY'), os.environ.get('OPENAI_MODEL')
        base = os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
        required = 'OPENAI_API_KEY and OPENAI_MODEL'
    else:
        raise ValueError('AGENT_PROVIDER must be gemini or openai.')
    if not key or not model:
        raise ValueError(f'Set {required}.')
    return provider, key, model, base


def call_model(messages: list[dict], tools: list[dict]) -> dict:
    """One request, normalized to the original scaffold's response shape."""
    provider, key, model, base = model_config()
    payload = {'model': model, 'messages': messages,
               'tools': [{'type': 'function', 'function': t} for t in tools],
               'tool_choice': 'auto'}
    if provider == 'openai':
        payload['parallel_tool_calls'] = False
    request = urllib.request.Request(
        base + '/chat/completions', data=json.dumps(payload).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not persist provider response bodies that might echo credentials.
        if exc.code in (500, 502, 503, 504):
            raise TransientModelError(f'Model HTTP {exc.code}: temporary provider failure.') from None
        if exc.code == 429:
            raise RuntimeError('Model HTTP 429: rate limit or quota exhausted. Check your '
                               'provider quota and retry later; no automatic retry made.') from None
        raise RuntimeError(f'Model HTTP {exc.code}; check endpoint, model, key and quota.') from None
    except (urllib.error.URLError, TimeoutError):
        raise TransientModelError('Model connection failed or timed out.') from None
    message = data['choices'][0]['message']
    calls = []
    for item in message.get('tool_calls') or []:
        fn = item['function']
        try:
            arguments = json.loads(fn['arguments'])
        except (ValueError, TypeError):
            arguments = None
        calls.append({'name': fn['name'], 'arguments': arguments})
    return {'text': message.get('content'), 'tool_calls': calls}


def system_prompt() -> str:
    return """Translate reference/version.py into rust/src/lib.rs using tools.
Read Python source, reference tests and Rust signatures before implementing.
Preserve the public interface. Only lib.rs may be edited. Implement parse,
to_string, compare, bump_major, bump_minor, bump_patch and meaningful Rust unit
tests ported from reference/test_*.py. Do not confuse next_version with bump_*.
REQUIRED CRATE-LEVEL PUBLIC INTERFACE (methods alone do not satisfy this):
pub struct Version {
    pub major: u64, pub minor: u64, pub patch: u64,
    pub prerelease: Option<String>, pub build: Option<String>,
}
pub fn parse(s: &str) -> Result<Version, String>
pub fn to_string(v: &Version) -> String
pub fn compare(a: &Version, b: &Version) -> std::cmp::Ordering
pub fn bump_major(v: &Version) -> Version
pub fn bump_minor(v: &Version) -> Version
pub fn bump_patch(v: &Version) -> Version
compare returns Ordering, NOT i8 or Result. main.rs is fixed and cannot change.
If the baseline fails to compile, first read current lib.rs and main.rs and fix
the reported interface/compiler errors with small patches. Do not restart the
translation or look for the Rust API in the Python source. Read reference tests
and the relevant Python methods before changing behavior.
Source behavior is authoritative: inspect edge cases rather than guessing.
Use std only: no dependencies, unsafe, Python callbacks, todo!, unimplemented!,
or panic!. Avoid unnecessary clone/to_owned and unwrap. Check strict parsing,
ASCII, leading zeros, prerelease ordering, ignored build metadata in precedence,
bumps and round trips. Implement general behavior, never tune to practice seed 0.
Use read_file for line ranges and replace_rust for exact unique local edits.
read_rust returns the whole Rust file when it fits. For larger files follow the
explicit Next start line. search_source locates function definitions by text.
Never repeat the same read when you need later lines: advance start or search.
Current Rust and relevant reference snippets are automatically attached. Do not
spend a call reading code already present. Fix all related signatures, return
expressions, helper functions and tests together, not just a return annotation.
After all tool actions in a reply, the runner automatically evaluates changed
Rust (build, tests, differential checks). No separate build request is needed.
Evaluation checkpoints good code and rolls back
regressions; after rollback read the current Rust before patching.
save_notes preserves findings, failed approaches and next steps across context
compaction. Prior tool exchanges are rendered as observation summaries in user
messages, not outstanding API tool calls. Retrieve omitted source as needed.
At most 40 model requests are available. The runner verifies completion: build,
nonempty passing tests, 100% differential results, precedence and no violations.
If stuck, explain remaining failures rather than claiming success."""


def build_context(history: list[dict], step: int) -> list[dict]:
    """Select recent observations, compress old actions, retain durable notes.

    No dangling tool_call IDs: old exchanges become plain observation messages.
    The full untruncated trajectory is on disk, not resent on every request.
    """
    progress = {k: STATE.get(k) for k in
                ('budget', 'best_score', 'last_report', 'stale', 'notes', 'last_change')}
    progress.update(step=step, current_sha256=code_hash())
    old = [m.get('name') for m in history[2:-6] if m['role'] == 'tool']
    progress['older_tool_counts'] = {name: old.count(name) for name in set(old)}
    messages = [history[0], history[1], {'role': 'user',
                'content': 'Runner state:\n' + clip(json.dumps(progress, ensure_ascii=False), 2500)}]
    if STATE.get('last_diagnostics'):
        messages.append({'role': 'user', 'content': 'Latest build/test/evaluation diagnostics '
                         '(may predate edits; rerun to verify):\n'
                         + clip(STATE['last_diagnostics'], 2500)})
    source = LIB.read_text(encoding='utf-8')
    source_limit = min(12000, CONTEXT_CHARS - sum(len(m['content']) for m in messages) - 2500)
    rust = source if len(source) <= source_limit else read_page(LIB, 1, 1000000, source_limit)
    messages.append({'role': 'user', 'content': 'Current rust/src/lib.rs (already read for you):\n' + rust})
    available = CONTEXT_CHARS - sum(len(m['content']) for m in messages)
    related = related_source(min(3500, max(0, available - 1500)))
    if related:
        messages.append({'role': 'user', 'content': related})
    remaining = CONTEXT_CHARS - sum(len(m['content']) for m in messages)
    selected = []
    # Reserve room for the newest result first. Do not cut the middle out of
    # source reads: doing so caused the model to repeatedly fetch missing code.
    for m in reversed(history[2:][-6:]):
        if m['role'] == 'assistant':
            content = json.dumps({'text': m.get('content'),
                                  'requested_tools': [c['name'] for c in m.get('tool_calls', [])]})
        else:
            content = f"{m.get('name', m['role'])}: {m['content']}"
        if remaining < 100:
            break
        is_source = m.get('name') in ('read_rust', 'read_python', 'read_file', 'search_source')
        limit = min(12500 if is_source else 3000, remaining)
        if is_source and len(content) > limit:
            content = 'Older source observation omitted for space; fetch a smaller line range.'
        else:
            content = clip(content, limit)
        selected.append({'role': 'user', 'content': content})
        remaining -= len(content)
    messages.extend(reversed(selected))
    return messages


def related_source(limit):
    """Local retrieval only: select relevant Python bodies and harness call sites."""
    if limit < 100:
        return ''
    diagnostic = STATE.get('last_diagnostics', '').lower()
    names = [n for n in ('compare', 'parse', 'bump_major', 'bump_minor', 'bump_patch', 'to_string')
             if n in diagnostic] or ['parse', 'compare']
    wanted = set(names)
    if 'compare' in wanted:
        wanted.add('_nat_cmp')
    chunks = []
    if PYSRC.exists():
        source = PYSRC.read_text(encoding='utf-8')
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
                chunks.append(f'reference/version.py:{node.lineno}\n' +
                              '\n'.join(lines[node.lineno - 1:node.end_lineno]))
    harness = RUST / 'src' / 'main.rs'
    if harness.exists():
        lines = harness.read_text(encoding='utf-8').splitlines()
        indexes = set()
        for i, line in enumerate(lines):
            if any('sv::' + name in line for name in names):
                indexes.update(range(max(0, i - 2), min(len(lines), i + 6)))
        chunks.insert(0, 'Fixed harness call sites:\n' + '\n'.join(f'{i+1}: {lines[i]}' for i in sorted(indexes)))
    result = 'Automatically selected reference context:\n'
    for chunk in chunks:
        if len(result) + len(chunk) + 2 <= limit:
            result += chunk + '\n\n'
    return result


def passed(report):
    tests = report.get('cargo_test') or {}
    families = report.get('differential') or {}
    return (report.get('build') is True and tests.get('passed', 0) > 0
            and report.get('cargo_test_returncode') == 0
            and tests.get('failed') == 0 and report.get('differential_pct') == 100
            and set(families) == {'parse valid', 'parse invalid', 'compare', 'bump', 'round-trip'}
            and all(f.get('total', 0) > 0 and f.get('pass') == f['total'] for f in families.values())
            and report.get('spec_precedence_chain') is True
            and report.get('violations') == [] and not report.get('error'))


def should_stop(history, step, budget, last_score):
    if STATE.get('verified_hash') == code_hash() and passed(STATE.get('last_report', {})):
        return True, 'verified completion on practice evaluation'
    if step >= budget:
        return True, f'budget exhausted ({budget} calls); not verified complete'
    if STATE.get('stale', 0) >= 4:
        return True, 'stuck: four evaluations without improvement'
    replies = [m for m in history if m['role'] == 'assistant']
    if len(replies) >= 3:
        recent = replies[-3:]
        if all(not m.get('tool_calls') for m in recent):
            return True, 'stuck: three replies without tool actions'
        actions = [json.dumps(m.get('tool_calls'), sort_keys=True) for m in recent]
        if len(set(actions)) == 1:
            return True, 'stuck: identical actions repeated three times'
    return False, ''


def _run(cmd, cwd=None, timeout=180):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
        return {'returncode': p.returncode, 'output': (p.stdout + p.stderr).strip()}
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        return {'returncode': -1, 'output': f'TIMEOUT after {timeout}s\n{output}'}
    except OSError as exc:
        return {'returncode': -1, 'output': str(exc)}


def source_path(name):
    allowed = {'reference/version.py', 'reference/test_parsing.py',
               'reference/test_compare.py', 'reference/test_bump.py',
               'rust/src/lib.rs', 'rust/src/main.rs'}
    if name not in allowed:
        raise ValueError('Only reference source/tests and Rust source can be read.')
    return LIB if name == 'rust/src/lib.rs' else HERE / name


def read_page(path, start, end, char_limit=12000):
    lines = path.read_text(encoding='utf-8').splitlines()
    output = [f'Total lines: {len(lines)}']
    size = len(output[0])
    next_line = start
    for i in range(start - 1, min(end, len(lines))):
        line = f'{i + 1}: {lines[i]}'
        if size + len(line) + 100 > char_limit:
            if next_line == start:
                return f'Line {start} exceeds the read limit; use search_source to locate a smaller region.'
            break
        output.append(line)
        size += len(line) + 1
        next_line = i + 2
    output.append(f'Next start: {next_line}' if next_line <= len(lines) else 'End of file.')
    return '\n'.join(output)


def t_read_file(args):
    path = source_path(args['path'])
    start = args.get('start', 1)
    end = args.get('end', start + 59)
    if not (1 <= start <= end and end - start < 160):
        raise ValueError('Use a 1-based inclusive range of at most 160 lines.')
    return read_page(path, start, end)


def t_search_source(args):
    lines = source_path(args['path']).read_text(encoding='utf-8').splitlines()
    if not args['text']:
        raise ValueError('Search text must not be empty.')
    hits = [i for i, line in enumerate(lines) if args['text'] in line]
    output = [f'{len(hits)} matching lines; showing at most 20 matches. Use read_file for bodies.']
    for i in hits[:20]:
        output.append(f'{i + 1}: {lines[i][:300]}')
    return '\n'.join(output)


def t_write_rust(args):
    content = args['content']
    if not content.strip():
        raise ValueError('Refusing empty Rust source.')
    LIB.write_text(content, encoding='utf-8')
    STATE['last_change'] = 'Rust written; previous evaluation is stale.'
    return f'wrote {len(content)} characters; sha256={code_hash()}'


def t_replace_rust(args):
    source = LIB.read_text(encoding='utf-8')
    if not args['old'] or source.count(args['old']) != 1:
        raise ValueError('old must match exactly once; read current Rust and retry.')
    return t_write_rust({'content': source.replace(args['old'], args['new'], 1)})


def report_rank(report):
    """Prefer compliance and passing tests over raw score, then fewer smells."""
    tests = report.get('cargo_test') or {}
    quality = report.get('quality') or {}
    complete = 'differential_pct' in report and 'violations' in report
    return (bool(report.get('build')), complete and not report['violations'],
            tests.get('passed', 0) > 0 and tests.get('failed') == 0
            and report.get('cargo_test_returncode') == 0,
            report.get('differential_pct', -1),
            -sum(quality.get(k, 0) for k in ('clone_calls', 'to_owned_calls', 'unwrap_calls')))


def t_evaluate(_args):
    LOGS.mkdir(exist_ok=True)
    path = LOGS / f'eval-{uuid.uuid4().hex}.json'
    result = _run([sys.executable, str(HERE / 'evaluate.py'), '--n', '300',
                   '--json', str(path)], cwd=HERE, timeout=300)
    report = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    STATE['last_report'] = report
    STATE['last_diagnostics'] = result['output']
    STATE['verified_hash'] = code_hash()
    rank, best = report_rank(report), STATE.get('best_rank')
    rollback = False
    if 'differential_pct' in report and (best is None or rank > best):
        STATE.update(best_rank=rank, best_score=report['differential_pct'], stale=0)
        STATE['best_code'] = LIB.read_text(encoding='utf-8')
        STATE['best_report'] = report
        checkpoint = pathlib.Path(STATE.get('checkpoint', LOGS / 'best-lib.rs'))
        checkpoint.write_text(STATE['best_code'], encoding='utf-8')
    else:
        STATE['stale'] = STATE.get('stale', 0) + 1
        if best is not None and rank < best:
            LIB.write_text(STATE['best_code'], encoding='utf-8')
            STATE['last_change'] = 'Regression: restored best Rust; read it before editing.'
            rollback = True
    return {'report': report, 'rollback': rollback, **result}


def t_save_notes(args):
    if len(args['notes']) > 3000:
        raise ValueError('Keep notes under 3000 characters.')
    STATE['notes'] = args['notes']
    return 'Notes saved in runner state and trajectory.'


def tool(name, description, fn, properties=None, required=None):
    return dict(name=name, description=description, fn=fn,
                parameters={'type': 'object', 'properties': properties or {},
                            'required': required or [], 'additionalProperties': False})


TEXT = {'type': 'string'}
TOOLS = [
    tool('read_file', 'Read source or reference tests by line range (max 160 lines).',
         t_read_file, {'path': TEXT, 'start': {'type': 'integer'}, 'end': {'type': 'integer'}}, ['path']),
    tool('read_python', 'Read Python source page; follow Next start using read_file.',
         lambda a: t_read_file({'path': 'reference/version.py'})),
    tool('read_rust', 'Read current Rust; accepts optional start/end for pagination.',
         lambda a: t_read_file({'path': 'rust/src/lib.rs', **a}) if a else read_page(LIB, 1, 1000000),
         {'start': {'type': 'integer'}, 'end': {'type': 'integer'}}),
    tool('search_source', 'Locate source definitions by literal text; returns line numbers.',
         t_search_source, {'path': TEXT, 'text': TEXT}, ['path', 'text']),
    tool('write_rust', 'Write complete lib.rs including tests.', t_write_rust, {'content': TEXT}, ['content']),
    tool('replace_rust', 'Replace one exact, unique substring of lib.rs.', t_replace_rust,
         {'old': TEXT, 'new': TEXT}, ['old', 'new']),
    tool('cargo_build', 'Compile Rust; return exit code and diagnostics.',
         lambda a: _run(['cargo', 'build', '--release'], cwd=RUST)),
    tool('cargo_test', 'Run Rust tests; return exit code and diagnostics.',
         lambda a: _run(['cargo', 'test', '--release'], cwd=RUST)),
    tool('evaluate', 'Full practice evaluation, checkpoint and regression rollback.', t_evaluate),
    tool('save_notes', 'Replace durable findings and next steps (max 3000 chars).',
         t_save_notes, {'notes': TEXT}, ['notes']),
]
BY_NAME = {t['name']: t for t in TOOLS}
SCHEMAS = [{k: t[k] for k in ('name', 'description', 'parameters')} for t in TOOLS]


def dispatch(call):
    entry = BY_NAME.get(call.get('name'))
    if entry is None:
        return {'error': f"unknown tool {call.get('name')!r}"}
    args = call.get('arguments')
    if not isinstance(args, dict):
        return {'error': 'Tool arguments must be a JSON object.'}
    schema = entry['parameters']
    if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
        return {'error': 'Missing required or unexpected arguments.'}
    for key, value in args.items():
        expected = str if schema['properties'][key]['type'] == 'string' else int
        if type(value) is not expected:
            return {'error': f'{key} must be {expected.__name__}.'}
    try:
        return entry['fn'](args)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {'error': str(exc)}


def preflight():
    issues = []
    try:
        model_config()
    except ValueError as exc:
        issues.append(str(exc))
    if not shutil.which('cargo'):
        issues.append('Install Rust/Cargo and restart terminal so cargo is on PATH.')
    if importlib.util.find_spec('semver') is None:
        issues.append('Install dependencies: python -m pip install -r requirements.txt')
    if not PYSRC.exists():
        issues.append('Download reference files: python fetch_source.py')
    return issues


def save_session(path, history):
    """Atomic local checkpoint; API credentials are never included."""
    payload = {'version': 1, 'workspace': str(HERE), 'code_hash': code_hash(),
               'state': STATE, 'history': history}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def load_session(path):
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('version') != 1 or payload.get('workspace') != str(HERE):
        raise ValueError('Incompatible checkpoint or different workspace.')
    if payload['code_hash'] != code_hash():
        raise ValueError('Rust changed since checkpoint; refusing to resume against different code.')
    state = payload['state']
    if not 0 <= state['steps'] <= state['budget'] <= 40:
        raise ValueError('Invalid saved call budget.')
    if state.get('best_rank') is not None:
        state['best_rank'] = tuple(state['best_rank'])
    STATE.clear()
    STATE.update(state)
    history = payload['history']
    history[0] = {'role': 'system', 'content': system_prompt()}
    return history


def wait_for_request(interval):
    """Space request starts, including retries/resumes; this adds no API calls."""
    delay = max(0, STATE.get('last_request_at', 0) + interval - time.time())
    if delay:
        print(f'Rate pacing: waiting {delay:.1f}s before next request.', flush=True)
        time.sleep(delay)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--budget', type=int, default=None, help='Total call cap, not additional calls (max 40).')
    parser.add_argument('--used-calls', type=int, default=0, help='Previously used calls when migrating older logs.')
    parser.add_argument('--resume', type=pathlib.Path, help='Continue a saved run-*.state.json checkpoint.')
    parser.add_argument('--min-interval', type=float, default=13, help='Seconds between request starts (default 13).')
    parser.add_argument('--task', default='Translate reference/version.py into rust/src/lib.rs.')
    args = parser.parse_args(argv)
    if args.budget is not None and not 1 <= args.budget <= 40:
        parser.error('--budget must be between 1 and 40')
    if not 0 <= args.min_interval <= 60:
        parser.error('--min-interval must be between 0 and 60')
    if args.resume and (args.budget is not None or args.used_calls):
        parser.error('--resume uses its saved budget; omit --budget and --used-calls')
    issues = preflight()
    if issues:
        print('\n'.join(issues), file=sys.stderr)
        return 2
    LOGS.mkdir(exist_ok=True)
    if args.resume:
        session = args.resume.resolve()
        try:
            history = load_session(session)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        log = session.with_name(session.name.removesuffix('.state.json') + '.jsonl')
    else:
        budget = args.budget if args.budget is not None else 40
        if not 0 <= args.used_calls <= budget:
            parser.error('--used-calls must be between zero and the total budget')
        run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        log = LOGS / f'{run_id}.jsonl'
        session = LOGS / f'{run_id}.state.json'
        STATE.clear()
        STATE.update(budget=budget, steps=args.used_calls, stale=0, notes='',
                     checkpoint=str(LOGS / f'{run_id}-best.rs'))
        history = [{'role': 'system', 'content': system_prompt()},
                   {'role': 'user', 'content': clip(args.task, 2000)}]
    args.budget = STATE['budget']

    def rec(**event):
        with log.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'t': time.time(), **event}, ensure_ascii=False) + '\n')

    provider, _, model, _ = model_config()
    rec(event='resume' if args.resume else 'start', budget=args.budget, used_calls=STATE['steps'],
        task=history[1]['content'], provider=provider, model=model,
        initial_code=LIB.read_text(encoding='utf-8'))
    step, last_score = STATE['steps'], STATE.get('last_report', {}).get('differential_pct')
    transient_failures = 0
    reason = 'interrupted'
    try:
        if STATE.get('verified_hash') != code_hash():
            baseline = t_evaluate({})
            rec(event='baseline', result=baseline)
            history.append({'role': 'tool', 'name': 'evaluate', 'content': json.dumps(baseline)})
        save_session(session, history)
        while True:
            stop, reason = should_stop(history, step, args.budget, last_score)
            if stop:
                break
            wait_for_request(args.min_interval)
            step += 1
            STATE.update(steps=step, last_request_at=time.time())
            # Reserve the call on disk before sending, even if the process crashes.
            save_session(session, history)
            context = build_context(history, step)
            rec(event='request', step=step, messages=context)
            try:
                reply = call_model(context, SCHEMAS)
            except TransientModelError as exc:
                transient_failures += 1
                rec(event='model_error', step=step, reason=str(exc), transient=True)
                if transient_failures >= 3 or step >= args.budget:
                    reason = f'stopped after transient API failure: {exc} ({step} calls used)'
                    break
                delay = 5 * transient_failures
                print(f'[{step}/{args.budget}] {exc} Retrying in {delay}s; retry counts toward budget.', flush=True)
                time.sleep(delay)
                continue
            transient_failures = 0
            rec(event='model', step=step, reply=reply)
            history.append({'role': 'assistant', 'content': reply.get('text') or '',
                            'tool_calls': reply.get('tool_calls') or []})
            print(f"[{step}/{args.budget}] {clip(reply.get('text') or '(tool actions)', 300)}")
            before_actions = code_hash()
            for call in reply.get('tool_calls') or []:
                output = dispatch(call)
                if call['name'] in ('cargo_build', 'cargo_test') and isinstance(output, dict):
                    STATE['last_diagnostics'] = output.get('output', str(output))
                content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
                rec(event='tool', step=step, name=call['name'], arguments=call['arguments'], output=output)
                history.append({'role': 'tool', 'name': call['name'], 'content': content})
                print(f"  -> {call['name']}: {clip(content, 400)}")
                if call['name'] == 'evaluate':
                    last_score = STATE.get('last_report', {}).get('differential_pct')
                save_session(session, history)
            if code_hash() != before_actions and STATE.get('verified_hash') != code_hash():
                output = t_evaluate({})
                last_score = STATE.get('last_report', {}).get('differential_pct')
                rec(event='auto_evaluate', step=step, result=output)
                history.append({'role': 'tool', 'name': 'evaluate', 'content': json.dumps(output)})
                print(f"  -> auto evaluation: {clip(output['output'], 1200)}")
            save_session(session, history)
    except KeyboardInterrupt:
        reason = 'user interrupted'
    except (RuntimeError, ValueError, KeyError, IndexError, TypeError, OSError) as exc:
        reason = f'run failed: {exc}'
        rec(event='error', step=step, reason=reason)
    if STATE.get('best_code') is not None and LIB.read_text(encoding='utf-8') != STATE['best_code']:
        LIB.write_text(STATE['best_code'], encoding='utf-8')
        rec(event='restore', reason='restore best evaluated code before final evaluation')
    final = (t_evaluate({}) if STATE.get('verified_hash') != code_hash() else
             {'report': STATE.get('last_report', {}), 'output': STATE.get('last_diagnostics', '')})
    rec(event='final_evaluation', result=final)
    rec(event='stop', reason=reason, steps=step, final_sha256=code_hash())
    STATE['steps'] = step
    save_session(session, history)
    print(f"\n[stop] {reason}\ntrajectory: {log}\nresume: python agent.py --resume \"{session}\"\n{final.get('output', '')}")
    return 0 if passed(final['report']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
