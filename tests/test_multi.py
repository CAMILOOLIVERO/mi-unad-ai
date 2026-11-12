"""Pruebas del modo multiusuario (Telegram y datateca simulados). py -m unittest discover tests -v"""
import io
import json
import logging
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

from app import multi as MU
from app import watcher as W
from app.automation.agenda_scraper import BOGOTA

FIXTURES = Path(__file__).parent / "fixtures"
REAL_PERIOD = {"102437374": "2026-2205", "102437375": "2026-2205", "102437376": "2026-2205",
               "202016902": "2026-2204", "212064": "2026-2204", "202016907": "2026-2204"}
CAMILO, EDUARDO, EXTRANO = "1001", "2002", "3003"
ADMIN_CODE = "clave-secreta"


class FakeTelegram:
    def __init__(self):
        self.inbox, self.sent, self.edits, self.next_id = [], [], [], 1

    def msg(self, uid, text, chat_type="private", first="Usuario"):
        self.inbox.append({"update_id": self.next_id, "message": {
            "chat": {"id": int(uid), "type": chat_type}, "from": {"id": int(uid), "first_name": first},
            "text": text}})
        self.next_id += 1

    def press(self, uid, data):
        self.inbox.append({"update_id": self.next_id, "callback_query": {
            "id": f"cb{self.next_id}", "data": data, "from": {"id": int(uid)},
            "message": {"chat": {"id": int(uid)}, "message_id": 77}}})
        self.next_id += 1

    def get_updates(self, offset=None):
        out = [u for u in self.inbox if offset is None or u["update_id"] >= offset]
        return out

    def send(self, text, chat_id=None, buttons=None):
        self.sent.append((str(chat_id), text, buttons))

    def to(self, uid):
        return [t for c, t, _ in self.sent if c == str(uid)]


