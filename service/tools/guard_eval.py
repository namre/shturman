#!/usr/bin/env python3
"""Измерение защиты от внедрённых инструкций на размеченном наборе.

Что меряет: для каждого оценщика — точность (доля верных среди скрытых), полноту (доля пойманных
атак), долю ложных срабатываний на «норме» и отдельно на «трудной норме» (виды `hard_*`:
повелительное наклонение между людьми, разговоры про ИИ, пересланные рабочие инструкции),
время на одно сообщение. Отдельно по каждому языку набора.

Оценщики (`--scorer`, можно несколько):
  rules                  правила из сервиса (`shturman.guard.rules`), без модели;
  tei=http://адрес:порт  работающий контейнер TEI с моделью-классификатором — тот же клиент,
                         которым пользуется сервис (`shturman.guard.tei`);
  hf=путь_или_имя[@рев]  модель напрямую через transformers и torch, без TEI. Нужна только там,
                         где нет Docker; сами библиотеки в образ сервиса не входят:
                         pip install torch transformers sentencepiece
С ключом `--with-rules` для каждой модели печатается и вариант «модель ИЛИ правила».

Примеры (из каталога service/):
  python tools/guard_eval.py --scorer rules
  python tools/guard_eval.py --scorer tei=http://127.0.0.1:8080 --with-rules
  python tools/guard_eval.py --scorer hf=Horizon-Labs/prompt-injection-guard-small@3215a27 --threshold 0.5 0.9

На сервере контейнер с моделью закрыт от интернета и от хоста, поэтому мерить нужно изнутри
контейнера сервиса; скрипт и набор в образ не входят, их копируют (команды — в docs/guard.md):
  docker exec shturman-service python /tmp/guard_eval.py --scorer tei=http://guard:80 --data /tmp/guard-data/ru.jsonl

Текст примеров никуда не отправляется, кроме указанного оценщика. Набор — синтетический,
имена и компании вымышлены; числа на нём — ориентир, а не оценка на настоящей переписке.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from shturman.guard import rules  # noqa: E402
from shturman.guard.tei import BENIGN_LABELS, DEFAULT_OVERLAP, DEFAULT_WINDOW, TeiScorer, windows  # noqa: E402

DATA = HERE.parent / "tests" / "guard" / "data"


class HfScorer:
    """Модель напрямую (transformers, CPU) — с тем же разрезанием на окна, что у клиента TEI."""

    def __init__(self, spec: str, window: int, overlap: int) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        model_id, _, revision = spec.partition("@")
        kwargs = {"revision": revision} if revision else {}
        self._torch = torch
        self._tok = AutoTokenizer.from_pretrained(model_id, **kwargs)
        self._model = AutoModelForSequenceClassification.from_pretrained(model_id, **kwargs).eval()
        labels = {int(k): str(v) for k, v in self._model.config.id2label.items()}
        self._benign = [i for i, name in labels.items() if name.strip().lower() in BENIGN_LABELS]
        if not self._benign:
            raise SystemExit(f"{spec}: не понял, какой класс «обычный текст»: {labels}")
        self.name = spec
        self.labels = labels
        self.window, self.overlap = window, overlap
        self.params = sum(p.numel() for p in self._model.parameters())
        self.max_tokens = min(int(getattr(self._model.config, "max_position_embeddings", 512)), 512)

    def _predict(self, pieces: list[str]) -> list[float]:
        torch = self._torch
        enc = self._tok(pieces, padding=True, truncation=True, max_length=self.max_tokens, return_tensors="pt")
        with torch.no_grad():
            logits = self._model(**enc).logits
        probs = torch.softmax(logits, -1)
        return (1 - probs[:, self._benign].sum(-1)).tolist()

    def score(self, texts: Sequence[str]) -> list[float]:
        pieces, owner = [], []
        for index, text in enumerate(texts):
            for piece in windows(text, self.window, self.overlap):
                pieces.append(piece)
                owner.append(index)
        out = [0.0] * len(texts)
        for start in range(0, len(pieces), 16):
            for offset, value in enumerate(self._predict(pieces[start:start + 16])):
                out[owner[start + offset]] = max(out[owner[start + offset]], value)
        return out


def build(spec: str, window: int, overlap: int):
    """Возвращает (имя, оценщик модели или None — тогда это правила)."""
    if spec == "rules":
        return spec, None
    kind, _, arg = spec.partition("=")
    if kind == "tei":
        scorer = TeiScorer(arg, "", window=window, overlap=overlap, timeout=120.0)
        info = scorer._request("GET", "/info").json()
        scorer.name = info.get("model_id") or arg
        scorer._verified = True
        return spec, scorer
    if kind == "hf":
        return spec, HfScorer(arg, window, overlap)
    raise SystemExit(f"неизвестный оценщик: {spec}")


def load(paths: Sequence[Path]) -> list[dict]:
    items = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                item.setdefault("lang", path.stem)
                items.append(item)
    return items


def metrics(items: Sequence[dict], flagged: Sequence[bool]) -> dict:
    pairs = list(zip(items, flagged))
    attacks = [f for it, f in pairs if it["label"] == "attack"]
    benign = [f for it, f in pairs if it["label"] == "benign"]
    hard = [f for it, f in pairs if it["label"] == "benign" and it["kind"].startswith("hard_")]
    tp, fp = sum(attacks), sum(benign)
    return {
        "n_attack": len(attacks), "n_benign": len(benign), "n_hard": len(hard),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / len(attacks) if attacks else None,
        "fpr": fp / len(benign) if benign else None,
        "fpr_hard": sum(hard) / len(hard) if hard else None,
    }


def by_kind(items: Sequence[dict], flagged: Sequence[bool]) -> dict[str, str]:
    out: dict[str, list[int]] = {}
    for it, f in zip(items, flagged):
        cell = out.setdefault(f"{it['label']}/{it['kind']}", [0, 0])
        cell[0] += int(f)
        cell[1] += 1
    return {k: f"{a}/{b}" for k, (a, b) in sorted(out.items())}


def fmt(value: float | None) -> str:
    return "  —  " if value is None else f"{value:5.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", nargs="*", type=Path, default=sorted(DATA.glob("*.jsonl")))
    parser.add_argument("--scorer", action="append", required=True)
    parser.add_argument("--threshold", nargs="*", type=float, default=[0.5],
                        help="пороги для модели (правила всегда со своим порогом)")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="знаков в окне; 0 — не резать")
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    parser.add_argument("--latency", type=int, default=60, help="сколько сообщений мерить по одному")
    parser.add_argument("--with-rules", action="store_true", help="добавить строки «модель ИЛИ правила»")
    parser.add_argument("--kinds", action="store_true", help="показать разбивку по видам примеров")
    parser.add_argument("--misses", action="store_true", help="показать идентификаторы ошибок")
    parser.add_argument("--json", type=Path, help="записать результаты в файл")
    args = parser.parse_args()

    items = load(args.data)
    langs = sorted({it["lang"] for it in items})
    texts = [it["text"] for it in items]
    rule_scores = [rules.score_one(t) for t in texts]
    report = []
    for spec in args.scorer:
        name, model = build(spec, args.window, args.overlap)
        entry: dict = {"scorer": name, "window": args.window, "results": []}
        if model is not None:
            model.score(texts[:2])   # прогрев
            started = time.perf_counter()
            scores = model.score(texts)
            entry["batch_per_s"] = round(len(texts) / (time.perf_counter() - started), 1)
            entry["model"] = getattr(model, "name", "")
            if hasattr(model, "params"):
                import resource

                entry["params"] = model.params
                entry["labels"] = model.labels
                # Память всего процесса измерения (torch и модель), а не контейнера TEI.
                entry["rss_peak_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
            # время меряется на самой большой языковой группе набора (обычно русской)
            main = max(langs, key=lambda lang: sum(it["lang"] == lang for it in items))
            sample = [it["text"] for it in items if it["lang"] == main][: args.latency]
            laps = []
            for text in sample:
                lap = time.perf_counter()
                model.score([text])
                laps.append((time.perf_counter() - lap) * 1000)
            laps.sort()
            entry["latency_ms_p50"] = round(statistics.median(laps), 1)
            entry["latency_ms_p95"] = round(laps[max(0, int(len(laps) * 0.95) - 1)], 1)
            thresholds = args.threshold
        else:
            scores = None
            started = time.perf_counter()
            for text in texts:
                rules.score_one(text)
            entry["latency_ms_p50"] = round((time.perf_counter() - started) * 1000 / len(texts), 3)
            thresholds = [rules.THRESHOLD]
        print(f"\n== {name}" + (f"  ({entry.get('model')})" if model is not None else ""))
        if "latency_ms_p50" in entry:
            extra = f", p95 {entry['latency_ms_p95']} мс, пачкой {entry['batch_per_s']} сообщ./с" if model is not None else ""
            print(f"   время на сообщение: {entry['latency_ms_p50']} мс{extra}")
        print("   порог  язык  атак/норма(трудн.)  точность  полнота  ложные  ложные(трудн.)")
        variants = [False, True] if (args.with_rules and scores is not None) else [scores is None]
        for threshold, with_rules in [(t, v) for v in variants for t in thresholds]:
            flagged = [
                (scores is not None and scores[i] >= threshold)
                or (with_rules and rule_scores[i] >= rules.THRESHOLD)
                for i in range(len(items))
            ]
            for lang in langs:
                part = [i for i, it in enumerate(items) if it["lang"] == lang]
                m = metrics([items[i] for i in part], [flagged[i] for i in part])
                row = {"threshold": threshold, "lang": lang, "with_rules": with_rules, **m}
                mark = "+пр" if with_rules and scores is not None else "   "
                print(f"{mark}{threshold:5.2f}  {lang:4}  {m['n_attack']:3}/{m['n_benign']:3}({m['n_hard']:3})"
                      f"         {fmt(m['precision'])}    {fmt(m['recall'])}   {fmt(m['fpr'])}   {fmt(m['fpr_hard'])}")
                if args.kinds:
                    row["kinds"] = by_kind([items[i] for i in part], [flagged[i] for i in part])
                    print("        " + "  ".join(f"{k} {v}" for k, v in row["kinds"].items()))
                if args.misses:
                    missed = [items[i].get("id", str(i)) for i in part
                              if items[i]["label"] == "attack" and not flagged[i]]
                    false = [items[i].get("id", str(i)) for i in part
                             if items[i]["label"] == "benign" and flagged[i]]
                    print("        пропущено:", ", ".join(missed) or "—")
                    print("        ложные:", ", ".join(false) or "—")
                entry["results"].append(row)
        if scores is not None:
            entry["scores"] = {it.get("id", str(i)): round(s, 4) for i, (it, s) in enumerate(zip(items, scores))}
        report.append(entry)
        close = getattr(model, "close", None)
        if callable(close):
            close()
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
