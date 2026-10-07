"""Правила — базовая линия без модели: регулярные выражения на русском и английском.

Это НЕ надёжная защита. Правила ловят только известные формулировки («игнорируй предыдущие
инструкции», «не говори владельцу», «покажи системный промпт») и обходятся перефразированием,
другим языком, опечатками. Они нужны для двух вещей:
  * запасной вариант, когда модели-классификатора нет или она недоступна;
  * второе мнение рядом с моделью — и понятная владельцу причина («почему скрыто»).
Измеренные числа и то, что правила пропускают, — в `docs/guard.md`.

Устройство. Текст приводится к виду для сравнения (регистр, «ё», невидимые символы, латинские
буквы-двойники внутри кириллических слов). Затем ищутся признаки; у каждого вес от 0 до 1.
Оценка — «вероятность, что сработал хотя бы один»: 1 − Π(1 − вес). Слабые признаки поодиночке
порог не проходят («теперь ты отвечаешь за объект», «перешли всю переписку юристам» — обычная
речь между людьми), а в сочетании друг с другом — проходят.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

NAME = "rules-1"

_ZERO_WIDTH = re.compile(r"[­​-‏‪-‮⁠-⁩﻿]")
_SPACE = re.compile(r"\s+")
_WORD = re.compile(r"[^\W_]+")
# Латинские буквы и цифры, которыми подменяют кириллицу внутри русского слова.
_LOOKALIKE = str.maketrans("aceopxykmhbt03", "асеорхукмнвтоз")
_CYR = re.compile(r"[а-я]")
_LAT = re.compile(r"[a-z]")


def normalize(text: str) -> str:
    """Вид текста для сравнения с правилами. Исходный текст не меняется нигде, кроме этой копии."""
    value = unicodedata.normalize("NFKC", text or "")
    value = _ZERO_WIDTH.sub("", value).casefold().replace("ё", "е")

    def fix(match: re.Match[str]) -> str:
        word = match.group(0)
        # Смешанное слово («игн0рируй», «инcтpукции»): латиница и цифры читаются как кириллица.
        if _CYR.search(word) and (_LAT.search(word) or any(ch in "03" for ch in word)):
            return word.translate(_LOOKALIKE)
        return word

    return _SPACE.sub(" ", _WORD.sub(fix, value)).strip()


@dataclass(frozen=True)
class Rule:
    code: str          # короткое имя признака
    reason: str        # объяснение для владельца
    weight: float
    pattern: re.Pattern[str]


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in parts))


# Слова, которыми называют ассистента.
_AI = r"(?:ассистент\w*|помощник\w*|бот\w*|ии|ai|нейросет\w*|штурман\w*|агент\w*|assistant|bot|llm|chatbot)"
# Владелец в третьем лице: так пишут тому, кто работает ЗА человека, а не самому человеку.
_OWNER = (r"(?:владел\w+|хозя\w+|пользовател\w+|начальник\w*|руководител\w+|шеф\w*|"
          r"owner|user|account holder|boss)")
_NORMS = (r"(?:инструкц\w+|указани\w+|правил\w*|ограничени\w+|настройк\w+|установк\w+|контекст\w*|промпт\w*|"
          r"instructions?|rules?|guidelines?|prompts?|restrictions?)")
_NORMS_LAT = r"(?:instrukts\w+|ukazani\w+|pravil\w*|ogranicheni\w+|prompt\w*)"
_PRIOR = (r"(?:предыдущ\w+|прежн\w+|прошл\w+|ранн\w+|все|всё|свои\w*|твои\w*|текущ\w+|системн\w+|"
          r"previous|prior|earlier|above|all|your|any)")

RULES: tuple[Rule, ...] = (
    Rule("override", "просьба отменить или забыть прежние инструкции", 0.9, _rx(
        r"(?:игнорир\w+|забуд\w+|не обращай внимания на|сбрось|отмени|не следуй|"
        r"ignore|disregard|forget|override)\s+(?:\w+\s+){0,3}?" + _PRIOR + r"\s+(?:\w+\s+){0,2}?" + _NORMS,
        r"(?:игнорир\w+|забуд\w+|не обращай внимания на|сбрось|отмени|ignore|disregard|forget)\s+(?:\w+\s+){0,2}?"
        + _NORMS,
        r"(?:забуд\w+|forget)\s+(?:все|всё|everything),?\s+(?:что|чему|you were|you know)",
        r"(?:ignoriru\w+|zabud\w*)\W+(?:\w+\W+){0,3}?(?:" + _NORMS_LAT + r"|predydushch\w+)",
    )),
    Rule("void", "заявление, что прежние инструкции больше не действуют", 0.8, _rx(
        _NORMS + r"\s+(?:\w+\s+){0,4}?(?:отменен\w*|недействительн\w+|аннулирован\w*|устарел\w*|"
                 r"больше не действу\w+|are void|no longer apply)",
        r"(?:новые|актуальные)\s+(?:правила|инструкции|указания)\s*[:—-]",
        r"нов\w+ правил\w* для тебя",
        r"new (?:instructions|rules?)\b.{0,20}(?:for the assistant|:)",
        r"ограничени\w+ больше не действу\w+|no restrictions anymore",
    )),
    Rule("sysprompt", "просьба раскрыть системные инструкции", 0.9, _rx(
        r"(?:покажи|выведи|пришли|раскрой|повтори|напиши|вставь|print|reveal|show|repeat|send)\s+(?:\w+\s+){0,4}?"
        r"(?:системн\w+ (?:промпт\w*|инструкц\w+|сообщени\w+)|скрыт\w+ (?:инструкц\w+|правил\w*|промпт\w*)|"
        r"свои (?:системные |скрытые )?инструкции|system prompt|hidden (?:prompt|instructions|rules)|"
        r"your (?:system )?(?:prompt|instructions))",
        r"(?:какой|что) у тебя (?:системн\w+ )?промпт",
        r"в начале твоего контекста|выше этого сообщения,? начиная",
        r"(?:pokazhi|prishli)\W+(?:\w+\W+){0,3}?sistemn\w+ prompt",
    )),
    Rule("secrecy", "требование скрыть сообщение от владельца", 0.6, _rx(
        r"не (?:говори|сообщай|показывай|рассказывай|уведомляй|беспокой\w*)\s+(?:\w+\s+){0,3}?" + _OWNER,
        _OWNER + r"\s+(?:\w+\s+){0,3}?(?:не говори|не сообщай|не показыва\w+|ни слова|ничего не говори|знать не)",
        r"(?:скрой|спрячь|утаи)\s+(?:\w+\s+){0,3}?от " + _OWNER,
        r"(?:без|не (?:спрашива\w+|запрашива\w+|дожида\w+)\s+(?:\w+\s+){0,2}?)(?:ведома|подтверждени\w+|разрешени\w+)\s+"
        + _OWNER,
        r"у " + _OWNER + r" не спрашивай",
        r"(?:do not|don't|never|without)\s+(?:\w+\s+){0,2}?(?:tell|notify|inform|ask|telling|notifying|asking)\s+"
        r"(?:\w+\s+){0,2}?" + _OWNER,
        r"hide this (?:message|paragraph|email) from",
        r"ne (?:govori|pokazyvay)\W+(?:\w+\W+){0,3}?vladel\w+|vladel\w+\W+ne govori",
    )),
    Rule("address", "обращение к ассистенту, а не к человеку", 0.45, _rx(
        r"(?:^|[.!?»\"\n:;—-]\s*|\bуважаемый\s+|\bэй,?\s+|\bhey,?\s+|\bdear\s+)" + _AI + r"\s*[,!:]",
        r"(?:для|к)\s+(?:\w+-)?" + _AI + r"(?:\s*:|,|\s+котор\w+|\s+обрабатывающ\w+|\s+читающ\w+)",
        _AI + r",?\s+(?:котор\w+|читающ\w+|обрабатывающ\w+|разбирающ\w+)\s+(?:\w+\s+){0,3}?"
              r"(?:чита\w+|письмо|сообщени\w+|чат|переписк\w+|почт\w+)",
        r"если (?:это|письмо|сообщение)\s+(?:\w+\s+){0,2}?(?:читает|разбирает|обрабатывает)\s+(?:\w+\s+){0,2}?"
        r"(?:" + _AI + r"|автоматическ\w+)",
        r"(?:note|instructions?|message)\s+(?:to|for)\s+(?:the\s+)?(?:ai\s+)?(?:assistants?|bot|ai|automated)",
        r"(?:assistant|bot|ai)\s+(?:reading|processing)\s+this",
        r"для автоматических (?:систем|помощников)|for automated (?:assistants|systems)",
        r"всем\s+(?:\w+-)?(?:ассистентам|агентам|ботам)",
        r"\bassistent\b",
    )),
    Rule("role", "попытка сменить роль ассистента", 0.4, _rx(
        r"(?:ты теперь|теперь ты|ты больше не|с этой минуты ты|с этого момента ты)\b",
        r"режим\w* разработчика|developer mode|без (?:правил|ограничений|фильтров)",
        r"you are now\b|from (?:now|this moment)(?: on)?,? you\b|stay in (?:character|this role)",
        r"оставайся в (?:этой )?роли|представь,? что ты|\bty teper",
    )),
    Rule("as_owner", "просьба действовать от имени владельца", 0.4, _rx(
        r"от (?:его|ее|своего) имени",
        r"от имени (?:своего |твоего )?" + _OWNER,
        r"on (?:his|her|the owner's) behalf|on behalf of (?:your|the) " + _OWNER,
        r"ot ego imeni",
    )),
    Rule("exfil", "просьба переслать переписку или данные", 0.4, _rx(
        r"(?:перешли|отправь|скинь|пришли|выгрузи|передай|собери|процитируй|forward|send|export)\s+(?:\w+\s+){0,5}?"
        r"(?:всю переписку|весь архив|все сообщения|историю чата|чат целиком|все договоры|список всех контакт\w+|"
        r"все найденное|парол\w+|коды|последние \w+ сообщени\w+|"
        r"entire conversation|all (?:the )?messages|chat history|full list|passwords)",
        r"(?:pereshli|otprav'?)\W+(?:\w+\W+){0,3}?(?:perepisk\w+|soobshcheni\w+)",
    )),
    Rule("target", "внешний адрес, куда просят отправить", 0.3, _rx(
        r"\bна (?:адрес )?[\w.+-]+@[\w-]+\.[a-z]{2,}",
        r"\bна @\w{3,}|\bto [\w.+-]+@[\w-]+\.[a-z]{2,}",
        r"https?://\S+",
        r"na moyu pochtu",
    )),
    Rule("tool", "просьба вызвать инструмент или команду", 0.6, _rx(
        r"(?:вызови|используй|запусти|выполни|call|use|run)\s+(?:\w+\s+){0,3}?"
        r"(?:инструмент\w*|tool\w*|команд\w+|терминал\w*|send_message|api\b)",
        r"\bsend_message\b|cat /|\.env\b|через свой api",
        r"(?:отключи\w*|выключи\w*) подтверждени\w+|не дожидаясь нажатия|подтверждени\w+ (?:владельца )?не (?:запрашивай|требуется|нужно)",
        r"(?:добавь|внеси|запомни)\s+(?:\w+\s+){0,4}?в (?:свою )?память|в список (?:обязательств|доверенных)",
        r"need no owner confirmation|do not ask the owner to confirm",
    )),
    Rule("marker", "поддельная служебная разметка", 0.5, _rx(
        r"(?:^|\s)(?:system|система|assistant override)\s*:",
        r"</?\s*(?:system|message|assistant)\s*>",
        r"#{3}\s*\w+",
        r"\[\s*(?:служебное|system|inst)\b",
    )),
)

THRESHOLD = 0.5


def explain(text: str) -> list[Rule]:
    """Сработавшие признаки — для объяснения владельцу и для оценки."""
    value = normalize(text)
    return [rule for rule in RULES if rule.pattern.search(value)]


def score_one(text: str) -> float:
    miss = 1.0
    for rule in explain(text):
        miss *= 1.0 - rule.weight
    return round(1.0 - miss, 4)


class RulesScorer:
    """`Scorer` на правилах. Модели не нужна, в сеть не ходит."""

    name = NAME
    threshold = THRESHOLD

    def score(self, texts: Sequence[str]) -> list[float]:
        return [score_one(text) for text in texts]