def fake_fetch_factory(calls):
    def fake_fetch(url):
        period, code = url.rsplit("/", 2)[-2], url.rsplit("/", 1)[-1][:-4]
        calls.append(code)
        if REAL_PERIOD.get(code) == period:
            return (FIXTURES / f"{code}.htm").read_text(encoding="utf-8")
        if code == "212064" and period == "2026-2205":   # el mismo curso, en otro periodo
            return (FIXTURES / "212064_2205.htm").read_text(encoding="utf-8")
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return fake_fetch


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "periods.json").write_text(json.dumps({"known": ["2026-2205", "2026-2204"]}))
        legacy = self.root / "legacy"
        legacy.mkdir()
        (legacy / "courses.json").write_text(json.dumps({"periodos": {
            "2026-2205": ["102437374", "102437376", "102437375"],
            "2026-2204": ["202016902", "212064", "202016907"]}}))
        (legacy / "notify_state.json").write_text(json.dumps({
            "activities": {}, "courses_seen": list(REAL_PERIOD),
            "sent": {"welcome:x": "2026-11-12T09:15:00-05:00",
                     "summary:2026-11-12": "2026-11-12T09:15:00-05:00"},
            "course_names": {}}))
        self.tg, self.fetch_calls, self.webhooks = FakeTelegram(), [], []
        self.now = datetime(2026, 11, 12, 11, 7, tzinfo=BOGOTA)
        self.patches = [
            mock.patch.dict("os.environ", {"DATA_DIR": str(self.root), "TELEGRAM_ADMIN_CODE": ADMIN_CODE}),
            mock.patch.object(MU, "get_updates", self.tg.get_updates),
            mock.patch.object(MU, "send_message", self.tg.send),
            mock.patch.object(MU, "edit_message", lambda c, m, t: self.tg.edits.append((str(c), t))),
            mock.patch.object(MU, "answer_callback", lambda *a, **k: None),
            mock.patch.object(MU, "set_commands", lambda c: None),
            mock.patch.object(MU, "set_webhook", lambda u, sec: self.webhooks.append(u)),
            mock.patch.object(MU, "fetch", fake_fetch_factory(self.fetch_calls)),
            mock.patch.object(W, "fetch", fake_fetch_factory(self.fetch_calls)),
            mock.patch.object(W, "in_quiet_hours", lambda now: False),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_bot(self, when=None):
        with mock.patch.object(MU, "datetime") as dt:
            dt.now.return_value = when or self.now
            dt.fromisoformat = datetime.fromisoformat
            return MU.run_multi()

    def db(self):
        return MU.DB(self.root)



class MultiTest(Base):
    # ------------------------------------------------------------------ registro

    def test_flujo_completo(self):
        # 1. Camilo reclama el bot con el código secreto: queda admin y se migran sus 6 cursos
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}", first="Camilo")
        self.run_bot()
        db = self.db()
        self.assertTrue(db.user(CAMILO)["is_admin"])
        self.assertEqual(len(db.user(CAMILO)["courses"]), 6)
        self.assertIn("Traje tus 6 cursos", self.tg.to(CAMILO)[0])
        # no repite la bienvenida ni el resumen de hoy (memoria migrada)
        self.assertFalse(any("Desde ahora vigilo" in t or "Resumen del" in t for t in self.tg.to(CAMILO)))

        # 2. Eduardo pide acceso: queda pendiente y a Camilo le llega la solicitud con botones
        self.tg.msg(EDUARDO, "/start", first="Eduardo")
        self.run_bot()
        self.assertEqual(self.db().user(EDUARDO)["status"], "pending")
        req = [(t, b) for c, t, b in self.tg.sent if c == CAMILO and "quiere usar el bot" in t][0]
        self.assertIn("Eduardo", req[0])
        self.assertEqual(req[1][0][0][1], f"ap:{EDUARDO}")

        # mientras está pendiente no puede agregar cursos
        self.tg.msg(EDUARDO, "102437374")
        self.run_bot()
        self.assertEqual(self.db().user(EDUARDO)["courses"], [])

        # 3. solo el admin puede aprobar: si Eduardo pulsa el botón, no pasa nada
        self.tg.press(EDUARDO, f"ap:{EDUARDO}")
        self.run_bot()
        self.assertEqual(self.db().user(EDUARDO)["status"], "pending")
        self.tg.press(CAMILO, f"ap:{EDUARDO}")
        self.run_bot()
        self.assertEqual(self.db().user(EDUARDO)["status"], "approved")
        self.assertTrue(any("Te aprobaron" in t for t in self.tg.to(EDUARDO)))

        # 4. Eduardo agrega un curso compartido, uno solo suyo… y uno inexistente
        self.tg.msg(EDUARDO, "102437374 212064 999999")
        self.run_bot()
        reply = [t for t in self.tg.to(EDUARDO) if "Vigilo" in t][-1]
        self.assertIn("✅ <b>Formulación Integral de Proyectos</b>", reply)
        self.assertIn("❔ <b>Inferencia Estadística</b>: elige tu periodo", reply)
        self.assertIn("❓ 999999", reply)
        with mock.patch.object(MU, "edit_message", lambda *a, **k: None):
            self.tg.press(EDUARDO, "pc:212064:2026-2204")
            self.run_bot()
        self.assertEqual({c["code"] for c in self.db().user(EDUARDO)["courses"]}, {"102437374", "212064"})

        # 5. /encurso: cada uno ve SOLO sus cursos
        self.tg.msg(EDUARDO, "/encurso")
        self.tg.msg(CAMILO, "/encurso@mi_unad_ia_bot")
        self.run_bot()
        edu = self.tg.to(EDUARDO)[-1]
        cam = self.tg.to(CAMILO)[-1]
        self.assertIn("Formulación Integral de Proyectos", edu)
        self.assertNotIn("Gestión de Proyectos Sostenibles", edu)
        self.assertIn("Gestión de Proyectos Sostenibles", cam)

        # 6. recordatorios personales: el 21 nov a las 7:07 Eduardo recibe solo lo suyo
        self.tg.sent.clear()
        self.fetch_calls.clear()
        self.run_bot(datetime(2026, 11, 21, 7, 7, tzinfo=BOGOTA))
        edu_txt = "\n".join(self.tg.to(EDUARDO))
        cam_txt = "\n".join(self.tg.to(CAMILO))
        self.assertIn("Inferencia Estadística", edu_txt)
        self.assertNotIn("Proyecto de Grado", edu_txt)            # curso solo de Camilo
        self.assertIn("Proyecto de Grado", cam_txt)
        # el curso compartido se descargó una sola vez para los dos
        self.assertEqual(self.fetch_calls.count("102437374"), 1)

    def test_entregas_son_personales(self):
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}")
        self.run_bot()
        db = self.db()
        fase2 = "102437376:fase-2-introduccion-al-estandar-p5-del-gpm"
        db.user(CAMILO)["delivered"][fase2] = "2026-11-15T10:00:00-05:00"
        db.save()
        self.tg.sent.clear()
        self.run_bot(datetime(2026, 11, 20, 7, 7, tzinfo=BOGOTA))  # Fase 2 cierra mañana
        self.assertFalse(any("Introducción al estándar P5" in t for t in self.tg.to(CAMILO)))

    def test_quitar_curso_con_boton(self):
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}")
        self.run_bot()
        self.tg.press(CAMILO, "rm:212064")
        self.run_bot()
        self.assertNotIn("212064", {c["code"] for c in self.db().user(CAMILO)["courses"]})
        self.assertTrue(any("Quité <b>" in t for t in self.tg.to(CAMILO)))

    def test_enlace_ensena_periodo_nuevo(self):
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}")
        self.run_bot()
        db = self.db()
        db.periods["known"] = ["2026-2205"]   # el bot aún no conoce 2204
        db.user(CAMILO)["courses"] = []
        db.save()
        self.tg.msg(CAMILO, "202016902")      # solo el código: no lo encuentra
        self.run_bot()
        self.assertIn("❓ 202016902", self.tg.to(CAMILO)[-1])
        self.tg.msg(CAMILO, "https://datateca.unad.edu.co/contenidos/agendas/2026-2204/202016902.htm")
        self.run_bot()
        self.assertIn("✅ <b>Administración de Bases de Datos</b>", self.tg.to(CAMILO)[-1])
        self.assertIn("2026-2204", self.db().periods["known"])

    def test_codigo_admin_incorrecto_o_extranos(self):
        self.tg.msg(EXTRANO, "/start clave-mala")
        self.run_bot()
        self.assertIsNone(self.db().admin_id())
        self.assertIn("todavía no está configurado", self.tg.to(EXTRANO)[-1])

    def test_grupos_se_ignoran(self):
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}", chat_type="group")
        self.run_bot()
        self.assertIsNone(self.db().admin_id())

    def test_logs_sin_datos_personales(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        try:
            self.tg.msg(CAMILO, f"/start {ADMIN_CODE}", first="Camilo")
            self.tg.msg(EDUARDO, "/start", first="Eduardo")
            self.run_bot()
        finally:
            logging.getLogger().removeHandler(handler)
        out = stream.getvalue()
        for secret in (CAMILO, EDUARDO, "Camilo", "Eduardo", ADMIN_CODE, "Formulación"):
            self.assertNotIn(secret, out)


class EntregasYColaTest(Base):
    def claim(self):
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}")
        self.run_bot()
        self.tg.sent.clear()

    def test_boton_entregada_detiene_recordatorios_y_se_puede_deshacer(self):
        self.claim()
        # 20 nov 7:07: recordatorio de la Fase 2 de Sostenibles (cierra mañana) con botón
        self.run_bot(datetime(2026, 11, 20, 7, 7, tzinfo=BOGOTA))
        rec = [(t, b) for c, t, b in self.tg.sent if c == CAMILO and "P5 del GPM" in t][0]
        boton = rec[1][0][0][1]
        self.assertTrue(boton.startswith("dl:"))
        self.assertLessEqual(len(boton.encode()), 64)          # límite de Telegram
        # la pulsa: queda entregada y ya no se le recuerda
        self.tg.press(CAMILO, boton)
        self.run_bot(datetime(2026, 11, 20, 9, 7, tzinfo=BOGOTA))
        self.assertIn("102437376:fase-2-introduccion-al-estandar-p5-del-gpm",
                      self.db().user(CAMILO)["delivered"])
        self.assertIn("como entregada", self.tg.to(CAMILO)[-1])
        self.tg.sent.clear()
        self.run_bot(datetime(2026, 11, 21, 18, 7, tzinfo=BOGOTA))  # últimas horas de esa fase
        self.assertFalse(any("P5 del GPM" in t for t in self.tg.to(CAMILO)))
        # deshacer
        self.tg.press(CAMILO, "ud:" + boton[3:])
        self.run_bot(datetime(2026, 11, 21, 18, 37, tzinfo=BOGOTA))
        self.assertNotIn("102437376:fase-2-introduccion-al-estandar-p5-del-gpm",
                         self.db().user(CAMILO)["delivered"])

    def test_entregadas_lista_y_deshacer(self):
        self.claim()
        self.tg.msg(CAMILO, "/entregadas")
        self.run_bot()
        self.assertIn("Aún no has marcado entregas", self.tg.to(CAMILO)[-1])
        sid = MU.short_id("102437376:fase-2-introduccion-al-estandar-p5-del-gpm")
        self.tg.press(CAMILO, f"dl:{sid}")
        self.tg.msg(CAMILO, "/entregadas")
        self.run_bot()
        text, buttons = [(t, b) for c, t, b in self.tg.sent if c == CAMILO][-1]
        self.assertIn("Entregadas (1)", text)
        self.assertIn("Introducción al estándar P5", text)
        self.assertEqual(buttons[0][0][1], f"ud:{sid}")
        self.tg.press(CAMILO, f"ud:{sid}")
        self.run_bot()
        self.assertEqual(self.db().user(CAMILO)["delivered"], {})

    def test_otro_usuario_no_puede_marcar_mis_entregas(self):
        self.claim()
        db = self.db()
        db.users["users"][EDUARDO] = MU.new_user({"first_name": "Eduardo"}, "approved")
        db.save()
        sid = MU.short_id("102437376:fase-2-introduccion-al-estandar-p5-del-gpm")
        self.tg.press(EDUARDO, f"dl:{sid}")   # Eduardo no tiene ese curso
        self.run_bot()
        self.assertEqual(self.db().user(CAMILO)["delivered"], {})
        self.assertEqual(self.db().user(EDUARDO)["delivered"], {})

    def test_pendientes(self):
        self.claim()
        self.tg.msg(CAMILO, "/pendientes")
        self.run_bot()
        text, buttons = [(t, b) for c, t, b in self.tg.sent if c == CAMILO][-1]
        self.assertIn("Abiertas y sin entregar", text)
        self.assertIn("Proyecto de Grado", text)
        self.assertNotIn("Fase 1 - Reconocimiento", text)      # ya cerró: no es pendiente
        self.assertNotIn("Fase 3 - Estudio de mercado", text)  # abre el 24 nov: aún no
        self.assertIn("abren más adelante", text)
        # el 12 nov hay 6 actividades abiertas (una por curso): 6 botones, no 15
        self.assertEqual(len(buttons), 6)
        self.assertTrue(all(b[0][1].startswith("dl:") for b in buttons))
        self.assertTrue(all(len(b[0][0]) <= 45 for b in buttons))  # caben en la pantalla

    def test_cola_del_receptor_y_duplicados(self):
        self.claim()
        qdir = self.root / "queue"
        qdir.mkdir(exist_ok=True)
        upd = {"update_id": 9001, "message": {"chat": {"id": int(CAMILO), "type": "private"},
                                              "from": {"id": int(CAMILO)}, "text": "/miscursos"}}
        (qdir / "9001.json").write_text(json.dumps(upd))
        with mock.patch.dict("os.environ", {"TELEGRAM_WEBHOOK_URL": "https://x.workers.dev",
                                            "TELEGRAM_WEBHOOK_SECRET": "s"}):
            db = self.db()
            db.state["webhook"] = "https://x.workers.dev"   # receptor ya confirmado
            db.save()
            with mock.patch.object(MU, "get_updates", side_effect=AssertionError("no debe llamarse")):
                self.run_bot()
                self.assertIn("Tus cursos", self.tg.to(CAMILO)[-1])
                self.assertFalse(list(qdir.glob("*.json")))            # la cola quedó vacía
                # Telegram reenvía el mismo mensaje: no se atiende dos veces
                (qdir / "9001.json").write_text(json.dumps(upd))
                n = len(self.tg.sent)
                self.run_bot()
                self.assertEqual(len(self.tg.sent), n)


