#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Линт стартовых промптов и runbook шага 0 (CI, задание ops).

1. Пути в обратных кавычках (`docs/…`, `prompts/…`, `ops/…`, `config/…`, `AGENTS.md` и т. п.) существуют в репозитории.
2. `./ops/<скрипт>.sh --ключ` — скрипт существует, а каждый `--ключ` упомянут в его тексте (шапка-справка или код).
3. Текст для владельца без технического жаргона: строки-цитаты (`> `) в docs/runbooks/hosting.md и разделы
   docs/hosting.md между <!-- owner-text --> и <!-- /owner-text --> не содержат слов из ops/tests/owner-terms-blocklist.txt.

Запуск из корня репозитория: python3 ops/tests/prompts_lint.py [файлы…]. Код выхода 1 — есть замечания.
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DEFAULT_FILES = ['prompts/deploy-remote.md', 'prompts/deploy.md', 'prompts/deploy-standalone.md',
                 'prompts/guided-chat.md', 'prompts/operate-codex.md',
                 'docs/runbooks/hosting.md', 'docs/hosting.md']
OWNER_TEXT_FILES = {'docs/hosting.md': 'marked', 'docs/runbooks/hosting.md': 'quotes'}
PATH_RE = re.compile(r'^(?:docs|prompts|ops|config|plugins|service|relay|asr|frpc|\.github)/[A-Za-z0-9_./\-]+$|'
                     r'^(?:AGENTS|README|UPGRADING|CHANGELOG|THIRD_PARTY_NOTICES|LICENSE)\.md$|^(?:VERSION|compose\.yaml|releases\.json)$')
SCRIPT_RE = re.compile(r'^\./ops/([A-Za-z0-9_\-]+\.sh)((?:\s+[^`]*)?)$')
FLAG_RE = re.compile(r'(?<!\S)(--?[A-Za-z][A-Za-z0-9\-]*)')


def spans(text):
    return re.findall(r'`([^`\n]+)`', text)


def check_paths(rel, text, problems):
    for span in spans(text):
        s = span.strip()
        if '…' in s or '<' in s or '*' in s:
            continue
        if PATH_RE.match(s):
            if not os.path.exists(os.path.join(ROOT, s)):
                problems.append(f'{rel}: путь `{s}` не существует')
            continue
        m = SCRIPT_RE.match(s)
        if m:
            script = os.path.join(ROOT, 'ops', m.group(1))
            if not os.path.exists(script):
                problems.append(f'{rel}: скрипта `./ops/{m.group(1)}` нет')
                continue
            with open(script, encoding='utf-8', errors='replace') as fh:
                body = fh.read()
            for flag in FLAG_RE.findall(m.group(2) or ''):
                if flag not in body:
                    problems.append(f'{rel}: ключ `{flag}` не упомянут в `ops/{m.group(1)}` — `{s}`')


def owner_lines(rel, text):
    mode = OWNER_TEXT_FILES.get(rel)
    if mode == 'quotes':
        return [(i + 1, l[1:]) for i, l in enumerate(text.splitlines()) if l.startswith('>')]
    if mode == 'marked':
        out, inside = [], False
        for i, l in enumerate(text.splitlines()):
            if '<!-- owner-text -->' in l:
                inside = True
                continue
            if '<!-- /owner-text -->' in l:
                inside = False
                continue
            if inside:
                out.append((i + 1, l))
        return out
    return []


def load_blocklist():
    path = os.path.join(ROOT, 'ops', 'tests', 'owner-terms-blocklist.txt')
    pats = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith('#'):
                pats.append(re.compile(line, re.IGNORECASE))
    return pats


def check_owner_text(rel, text, blocklist, problems):
    for lineno, line in owner_lines(rel, text):
        plain = re.sub(r'`[^`]*`', '', line)   # в обратных кавычках — команды и адреса, их не считаем
        for pat in blocklist:
            if pat.search(plain):
                problems.append(f'{rel}:{lineno}: в тексте для владельца слово по шаблону `{pat.pattern}`')


def main(argv):
    files = argv or DEFAULT_FILES
    blocklist = load_blocklist()
    problems = []
    for rel in files:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            problems.append(f'{rel}: файла нет')
            continue
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        check_paths(rel, text, problems)
        check_owner_text(rel, text, blocklist, problems)
    for p in problems:
        print(p)
    print(f'prompts_lint: файлов {len(files)}, замечаний {len(problems)}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
