"""Regresión con 6 agendas reales (descripciones recortadas). Ejecutar: py -m unittest discover tests -v"""
import unittest
from pathlib import Path

from app.automation.agenda_scraper import parse_agenda

FIXTURES = Path(__file__).parent / "fixtures"
ESPERADO = {  # código: (nombre, actividades, cierre de la última)
    "102437374": ("Formulación Integral de Proyectos", 5, "2027-02-08T23:55:00-05:00"),
    "102437375": ("Gestión Integral de Proyectos I", 5, "2027-02-08T23:55:00-05:00"),
    "102437376": ("Gestión de Proyectos Sostenibles", 5, "2027-02-08T23:55:00-05:00"),
    "202016902": ("Administración de Bases de Datos", 5, "2026-12-06T23:55:00-05:00"),
    "212064": ("Inferencia Estadística", 7, "2026-12-01T23:55:00-05:00"),
    "202016907": ("Proyecto de Grado", 5, "2026-12-06T23:55:00-05:00"),
}


class AgendasRealesTest(unittest.TestCase):
    def test_todas(self):
        for code, (name, n, last_due) in ESPERADO.items():
            with self.subTest(code=code):
                ag = parse_agenda((FIXTURES / f"{code}.htm").read_text(encoding="utf-8"), code)
                self.assertEqual(ag.name, name)
                self.assertEqual(len(ag.activities), n)
                self.assertEqual(ag.activities[-1].due_at, last_due)
                for a in ag.activities:
                    self.assertTrue(a.due_at and a.opens_at and a.weight and a.moment and a.rac, a.title)

    def test_rowspan_anidado_inferencia(self):
        ag = parse_agenda((FIXTURES / "212064.htm").read_text(encoding="utf-8"), "212064")
        sim = [a for a in ag.activities if "Actividad simulada" in a.title]
        self.assertEqual([(a.moment, a.rac, a.weight) for a in sim],
                         [("Intermedio", "RAC 2", 28), ("Intermedio", "RAC 3", 22)])

    def test_fase4_sostenibles_hereda_rac3(self):
        ag = parse_agenda((FIXTURES / "102437376.htm").read_text(encoding="utf-8"), "102437376")
        f4 = ag.activities[3]
        self.assertEqual((f4.moment, f4.rac, f4.opens_at[:10], f4.due_at[:10]),
                         ("Intermedio", "RAC 3", "2026-12-25", "2027-01-25"))


if __name__ == "__main__":
    unittest.main()
