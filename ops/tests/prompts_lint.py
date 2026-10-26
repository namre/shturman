#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Линт стартовых промптов и runbook шага 0 (CI, задание ops).

1. Пути в обратных кавычках (`docs/…`, `prompts/…`, `ops/…`, `config/…`, `AGENTS.md`, `DEVELOPING.md` и т. п.) существуют в репозитории.
2. `./ops/<скрипт>.sh --ключ` — скрипт существует, а каждый `--ключ` упомянут в его тексте (шапка-справка или код).
3. Текст для владельца без технического жаргона: строки-цитаты (`> `) в runbook из QUOTE_OWNER_FILES и разделы
   между строками <!-- owner-text --> и <!-- /owner-text --> (метка — одна на строке; упоминание в тексте не в счёт)
   во всех файлах *.md репозитория (находятся сами) не содержат слов из ops/tests/owner-terms-blocklist.txt;
   каждый такой раздел закрыт.
4. Относительные ссылки Markdown `[…](путь)` в проверяемых файлах ведут на существующие файлы.

Запуск из корня репозитория: python3 ops/tests/prompts_lint.py [файлы…]. Без файлов — промпты, runbook из QUOTE_OWNER_FILES,
README.md, AGENTS.md, DEVELOPING.md и все *.md с разделами для владельца. Код выхода 1 — есть замечания.
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DEFAULT_FILES = ['prompts/deploy-remote.md', 'prompts/deploy.md', 'prompts/deploy-standalone.md',
                 'prompts/guided-chat.md', 'prompts/operate.md',
                 'docs/runbooks/hosting.md', 'docs/hosting.md', 'README.md', 'AGENTS.md', 'DEVELOPING.md',
                 'docs/runbooks/server-prepare.md', 'docs/runbooks/deploy.md', 'docs/runbooks/update.md',
                 'docs/runbooks/restore.md', 'docs/runbooks/symptoms.md', 'docs/runbooks/deploy-standalone.md']
# Здесь текст для владельца — строки-цитаты «> » (готовые фразы на стоп-точках runbook).
QUOTE_OWNER_FILES = {'docs/runbooks/hosting.md', 'docs/runbooks/server-prepare.md', 'docs/runbooks/deploy.md',
                     'docs/runbooks/update.md', 'docs/runbooks/restore.md', 'docs/runbooks/symptoms.md',
                     'docs/runbooks/deploy-standalone.md'}
OPEN_MARK, CLOSE_MARK = '<!-- owner-text -->', '<!-- /owner-text -->'
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'dist'}
PATH_RE = re.compile(r'^(?:docs|prompts|ops|config|plugins|service|relay|asr|frpc|\.github)/[A-Za-z0-9_./\-]+$|'
                     r'^(?:AGENTS|DEVELOPING|README|UPGRADING|CHANGELOG|THIRD_PARTY_NOTICES|LICENSE)\.md$|^(?:VERSION|compose\.yaml|releases\.json)$')
SCRIPT_RE = re.compile(r'^\./ops/([A-Za-z0-9_\-]+\.sh)((?:\s+[^`]*)?)$')
FLAG_RE = re.compile(r'(?<!\S)(--?[A-Za-z][A-Za-z0-9\-]*)')
LINK_RE = re.compile(r'\]\(([^)\s]+)\)')


def marked_files():
    """Все *.md репозитория с разделом для владельца — пути от корня, по алфавиту."""
    found = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in names:
            if not name.endswith('.md'):
                continue
            path = os.path.join(base, name)
            with open(path, encoding='utf-8', errors='replace') as fh:
                if any(line.strip() == OPEN_MARK for line in fh):
                    found.append(os.path.relpath(path, ROOT).replace(os.sep, '/'))
    return sorted(found)


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


def check_links(rel, text, problems):
    """Относительные ссылки Markdown ведут на существующие файлы (якорь после «#» не проверяется)."""
    for target in LINK_RE.findall(text):
        if re.match(r'^[a-z][a-z0-9+.\-]*:', target, re.IGNORECASE) or target.startswith('#'):
            continue
        path = target.split('#', 1)[0]
        if path and not os.path.exists(os.path.normpath(os.path.join(ROOT, os.path.dirname(rel), path))):
            problems.append(f'{rel}: ссылка `{target}` ведёт на несуществующий файл')


def owner_lines(rel, text, problems):
    out = []
    if rel in QUOTE_OWNER_FILES:
        out += [(i + 1, l[1:]) for i, l in enumerate(text.splitlines()) if l.startswith('>')]
    inside, opened = False, 0
    for i, l in enumerate(text.splitlines()):
        if l.strip() == OPEN_MARK:
            if inside:
                problems.append(f'{rel}:{i + 1}: раздел для владельца открыт повторно, не закрыв прежний')
            inside, opened = True, i + 1
            continue
        if l.strip() == CLOSE_MARK:
            if not inside:
                problems.append(f'{rel}:{i + 1}: закрытие раздела для владельца без открытия')
            inside = False
            continue
        if inside:
            out.append((i + 1, l))
    if inside:
        problems.append(f'{rel}:{opened}: раздел для владельца не закрыт ({CLOSE_MARK})')
    return out


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
    for lineno, line in owner_lines(rel, text, problems):
        plain = re.sub(r'`[^`]*`', '', line)   # в обратных кавычках — команды и адреса, их не считаем
        for pat in blocklist:
            if pat.search(plain):
                problems.append(f'{rel}:{lineno}: в тексте для владельца слово по шаблону `{pat.pattern}`')


def main(argv):
    files = argv or DEFAULT_FILES + [f for f in marked_files() if f not in DEFAULT_FILES]
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
        check_links(rel, text, problems)
        check_owner_text(rel, text, blocklist, problems)
    for p in problems:
        print(p)
    print(f'prompts_lint: файлов {len(files)}, замечаний {len(problems)}')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
