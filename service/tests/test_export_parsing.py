import io

import pytest

from shturman.importer import scan
from shturman.telegram_export import ExportFormatError, iter_export

from conftest import IVAN, OWNER, as_file, msg


def test_stream_yields_owner_chats_and_messages(sample_export):
    events = list(iter_export(as_file(sample_export)))
    kinds = [e[0] for e in events]
    assert kinds[0] == "owner" and events[0][1].tg_user_id == OWNER
    assert kinds.count("chat") == 4  # пустой чат тоже объявлен
    assert kinds.count("message") == 8


def test_text_fragments_are_flattened_and_markup_kept(sample_export):
    msgs = {e[2].tg_message_id: e[2] for e in iter_export(as_file(sample_export))
            if e[0] == "message" and e[1].tg_id == IVAN}
    m = msgs[3]
    assert m.text == "Договор лежит https://example.org/doc, посмотрите"
    assert m.entities == [{"type": "link", "text": "https://example.org/doc"}]
    assert m.reply_to_tg_id == 2
    assert msgs[1].entities is None


def test_service_media_and_missing_files(sample_export):
    msgs = {e[2].tg_message_id: e[2] for e in iter_export(as_file(sample_export))
            if e[0] == "message" and e[1].tg_id == IVAN}
    assert msgs[4].kind == "service" and msgs[4].service_action == "phone_call"
    assert msgs[4].sender_tg_id == IVAN
    assert msgs[5].media_type == "voice_message" and msgs[5].media_path.endswith("audio_1.ogg")
    # файл не выгружен: тип известен, пути нет
    assert msgs[6].media_type == "photo" and msgs[6].media_path is None


def test_single_chat_export():
    single = {"name": "Иван Петров", "type": "personal_chat", "id": IVAN,
              "messages": [msg(1, 1789200000, IVAN, "Иван Петров", "Привет")]}
    events = list(iter_export(as_file(single)))
    assert [e[0] for e in events] == ["chat", "message"]
    assert events[0][1].peer_class == "user"


def test_channel_sender_and_classes(sample_export):
    by_chat = {e[1].tg_id: e for e in iter_export(as_file(sample_export)) if e[0] == "message"}
    assert by_chat[4001][1].peer_class == "channel"
    assert by_chat[4001][2].sender_class == "channel"
    assert by_chat[3001][1].peer_class == "chat"


def test_scan_lists_chats_without_database(sample_export):
    owner, chats = scan(as_file(sample_export))
    assert owner.tg_user_id == OWNER
    assert [c.name for c in chats][0] == "Иван Петров"  # самый большой — первым
    assert {c.name: c.messages for c in chats}["Пустой"] == 0


def test_not_an_export_is_rejected():
    with pytest.raises(ExportFormatError):
        list(iter_export(io.BytesIO(b'{"hello": [1, 2, 3]}')))
