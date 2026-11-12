"""Pruebas de las reglas de aviso. Ejecutar: py -m unittest discover tests -v"""
import unittest
from datetime import datetime

from app.automation.agenda_scraper import BOGOTA, Activity, CourseAgenda
from app.watcher import evaluate, in_quiet_hours, is_summary_command

COURSES = [{"code": "C1", "name": "Proyectos"}]


def act(title, opens, due):
    return Activity(external_id=f"C1:{title}", course_code="C1", section=None, moment=None,
                    rac=None, title=title, description=None, activity_type="Colaborativa",
                    weight=100, opens_at=opens, due_at=due, feedback_from=None, feedback_to=None,
                    opens_raw=None, due_raw=None, feedback_raw=None, content_hash="x")


def agenda(*activities):
    return {"C1": CourseAgenda(code="C1", period="p", title="T", url="u", fetched_at="",
                               activities=list(activities))}


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=BOGOTA)


FASE2 = act("Fase 2", "2026-10-27T00:00:00-05:00", "2026-11-23T23:55:00-05:00")


def run(state, now, ag):
    msgs, new = evaluate(COURSES, ag, [], state, now)
    for m in msgs:  # simula envío exitoso
        for k in m.keys:
            new["sent"][k] = now.isoformat()
    return [k for m in msgs for k in m.keys if not k.startswith("summary:")], new


def run_all(state, now, ag):
    msgs, new = evaluate(COURSES, ag, [], state, now)
    for m in msgs:
        for k in m.keys:
            new["sent"][k] = now.isoformat()
    return [k for m in msgs for k in m.keys], new


class ReglasTest(unittest.TestCase):
    def empty(self):
        return {"activities": {}, "courses_seen": [], "sent": {}}

    def test_primera_vez_bienvenida_sin_spam(self):
        keys, _ = run(self.empty(), at("2026-11-11T06:30:00"), agenda(FASE2))
        self.assertEqual(keys, ["welcome:C1"])  # 12 días: ningún recordatorio, antes de las 7

    def test_recordatorio_una_sola_vez(self):
        _, st = run(self.empty(), at("2026-11-11T06:30:00"), agenda(FASE2))
        keys, st = run(st, at("2026-11-16T07:07:00"), agenda(FASE2))  # faltan 7 días
        self.assertEqual(keys, ["due:C1:Fase 2:d7"])
        keys, st = run(st, at("2026-11-16T07:37:00"), agenda(FASE2))
        self.assertEqual(keys, [])  # no se repite

    def test_varios_umbrales_cruzados_solo_el_mas_urgente(self):
        _, st = run(self.empty(), at("2026-11-11T06:30:00"), agenda(FASE2))
        keys, st = run(st, at("2026-11-23T19:00:00"), agenda(FASE2))  # quedan <6 h
        self.assertEqual(keys, ["due:C1:Fase 2:h6"])
        keys, _ = run(st, at("2026-11-23T19:30:00"), agenda(FASE2))
        self.assertEqual(keys, [])

    def test_cambio_de_fecha_reactiva_recordatorios(self):
        _, st = run(self.empty(), at("2026-11-11T06:30:00"), agenda(FASE2))
        _, st = run(st, at("2026-11-22T06:30:00"), agenda(FASE2))  # recordatorio 1d/3d
        movida = act("Fase 2", FASE2.opens_at, "2026-11-30T23:55:00-05:00")
        keys, st = run(st, at("2026-11-22T06:40:00"), agenda(movida))
        self.assertIn("change:C1:Fase 2:2026-11-30T23:55:00-05:00", keys)
        self.assertFalse([k for k in keys if k.startswith("due:")])  # faltan 8 días: aún no
        keys, _ = run(st, at("2026-11-23T07:10:00"), agenda(movida))  # faltan 7 días
        self.assertEqual(keys, ["due:C1:Fase 2:d7"])  # el aviso de 7 días vuelve a salir

    def test_actividad_nueva_y_desaparecida(self):
        _, st = run(self.empty(), at("2026-11-11T06:30:00"), agenda(FASE2))
        nueva = act("Fase 9", "2027-01-01T00:00:00-05:00", "2027-02-01T23:55:00-05:00")
        keys, st = run(st, at("2026-11-11T06:40:00"), agenda(nueva))
        self.assertIn("new:C1:Fase 9", keys)
        self.assertIn("gone:C1:Fase 2", keys)

    def test_resumen_diario_una_vez(self):
        _, st = run(self.empty(), at("2026-11-11T06:30:00"), agenda(FASE2))
        keys, st = run_all(st, at("2026-11-18T07:05:00"), agenda(FASE2))
        self.assertIn("summary:2026-11-18", keys)
        keys, _ = run_all(st, at("2026-11-18T12:00:00"), agenda(FASE2))
        self.assertNotIn("summary:2026-11-18", keys)

    def test_cerrada_no_genera_avisos(self):
        fase1 = act("Fase 1", "2026-10-13T00:00:00-05:00", "2026-10-26T23:55:00-05:00")
        keys, _ = run(self.empty(), at("2026-11-11T06:30:00"), agenda(fase1))
        self.assertEqual(keys, ["welcome:C1"])

    def test_error_de_descarga_un_aviso_por_dia(self):
        msgs, _ = evaluate(COURSES, {}, ["C1"], self.empty(), at("2026-11-11T09:00:00"))
        self.assertEqual([m.key for m in msgs], ["error:C1:2026-11-11", "summary:2026-11-11"])

    def test_varios_errores_un_solo_mensaje(self):
        cursos = [{"code": "C1", "name": "A"}, {"code": "C2", "name": "B"}]
        msgs, _ = evaluate(cursos, {}, ["C1", "C2"], self.empty(), at("2026-11-11T06:30:00"))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].keys, ("error:C1:2026-11-11", "error:C2:2026-11-11"))
        self.assertIn("No pude leer 2 agendas", msgs[0].text)

    def test_silencio(self):
        self.assertTrue(in_quiet_hours(at("2026-11-11T23:00:00")))
        self.assertTrue(in_quiet_hours(at("2026-11-11T05:59:00")))
        self.assertFalse(in_quiet_hours(at("2026-11-11T06:00:00")))


