"""Очистка диагностического пакета (ops/diag_filter.py, Р-98): заведомо личные и секретные строки
тестового набора (fixtures/secrets.txt) в пакет не попадают, а нужное для разбора — остаётся.

Запуск: python3 -m unittest discover -s ops/tests -p 'test_*.py'"""

import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("diag_filter", HERE.parent / "diag_filter.py")
diag_filter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag_filter)

SECRETS = [s for s in (HERE / "fixtures" / "secrets.txt").read_text(encoding="utf-8").splitlines() if s.strip()]
SECRETS.append("ivanpetrov")


def lines(name: str) -> list[str]:
    return (HERE / "fixtures" / name).read_text(encoding="utf-8").splitlines()


class Clean(unittest.TestCase):
    def assertClean(self, out: list[str]) -> None:
        text = "\n".join(out)
        for secret in SECRETS:
            self.assertNotIn(secret, text, f"в пакет попало: {secret}")

    def test_service_log_keeps_level_module_and_error_kind_only(self):
        out = diag_filter.service_log(lines("service.log"))
        self.assertClean(out)
        text = "\n".join(out)
        self.assertIn("2026-10-16 09:12 WARNING shturman.tg [ConnectionError]", text)
        self.assertIn("2026-10-16 09:16 WARNING shturman.tg [AuthKeyUnregisteredError]", text)
        self.assertIn("2026-10-16 09:14 WARNING shturman.executor.bot [HTTP 401]", text)
        self.assertIn("2026-10-16 09:13 ERROR shturman.processing [ValueError]", text)
        self.assertIn("строк другого вида (не взяты): 6", text)
        self.assertNotIn("RuntimeError: ", text)          # строка трассировки не взята вовсе

    def test_service_config_error_keeps_variable_name_only(self):
        out = diag_filter.service_log([
            "SHTURMAN_SETUP_ORIGIN: нужен адрес вида https://assistant.example.com:8443",
            "SHTURMAN_FEEDBACK_CHAT: номер чата записан неверно",
            "не заданы переменные окружения: SHTURMAN_API_TOKEN, SHTURMAN_MCP_TOKEN",
            "Иван Петров написал что-то"])
        self.assertClean(out)
        text = "\n".join(out)
        self.assertIn("SHTURMAN_SETUP_ORIGIN — 1", text)
        self.assertIn("SHTURMAN_FEEDBACK_CHAT — 1", text)
        self.assertIn("SHTURMAN_API_TOKEN — 1", text)
        self.assertIn("строк другого вида (не взяты): 1", text)
        self.assertNotIn("example.com", text)

    def test_hermes_errors_are_counts_only(self):
        out = diag_filter.hermes_errors(lines("hermes-errors.log"))
        self.assertClean(out)
        text = "\n".join(out)
        self.assertIn("gateway.run_turn", text)
        self.assertIn("tools.mcp_tool", text)
        self.assertNotIn("inbound", text)
        self.assertNotIn("gateway.platforms.telegram", text)   # старше суток до последней записи
        self.assertTrue(all(line.startswith("#") for line in out))

    def test_doctor_keeps_checks_and_masks_addresses(self):
        out = diag_filter.doctor(lines("doctor.txt"))
        self.assertClean(out)
        text = "\n".join(out)
        self.assertIn("PASS  public-url: <адрес>", text)
        self.assertIn("PASS  proxy-cert: <ip>:8443", text)
        self.assertIn("модель gpt-6.1-sol", text)
        self.assertIn("с 2026-10-16 10:00 UTC", text)
        self.assertIn("ИТОГ: FAIL (1)", text)
        self.assertNotIn("посторонняя", text)
        self.assertEqual(len(out), 12)

    def test_state_keeps_simple_values_and_journal_shape(self):
        raw = ["# режим установки: hermes — с Hermes (runbook: docs/runbooks/deploy.md)",
               "# состояние (local/state)", "address=domain", "step.deploy=done@2026-10-12T10:00:00Z",
               "public=https://assistant.example.com", "note=Иван Петров", "", "# последние записи журнала",
               "2026-10-12T10:00:00Z set address",
               "2026-10-12T10:01:00Z отправил владельцу ссылку https://setup.example.com:8443/shturman-setup/#k=onetime"]
        out = diag_filter.state(raw)
        self.assertClean(out)
        self.assertIn("address=domain", out)
        self.assertIn("step.deploy=done@2026-10-12T10:00:00Z", out)
        self.assertIn("public=<значение скрыто>", out)
        self.assertIn("2026-10-12T10:00:00Z set address", out)
        self.assertTrue(any("<запись журнала:" in line for line in out))

    def test_counts(self):
        out = diag_filter.counts(["ERROR: duplicate key value Иван Петров", "LOG: ok", "FATAL: x"])
        self.assertClean(out)
        self.assertEqual(out[0], "строк: 3")

    def test_relay_keeps_only_known_reasons_and_failure_kinds(self):
        log = ["2026-10-16 09:12:01.123 [W] [client/service.go:295] login to the server failed: "
               "посредник: экземпляр отключён держателем посредника. With loginFailExit enabled…",
               "2026-10-16 09:12:00.001 [I] [client/service.go:287] [a1b2c3d4e5f60718] login to server success, "
               "get run id [a1b2c3d4e5f60718]",
               "2026-10-16 09:13:00 [W] [proxy] [dashboard-https] start error: посредник: этому экземпляру "
               "разрешены только его имена (ivanpetrov.sh.namre.app)",
               "2026-10-16 09:14:00 [W] connect to server error: dial tcp 203.0.113.7:7000: i/o timeout"]
        out = diag_filter.relay(log)
        self.assertClean(out)
        text = "\n".join(out)
        self.assertEqual(out[0], "строк: 4")
        self.assertIn("«экземпляр отключён держателем посредника» — 1", text)
        self.assertIn("«этому экземпляру разрешены только его имена» — 1", text)
        self.assertIn("вход к посреднику не удался — 1", text)
        self.assertIn("посредник не ответил вовремя — 1", text)
        for leak in ("a1b2c3d4e5f60718", "203.0.113.7", "namre"):
            self.assertNotIn(leak, text)

    def test_relay_reasons_match_relayd(self):
        import re
        code = (HERE.parent.parent / "relay" / "relayd.py").read_text(encoding="utf-8")
        found = set(re.findall(r'"посредник: ([^"]+)"', code))
        self.assertEqual(found, set(diag_filter.RELAY_REASONS))

    def test_mask_spares_names_of_code_and_settings(self):
        line = ("смотрите docs/deployment.md и config/Caddyfile.example; SHTURMAN_PROCESSING_HOURLY_CAP; "
                "модель intfloat/multilingual-e5-small; пт 16.10 08:27; Caddy 2.11.7")
        self.assertEqual(diag_filter.mask(line), line)


if __name__ == "__main__":
    unittest.main()