class MismoCursoDosPeriodosTest(Base):
    """Inferencia Estadística (212064) existe en 16-04 (2204) y en 16-05 (2205) con fechas distintas."""

    def setUp(self):
        super().setUp()
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}")
        self.run_bot()
        db = self.db()
        db.user(CAMILO)["courses"] = [c for c in db.user(CAMILO)["courses"] if c["code"] != "212064"]
        db.save()
        self.tg.sent.clear()

    def test_codigo_escrito_pregunta_el_periodo(self):
        self.tg.msg(CAMILO, "212064")
        self.run_bot()
        q = [(t, b) for c, t, b in self.tg.sent if c == CAMILO and "existe en 2 periodos" in t][0]
        self.assertIn("Inferencia Estadística", q[0])
        datos = [row[0][1] for row in q[1]]
        self.assertEqual(set(datos), {"pc:212064:2026-2204", "pc:212064:2026-2205"})
        self.assertTrue(any("16-04" in row[0][0] and "1 dic" in row[0][0] for row in q[1]))
        self.assertNotIn("212064", {c["code"] for c in self.db().user(CAMILO)["courses"]})
        # elige su periodo (16-04)
        with mock.patch.object(MU, "edit_message", lambda *a, **k: None):
            self.tg.press(CAMILO, "pc:212064:2026-2204")
            self.run_bot()
        curso = [c for c in self.db().user(CAMILO)["courses"] if c["code"] == "212064"][0]
        self.assertEqual(curso["period"], "2026-2204")

    def test_captura_usa_el_periodo_de_su_encabezado(self):
        with mock.patch.object(MU, "download_file", lambda fid: b"png"), \
             mock.patch.object(MU, "extract_codes",
                               lambda data: (["212064"], ["2026-2204", "2026-2205"], {"212064": "2026-2204"})):
            self.tg.inbox.append({"update_id": self.tg.next_id, "message": {
                "chat": {"id": int(CAMILO), "type": "private"}, "from": {"id": int(CAMILO)},
                "photo": [{"file_id": "f", "file_size": 9}]}})
            self.tg.next_id += 1
            self.run_bot()
        pend = self.db().user(CAMILO)["ocr_pending"]["items"]
        self.assertEqual(pend, [{"code": "212064", "period": "2026-2204", "name": "Inferencia Estadística",
                                 "label": "periodo 16-04"}])
        text = [t for c, t, b in self.tg.sent if c == CAMILO and "Encontré" in t][0]
        self.assertIn("Inferencia Estadística <i>(periodo 16-04)</i>", text)
        self.assertIn("Cómo funcionan los botones", text)

    def test_captura_sin_periodo_legible_pregunta(self):
        with mock.patch.object(MU, "download_file", lambda fid: b"png"), \
             mock.patch.object(MU, "extract_codes", lambda data: (["212064"], [], {})):
            self.tg.inbox.append({"update_id": self.tg.next_id, "message": {
                "chat": {"id": int(CAMILO), "type": "private"}, "from": {"id": int(CAMILO)},
                "photo": [{"file_id": "f", "file_size": 9}]}})
            self.tg.next_id += 1
            self.run_bot()
        self.assertTrue(any("existe en 2 periodos" in t for t in self.tg.to(CAMILO)))
        self.assertNotIn("ocr_pending", self.db().user(CAMILO))