class CuentaRegresivaTest(unittest.TestCase):
    def test_un_aviso_cada_manana_de_5_a_1(self):
        st = {"activities": {}, "courses_seen": ["C1"], "sent": {}}
        dias = []
        for d in range(16, 24):  # 16 al 23 nov, revisión 7:07 a. m.
            keys, st = run(st, at(f"2026-11-{d}T07:07:00"), agenda(FASE2))
            dias += [k.rsplit(":", 1)[1] for k in keys if k.startswith("due:")]
        self.assertEqual(dias, ["d7", "d5", "d4", "d3", "d2", "d1"])

    def test_no_avisa_antes_de_las_7(self):
        _, st = run({"activities": {}, "courses_seen": [], "sent": {}}, at("2026-11-17T07:07:00"),
                    agenda(FASE2))  # ya avisó el de 7 días
        keys, _ = run(st, at("2026-11-18T06:30:00"), agenda(FASE2))  # faltan 5 días, 6:30 a. m.
        self.assertEqual(keys, [])

    def test_aviso_atrasado_dice_los_dias_reales(self):
        st = {"activities": {}, "courses_seen": ["C1"], "sent": {}}
        msgs, _ = evaluate(COURSES, agenda(FASE2), [], st, at("2026-11-18T06:30:00"))
        due = [m for m in msgs if m.key.startswith("due:")][0]
        self.assertIn("Faltan 5 días", due.text)
        self.assertNotIn("una semana", due.text)

    def test_actividad_corta_no_repite_el_mismo_dia(self):
        corta = act("Eval", "2026-11-26T00:00:00-05:00", "2026-12-01T23:55:00-05:00")
        st = {"activities": {}, "courses_seen": ["C1"], "sent": {}}
        k1, st = run(st, at("2026-11-26T06:07:00"), agenda(corta))
        k2, st = run(st, at("2026-11-26T07:07:00"), agenda(corta))
        k3, st = run(st, at("2026-11-27T07:07:00"), agenda(corta))
        self.assertTrue([k for k in k1 if k.startswith("due:")])
        self.assertFalse([k for k in k2 if k.startswith("due:")])
        self.assertEqual([k for k in k3 if k.startswith("due:")], ["due:C1:Eval:d4"])

    def test_actividad_corta_avisa_al_abrir(self):
        corta = act("Eval", "2026-11-26T00:00:00-05:00", "2026-12-01T23:55:00-05:00")
        st = {"activities": {}, "courses_seen": ["C1"], "sent": {}}
        keys, st = run(st, at("2026-11-26T07:07:00"), agenda(corta))  # abre con 5 días
        self.assertIn("due:C1:Eval:d5", keys)
        self.assertIn("open:C1:Eval", keys)


