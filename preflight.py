"""Preflight checks for a Mediforce workflow package before you register or run it.

Catches the failure modes schema validation does not: a Dockerfile whose COPY
sources sit outside the build context, a `command` pointing at a path no COPY
produces, build mode without working repo auth, a prebuilt image that only
exists locally, and a `commit` that was never pushed.

    python preflight.py [path/to/workflow.wd.json]

Copy this file to the root of your workflow repo. It resolves `dockerfile` paths
and git state relative to its own directory, which must be the repo root.

Exits non-zero if any check fails. Platform secrets cannot be read from here, so
that check is advisory — it prints the command to verify.

Each failure references the rule it enforces in the build-workflow skill's
references/platform-contract.md.
"""
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

failures = []
notes = []


def fail(message):
    failures.append(message)
    print(f'FAIL  {message}')


def ok(message):
    print(f'ok    {message}')


def note(message):
    notes.append(message)
    print(f'note  {message}')


def copy_pairs(dockerfile_path):
    """Each COPY's (source, destination), as written. Ignores --flags."""
    pairs = []
    with open(dockerfile_path) as handle:
        for line in handle:
            match = re.match(r'^\s*COPY\s+(?:--\S+\s+)*(\S+)\s+(\S+)', line)
            if match:
                pairs.append((match.group(1), match.group(2)))
    return pairs


def check_build_context(step_id, script, build_context, dockerfile_path):
    """§2 — every COPY source must exist relative to the Dockerfile's directory.
    §3 — the command path must be under a COPY destination."""
    pairs = copy_pairs(dockerfile_path)
    if not pairs:
        note(f'{step_id}: Dockerfile has no COPY — nothing from the package is in the image')
        return

    for src, _dest in pairs:
        if os.path.exists(os.path.join(build_context, src)):
            ok(f'{step_id}: COPY {src} exists in build context ({os.path.relpath(build_context, REPO_ROOT)})')
        else:
            fail(
                f'{step_id}: [contract §2] COPY source "{src}" is not in the build '
                f'context "{os.path.relpath(build_context, REPO_ROOT)}". The build '
                f'context is the DIRECTORY OF THE DOCKERFILE, not the repo root — '
                f'move the Dockerfile next to {src}.'
            )

    command = script.get('command', '')
    script_path = next((part for part in command.split() if part.startswith('/')), None)
    if script_path is None:
        note(f'{step_id}: command has no absolute path — cannot check it against COPY')
        return

    if any(script_path.startswith(dest) for _src, dest in pairs):
        ok(f'{step_id}: command path {script_path} is under a COPY destination')
    else:
        dests = ', '.join(dest for _src, dest in pairs)
        fail(
            f'{step_id}: [contract §3] command runs "{script_path}" but no COPY puts '
            f'anything there (COPY destinations: {dests})'
        )


def check_auth(step_id, step, script, workflow_env):
    """§4 — build mode always clones over SSH unless a token is wired end to end.

    The token is read from the RESOLVED env, which merges workflow-level env with
    step-level env. Either level satisfies it.
    """
    repo_auth = script.get('repoAuth')
    if not repo_auth:
        fail(
            f'{step_id}: [contract §4] build mode without repoAuth. normalizeRepoUrls '
            'rewrites every https:// repo URL to its git@github.com SSH form before '
            'cloning, so this clones over SSH and fails. Set repoAuth even for a '
            'public repo.'
        )
        return

    ok(f'{step_id}: repoAuth = {repo_auth}')

    merged_env = {**workflow_env, **(step.get('env') or {})}
    if repo_auth in merged_env:
        level = 'step' if repo_auth in (step.get('env') or {}) else 'workflow'
        ok(f'{step_id}: {repo_auth} is declared in {level}-level env')
    else:
        fail(
            f'{step_id}: [contract §4] repoAuth names "{repo_auth}" but neither '
            f'workflow env nor step env declares it. resolveStepEnv only exposes '
            f'explicitly declared keys, so the token resolves to undefined and it '
            f'SILENTLY falls back to SSH. Add at the workflow level: '
            f'"env": {{"{repo_auth}": "{{{{{repo_auth}}}}}"}}'
        )