class ReceptorTest(Base):
    def test_activa_receptor_y_mientras_tanto_sigue_pidiendo_mensajes(self):
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}")
        env = {"TELEGRAM_WEBHOOK_URL": "https://x.workers.dev", "TELEGRAM_WEBHOOK_SECRET": "ok_clave-1"}
        with mock.patch.dict("os.environ", env):
            self.run_bot()                                  # aún no conectado: pide mensajes
        self.assertIsNotNone(self.db().admin_id())
        self.assertEqual(self.webhooks, ["https://x.workers.dev"])
        self.assertEqual(self.db().state["webhook"], "https://x.workers.dev")

    def test_clave_invalida_no_rompe_nada(self):
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}")
        env = {"TELEGRAM_WEBHOOK_URL": "https://x.workers.dev", "TELEGRAM_WEBHOOK_SECRET": "mala clave!"}
        with mock.patch.dict("os.environ", env):
            self.run_bot()
        self.assertIsNotNone(self.db().admin_id())         # el mensaje sí se atendió
        self.assertEqual(self.webhooks, [])                 # no se intentó conectar
        self.assertNotIn("webhook", self.db().state)


class FotoTest(Base):
    def setUp(self):
        super().setUp()
        self.tg.msg(EDUARDO, "/start")  # Eduardo pide acceso…
        self.tg.msg(CAMILO, f"/start {ADMIN_CODE}")
        self.run_bot()
        db = self.db()
        db.users["users"][EDUARDO] = MU.new_user({"first_name": "Eduardo"}, "approved")
        db.user(EDUARDO)["courses"] = [{"code": "102437374", "period": "2026-2205"}]
        db.save()
        self.tg.sent.clear()

    def send_photo(self, uid):
        self.tg.inbox.append({"update_id": self.tg.next_id, "message": {
            "chat": {"id": int(uid), "type": "private"}, "from": {"id": int(uid)},
            "photo": [{"file_id": "chico", "file_size": 10}, {"file_id": "grande", "file_size": 900}]}})
        self.tg.next_id += 1

    def ocr(self, codes, periods=("2026-2205", "2026-2204")):
        downloads = []
        return (mock.patch.object(MU, "download_file", lambda fid: downloads.append(fid) or b"png"),
                mock.patch.object(MU, "extract_codes", lambda data: (
                    list(codes), list(periods), {c: REAL_PERIOD[c] for c in codes if c in REAL_PERIOD})),
                downloads)

    def last_buttons(self, uid):
        return [b for c, t, b in self.tg.sent if c == uid and b][-1]

    def test_captura_seleccion_y_confirmacion(self):
        p1, p2, downloads = self.ocr(["102437374", "212064", "202016907", "02037376"])
        with p1, p2:
            self.send_photo(EDUARDO)
            self.run_bot()
        self.assertEqual(downloads, ["grande"])                      # la foto de mayor calidad
        text = [t for c, t, b in self.tg.sent if c == EDUARDO and "Encontré" in t][0]
        self.assertIn("Encontré <b>2 cursos nuevos</b>", text)
        self.assertIn("Ya tenías: Formulación Integral de Proyectos", text)
        self.assertIn("Descarté 1 número", text)                     # el número falso del OCR
        botones = self.last_buttons(EDUARDO)
        self.assertTrue(botones[0][0][0].startswith("☑️ Inferencia"))
        # desmarca Inferencia (reprobada) y confirma
        edits = []
        with mock.patch.object(MU, "edit_message", lambda c, m, t, b=None: edits.append((t, b))):
            self.tg.press(EDUARDO, "tg:212064")
            self.run_bot()
            self.assertTrue(edits[-1][1][0][0][0].startswith("⬜ Inferencia"))
            self.tg.press(EDUARDO, "oa:")
            self.run_bot()
        codes = {c["code"] for c in self.db().user(EDUARDO)["courses"]}
        self.assertEqual(codes, {"102437374", "202016907"})           # agregó solo lo marcado
        self.assertTrue(any("✅ <b>Proyecto de Grado</b>" in t for t in self.tg.to(EDUARDO)))
        self.assertNotIn("ocr_pending", self.db().user(EDUARDO))

    def test_cancelar_no_agrega_nada(self):
        p1, p2, _ = self.ocr(["212064"])
        with p1, p2:
            self.send_photo(EDUARDO)
            self.run_bot()
            self.tg.press(EDUARDO, "ox:")
            self.run_bot()
        self.assertEqual({c["code"] for c in self.db().user(EDUARDO)["courses"]}, {"102437374"})

    def test_sin_cursos_en_la_imagen(self):
        p1, p2, _ = self.ocr(["99999999"])
        with p1, p2:
            self.send_photo(EDUARDO)
            self.run_bot()
        self.assertTrue(any("No pude leer códigos de curso" in t for t in self.tg.to(EDUARDO)))

    def test_lectura_parcial_no_dice_que_tiene_todos(self):
        p1, p2, _ = self.ocr(["102437374"])          # solo leyó un código, y ya lo tenía
        with p1, p2:
            self.send_photo(EDUARDO)
            self.run_bot()
        reply = [t for t in self.tg.to(EDUARDO) if "Leí 1 código" in t][0]
        self.assertIn("Formulación Integral de Proyectos", reply)
        self.assertNotIn("todos", reply)
        self.assertIn("como <b>archivo</b>", reply)

    def test_sin_ocr_disponible(self):
        def no_ocr(data):
            raise MU.OCRUnavailable("x")
        with mock.patch.object(MU, "download_file", lambda fid: b"png"), \
             mock.patch.object(MU, "extract_codes", no_ocr):
            self.send_photo(EDUARDO)
            self.run_bot()
        self.assertTrue(any("Envíame los códigos escritos" in t for t in self.tg.to(EDUARDO)))

    def test_pendiente_no_puede_enviar_fotos(self):
        db = self.db()
        db.user(EDUARDO)["status"] = "pending"
        db.save()
        p1, p2, downloads = self.ocr(["212064"])
        with p1, p2:
            self.send_photo(EDUARDO)
            self.run_bot()
        self.assertEqual(downloads, [])                               # ni siquiera la descarga


if __name__ == "__main__":
    unittest.main()
