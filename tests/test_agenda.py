"""Pruebas del lector de agendas. Ejecutar: py -m unittest discover tests -v"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.automation import agenda_scraper as S

ROW = ("<tr><td>Intermedio</td><td>RAC 3</td><td>{name}</td><td>desc</td><td>Colaborativa</td>"
       "<td>125</td><td>{o} 00:00</td><td>{d} 23:55</td><td></td><td>-</td></tr>")
HEADER = ("<tr><th>1. Momento</th><th>2. Resultado de aprendizaje</th><th>3. Nombre de la actividad</th>"
          "<th>4. Descripción</th><th>5. Tipo de actividad</th><th>6. Peso</th>"
          "<th>7. Actividad inicia en:</th><th>8. Actividad finaliza en:</th><th>9. Alerta</th>"
          "<th>10. Fecha de entrega realimentación</th></tr>")


def page(title, header_line="", rows=()):
    return (f"<html><head><title>{title}</title></head><body>"
            f"<table><tr><td>Agenda del curso<br>{header_line}</td></tr></table>"
            f"<table>{HEADER}{''.join(rows)}</table></body></html>")


class NombreCursoTest(unittest.TestCase):
    def test_nombre_desde_title(self):
        html = page("Agenda - 102437376 - GESTIóN DE PROYECTOS SOSTENIBLES - 2026 II PERIODO 16-05 (2205)",
                    rows=[ROW.format(name="Fase 1", o="13/OCT/2026", d="26/OCT/2026")])
        ag = S.parse_agenda(html, "102437376")
        self.assertEqual(ag.name, "Gestión de Proyectos Sostenibles")
        self.assertEqual(ag.period_label, "2026 II PERIODO 16-05 (2205)")

    def test_titulo_con_texto_antes(self):
        html = page("Idioma de la oferta es :: Agenda - 212064 - INFERENCIA ESTADÍSTICA - 2026 II PERIODO 16-05 (2205)",
                    rows=[ROW.format(name="Fase 1", o="13/OCT/2026", d="26/OCT/2026")])
        self.assertEqual(S.parse_agenda(html, "212064").name, "Inferencia Estadística")

    def test_numeral_romano_y_palabras_menores(self):
        self.assertEqual(S.nice_case("GESTIÓN INTEGRAL DE PROYECTOS I"), "Gestión Integral de Proyectos I")
        self.assertEqual(S.nice_case("ADMINISTRACIÓN DE BASES DE DATOS"), "Administración de Bases de Datos")
        self.assertEqual(S.nice_case("INFERENCIA ESTADÍSTICA"), "Inferencia Estadística")

    def test_respaldo_desde_encabezado(self):
        html = page("Agenda", "GESTIóN DE PROYECTOS SOSTENIBLES - Curso Metodológico (TP) - 102437376 de 3 créditos",
                    rows=[ROW.format(name="Fase 1", o="13/OCT/2026", d="26/OCT/2026")])
        self.assertEqual(S.parse_agenda(html, "102437376").name, "Gestión de Proyectos Sostenibles")

    def test_sin_nombre_no_falla(self):
        html = page("", rows=[ROW.format(name="Fase 1", o="13/OCT/2026", d="26/OCT/2026")])
        ag = S.parse_agenda(html, "999")
        self.assertIsNone(ag.name)
        self.assertEqual(len(ag.activities), 1)

    def test_actividades_con_mismo_nombre_no_se_pierden(self):
        name = "Componente práctico - Prácticas simuladas"
        html = page("Agenda - 1 - X - 2026 II PERIODO 16-05 (2205)", rows=[
            ROW.format(name=name, o="24/NOV/2026", d="24/DIC/2026"),
            ROW.format(name=name, o="26/DIC/2026", d="25/ENE/2027")])
        ag = S.parse_agenda(html, "1")
        self.assertEqual(len(ag.activities), 2)
        self.assertEqual(len({a.external_id for a in ag.activities}), 2)


class FetchReintentosTest(unittest.TestCase):
    def test_reintenta_si_cortan_la_conexion_pero_no_si_es_404(self):
        import urllib.error
        calls = []

        class Resp:
            headers = {"Content-Type": "text/html; charset=utf-8"}
            def read(self): return b"<html>ok</html>"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def flaky(req, timeout=30):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.URLError(ConnectionResetError(104, "reset"))
            return Resp()
        with mock.patch.object(S.urllib.request, "urlopen", flaky), mock.patch.object(S.time, "sleep", lambda s: None):
            self.assertIn("ok", S.fetch("https://x/1.htm"))
        self.assertEqual(len(calls), 3)

        calls.clear()
        def not_found(req, timeout=30):
            calls.append(1)
            raise urllib.error.HTTPError("u", 404, "nf", {}, None)
        with mock.patch.object(S.urllib.request, "urlopen", not_found):
            with self.assertRaises(urllib.error.HTTPError):
                S.fetch("https://x/2.htm")
        self.assertEqual(len(calls), 1)


class ConfigTest(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "courses.json"
            f.write_text(json.dumps(data), encoding="utf-8")
            with mock.patch.object(S, "CONFIG_FILE", f):
                return S.load_config()

    def test_formato_por_periodos(self):
        cs = self.load({"periodos": {"2026-2205": ["102437374", "102437376"], "2026-2204": [212064]}})
        self.assertEqual([(c["code"], c["period"]) for c in cs],
                         [("102437374", "2026-2205"), ("102437376", "2026-2205"), ("212064", "2026-2204")])

    def test_formato_anterior_sigue_funcionando(self):
        cs = self.load({"period": "2026-2205", "courses": [{"code": "102437374", "name": "X"}]})
        self.assertEqual(cs[0]["period"], "2026-2205")

    def test_codigo_duplicado_una_sola_vez(self):
        cs = self.load({"periodos": {"2026-2205": ["1", "1"]}})
        self.assertEqual(len(cs), 1)

    def test_codigo_invalido(self):
        with self.assertRaises(ValueError):
            self.load({"periodos": {"2026-2205": ["10243737A"]}})


if __name__ == "__main__":
    unittest.main()
