"""Read-only creative entry context; semantic routing stays with the assistant.

The existing learning CLI retrieves evidence, but does not capture the active
skill versions or lifecycle reminder. This small adapter does both without a
classifier, task database, transcript reader, network call, or continuation loop.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from creative_learning import DEFAULT, creator_library, read, render, select, validate, write_new

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ('us-commerce-strategy', 'us-creator-video', 'us-photo-copy')
INTENTS = ('new', 'continue', 'feedback', 'pause', 'edit', 'status')


def reminder():
    # Only repository-authored routing enters hook context. Never interpolate
    # user prompts, case content or external run documents as instructions.
    return ('Read AGENTS.md and tracks/us/creative/learning/workflow-skills.md. '
            'Resolve market, exact product and current decisions before choosing skills semantically. '
            'US new creation: strategy then video/photo as requested. Preserve selected directions. '
            'For edit/status do only the requested work; SEA uses its own sources. '
            'Feedback/pause: preserve the exact artifact, source and scope; do not regenerate automatically. '
            'Gym/v14 remains paused; image approval and spending approval are separate. '
            'Use creative_session.py context to capture skill hashes and relevant evidence in the existing run. '
            'User silence is pending. Hooks never authorize or resume execution.')


def hook_output(event):
    name = event.get('hook_event_name')
    if name not in ('SessionStart', 'UserPromptSubmit', 'PostCompact', 'Stop', 'Interrupt'):
        raise ValueError('Unsupported lifecycle event')
    if name in ('Stop', 'Interrupt'):
        return {}  # Waiting, interruption and pause are valid terminal states.
    return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': reminder()}}


def context(market, intent, output_format, tags=(), library=None):
    if market not in ('US', 'SEA') or intent not in INTENTS:
        raise ValueError('Resolve market and intent before calling the context adapter')
    if output_format not in ('video', 'photo', 'none'):
        raise ValueError('Unknown output format')
    chosen = []
    if market == 'US' and intent in ('new', 'continue'):
        if output_format == 'none':
            raise ValueError('Creation requires the requested delivery format')
        chosen = [SKILLS[0], SKILLS[1 if output_format == 'video' else 2]]
    versions = []
    for name in chosen:
        path = ROOT / '.agents/skills' / name / 'SKILL.md'
        versions.append({'name': name, 'path': path.relative_to(ROOT).as_posix(),
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    result = {'schema_version': 1, 'market': market, 'intent': intent,
              'format': output_format, 'skills': versions,
              'selected_direction': 'preserve_exact_source_version' if intent == 'continue' else None,
              'execution_authorized': False, 'user_result': None,
              'routing_evidence': 'assistant_supplied_semantic_intent_not_model_behavior_test'}
    evidence = ''
    if chosen:
        path = Path(library or DEFAULT)
        doc = read(path)
        errors = validate(doc)
        if errors:
            raise ValueError('; '.join(errors))
        cases = select(doc, list(tags), 'message', 4)
        evidence = render(doc, cases, 'message')
        result['library_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        result['library_version'] = doc.get('library_version', 1)
        result['context_sha256'] = hashlib.sha256(evidence.encode('utf-8')).hexdigest()
        result['seed_case_ids'] = [c['id'] for c in cases]
        result['case_ids'] = [c['id'] for c in creator_library(doc, result['seed_case_ids'])['cases']]
    return result, evidence


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('hook')
    entry = commands.add_parser('context')
    entry.add_argument('--market', choices=('US', 'SEA'), required=True)
    entry.add_argument('--intent', choices=INTENTS, required=True)
    entry.add_argument('--format', choices=('video', 'photo', 'none'), default='none')
    entry.add_argument('--tags', default='')
    entry.add_argument('--library', type=Path)
    entry.add_argument('--output-dir', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'hook':
            print(json.dumps(hook_output(json.load(sys.stdin)), ensure_ascii=False))
            return 0
        result, evidence = context(args.market, args.intent, args.format,
                                   [x for x in args.tags.split(',') if x], args.library)
        if args.output_dir:
            out = args.output_dir.resolve()
            if out.is_relative_to(ROOT):
                raise ValueError('Run context must remain outside Git')
            out.mkdir(parents=True, exist_ok=False)
            write_new(out / 'context.json', json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            write_new(out / 'learning-context.md', evidence or 'No US creative evidence needed for this intent.\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.output_dir:
            print(evidence)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