def check_commit_pushed(step_id, script):
    """§6 — the image build clones the remote, so a local-only commit cannot build."""
    repo, commit = script.get('repo'), script.get('commit')
    if not commit:
        fail(f'{step_id}: [contract §6] repo is set but commit is missing — the build silently no-ops')
        return
    if set(commit) == {'0'}:
        fail(f'{step_id}: [contract §6] commit is the all-zeros placeholder — fill the real SHA before running')
        return

    try:
        subprocess.run(
            ['git', 'cat-file', '-e', f'{commit}^{{commit}}'],
            cwd=REPO_ROOT, check=True, capture_output=True, timeout=30,
        )
    except subprocess.CalledProcessError:
        fail(f'{step_id}: [contract §6] commit {commit[:12]} does not exist in this checkout')
        return
    except (OSError, subprocess.TimeoutExpired) as error:
        note(f'{step_id}: could not check commit locally ({error})')
        return

    try:
        result = subprocess.run(
            ['git', 'branch', '-r', '--contains', commit],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        if result.stdout.strip():
            ok(f'{step_id}: commit {commit[:12]} is pushed ({result.stdout.split()[0]})')
        else:
            fail(
                f'{step_id}: [contract §6] commit {commit[:12]} exists locally but is '
                f'NOT on any remote branch. `register --file` reads your working tree, '
                f'but the image build clones {repo} at this SHA. Push first.'
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        note(f'{step_id}: could not check whether the commit is pushed ({error})')


def check_graph(wd):
    """§10 — graph rules validateStepGraph enforces but the Zod schema does not."""
    steps = wd['steps']
    step_ids = {s['id'] for s in steps}
    transitions = wd.get('transitions') or []

    if not any(s.get('type') == 'terminal' for s in steps):
        fail('[contract §10] no step has type "terminal" — validateStepGraph rejects this')
    else:
        ok('a terminal step exists')

    for step in steps:
        if step.get('type') == 'terminal':
            continue
        has_transition = any(t['from'] == step['id'] for t in transitions)
        has_verdicts = bool(step.get('verdicts'))
        if not has_transition and not has_verdicts:
            fail(f'[contract §10] non-terminal step "{step["id"]}" has no outgoing transition and no verdicts')

    for step in steps:
        if step.get('selection') is not None and step.get('type') != 'review':
            fail(
                f'[contract §10] step "{step["id"]}" sets selection but type is '
                f'"{step.get("type")}" — selection is only valid on type "review"'
            )

    for target in [t['to'] for t in transitions] + [t['from'] for t in transitions]:
        if target not in step_ids:
            fail(f'transition references unknown step "{target}"')

    for step in steps:
        for key, verdict in (step.get('verdicts') or {}).items():
            if verdict.get('target') not in step_ids:
                fail(f'step "{step["id"]}" verdict "{key}" targets unknown step "{verdict.get("target")}"')


def main():
    if len(sys.argv) > 1:
        wd_path = sys.argv[1]
    else:
        candidates = []
        for base in (REPO_ROOT, *[os.path.join(REPO_ROOT, d) for d in sorted(os.listdir(REPO_ROOT))]):
            src = os.path.join(base, 'src')
            if os.path.isdir(src):
                candidates += [os.path.join(src, f) for f in sorted(os.listdir(src)) if f.endswith('.wd.json')]
        if len(candidates) != 1:
            print('Pass the .wd.json path explicitly:\n    python preflight.py src/<name>.wd.json')
            if candidates:
                print('\nFound:\n  ' + '\n  '.join(os.path.relpath(c, REPO_ROOT) for c in candidates))
            return 2
        wd_path = candidates[0]

    with open(wd_path) as handle:
        wd = json.load(handle)

    print(f'Preflight: {os.path.relpath(wd_path, REPO_ROOT)} ({wd["name"]})\n')
    workflow_env = wd.get('env') or {}
    secrets_needed = set()

    check_graph(wd)

    for step in wd['steps']:
        step_id = step['id']
        for key in ('script', 'agent'):
            config = step.get(key)
            if not config:
                continue

            if config.get('inlineScript'):
                note(f'{step_id}: inline script ({len(config["inlineScript"])} chars) — '
                     'move to a package file once it outgrows a screen')
                continue

            if not config.get('command'):
                continue

            build_mode = bool(config.get('dockerfile') or config.get('repo'))
            if not build_mode:
                fail(
                    f'{step_id}: [contract §1/§5] command mode with image '
                    f'"{config.get("image")}" and no dockerfile/repo. Command mode '
                    f'cannot reach your package — the code must be baked into the '
                    f'image, and a locally-built tag does not exist on the runner. '
                    f'/workspace is the run worktree, NOT your repo. Use build mode '
                    f'(dockerfile + repo + commit) and omit image.'
                )
                continue

            if config.get('image'):
                note(f'{step_id}: image "{config["image"]}" is set alongside build mode — '
                     'omit it to get the commit-derived tag and automatic rebuilds')

            dockerfile = config.get('dockerfile', 'Dockerfile')
            dockerfile_path = os.path.join(REPO_ROOT, dockerfile)
            if not os.path.isfile(dockerfile_path):
                fail(f'{step_id}: dockerfile "{dockerfile}" not found (resolved from repo root)')
                continue
            ok(f'{step_id}: dockerfile {dockerfile} exists')

            check_build_context(step_id, config, os.path.dirname(dockerfile_path), dockerfile_path)
            check_auth(step_id, step, config, workflow_env)
            if config.get('repo'):
                check_commit_pushed(step_id, config)
            if config.get('repoAuth'):
                secrets_needed.add(config['repoAuth'])

    for template in {**workflow_env, **{k: v for s in wd['steps'] for k, v in (s.get('env') or {}).items()}}.values():
        match = re.match(r'^\{\{(?:SECRET:)?([A-Za-z0-9_-]+)\}\}$', str(template))
        if match:
            secrets_needed.add(match.group(1))

    for key in sorted(secrets_needed):
        note(f'platform secret "{key}" must exist — verify with:\n'
             f'      pnpm exec mediforce secret list --namespace <ns>\n'
             f'      pnpm exec mediforce secret list --namespace <ns> --workflow {wd["name"]}')

    print()
    if failures:
        print(f'{len(failures)} check(s) FAILED — fix before registering.')
        return 1
    print(f'All checks passed ({len(notes)} note(s)).')
    print('Reminder: this proves none of it runs. Register, run, and read the output.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
