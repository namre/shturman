"""Reproducible, deliberately small security mutation check (not a pytest suite).

Run: python tests/security_mutations.py --output-dir /tmp/security-mutations
CI with an isolated test database may add --include-db. Only src/, tests/ and
pyproject.toml are copied; no instance files, real Telegram or providers are used.
Every mutation changes one exact anchor in a fresh temporary snapshot. This is
not an estimate of whole-project coverage. Exit 0 requires all selected mutants
killed by call-phase security assertions, with clean baseline/setup/collection.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    before: str
    after: str
    test: str
    database: bool = False


CONTROLS = "tests/replies/test_controls.py::"
AUTHORITY = "tests/test_authority.py::"
CODE_LOGIN = "tests/setup_page/test_code_login.py::"
TRIAGE = "tests/triage/test_triage_core.py::"
TRIAGE_PAGE = "tests/triage/test_triage_page.py::"
APP_4A = "tests/setup_page/test_pack4a_app.py::"
PACK4B = "tests/setup_page/test_pack4b.py::"
OPS_CONSENT = "tests/executor/test_ops_consent.py::"
OPS_REPORT = "tests/executor/test_ops_report.py::"
UPDATES = "tests/test_updates.py::"
HERMES = "tests/test_hermes_access.py::"
BOUNDARY = "tests/test_archive_guard.py::"
ASK = "tests/test_hermes_ask_answers.py::"
REVOKE = "tests/outbox/test_send_revocation.py::"
MUTATIONS = (
    Mutation("background_owner_context", "src/shturman/authority.py",
             "    context.run(_owner.set, None)", "    # mutation: retain inherited owner context",
             AUTHORITY + "test_owner_authority_does_not_leak_to_background_tasks"),
    Mutation("claimed_owner_callback", "src/shturman/authority.py",
             "def requires_owner() -> AuthorityPrincipal:\n    principal = _owner.get()",
             "def requires_owner() -> AuthorityPrincipal:\n    principal = _owner.get() or AuthorityPrincipal('telegram:1000', 'telegram', 1000, 1000)",
             AUTHORITY + "test_legacy_claimed_owner_callback_cannot_touch_confirmation_database"),
    Mutation("independent_owner_bot", "src/shturman/replies/owner.py",
             "return bool(bridge.owns_bot() and principal and principal.source == 'telegram'",
             "return bool(principal and principal.source == 'telegram'",
             CONTROLS + "test_legacy_bot_does_not_grant"),
    Mutation("private_owner_chat", "src/shturman/replies/owner.py",
             "and principal.chat_id == int(owner['chat_id']))", "and True)",
             CONTROLS + "test_model_or_api_user_id_does_not_establish_owner"),
    Mutation("immutable_source_receipts", "src/shturman/replies/workflow.py",
             "    if sources != task['source_refs']:", "    if False:",
             CONTROLS + "test_presend_rechecks_immutable_bindings[sources]"),
    Mutation("policy_revision_presend", "src/shturman/replies/workflow.py",
             "    if row.get('task_policy_revision') != task['policy_revision']:", "    if False:",
             CONTROLS + "test_presend_rechecks_immutable_bindings[revision]"),
    Mutation("read_is_not_disclosure", "src/shturman/replies/workflow.py",
             "        if task['requires_approval'] or not await broker.disclosure_allowed(conn, task, task['source_refs']):",
             "        if task['requires_approval']:",
             CONTROLS + "test_revoked_disclosure_grant_blocks_auto_even_if_read_is_valid"),
    Mutation("remote_origin_api_denial", "src/shturman/app.py",
             "        if self.remote is not None and any(self.remote.handles(path, host) for host in hosts):",
             "        if False:",
             AUTHORITY + "test_remote_host_cannot_use_internal_static_token_for_api"),
    Mutation("model_unoffered_source", "src/shturman/replies/model.py",
             "not isinstance(key, str) or key not in offered for key in keys",
             "not isinstance(key, str) for key in keys",
             "tests/replies/test_model.py::test_invalid_outcome_is_not_authority"),
    Mutation("group_reply_exact_topic", "src/shturman/outbox/scopes.py",
             "AND p.class='user' AND m.topic_tg_id IS NOT DISTINCT FROM $3::bigint",
             "AND p.class='user' AND ($3::bigint IS NULL OR true)",
             "tests/test_group_replies.py::test_database_session_reply_requires_same_chat_topic_and_visible_actual_sender",
             database=True),
    # Mini App бота согласований (docs/miniapp.md): подпись Telegram, одна сессия на одни данные
    # входа, только кнопка, которая сейчас в списке.
    Mutation("miniapp_signature", "src/shturman/setup_page/initdata.py",
             "    if not hmac.compare_digest(sign(fields, bot_token), got):", "    if False:",
             "tests/setup_page/test_miniapp.py::test_init_data_is_checked_exactly_as_telegram_describes"),
    Mutation("miniapp_replay", "src/shturman/setup_page/miniapp.py",
             "           ON CONFLICT (init_hash) DO NOTHING RETURNING expires_at\"\"\",\n        auth.token_hash(token), _init_hash(init),",
             "           ON CONFLICT (init_hash) DO NOTHING RETURNING expires_at\"\"\",\n        auth.token_hash(token), _init_hash(init) + token,",
             "tests/setup_page/test_miniapp.py::test_every_refusal_looks_the_same_and_tells_nothing",
             database=True),
    Mutation("miniapp_rebind_revokes", "src/shturman/bridge.py",
             "        await _end_app_sessions(conn)\n        previous = await get_owner(conn)",
             "        previous = await get_owner(conn)",
             "tests/setup_page/test_miniapp.py::test_rebinding_back_to_the_same_owner_does_not_revive_old_sessions",
             database=True),
    Mutation("miniapp_listed_button_only", "src/shturman/setup_page/miniapp.py",
             "        if found is None:\n            # Кнопки нет",
             "        if found is None:\n            found = ({'kind_title': 'x', 'id': 0, '_card_message_id': None}, {'label': 'x'})\n        if False:\n            # Кнопки нет",
             "tests/setup_page/test_miniapp.py::test_only_known_buttons_of_the_screen_are_accepted",
             database=True),
    # Экран «Чаты» приложения (Р-80): каждое изменение сообщается владельцу в бот; список диалогов —
    # только из базы, обновление из Telegram — только по кнопке «Обновить» (Р-81).
    Mutation("miniapp_chats_tell_owner", "src/shturman/setup_page/miniapp.py",
             "        await bridge.notify_owner(conn, text)", "        pass",
             "tests/setup_page/test_miniapp_chats.py::test_enable_and_disable_with_depth_are_told_to_the_owner_and_logged",
             database=True),
    Mutation("miniapp_chats_cached_list", "src/shturman/setup_page/miniapp.py",
             "_account_id(request), request,\n                                                 cached=True))",
             "_account_id(request), request,\n                                                 cached=False))",
             "tests/setup_page/test_miniapp_chats.py::test_list_comes_from_the_database_and_telegram_is_asked_only_by_refresh",
             database=True),
    # Бот согласований в одно нажатие (Р-77): только создатель становится владельцем; токен — именно
    # того бота, что создавался по ссылке; без живой сессии страницы токен не ждёт подтверждения;
    # без «Да, это мой аккаунт» токен не сохраняется.
    Mutation("managed_bot_creator_only", "src/shturman/executor/binding.py",
             "return creator if isinstance(creator, int) and not isinstance(creator, bool) else None",
             "return None",
             "tests/setup_page/test_setup_managed_bot.py::test_only_the_creator_can_become_the_owner",
             database=True),
    Mutation("managed_bot_same_bot", "src/shturman/setup_page/bot_setup.py",
             "    return expect is None or (bot[\"id\"] == expect.get(\"id\")",
             "    return True or (bot[\"id\"] == expect.get(\"id\")",
             "tests/setup_page/test_setup_managed_bot.py::test_token_of_another_bot_is_refused",
             database=True),
    Mutation("managed_bot_live_session", "src/shturman/setup_page/bot_setup.py",
             "    if not await _session_alive(state, pairing.session_id):",
             "    if False:",
             "tests/setup_page/test_setup_managed_bot.py::test_token_is_not_applied_after_the_session_ended",
             database=True),
    Mutation("managed_bot_owner_confirms", "src/shturman/setup_page/bot_setup.py",
             "    pairing.status = CONFIRM\n",
             "    pairing.status = CONFIRM\n    await confirm(state, getattr(state.extras.get('setup_page'), 'managed', None) and state.extras['setup_page'], pairing.session_id, accept=True)\n",
             "tests/setup_page/test_setup_managed_bot.py::test_bot_created_by_someone_else_is_never_accepted_silently",
             database=True),
    Mutation("managed_bot_claim_needs_code", "src/shturman/setup_page/bot_setup.py",
             "    claimed = data.get(\"claimed\") is True and bool(pairing.claim_link)\n",
             "    claimed = data.get(\"claimed\") is True\n",
             "tests/setup_page/test_setup_managed_bot.py::test_bot_with_other_address_needs_both_claim_code_and_claimed_mark",
             database=True),
    # Раскладки и сферы (Р-82): из приложения массово — только выключить; изменение сфер через внутренний
    # API ждёт нажатия владельца в боте согласований.
    Mutation("miniapp_no_bulk_enable", "src/shturman/setup_page/spheres.py",
             "    if enabled:\n        await page._log(request, \"app.bulk_on\"",
             "    if False:\n        await page._log(request, \"app.bulk_on\"",
             "tests/setup_page/test_spheres_page.py::test_in_the_app_enable_all_is_refused_and_disable_all_is_told_to_the_owner",
             database=True),
    Mutation("spheres_api_waits_for_owner", "src/shturman/spheres_service.py",
             "        answer, result = await settle(conn, SPHERE_CHATS, {\"sphere_id\": sphere_id, \"add\": add, \"remove\": remove},\n                                      summary=summary)",
             "        answer, result = await settle(conn, SPHERE_CHATS, {\"sphere_id\": sphere_id, \"add\": add, \"remove\": remove},\n                                      summary=None)",
             "tests/processing/test_spheres_tools.py::test_sphere_changes_through_the_api_wait_for_the_owner",
             database=True),
    # Уведомления владельцу (Р-84): расширение через внутренний API ждёт владельца; из приложения —
    # только сузить; скрытое защитой уходит без текста.
    Mutation("notify_api_widening_waits", "src/shturman/notify/service.py",
             "        summary = None\n        if wider:", "        summary = None\n        if False:",
             "tests/notify/test_notify_api.py::test_widening_waits_for_the_owner_in_the_bot",
             database=True),
    Mutation("notify_app_only_narrows", "src/shturman/setup_page/notify.py",
             "        if wider:\n            await page._log(request, \"app.notify\"",
             "        if False:\n            await page._log(request, \"app.notify\"",
             "tests/setup_page/test_notify_page.py::test_app_can_only_narrow_and_owner_is_told",
             database=True),
    Mutation("notify_hidden_without_text", "src/shturman/notify/core.py",
             "    if not row[\"agent_visible\"]:\n        return HIDDEN_TEXT", "    if False:\n        return HIDDEN_TEXT",
             "tests/notify/test_notify_core.py::test_hidden_by_guard_is_told_without_text",
             database=True),
    # Репетиция ответов на основном аккаунте (Р-85): основной аккаунт не отправляет ни при каком положении
    # выключателей; под карточкой — только оценки; только люди из списка; включение через внутренний API
    # ждёт владельца; скрытое защитой не уходит модели справкой; уведомление не приходит вторым сообщением.
    Mutation("owner_role_never_sends", "src/shturman/tg/client.py",
             "        return self.role == \"assistant\" and self.sending", "        return self.sending",
             "tests/tg/test_tg_policy.py::test_owner_role_never_sends_whatever_the_switches"),
    # Р-105: «Отправить» под «Черновиком ответа» — только при включённой отправке и только в окне суток
    # бизнес-бота; кнопки в боте и в приложении — одни и те же (Д-46); черновик сервиса через API не создаётся;
    # доверенному черновик сам — только при включённом автоответе (его сообщения модели владелец уже отдал).
    Mutation("suggest_send_needs_switch", "src/shturman/replies/delivery.py",
             "    if not f.sending:\n        missing.append(\"sending_off\")",
             "    if False:\n        missing.append(\"sending_off\")",
             "tests/rehearsal/test_rehearsal_no_delivery.py::test_without_sending_there_is_no_send_button_and_the_proposal_is_not_in_the_row",
             database=True),
    Mutation("send_only_in_window", "src/shturman/replies/delivery.py",
             "        if not f.window:\n            missing.append(\"window_closed\")",
             "        if False:\n            missing.append(\"window_closed\")",
             "tests/replies/test_delivery_matrix.py::test_decide_exhaustive"),
    Mutation("same_buttons_bot_and_app", "src/shturman/replies/delivery.py",
             "    if draft is not None and avail.send:\n",
             "    if draft is not None and (avail.send or bot):\n",
             "tests/replies/test_delivery_matrix.py::test_decide_exhaustive"),
    Mutation("suggest_not_from_api", "src/shturman/outbox/drafts.py",
             "               VALUES ($1, $2, $3, $4, $5, $6, 'agent', $7, $8, 'pending',",
             "               VALUES ($1, $2, $3, $4, $5, $6, 'suggest', $7, $8, 'pending',",
             "tests/outbox/test_drafts_switch.py::test_create_takes_no_preparation_flag_and_is_always_the_agent",
             database=True),
    Mutation("trusted_draft_needs_autoreply", "src/shturman/rehearsal/core.py",
             "                  AND EXISTS (SELECT 1 FROM outbox_accounts WHERE account_id = $2 AND autoreply_enabled)\"\"\",",
             "                  AND $2::bigint IS NOT NULL\"\"\",",
             "tests/rehearsal/test_rehearsal_autoreply.py::test_trusted_person_gets_a_draft_when_the_autoreply_cannot_answer",
             database=True),
    # «В чат» (Р-105, часть 2): единственная запись основного аккаунта — облачный черновик, только взведённым
    # (по нажатию владельца), только простым текстом и никогда поверх текста владельца в поле ввода.
    Mutation("owner_write_is_savedraft_only", "src/shturman/tg/client.py",
             "            return arm is not None and not arm.used and arm.request is request and _draft_shape_ok(request)",
             "            return _draft_shape_ok(request)",
             "tests/tg/test_tg_policy.py::test_save_draft_passes_only_armed_on_this_very_request_only_for_owner"),
    Mutation("owner_write_arm_is_one_shot", "src/shturman/tg/client.py",
             "    if arm is not None and arm.request is inner:\n        arm.used = True",
             "    if False:\n        arm.used = True",
             "tests/tg/test_tg_policy.py::test_arming_is_one_shot_and_not_inherited_by_background_tasks"),
    Mutation("savedraft_shape", "src/shturman/tg/client.py",
             "            and getattr(request, \"media\", None) is None and getattr(request, \"entities\", None) is None",
             "            and getattr(request, \"media\", None) is None",
             "tests/tg/test_tg_policy.py::test_save_draft_shape_is_visible_plain_text_to_a_person"),
    Mutation("savedraft_never_empty", "src/shturman/tg/client.py",
             "    return (isinstance(message, str) and bool(message.strip()) and _visible(message)",
             "    return (isinstance(message, str)",
             "tests/tg/test_tg_policy.py::test_save_draft_shape_is_visible_plain_text_to_a_person"),
    Mutation("savedraft_person_only", "src/shturman/tg/client.py",
             "            and type(getattr(request, \"peer\", None)) is types.InputPeerUser",
             "            and getattr(request, \"peer\", None) is not None",
             "tests/tg/test_tg_policy.py::test_save_draft_shape_is_visible_plain_text_to_a_person"),
    Mutation("put_requires_owner_press", "src/shturman/replies/put.py",
             "    if principal is None or principal.source != \"telegram\" or principal.action not in PRESS_ACTIONS:",
             "    if principal is None:",
             "tests/rehearsal/test_put.py::test_put_needs_the_owners_press",
             database=True),
    Mutation("put_never_overwrites_owner_text", "src/shturman/tg/manager.py",
             "        if now.strip():\n            raise gateway.DraftBusy",
             "        if False:\n            raise gateway.DraftBusy",
             "tests/tg/test_tg_policy.py::test_put_draft_never_overwrites_the_owners_text"),
    Mutation("put_only_when_online", "src/shturman/tg/manager.py",
             "        if not _online(rt.client):\n            raise gateway.AccountUnavailable(\"клиент Telegram не на связи\")\n\n",
             "        if False:\n            raise gateway.AccountUnavailable(\"клиент Telegram не на связи\")\n\n",
             "tests/tg/test_tg_policy.py::test_put_draft_offline_reconnecting_flood_or_slow_read_writes_nothing"),
    Mutation("put_halt_right_before_write", "src/shturman/replies/put.py",
             "            return not await conn.fetchval(\"SELECT shturman_halted()\")",
             "            return True",
             "tests/rehearsal/test_put.py::test_stop_between_the_press_and_the_write_puts_nothing",
             database=True),
    Mutation("rehearsal_listed_people_only", "src/shturman/rehearsal/core.py",
             "    if s[\"enabled\"] and int(row[\"peer_tg_id\"]) in set(s[\"people\"]):\n        return None",
             "    if True:\n        return None",
             "tests/rehearsal/test_rehearsal_core.py::test_what_does_not_start_a_rehearsal[not_listed]",
             database=True),
    Mutation("rehearsal_enable_waits_for_owner", "src/shturman/rehearsal/settings.py",
             "    if after[\"enabled\"] and not before[\"enabled\"]:\n        out.append(\"enabled\")",
             "    if False:\n        out.append(\"enabled\")",
             "tests/rehearsal/test_rehearsal_api.py::test_enabling_and_adding_people_wait_for_the_owner",
             database=True),
    # «Поднять до …» под «предел исчерпан»: только нажатие в самом боте (не из приложения) и только
    # пока предел тот, при котором кнопка выдана.
    Mutation("rehearsal_raise_cap_bot_only", "src/shturman/rehearsal/core.py",
             "    if principal.action != \"telegram.callback\":\n        return {**refused,",
             "    if False:\n        return {**refused,",
             "tests/rehearsal/test_rehearsal_cap.py::test_cap_message_has_raise_buttons_and_the_owners_press_applies_at_once",
             database=True),
    Mutation("rehearsal_raise_cap_not_stale", "src/shturman/rehearsal/core.py",
             "    if s[\"daily_cap\"] != old:\n", "    if False:\n",
             "tests/rehearsal/test_rehearsal_cap.py::test_cap_message_has_raise_buttons_and_the_owners_press_applies_at_once",
             database=True),
    # Д-31: «ответ готовит автоответ» — только когда он на самом деле готовит это сообщение: не по
    # одному «чат разрешён» (тогда старое сообщение доверенного оставалось без ответа) и не мимо его
    # работающей задачи (тогда на одно сообщение было бы два ответа).
    Mutation("rehearsal_autoreply_claim_fresh_only", "src/shturman/outbox/autoreply.py",
             "    if not (await _trigger_state(conn, await load(conn), tgt, last)).ok:\n        return None",
             "    if False:\n        return None",
             "tests/rehearsal/test_rehearsal_autoreply.py::test_old_message_of_a_trusted_person_gets_a_rehearsal",
             database=True),
    Mutation("rehearsal_autoreply_claim_working_task", "src/shturman/outbox/autoreply.py",
             "        if task[\"working\"]:\n            return Claim(task, None)",
             "        if False:\n            return Claim(task, None)",
             "tests/rehearsal/test_rehearsal_autoreply.py::test_fresh_message_whose_autoreply_is_working_says_so",
             database=True),
    Mutation("reply_context_hides_guarded", "src/shturman/outbox/autoreply.py",
             "WHERE chat_id = $1 AND id <> $2 AND kind = 'message' AND deleted_at IS NULL AND agent_visible\n",
             "WHERE chat_id = $1 AND id <> $2 AND kind = 'message' AND deleted_at IS NULL\n",
             "tests/rehearsal/test_rehearsal_core.py::test_hidden_by_guard_is_neither_a_trigger_nor_context",
             database=True),
    Mutation("rehearsal_holds_notification", "src/shturman/notify/core.py",
             "        if hold == \"hold\":\n            due = max(due, ready + rehearsal.NOTIFY_HOLD)",
             "        if False:\n            due = max(due, ready + rehearsal.NOTIFY_HOLD)",
             "tests/rehearsal/test_rehearsal_core.py::test_listed_person_gets_one_card_with_ratings_and_nothing_is_sent",
             database=True),
    # Вход на страницу по коду из бота (Р-88): код действует только в браузере, который его запросил,
    # пять минут и один раз; не больше трёх запросов за 15 минут и пяти попыток на код; с Hermes — только
    # через сокет обратного прокси; две cookie попытки — не вход.
    Mutation("code_login_browser_binding", "src/shturman/setup_page/auth.py",
             "row = await conn.fetchrow(\"SELECT * FROM setup_codes WHERE attempt_hash = $1 FOR UPDATE\", token_hash(attempt))",
             "row = await conn.fetchrow(\"SELECT * FROM setup_codes WHERE attempt_hash = $1 OR delivered ORDER BY id DESC LIMIT 1 FOR UPDATE\", token_hash(attempt))",
             CODE_LOGIN + "test_code_of_one_browser_does_not_work_in_another",
             database=True),
    Mutation("code_login_expiry", "src/shturman/setup_page/auth.py",
             "        if await conn.fetchval(\"SELECT $1::timestamptz <= now()\", row[\"expires_at\"]):",
             "        if False:",
             CODE_LOGIN + "test_expired_code_does_not_work",
             database=True),
    Mutation("code_login_one_time", "src/shturman/setup_page/auth.py",
             "        if row is None or row[\"used_at\"] is not None or row[\"cancelled_at\"] is not None:",
             "        if row is None or row[\"cancelled_at\"] is not None:",
             CODE_LOGIN + "test_code_from_the_bot_logs_in_once_in_the_browser_that_asked",
             database=True),
    Mutation("code_login_request_limit", "src/shturman/setup_page/auth.py",
             "        if len(recent) >= REQUEST_LIMIT:", "        if False:",
             CODE_LOGIN + "test_three_requests_per_15_minutes_for_everyone",
             database=True),
    Mutation("code_login_tries_limit", "src/shturman/setup_page/auth.py",
             "        if tries < CODE_MAX_ATTEMPTS:", "        if True:",
             CODE_LOGIN + "test_five_wrong_codes_cancel_the_code_and_pause_code_login",
             database=True),
    Mutation("code_login_proxy_socket_only", "src/shturman/setup_page/service.py",
             "    return request.scope.get(\"client\") is None or not state.config.dashboard_origin",
             "    return True",
             CODE_LOGIN + "test_dashboard_origin_and_service_port_cannot_ask_or_enter_a_code",
             database=True),
    Mutation("code_login_single_cookie", "src/shturman/setup_page/code_login.py",
             "    return found[0] if len(found) == 1 and auth.well_formed(found[0]) else None",
             "    return found[-1] if found and auth.well_formed(found[-1]) else None",
             CODE_LOGIN + "test_two_cookies_with_the_same_name_mean_no_attempt",
             database=True),
    # «Остановить всё» (Р-87): пока остановлено, подтверждения не заводятся и отправка не проходит;
    # «Продолжить» и включение входа по коду подтверждаются только в самом боте, не из приложения.
    # Код и база проверяют флаг оба: выключенная проверка в коде упирается в триггер (это не «убита»
    # и не «выжила» — ошибка базы), поэтому вторая линия проверяется отдельно.
    Mutation("halt_database_guard", "src/shturman/migrations/0035_stop_answer_code.sql",
             "    IF shturman_halted() THEN\n        RAISE EXCEPTION 'assistant_stopped'",
             "    IF false THEN\n        RAISE EXCEPTION 'assistant_stopped'",
             "tests/test_halt.py::test_database_is_the_second_line",
             database=True),
    Mutation("halt_refuses_sending", "src/shturman/outbox/policy.py",
             "    decision = await check_halt(conn)\n    if not decision.ok:\n        return decision\n    decision = check_switch(rules)",
             "    decision = check_switch(rules)",
             "tests/test_halt.py::test_every_point_where_the_assistant_acts_refuses_while_stopped[send_check]",
             database=True),
    # Отправка от имени владельца — только через бота согласований сервиса (Д-45): подключение бота
    # в Hermes для отправки не выбирается, и задание отправки плагину не выдаётся — его исполнитель
    # не проверяет ни выключатель, ни «Остановить всё».
    Mutation("send_only_via_own_bot_connection", "src/shturman/outbox/policy.py",
             "           WHERE account_id = $1 AND enabled AND can_reply AND via = 'service'",
             "           WHERE account_id = $1 AND enabled AND can_reply",
             "tests/outbox/test_outbox_drafts.py::test_hermes_bot_business_connection_is_never_a_send_channel",
             database=True),
    Mutation("send_job_never_to_plugin", "src/shturman/bridge.py",
             "        executor=\"builtin\",\n    )",
             "        executor=\"builtin\" if await conn.fetchval(\"SELECT via FROM business_connections WHERE id = $1\", "
             "business_connection_id) == \"service\" else \"plugin\",\n    )",
             "tests/executor/test_executor_jobs.py::test_plugin_connection_send_is_never_given_to_the_plugin",
             database=True),
    # Разбор входящих (Р-92): включает только нажатие в самом боте; скрытое защитой не уходит модели ни
    # поводом, ни справкой; разбираемое не приходит второй раз пересылкой; ночью будит только «срочно» от
    # важных — слово «срочно» в чужом тексте прав не даёт.
    Mutation("triage_enable_only_bot_press", "src/shturman/triage/service.py",
             "    if principal.source != \"telegram\" or principal.action == \"telegram.miniapp\":\n        raise confirm.Refused(BOT_ONLY_TEXT",
             "    if False:\n        raise confirm.Refused(BOT_ONLY_TEXT",
             TRIAGE_PAGE + "test_enabling_is_only_a_press_in_the_bot",
             database=True),
    Mutation("triage_page_cannot_enable", "src/shturman/setup_page/triage.py",
             "    if data.get(\"enabled\") is True:", "    if False:",
             TRIAGE_PAGE + "test_enabling_is_only_a_press_in_the_bot",
             database=True),
    Mutation("triage_hidden_not_read", "src/shturman/triage/core.py",
             "            if row is None or row[\"chat_id\"] != chat_id or not row[\"agent_visible\"] \\\n",
             "            if row is None or row[\"chat_id\"] != chat_id \\\n",
             TRIAGE + "test_what_does_not_go_to_the_model[hidden]",
             database=True),
    Mutation("triage_context_hides_guarded", "src/shturman/triage/core.py",
             "WHERE m.chat_id = $1 AND m.kind = 'message' AND m.deleted_at IS NULL AND m.agent_visible\n",
             "WHERE m.chat_id = $1 AND m.kind = 'message' AND m.deleted_at IS NULL\n",
             TRIAGE + "test_hidden_message_is_not_context_either",
             database=True),
    Mutation("triage_no_double_forwarding", "src/shturman/notify/core.py",
             "            if await triage.takes(conn, row):\n                return None",
             "            if False:\n                return None",
             TRIAGE + "test_personal_message_is_read_by_the_model_and_plain_forwarding_is_not_sent",
             database=True),
    Mutation("triage_night_only_important_urgent", "src/shturman/triage/core.py",
             "    return urgency == \"urgent\" and level == \"important\"",
             "    return urgency == \"urgent\"",
             TRIAGE + "test_night_waits_for_morning_except_urgent_from_important",
             database=True),
    # Пачки (Р-93): скрытое защитой в пачке модели не уходит; пачка из одних пересланных автоматически ничего
    # не запускает; «Подготовить ответ» не показывается, когда ответ невозможен.
    # Скрытое защитой отсекается дважды — в `Pack.visible` (им пользуются все, кто строит запрос) и в самом
    # `prompt_text`; каждая линия проверяется отдельно.
    Mutation("pack_hidden_not_in_visible", "src/shturman/packs.py",
             "        return [r for r in self.rows if r[\"agent_visible\"]]",
             "        return list(self.rows)",
             "tests/test_packs.py::test_hidden_message_stays_in_the_pack_but_not_in_what_the_model_gets"),
    Mutation("pack_hidden_not_in_prompt", "src/shturman/packs.py",
             "        if not row[\"agent_visible\"]:\n            continue\n        body = row_text(",
             "        if False:\n            continue\n        body = row_text(",
             "tests/test_packs.py::test_hidden_message_stays_in_the_pack_but_not_in_what_the_model_gets"),
    Mutation("pack_forwarded_is_not_a_trigger", "src/shturman/packs.py",
             "        if on_demand:\n            seen = ",
             "        if True:\n            seen = ",
             "tests/rehearsal/test_rehearsal_packs.py::"
             "test_forwarded_only_pack_does_not_start_an_automatic_rehearsal_but_a_comment_does",
             database=True),
    Mutation("prepare_button_only_when_possible", "src/shturman/notify/core.py",
             "                prepare = personal and await notify_buttons.can_prepare(conn, chat_rows[-1][\"id\"]) is None",
             "                prepare = personal",
             "tests/rehearsal/test_rehearsal_packs.py::test_hidden_message_notification_has_no_prepare_button",
             database=True),
    Mutation("bot_only_not_from_app", "src/shturman/confirm.py",
             "    if row[\"kind\"] in BOT_ONLY and principal.action == \"telegram.miniapp\":",
             "    if False:",
             CODE_LOGIN + "test_managed_bot_has_code_login_off_until_the_owner_presses_in_the_bot",
             database=True),
    # Разрешения на сопровождение (Р-100): выдаёт только нажатие «Разрешить» под карточкой в самом боте;
    # израсходовать — один раз; на пути без сервиса — нажатие только привязанного владельца в его чате.
    Mutation("ops_consent_needs_card_press", "src/shturman/ops_consent.py",
             "    if not confirm.card_press():", "    if False:",
             OPS_CONSENT + "test_no_route_or_owner_channel_grants_without_a_bot_press", database=True),
    Mutation("ops_consent_bot_only", "src/shturman/confirm.py",
             "\"guard.release_all\", OPS})", "\"guard.release_all\"})",
             OPS_CONSENT + "test_no_route_or_owner_channel_grants_without_a_bot_press", database=True),
    Mutation("ops_consent_used_once", "src/shturman/ops_consent.py",
             "AND target = $3 AND status = 'granted' AND valid_until > now()",
             "AND target = $3 AND valid_until > now()",
             OPS_CONSENT + "test_request_card_press_and_one_time_use", database=True),
    Mutation("ops_consent_direct_owner_only", "src/shturman/ops_consent.py",
             "            mine = (sender.get(\"id\") == owner[\"user_id\"] and chat.get(\"id\")",
             "            mine = (chat.get(\"id\")",
             OPS_CONSENT + "test_direct_stranger_press_is_refused_and_rejection_denies"),
    # Отчёт сопровождения (Р-102) уходит разработчику только по нажатию владельца «Отправить» в самом боте.
    Mutation("ops_report_needs_card_press", "src/shturman/ops_report.py",
             "    if not confirm.card_press():", "    if False:",
             OPS_REPORT + "test_nothing_is_sent_without_the_owner_pressing_in_the_bot", database=True),
    Mutation("ops_report_bot_only", "src/shturman/confirm.py",
             "    | frozenset({OPS_REPORT})", "    | frozenset()",
             OPS_REPORT + "test_nothing_is_sent_without_the_owner_pressing_in_the_bot", database=True),
    # Ответ голосом и контекст в карточке (Р-103): голос принимается только от привязанного владельца в его
    # личном чате с ботом; в свёрнутую цитату разговора над черновиком скрытое защитой не попадает.
    Mutation("dictation_voice_owner_only", "src/shturman/dictation/core.py",
             "        if not await _owner_ok(conn, user_id, chat_id):\n            return None\n",
             "        if False:\n            return None\n",
             "tests/dictation/test_dictation.py::test_voice_is_accepted_only_from_the_owner", database=True),
    Mutation("card_context_hides_guarded", "src/shturman/convo.py",
             "WHERE m.chat_id = $1 AND m.kind = 'message' AND m.deleted_at IS NULL AND m.agent_visible\n"
             "                  AND m.topic_tg_id",
             "WHERE m.chat_id = $1 AND m.kind = 'message' AND m.deleted_at IS NULL\n"
             "                  AND m.topic_tg_id",
             "tests/dictation/test_reply_context.py::"
             "test_rehearsal_card_quotes_the_conversation_without_hidden_and_defanged", database=True),
    # Список выпусков (Р-101): просроченный не даёт уведомлений о версиях.
    Mutation("updates_expired_manifest_refused", "src/shturman/updates.py",
             "    if expires < today:", "    if False:",
             UPDATES + "test_expired_list_gives_no_release_news_and_one_problem_message", database=True),
    # Решение разом по скрытому защитой (Р-99): «Показать все» — только нажатием владельца в самом боте.
    Mutation("guard_release_all_needs_press", "src/shturman/guard/alerts.py",
             "    confirm.must_not_widen(True)\n    return await _bulk(conn, payload, \"released\")",
             "    return await _bulk(conn, payload, \"released\")",
             "tests/guard/test_guard_review.py::"
             "test_bulk_kinds_have_no_route_and_cannot_be_applied_without_the_owner",
             database=True),
    Mutation("guard_release_all_bot_only", "src/shturman/confirm.py",
             "\"owner.widen\", \"guard.release_all\", OPS})",
             "\"owner.widen\", OPS})",
             "tests/guard/test_guard_review.py::"
             "test_release_all_needs_a_second_press_in_the_bot_and_is_remembered",
             database=True),
    # Договорённости в приложении (Р-94): отсюда трогаются только принятые владельцем и только его стороны —
    # не неподтверждённые, не скрытые защитой и не «между другими»; «Не так» и перенос датой — только владелец.
    Mutation("app_commitments_only_accepted_own", "src/shturman/setup_page/agenda.py",
             "    if item is None or item[\"status\"] != \"open\" or item[\"direction\"] not in agenda.DIRECTIONS:",
             "    if item is None:",
             APP_4A + "test_done_move_wrong_apply_at_once_with_journal",
             database=True),
    Mutation("commitment_mark_wrong_owner_only", "src/shturman/processing/commitments.py",
             "    if not authority.is_owner():\n        return _result(False, error=\"Это решает владелец.\"",
             "    if False:\n        return _result(False, error=\"Это решает владелец.\"",
             APP_4A + "test_agent_cannot_mark_wrong_or_move_by_date",
             database=True),
    # Договорённости (Р-96): тихо, без владельца, снимается только ещё не принятое им — принятое (open)
    # меняется только нажатием (модель лишь предлагает изменение); «сделано» засчитывается только по
    # сообщению ПОЗЖЕ обещания; «Скрыть» в «В команде» — только владелец.
    Mutation("quiet_close_never_open", "src/shturman/processing/pipeline.py",
             "        if row[\"status\"] in commitments.QUIET_CLOSABLE:",
             "        if True:",
             "tests/processing/test_commitments_quality.py::"
             "test_resolve_quietly_drops_proposed_but_only_proposes_change_for_open",
             database=True),
    Mutation("evidence_after_promise", "src/shturman/processing/extract.py",
             "            if valid and not is_later(evidence, message):",
             "            if False:",
             "tests/processing/test_commitments_quality.py::test_evidence_checks_in_validation"),
    Mutation("team_hide_owner_only", "src/shturman/processing/commitments.py",
             "    if not authority.is_owner():\n        return _result(False, error=\"Скрыть может только владелец.\"",
             "    if False:\n        return _result(False, error=\"Скрыть может только владелец.\"",
             "tests/processing/test_commitments_quality.py::"
             "test_others_go_to_team_not_to_digest_and_owner_involvement_is_not_others",
             database=True),
    # Пакет 4б (Р-95): из приложения ослабление — только карточкой в бот; «Вернуть как было», если возврат
    # расширяет, — тоже только карточкой (и со страницы); `owner.widen` применяется только нажатием в боте;
    # разрешить группу и добавить обращение — ослабления, решает функция применения.
    Mutation("app_widening_only_card", "src/shturman/history.py",
             "        try:\n            out = await confirm.apply_narrowing(conn, kind, payload)\n",
             "        try:\n            out = await confirm.apply_owner(conn, kind, payload)\n",
             PACK4B + "test_page_widens_at_once_app_only_asks_the_bot", database=True),
    Mutation("narrowing_is_strict", "src/shturman/confirm.py",
             "    strict = _unconfirmed.set(True)", "    strict = _unconfirmed.set(False)",
             PACK4B + "test_page_widens_at_once_app_only_asks_the_bot", database=True),
    Mutation("revert_widening_only_card", "src/shturman/history.py",
             "        await confirm.apply_narrowing(conn, kind, payload, quiet=where == \"page\")",
             "        await confirm.apply_owner(conn, kind, payload)",
             PACK4B + "test_revert_tightening_at_once_widening_only_in_the_bot", database=True),
    Mutation("owner_widen_needs_bot_press", "src/shturman/history.py",
             "    if principal.source != \"telegram\" or principal.action == \"telegram.miniapp\" or not confirm.card_press():",
             "    if False:",
             PACK4B + "test_owner_widen_applies_only_under_a_press_in_the_bot", database=True),
    Mutation("group_scope_is_widening", "src/shturman/outbox/service.py",
             "    confirm.must_not_widen(data[\"enabled\"] and not was)", "    confirm.must_not_widen(False)",
             PACK4B + "test_groups_drafting_addressing_autoreply_from_the_page_and_the_app", database=True),
    Mutation("new_alias_is_widening", "src/shturman/outbox/service.py",
             "    confirm.must_not_widen(any(a.casefold() not in folded for a in after))", "    confirm.must_not_widen(False)",
             PACK4B + "test_groups_drafting_addressing_autoreply_from_the_page_and_the_app", database=True),
    # Р-104: граница токенов, видимых ассистенту в Hermes, и выдачи архива.
    Mutation("hermes_token_is_not_operator", "src/shturman/app.py",
             '"/api": [(f"Bearer {config.api_token}".encode(), access.HERMES)],',
             '"/api": [(f"Bearer {config.api_token}".encode(), access.OPERATOR)],',
             HERMES + "test_owner_decisions_are_closed_to_the_hermes_token", database=True),
    Mutation("hermes_route_allowlist", "src/shturman/access.py",
             "            return kind, metered\n    return None", "            return kind, metered\n    return (READ, False)",
             HERMES + "test_owner_decisions_are_closed_to_the_hermes_token", database=True),
    Mutation("hermes_commitment_change_needs_press", "src/shturman/processing/service.py",
             "        if access.is_hermes():\n            # Токен ассистента (Р-104)", "        if False:\n            # Токен ассистента (Р-104)",
             HERMES + "test_hermes_cannot_close_cancel_or_move_an_approved_commitment_without_a_press[close-body0]",
             database=True),
    Mutation("hermes_api_reads_metered", "src/shturman/app.py",
             "            if metered:", "            if False:",
             BOUNDARY + "test_hermes_api_reads_are_metered_redacted_and_limited", database=True),
    Mutation("archive_hourly_limit", "src/shturman/archive_guard.py",
             "                    if have >= cap:", "                    if False:",
             BOUNDARY + "test_full_export_is_cut_by_the_hourly_limit_and_raises_one_alarm", database=True),
    Mutation("archive_reservation_before_answer", "src/shturman/archive_guard.py",
             "RETURNING id\"\"\", caller, tool[:80], max_m, max_c, pattern)",
             "RETURNING id\"\"\", caller, tool[:80], 0, 0, pattern)",
             BOUNDARY + "test_parallel_calls_do_not_bypass_the_limit", database=True),
    Mutation("archive_output_redacts_keys", "src/shturman/archive_guard.py",
             "                red = redact_secrets(v)", "                red = v",
             BOUNDARY + "test_key_in_message_text_is_redacted_everywhere", database=True),
    Mutation("archive_output_seals_frames", "src/shturman/archive_guard.py",
             "                out = seal(red)", "                out = red",
             BOUNDARY + "test_seal_is_a_second_line_on_output"),
    Mutation("archive_canary_alarm", "src/shturman/archive_guard.py",
             "    if stats.canaries and not _quiet_for(guard, \"canary\"):", "    if False:",
             BOUNDARY + "test_canary_shows_only_in_broad_listings_and_its_content_closes_the_archive", database=True),
    Mutation("archive_alarm_once", "src/shturman/archive_guard.py",
             "            if guard[\"state\"] == \"closed\" or (guard[\"state\"] == \"slowed\" and target == \"slowed\"):",
             "            if False:",
             BOUNDARY + "test_full_export_is_cut_by_the_hourly_limit_and_raises_one_alarm", database=True),
    Mutation("archive_continue_bot_only", "src/shturman/archive_guard.py",
             "    if widens and principal.action != \"telegram.callback\":", "    if False:",
             BOUNDARY + "test_owner_buttons", database=True),
    # Д-53 (внешняя ревизия 2026-10-20, SEC-01): ответы на просьбы ассистента — только технический итог.
    Mutation("hermes_ask_answer_filtered", "src/shturman/app.py",
             "            elif kind == access.ASK:", "            elif False:",
             ASK + "test_ask_answers_carry_no_private_content[own_bot]", database=True),
    Mutation("hermes_ask_closed_archive", "src/shturman/archive_guard.py",
             "        closed = await conn.fetchval(\"SELECT state = 'closed' FROM archive_guard WHERE id = 1\")",
             "        closed = False",
             ASK + "test_with_the_archive_closed_ask_routes_answer_423_before_the_handler", database=True),
    Mutation("hermes_ask_summary_dropped", "src/shturman/access.py",
             "        return 202, {k: body.get(k) for k in PENDING_KEYS}", "        return 202, body",
             ASK + "test_ask_answers_carry_no_private_content[own_bot]", database=True),
    # Д-55, Д-56 (SEC-03, F-01): отзыв согласия после постановки отправки и между частями.
    Mutation("business_send_final_check", "src/shturman/executor/worker.py",
             "        if not decision.ok:\n            return not_sent(f\"черновик не годится",
             "        if False:\n            return not_sent(f\"черновик не годится",
             REVOKE + "test_revocation_after_the_job_is_taken_stops_delivery[excluded]", database=True),
    Mutation("business_send_job_is_the_drafts", "src/shturman/outbox/drafts.py",
             "        if (row[\"channel\"] != \"business\" or named != str(row[\"id\"])",
             "        if (row[\"channel\"] != \"business\"",
             REVOKE + "test_revocation_after_the_job_is_taken_stops_delivery[context_lost]", database=True),
    Mutation("business_send_same_owner", "src/shturman/outbox/drafts.py",
             "        return int(row[\"approved_by\"]) == current", "        return True",
             REVOKE + "test_final_check_itself_refuses[owner_changed]", database=True),
    Mutation("business_send_final_halt", "src/shturman/outbox/drafts.py",
             "                   tgt: Target | None, *, next_part: bool = False) -> Decision:\n    \"\"\"",
             "                   tgt: Target | None, *, next_part: bool = False) -> Decision:\n    return policy.ALLOW\n    \"\"\"",
             REVOKE + "test_final_check_itself_refuses[halt]", database=True),
    Mutation("hermes_ask_noop_uniform", "src/shturman/access.py",
             "        return 404, {\"error\": UNAVAILABLE_TEXT, \"code\": \"not_found\"}\n    code =",
             "        return 200, {\"ok\": True, \"changed\": False}\n    code =",
             ASK + "test_existing_and_missing_records_answer_alike_without_a_card", database=True),
    Mutation("session_part_check_after_pause", "src/shturman/outbox/drafts.py",
             "            decision = await part_check(conn, mod.state, draft_id, first=index == 0)",
             "            decision = policy.ALLOW",
             REVOKE + "test_halt_during_the_pause_between_parts_stops_the_next_part", database=True),
)

# Capture actual pytest phases and exception types, rather than treating a
# nonzero exit (including broken imports, setup, connection errors) as a kill.
REPORT_PLUGIN = r'''
import json
import os
from pathlib import Path
import pytest

REPORT = {"collected": 0, "collection_errors": [], "reports": [], "deselected": 0}

def pytest_collection_finish(session):
    REPORT["collected"] = len(session.items)

def pytest_collectreport(report):
    if report.failed:
        REPORT["collection_errors"].append(str(report.longrepr))

def pytest_deselected(items):
    REPORT["deselected"] += len(items)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    REPORT["reports"].append({
        "nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
        "exception": call.excinfo.typename if call.excinfo else None,
        "detail": str(report.longrepr) if report.failed else None,
    })

def pytest_sessionfinish(session, exitstatus):
    REPORT["exitstatus"] = int(exitstatus)
    Path(os.environ["SHTURMAN_MUTATION_PHASE_REPORT"]).write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
'''


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args],
                                       text=True, timeout=30).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable:{type(exc).__name__}"


def copy_service(source: Path, target: Path) -> None:
    target.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    for name in ("src", "tests"):
        shutil.copytree(source / name, target / name, ignore=ignore)
    shutil.copy2(source / "pyproject.toml", target / "pyproject.toml")


def manifest(service: Path) -> dict[str, str]:
    return {str(path.relative_to(service)): sha(path.read_bytes())
            for path in sorted(service.rglob("*")) if path.is_file()}


def classify(returncode: int, phases: dict) -> str:
    records = phases.get("reports", [])
    if phases.get("collection_errors") or not phases.get("collected"):
        return "error"
    failures = [r for r in records if r["outcome"] == "failed"]
    def security_assertion(record: dict) -> bool:
        return record["phase"] == "call" and (
            record["exception"] == "AssertionError" or
            (record["exception"] == "Failed" and "DID NOT RAISE" in (record.get("detail") or "")))
    if any(not security_assertion(r) for r in failures):
        return "error"
    passed = [r for r in records if r["phase"] == "call" and r["outcome"] == "passed"]
    if failures and returncode == 1:
        return "assertion_failure"
    if returncode != 0:
        return "error"
    if not passed:
        return "untested"
    if any(r["outcome"] == "skipped" for r in records):
        return "untested"
    return "pass"


def run_test(service: Path, test: str, output: Path, label: str, timeout: int) -> dict:
    phases_path = output / f"{label}.phases.json"
    log_path = output / f"{label}.log"
    plugin_path = service / "_security_mutation_reports.py"
    plugin_path.write_text(REPORT_PLUGIN, encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(service / "src"), str(service)))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SHTURMAN_MUTATION_PHASE_REPORT"] = str(phases_path)
    command = [sys.executable, "-m", "pytest", "-q", "-p",
               "_security_mutation_reports", test]
    started = time.monotonic()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(command, cwd=service, env=env, stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout)
        phases = json.loads(phases_path.read_text(encoding="utf-8")) if phases_path.exists() else {}
        result = classify(proc.returncode, phases)
        return {"status": result, "exit_code": proc.returncode,
                "phases": phases, "command": command, "working_directory": str(service),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "log": str(log_path), "phase_report": str(phases_path)}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "command": command, "timeout_seconds": timeout,
                "elapsed_seconds": round(time.monotonic() - started, 3), "log": str(log_path)}
    except (OSError, ValueError) as exc:
        return {"status": "error", "command": command, "error": str(exc), "log": str(log_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=90, help="Seconds per focused test invocation")
    parser.add_argument("--include-db", action="store_true",
                        help="Run the exact-topic mutant against an explicitly named isolated test DB")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.include_db:
        database_name = urlsplit(os.environ.get("SHTURMAN_TEST_DSN", "")).path.strip("/")
        if "test" not in database_name.lower():
            parser.error("--include-db requires SHTURMAN_TEST_DSN with an explicitly test-named database")
    source = Path(__file__).resolve().parents[1]
    repo = source.parent
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    status = git(repo, "status", "--porcelain")
    report = {
        "schema_version": 1,
        "scope": f"{len(MUTATIONS)} targeted security mutations, not whole-project mutation coverage",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provenance": {
            "git_head": git(repo, "rev-parse", "HEAD"),
            "dirty": bool(status), "dirty_files": status.splitlines(),
            "python": sys.version,
            "dependencies": {name: importlib.metadata.version(name)
                             for name in ("pytest", "pytest-asyncio", "asyncpg", "starlette")},
        },
        "baselines": {}, "mutations": [],
    }
    with tempfile.TemporaryDirectory(prefix="shturman-security-mutations-") as temp:
        root = Path(temp)
        frozen = root / "snapshot"
        copy_service(source, frozen)
        files = manifest(frozen)
        report["provenance"]["files_sha256"] = files
        report["provenance"]["tree_sha256"] = sha(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode())
        selected = [m for m in MUTATIONS if args.include_db or not m.database]
        # All required baselines finish before the first mutation runs.
        for number, test in enumerate(dict.fromkeys(m.test for m in selected)):
            base = root / f"baseline_{number}"
            copy_service(frozen, base)
            report["baselines"][test] = run_test(base, test, output, f"baseline_{number}", args.timeout)
        baseline_ok = all(r["status"] == "pass" for r in report["baselines"].values())
        for number, mutation in enumerate(MUTATIONS):
            item = asdict(mutation)
            if mutation.database and not args.include_db:
                item.update(status="untested", reason="isolated PostgreSQL test database not selected")
            elif not baseline_ok:
                item.update(status="blocked", reason="one or more required baselines did not pass")
            else:
                isolated = root / f"mutation_{number}"
                copy_service(frozen, isolated)
                path = isolated / mutation.file
                text = path.read_text(encoding="utf-8")
                count = text.count(mutation.before)
                if count != 1:
                    item.update(status="error", reason="exact anchor precondition failed", anchor_count=count)
                else:
                    path.write_text(text.replace(mutation.before, mutation.after, 1), encoding="utf-8")
                    item["mutated_file_sha256"] = sha(path.read_bytes())
                    result = run_test(isolated, mutation.test, output, mutation.name, args.timeout)
                    result["status"] = {"assertion_failure": "killed", "pass": "survived"}.get(
                        result["status"], result["status"])
                    item.update(result)
            report["mutations"].append(item)
            print(f"{item['name']}: {item['status']}", flush=True)
    counts = dict(Counter(item["status"] for item in report["mutations"]))
    tested = counts.get("killed", 0) + counts.get("survived", 0)
    report["summary"] = {
        "selected": len(selected), "total_planned": len(MUTATIONS), "counts": counts,
        "baseline_passed": baseline_ok, "tested_viable": tested,
        "killed_fraction_of_viable": counts.get("killed", 0) / tested if tested and baseline_ok else None,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    report_path = output / "security-mutations.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Provenance: " + json.dumps({key: report["provenance"][key] for key in
          ("git_head", "dirty", "dirty_files", "tree_sha256")}, ensure_ascii=False), flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    print(f"Report: {report_path}", flush=True)
    if not baseline_ok or any(item["status"] in ("error", "timeout", "blocked") for item in report["mutations"]):
        return 2
    return 0 if all(item["status"] == "killed" for item in report["mutations"]
                    if not item.get("database") or args.include_db) else 1


if __name__ == "__main__":
    raise SystemExit(main())
