#!/usr/bin/env python3
"""Измерение памяти на настоящей модели: размеченный набор разговоров (ОТ-45, часть).

Что меряет — то, что решает модель в запросе на извлечение (`shturman.processing.extract`), после той же
проверки ответа, что у сервиса (`validate_facts`, `validate_projects`):
  facts      найден ли ожидаемый факт (по сообщению и цитате);
  about      о ком он: владелец, участник, проект;
  project    к какому проекту, организации или подразделению отнесён (Р-112: значение работы — у проекта);
  slot       ключ значения или «без ключа»;
  detail     уточнение ключа («бюджет (кровля)», Р-111);
  nature     вид сведения: свойство, событие, положение дел (Р-111);
  absent     сколько раз появилось то, чего быть не должно (разовое действие владельца в профиле,
             вопрос и оговорка как факт, значение работы у организации);
  kind       вид упоминания: проект, организация, подразделение (Р-112);
  documents  не больше двух фактов из документа;
  extra      факты, которых нет в разметке (не ошибка сама по себе — повод посмотреть).
Итог — по каждой строке: верно из всего и доля; `--by-tag` — то же по меткам примеров.

Запрос к модели — тот же, что у сервиса: инструкция `EXTRACT_INSTRUCTIONS`, данные `build_extract_input`,
схема `EXTRACT_SCHEMA`, сообщения `structured_messages` и клиент своей модели сервиса (`executor.llm`).
Адрес, ключ и модель — как у сервиса: `SHTURMAN_LLM_BASE_URL`, `SHTURMAN_LLM_API_KEY`,
`SHTURMAN_LLM_MODEL_EXTRACT` или `SHTURMAN_LLM_MODEL` (или `--base-url`, `--model`; ключ — только из
окружения). Ключ не печатается.

Примеры (из каталога service/):
  python tools/memory_eval.py                                  # своя модель сервиса из окружения
  python tools/memory_eval.py --model gpt-4.1-mini --by-tag
  python tools/memory_eval.py --save answers.jsonl             # сохранить ответы модели
  python tools/memory_eval.py --answers answers.jsonl          # пересчитать без модели

На сервере — изнутри контейнера сервиса (ключ своей модели есть только там); скрипт и набор в образ
не входят, их копируют (docs/memory.md, «Проверка качества»):
  docker cp tools/memory_eval.py shturman-service:/tmp/ && \\
  docker cp tests/processing/data/memory_eval.jsonl shturman-service:/tmp/
  docker exec shturman-service python /tmp/memory_eval.py --data /tmp/memory_eval.jsonl --by-tag

Набор синтетический: имена, компании и объекты вымышлены, провайдеру модели уходит только он. Числа
на нём — ориентир для сравнения моделей и правок инструкции, а не оценка на настоящей переписке.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "tests" / "processing" / "data" / "memory_eval.jsonl"
if (HERE.parent / "src").is_dir():
    sys.path.insert(0, str(HERE.parent / "src"))

from shturman.processing import extract  # noqa: E402

T0 = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
MAX_TOKENS = 4000
METRICS = ("facts", "about", "project", "slot", "detail", "nature", "kind", "documents")


# --- набор -----------------------------------------------------------------------------------------

def load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def episode_of(case: dict[str, Any]) -> extract.Episode:
    """Разговор примера как эпизод обработки: владелец — исходящие, остальные — по имени."""
    peers: dict[str, int] = {}
    messages = []
    for n, item in enumerate(case["messages"], start=1):
        owner = item["who"] == "owner"
        peer = None if owner else peers.setdefault(item["who"], 1000 + len(peers))
        messages.append(extract.Msg(
            id=n, chat_id=1, sent_at=T0 + timedelta(minutes=n), sender_peer_id=peer,
            sender_name=None if owner else item["who"], is_outgoing=owner, text=item["text"]))
    return extract.Episode(chat_id=1, messages=messages)


def request(case: dict[str, Any]) -> tuple[extract.Episode, dict[tuple, str], str]:
    episode = episode_of(case)
    labels = extract.speaker_labels(episode)
    data = extract.build_extract_input(
        episode, labels, timezone.utc, chat_kind=case.get("chat", "групповой"),
        projects=case.get("projects", ()), organizations=case.get("organizations", ()))
    return episode, labels, data


# --- оценка ----------------------------------------------------------------------------------------

@dataclass
class Score:
    ok: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    absent: int = 0
    absent_total: int = 0
    extra: int = 0
    misses: list[str] = field(default_factory=list)

    def mark(self, metric: str, good: bool, case_id: str, what: str = "") -> None:
        self.total[metric] += 1
        self.ok[metric] += int(good)
        if not good:
            self.misses.append(f"{case_id}: {metric}{' — ' + what if what else ''}")

    def add(self, other: "Score") -> None:
        for key, value in other.total.items():
            self.total[key] += value
            self.ok[key] += other.ok[key]
        self.absent += other.absent
        self.absent_total += other.absent_total
        self.extra += other.extra
        self.misses.extend(other.misses)


def _quote_hits(found: extract.FactCandidate, message: int, quote: str) -> bool:
    if found.message.id != message:
        return False
    a, b = extract.normalize(found.quote), extract.normalize(quote)
    return bool(a and b) and (a in b or b in a)


def _same_title(a: str | None, b: str | None) -> bool:
    x, y = extract.norm_title(a), extract.norm_title(b)
    return bool(x and y) and (x == y or x in y or y in x)


def _about(expected: str, labels: dict[tuple, str], episode: extract.Episode) -> tuple[str, tuple | None]:
    """Ожидаемое «о ком» в терминах FactCandidate: owner | project | peer (+ ключ говорящего)."""
    if expected in ("owner", "project"):
        return expected, None
    for message in episode.messages:
        if message.sender_name == expected:
            return "peer", message.speaker_key
    raise ValueError(f"в примере нет участника {expected!r}")


def score(case: dict[str, Any], parsed: Any) -> Score:
    """Ответ модели на один пример против разметки — той же проверкой, что у сервиса."""
    out = Score()
    episode, labels, _ = request(case)
    found, _ = extract.validate_facts(parsed, episode, labels)
    used: set[int] = set()
    for exp in case.get("facts", []):
        hit = next((i for i, f in enumerate(found) if i not in used and _quote_hits(f, exp["message"], exp["quote"])),
                   None)
        out.mark("facts", hit is not None, case["id"], exp["quote"])
        if hit is None:
            continue
        used.add(hit)
        got = found[hit]
        about, speaker = _about(exp["about"], labels, episode)
        out.mark("about", got.about == about and (speaker is None or got.speaker_key == speaker), case["id"],
                 f"{got.about}")
        if about == "project":
            out.mark("project", _same_title(got.project, exp.get("project")), case["id"], f"{got.project}")
        out.mark("slot", got.slot == exp.get("slot"), case["id"], f"{got.slot}")
        if exp.get("slot"):
            want = exp.get("detail")
            good = (got.slot_detail is None) if want is None else _same_title(got.slot_detail, want)
            out.mark("detail", good, case["id"], f"{got.slot_detail}")
        out.mark("nature", got.nature == exp.get("nature", "state"), case["id"], got.nature)
    for bad in case.get("absent", []):
        out.absent_total += 1
        about, _ = _about(bad["about"], labels, episode)
        for i, f in enumerate(found):
            if _quote_hits(f, bad["message"], bad["quote"]) and f.about == about and (
                    "project" not in bad or _same_title(f.project, bad["project"])):
                out.absent += 1
                out.misses.append(f"{case['id']}: absent — {bad['quote']}")
                used.add(i)
                break
    out.extra += len([f for i, f in enumerate(found) if i not in used])
    if "max_facts" in case:
        out.mark("documents", len(found) <= case["max_facts"], case["id"], f"фактов {len(found)}")
    mentions, _ = extract.validate_projects(parsed, episode)
    for exp in case.get("projects_expect", []):
        got = next((m for m in mentions if _same_title(m.title, exp["title"])), None)
        out.mark("kind", got is not None and (got.kind or "project") == exp["kind"], case["id"],
                 f"{exp['title']}: {got.kind if got else 'не названо'}")
    return out


def gold(case: dict[str, Any]) -> dict[str, Any]:
    """Ответ, совпадающий с разметкой: проверка самого набора (tests/processing/test_memory_eval.py)."""
    episode, labels, _ = request(case)
    facts = []
    for exp in case.get("facts", []):
        about, speaker = _about(exp["about"], labels, episode)
        facts.append({"message": exp["message"], "source_quote": exp["quote"], "text": exp["quote"],
                      "about": {"owner": extract.OWNER_LABEL, "project": "ПРОЕКТ"}.get(about) or labels[speaker],
                      "project": exp.get("project"), "slot": exp.get("slot"), "slot_detail": exp.get("detail"),
                      "kind": exp.get("kind", "fact"), "nature": exp.get("nature", "state")})
    return {"commitments": [], "facts": facts,
            "projects": [{"title": p["title"], "kind": p["kind"]} for p in case.get("projects_expect", [])]}


def report(score_: Score, title: str = "итого") -> list[str]:
    lines = [f"— {title}"]
    for metric in METRICS:
        if score_.total.get(metric):
            ok, total = score_.ok[metric], score_.total[metric]
            lines.append(f"  {metric:<10} {ok:>3} из {total:<3} {100 * ok / total:5.1f}%")
    if score_.absent_total:
        lines.append(f"  {'absent':<10} {score_.absent:>3} из {score_.absent_total:<3} (меньше — лучше)")
    lines.append(f"  {'extra':<10} {score_.extra:>3}")
    return lines


# --- модель ----------------------------------------------------------------------------------------

async def ask_model(cases: list[dict[str, Any]], *, base_url: str, model: str) -> dict[str, Any]:
    from shturman.executor import llm

    key = os.environ.get("SHTURMAN_LLM_API_KEY", "")
    if not key:
        raise SystemExit("нет ключа: SHTURMAN_LLM_API_KEY в окружении")
    client = llm.LlmClient(base_url=base_url, api_key=key, model=model)
    answers: dict[str, Any] = {}
    try:
        for case in cases:
            _, _, data = request(case)
            messages = llm.structured_messages(extract.EXTRACT_INSTRUCTIONS, data, extract.EXTRACT_SCHEMA,
                                               "commitments")
            try:
                text, _ = await client.chat(messages, task="extract", max_tokens=MAX_TOKENS, json_mode=True)
            except llm.LlmError as exc:
                print(f"{case['id']}: модель не ответила ({exc.code})", file=sys.stderr)
                answers[case["id"]] = None
                continue
            answers[case["id"]] = llm.parse_json(text)
    finally:
        await client.aclose()
    return answers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=DATA, help="набор (jsonl)")
    parser.add_argument("--answers", type=Path, help="готовые ответы модели (jsonl {id, parsed}) вместо запросов")
    parser.add_argument("--save", type=Path, help="записать ответы модели сюда (jsonl)")
    parser.add_argument("--base-url", default=os.environ.get("SHTURMAN_LLM_BASE_URL") or "https://api.openai.com/v1")
    parser.add_argument("--model", default=os.environ.get("SHTURMAN_LLM_MODEL_EXTRACT")
                        or os.environ.get("SHTURMAN_LLM_MODEL") or "")
    parser.add_argument("--tag", action="append", help="только примеры с этой меткой (можно несколько)")
    parser.add_argument("--by-tag", action="store_true", help="итог ещё и по каждой метке")
    parser.add_argument("--misses", action="store_true", help="перечислить промахи (номера примеров и поля)")
    args = parser.parse_args(argv)

    cases = load(args.data)
    if args.tag:
        cases = [c for c in cases if set(c.get("tags", ())) & set(args.tag)]
    if args.answers:
        answers = {row["id"]: row.get("parsed") for row in load(args.answers)}
        model = "ответы из файла"
    else:
        if not args.model:
            raise SystemExit("не задана модель: --model или SHTURMAN_LLM_MODEL")
        answers = asyncio.run(ask_model(cases, base_url=args.base_url, model=args.model))
        model = args.model
        if args.save:
            args.save.write_text("".join(json.dumps({"id": k, "parsed": v}, ensure_ascii=False) + "\n"
                                         for k, v in answers.items()), encoding="utf-8")
    total, by_tag = Score(), defaultdict(Score)
    for case in cases:
        one = score(case, answers.get(case["id"]))
        total.add(one)
        for tag in case.get("tags", ()):
            by_tag[tag].add(one)
    print(f"Память: {len(cases)} примеров, модель — {model}, инструкция v{extract.PROMPT_VERSION}")
    print("\n".join(report(total)))
    if args.by_tag:
        for tag in sorted(by_tag):
            print("\n".join(report(by_tag[tag], f"метка {tag}")))
    if args.misses and total.misses:
        print("— промахи")
        print("\n".join(f"  {line}" for line in total.misses))
    return 0


if __name__ == "__main__":
    sys.exit(main())
