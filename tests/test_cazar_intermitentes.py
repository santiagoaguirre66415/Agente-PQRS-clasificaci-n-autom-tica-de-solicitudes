"""Prueba de la herramienta que caza pruebas intermitentes, con una prueba que falla A PROPÓSITO de forma intermitente."""
import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("cazar", Path(__file__).parent.parent / "herramientas" / "cazar_intermitentes.py")
cazar = importlib.util.module_from_spec(_spec)
sys.modules["cazar"] = cazar
_spec.loader.exec_module(cazar)


def proyecto(tmp_path, cuerpo: str) -> Path:
    carpeta = tmp_path / "proy"
    carpeta.mkdir()
    (carpeta / "test_demo.py").write_text(textwrap.dedent(cuerpo), encoding="utf-8")
    return carpeta


def test_detecta_una_prueba_que_falla_una_de_cada_tres_veces_y_guarda_la_traza(tmp_path, capsys):
    contador = tmp_path / "contador.txt"
    contador.write_text("0")
    carpeta = proyecto(tmp_path, f"""
        from pathlib import Path
        def test_siempre_pasa():
            assert True
        def test_intermitente():
            c = Path(r"{contador}")
            n = int(c.read_text()) + 1
            c.write_text(str(n))
            assert n % 3 != 0, "falla cada tres ejecuciones"
    """)
    log = tmp_path / "log.txt"
    codigo = cazar.main(["x", "--veces", "6", "--ruta", str(carpeta), "--log", str(log)])
    salida = capsys.readouterr().out
    assert codigo == 1
    assert "2 fallo(s) en 6 ejecuciones, 1 prueba(s) distinta(s)" in salida and "2/6  test_demo.py::test_intermitente" in salida
    texto = log.read_text(encoding="utf-8")
    assert "test_intermitente: falló 2 de 6" in texto and "falla cada tres ejecuciones" in texto
    assert "test_siempre_pasa" not in texto          # las que siempre pasan no ensucian el informe


def test_una_suite_estable_devuelve_0_y_no_reporta_nada(tmp_path, capsys):
    carpeta = proyecto(tmp_path, """
        def test_estable():
            assert 1 + 1 == 2
    """)
    assert cazar.main(["x", "--veces", "3", "--ruta", str(carpeta), "--log", str(tmp_path / "log.txt")]) == 0
    assert "0 fallo(s)" in capsys.readouterr().out


def test_un_fallo_constante_se_reporta_como_3_de_3(tmp_path, capsys):
    carpeta = proyecto(tmp_path, """
        def test_roto():
            assert False, "siempre falla"
    """)
    assert cazar.main(["x", "--veces", "3", "--ruta", str(carpeta), "--log", str(tmp_path / "log.txt")]) == 1
    assert "3/3  test_demo.py::test_roto" in capsys.readouterr().out


def test_el_analizador_separa_las_trazas_por_prueba():
    salida = textwrap.dedent("""
        ______________________________ test_uno ______________________________
        E   assert 1 == 2
        ______________________________ test_dos[a] ______________________________
        E   ValueError: dos
        =========================== short test summary info ===========================
        FAILED tests/x.py::test_uno - assert 1 == 2
        FAILED tests/x.py::test_dos[a] - ValueError: dos
    """)
    f = cazar.fallos(salida)
    assert set(f) == {"tests/x.py::test_uno", "tests/x.py::test_dos[a]"}
    assert "1 == 2" in f["tests/x.py::test_uno"] and "ValueError: dos" in f["tests/x.py::test_dos[a]"]


def test_la_carga_artificial_arranca_y_se_apaga_sola():
    with cazar.Carga(2) as c:
        assert len(c.procs) == 2 and all(p.poll() is None for p in c.procs)       # están consumiendo CPU
    assert all(p.poll() is not None for p in c.procs)                              # y se matan al salir


def test_sin_carga_no_lanza_procesos():
    with cazar.Carga(0) as c:
        assert c.procs == []