class DiaDelCierreTest(unittest.TestCase):
    def test_aviso_atrasado_el_dia_del_cierre_dice_hoy(self):
        st = {"activities": {}, "courses_seen": ["C1"], "sent": {}}
        msgs, _ = evaluate(COURSES, agenda(FASE2), [], st, at("2026-11-23T07:07:00"))
        due = [m for m in msgs if m.key.startswith("due:")][0]
        self.assertIn("¡Cierra hoy!", due.text)
        self.assertNotIn("Faltan 0", due.text)


class SimulacionTest(unittest.TestCase):
    def test_simulacion_no_toca_el_estado_y_muestra_avisos(self):
        import io, contextlib
        from unittest import mock
        import app.watcher as w
        ag = agenda(FASE2)
        real = {"activities": {}, "courses_seen": ["C1"], "sent": {}, "course_names": {}}
        out = io.StringIO()
        with mock.patch.object(w, "load_state", return_value=real), \
             mock.patch.object(w, "collect", return_value=(COURSES, ag, [])), \
             contextlib.redirect_stdout(out):
            w.simulate(at("2026-11-23T18:07:00"), {"due"}, send=False)
        self.assertIn("Últimas horas", out.getvalue())
        self.assertNotIn("Cierra mañana", out.getvalue())  # ya se habría enviado antes
        self.assertEqual(real["sent"], {})                   # el estado real no cambió


class ComandosTest(unittest.TestCase):
    def upd(self, text, chat=-1001234567890):
        return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}

    def test_reconoce_variantes(self):
        for t in ["/encurso", "/encurso@mi_unad_ia_bot", "/ENCURSO", "/resumen", " /encurso hoy"]:
            self.assertTrue(is_summary_command(self.upd(t), "-1001234567890"), t)

    def test_ignora_otros(self):
        self.assertFalse(is_summary_command(self.upd("encurso"), "-1001234567890"))
        self.assertFalse(is_summary_command(self.upd("/start"), "-1001234567890"))
        self.assertFalse(is_summary_command(self.upd("/encurso", chat=123), "-1001234567890"))


class AgrupacionTest(unittest.TestCase):
    def test_tres_cierres_mismo_dia_un_solo_mensaje(self):
        a = act("A", "2026-11-01T00:00:00-05:00", "2026-11-22T23:55:00-05:00")
        b = act("B", "2026-11-01T00:00:00-05:00", "2026-11-22T23:55:00-05:00")
        c = act("C", "2026-11-01T00:00:00-05:00", "2026-11-22T23:55:00-05:00")
        st = {"activities": {}, "courses_seen": ["C1"], "sent": {}}
        msgs, _ = evaluate(COURSES, agenda(a, b, c), [], st, at("2026-11-22T18:07:00"))
        due = [m for m in msgs if m.key.startswith("due:")]
        self.assertEqual(len(due), 1)
        self.assertEqual(len(due[0].keys), 3)
        self.assertIn("3 actividades", due[0].text)
        self.assertEqual(due[0].text.count("Colaborativa · 100 puntos"), 3)  # tipo y puntos en cada una

    def test_uno_solo_usa_plantilla_individual(self):
        st = {"activities": {}, "courses_seen": ["C1"], "sent": {}}
        msgs, _ = evaluate(COURSES, agenda(FASE2), [], st, at("2026-11-23T18:07:00"))
        due = [m for m in msgs if m.key.startswith("due:")]
        self.assertEqual(len(due), 1)
        self.assertIn("Últimas horas!</b>\n", due[0].text)


if __name__ == "__main__":
    unittest.main()
